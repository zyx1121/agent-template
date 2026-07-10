#!/usr/bin/env python3
"""Minimal Telegram <-> claude bridge — owner DM + optional allow-listed groups, long-polling.

One message = one `claude -p` turn (persona from AGENT.md, per-chat rolling session via
--resume), reply relayed back. No queue: turns are serialized by an asyncio.Lock.

Access control (the bot token is NOT access control — every update is gated here):
  • The owner (OWNER_USER_ID) is always served, in DM or any group.
  • A group is served only if its chat id is in ALLOWED_GROUP_IDS, and only when the bot is
    @-mentioned or replied to (so it stays quiet in normal group chatter).
  • Everyone else is logged and ignored.
Anyone who can address the bot in an allow-listed group can drive claude on this host — keep
the allow-list to groups whose members you trust with that.
"""
import asyncio
import json
import logging
import os
import random
import re
import subprocess
from pathlib import Path

from telegram import BotCommand, ReactionTypeEmoji, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tg_send import send_telegram

HOME = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parent)


def _load_env(path: Path) -> None:
    """Minimal .env loader so `python bot.py` works without python-dotenv. systemd uses
    EnvironmentFile instead; already-set env always wins (setdefault)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(HOME / ".env")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs each request URL at INFO — and PTB embeds the bot token in it. Pin to WARNING so
# the token never lands in the journal.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("agent")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_USER_ID"])
AGENT_NAME = os.environ.get("AGENT_NAME", "Agent")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
TURN_TIMEOUT = int(os.environ.get("AGENT_TURN_TIMEOUT", "1800"))
PERSONA = HOME / "AGENT.md"
RUN = HOME / "run"
RUN.mkdir(exist_ok=True)
# Groups allowed to use the bot: comma-separated chat ids (always negative). Empty = owner-only.
def _parse_group_ids(raw: str) -> set:
    """Skip malformed entries (no crash on a bad env value) and non-negative ids — group /
    supergroup chat ids are always negative; a non-negative id would be a user's DM, not a
    group, and would bypass the @-mention gate."""
    out = set()
    for x in raw.replace(" ", "").split(","):
        if not x:
            continue
        try:
            gid = int(x)
        except ValueError:
            log.warning("ALLOWED_GROUP_IDS: ignoring non-integer entry %r", x)
            continue
        if gid >= 0:
            log.warning("ALLOWED_GROUP_IDS: ignoring non-negative id %s (groups are negative)", gid)
            continue
        out.add(gid)
    return out


ALLOWED_GROUPS = _parse_group_ids(os.environ.get("ALLOWED_GROUP_IDS", ""))

_lock = asyncio.Lock()  # one claude turn at a time (a resumed session can't run concurrently)

# "got it" ack reactions (from Telegram's allowed bot set); a random one is set when a message
# arrives, then overwritten with the outcome (👍/👎) when the turn ends.
REACTIONS = ["👍", "🔥", "🎉", "👀", "🤔", "🙏", "💯", "⚡", "🤩", "👌", "🫡", "✍️", "🤝", "👏", "🤓"]


def _session_file(chat_id: int) -> Path:
    # Per-chat rolling session: a group conversation and the owner's DM never share claude context.
    return RUN / f"session-{chat_id}"


def run_claude(prompt: str, chat_id: int) -> str:
    """Run ONE claude turn (blocking — call via asyncio.to_thread). Returns reply text.
    Persona via --append-system-prompt; per-chat rolling session via --resume. Retries once
    with a fresh session if a stale/expired session id fails to resume."""
    persona = PERSONA.read_text() if PERSONA.exists() else ""
    sf = _session_file(chat_id)

    def _invoke(sid: str) -> subprocess.CompletedProcess:
        cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json",
               "--permission-mode", "bypassPermissions"]
        if persona.strip():
            cmd += ["--append-system-prompt", persona]
        if sid:
            cmd += ["--resume", sid]
        return subprocess.run(cmd, cwd=str(HOME), capture_output=True, text=True, timeout=TURN_TIMEOUT)

    sid = sf.read_text().strip() if sf.exists() else ""
    proc = _invoke(sid)
    if proc.returncode != 0 and sid and re.search(r"no (conversation|rollout) found", proc.stderr, re.I):
        sf.unlink(missing_ok=True)  # stale session — start fresh
        proc = _invoke("")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:500] or f"claude exited {proc.returncode}")
    data = json.loads(proc.stdout)
    new_sid = data.get("session_id", "")
    if new_sid:
        sf.write_text(new_sid)
    return data.get("result") or "(claude 回了空訊息)"


async def _set_reaction(msg, emoji: str) -> None:
    try:
        await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji=emoji)])
    except Exception as e:  # reactions can be unavailable (older chats / API hiccup); non-fatal
        log.warning("set_reaction failed: %s", e)


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Re-send the typing indicator every 4s (Telegram clears it after ~5s). Cancelled when
    the turn finishes."""
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception as e:  # cosmetic; never let it break the turn
        log.warning("typing failed: %s", e)


def _addressed_to_bot(msg, bot_username: str, bot_id: int) -> bool:
    """In a group, only act when the bot is explicitly addressed: @-mentioned, or the message
    is a reply to one of the bot's own messages."""
    r = msg.reply_to_message
    if r and r.from_user and r.from_user.id == bot_id:
        return True
    handle = f"@{bot_username}".lower()
    for e in (msg.entities or []):
        # parse_entity handles UTF-16 offsets correctly — raw text[offset:length] slicing
        # breaks when the message contains emoji/astral chars, silently missing a real mention.
        if e.type == "mention" and msg.parse_entity(e).lower() == handle:
            return True
    return False


def _strip_mention(text: str, bot_username: str) -> str:
    return re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.I).strip()


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # effective_message (not message): an edited text passes the TEXT filter but has
    # update.message=None — treat it as a fresh turn.
    msg = update.effective_message
    chat = update.effective_chat
    user = msg.from_user
    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    if is_group:
        if chat.id not in ALLOWED_GROUPS:
            # The filter only lets an unlisted group through when the sender is the owner, so
            # this is the owner probing for the group id to add to the allow-list.
            if user and user.id == OWNER_ID and _addressed_to_bot(msg, context.bot.username, context.bot.id):
                await msg.reply_text(
                    f"這個群組還沒授權。group id = {chat.id}\n"
                    "填進 .env 的 ALLOWED_GROUP_IDS 再 restart 就能用。"
                )
            return
        if not _addressed_to_bot(msg, context.bot.username, context.bot.id):
            return  # allow-listed group, but not addressed to the bot — stay quiet

    text = msg.text or ""
    if is_group:
        sender = (user.full_name if user else "") or (user.username if user else "") or "someone"
        prompt = f"[{sender}]: {_strip_mention(text, context.bot.username)}"
    else:
        prompt = text

    async with _lock:  # serialize turns
        await _set_reaction(msg, random.choice(REACTIONS))  # "got it" ack
        typing = asyncio.create_task(_keep_typing(context, chat.id))
        ok = True
        try:
            reply = await asyncio.to_thread(run_claude, prompt, chat.id)
        except subprocess.TimeoutExpired:
            reply = f"⚠️ claude 逾時({TURN_TIMEOUT}s)"
            ok = False
        except Exception as e:
            log.exception("claude turn failed")
            reply = f"⚠️ claude 失敗:{e}"
            ok = False
        finally:
            typing.cancel()
        await asyncio.to_thread(send_telegram, TOKEN, chat.id, reply)
        await _set_reaction(msg, "👍" if ok else "👎")  # overwrite the ack with the outcome


async def on_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _session_file(update.effective_chat.id).unlink(missing_ok=True)  # fresh session for THIS chat
    await update.effective_message.reply_text("🆕 開了新對話(清掉這個 chat 的 session 記憶)。")


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"{AGENT_NAME} 上線。傳訊息給我就是一次對話。\n/new 開新對話。"
    )


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Only in DM — in a group an unrecognized command isn't ours to answer (avoid noise).
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.effective_message.reply_text("未知指令。可用:/new(開新對話)、/start。")


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Text-only skeleton. Stay silent on group media (avoid noise); only answer in DM. See
    # README「擴充」for wiring media (download the file, pass its local path) through claude.
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.effective_message.reply_text("目前這個骨架只收文字訊息。")


async def on_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Not the owner and not an allow-listed group: log and ignore.
    u = update.effective_user
    c = update.effective_chat
    log.warning("ignored message from id=%s (%s) in chat=%s (%s)",
                getattr(u, "id", "?"), getattr(u, "username", "?"),
                getattr(c, "id", "?"), getattr(c, "type", "?"))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # PTB swallows handler exceptions and never redelivers the update — surface real bugs to
    # the owner, but stay quiet on transient long-poll blips (they auto-recover).
    from telegram.error import NetworkError
    log.error("handler exception", exc_info=context.error)
    if isinstance(context.error, NetworkError):
        return
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=f"⚠️ bot handler 掛了:{context.error}")
    except Exception:
        pass


async def post_init(app: Application) -> None:
    # Sync the bot's Telegram-side presentation on every start. Idempotent; wrapped so a
    # transient API hiccup can't crash startup.
    try:
        await app.bot.set_my_commands([BotCommand("new", "開新對話(清掉 session 記憶)")])
        await app.bot.set_my_name(AGENT_NAME)
    except Exception as e:
        log.warning("post_init presentation failed (non-fatal): %s", e)


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    # served = owner (DM or any group) OR any member of an allow-listed group. Everything else
    # falls to on_unauthorized. Group messages are further gated to @-mentions inside on_message.
    served = filters.User(user_id=OWNER_ID)
    if ALLOWED_GROUPS:
        served = served | filters.Chat(chat_id=list(ALLOWED_GROUPS))
    app.add_handler(CommandHandler("start", on_start, filters=served))
    app.add_handler(CommandHandler("new", on_new, filters=served))
    app.add_handler(MessageHandler(served & filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(served & filters.COMMAND, on_unknown_command))
    app.add_handler(MessageHandler(served & ~filters.TEXT, on_media))
    app.add_handler(MessageHandler(~served, on_unauthorized))
    app.add_error_handler(on_error)
    log.info("%s bot starting (long-poll); owner=%s, allowed groups=%s",
             AGENT_NAME, OWNER_ID, sorted(ALLOWED_GROUPS) or "(none)")
    # Keep pending updates: messages sent while the bot was down must survive a restart.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
