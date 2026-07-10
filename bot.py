#!/usr/bin/env python3
"""Minimal Telegram <-> claude bridge — single-user, long-polling.

One owner message = one `claude -p` turn (persona from AGENT.md, rolling session
via --resume), reply relayed back to Telegram. No queue / dispatcher: turns are
serialized by an asyncio.Lock. Long-polling needs only outbound HTTPS, so it runs
behind NAT with no port-forward.

Security: only OWNER_USER_ID is served — the bot token is NOT access control.
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
from telegram.constants import ChatAction
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
SESSION_FILE = RUN / "session"  # rolling claude session id (for --resume); /new clears it

_lock = asyncio.Lock()  # one claude turn at a time (a resumed session can't run concurrently)


def run_claude(prompt: str) -> str:
    """Run ONE claude turn (blocking — call via asyncio.to_thread). Returns reply text.
    Persona via --append-system-prompt; rolling session via --resume. Retries once with a
    fresh session if a stale/expired session id fails to resume."""
    persona = PERSONA.read_text() if PERSONA.exists() else ""

    def _invoke(sid: str) -> subprocess.CompletedProcess:
        cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json",
               "--permission-mode", "bypassPermissions"]
        if persona.strip():
            cmd += ["--append-system-prompt", persona]
        if sid:
            cmd += ["--resume", sid]
        return subprocess.run(cmd, cwd=str(HOME), capture_output=True, text=True, timeout=TURN_TIMEOUT)

    sid = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else ""
    proc = _invoke(sid)
    if proc.returncode != 0 and sid and re.search(r"no (conversation|rollout) found", proc.stderr, re.I):
        SESSION_FILE.unlink(missing_ok=True)  # stale session — start fresh
        proc = _invoke("")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:500] or f"claude exited {proc.returncode}")
    data = json.loads(proc.stdout)
    new_sid = data.get("session_id", "")
    if new_sid:
        SESSION_FILE.write_text(new_sid)
    return data.get("result") or "(claude 回了空訊息)"


# "got it" ack reactions (from Telegram's allowed bot set); a random one is set when a message
# arrives, then overwritten with the outcome (👍/👎) when the turn ends.
REACTIONS = ["👍", "🔥", "🎉", "👀", "🤔", "🙏", "💯", "⚡", "🤩", "👌", "🫡", "✍️", "🤝", "👏", "🤓"]


async def _set_reaction(msg, emoji: str) -> None:
    try:
        await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji=emoji)])
    except Exception as e:  # reactions can be unavailable (older chats / API hiccup); non-fatal
        log.warning("set_reaction failed: %s", e)


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Re-send the typing indicator every 4s (Telegram clears it after ~5s) so a long turn
    feels responsive. Cancelled when the turn finishes."""
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception as e:  # cosmetic; never let it break the turn
        log.warning("typing failed: %s", e)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # effective_message (not message): an edited text passes the TEXT filter but has
    # update.message=None — treat it as a fresh turn.
    msg = update.effective_message
    chat_id = update.effective_chat.id
    async with _lock:  # serialize turns
        await _set_reaction(msg, random.choice(REACTIONS))  # "got it" ack
        typing = asyncio.create_task(_keep_typing(context, chat_id))
        ok = True
        try:
            reply = await asyncio.to_thread(run_claude, msg.text or "")
        except subprocess.TimeoutExpired:
            reply = f"⚠️ claude 逾時({TURN_TIMEOUT}s)"
            ok = False
        except Exception as e:
            log.exception("claude turn failed")
            reply = f"⚠️ claude 失敗:{e}"
            ok = False
        finally:
            typing.cancel()
        await asyncio.to_thread(send_telegram, TOKEN, chat_id, reply)
        await _set_reaction(msg, "👍" if ok else "👎")  # overwrite the ack with the outcome


async def on_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    SESSION_FILE.unlink(missing_ok=True)  # next message starts a fresh claude session
    await update.effective_message.reply_text("🆕 開了新對話(清掉先前的 session 記憶)。")


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"{AGENT_NAME} 上線。傳訊息給我就是一次對話。\n/new 開新對話。"
    )


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Any /command from the owner that isn't /start or /new. Without this it matches no handler
    # and dies silently — a bare /help would get dead air.
    await update.effective_message.reply_text("未知指令。可用:/new(開新對話)、/start。")


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # This skeleton is text-only. See README「擴充」for wiring media (download the file,
    # pass its local path into the prompt) through claude.
    await update.effective_message.reply_text("目前這個骨架只收文字訊息。")


async def on_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Anyone who isn't the owner: log and ignore. The bot serves exactly one person.
    u = update.effective_user
    log.warning("ignored message from unauthorized id=%s (%s)",
                getattr(u, "id", "?"), getattr(u, "username", "?"))


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
    only_me = filters.User(user_id=OWNER_ID)
    app.add_handler(CommandHandler("start", on_start, filters=only_me))
    app.add_handler(CommandHandler("new", on_new, filters=only_me))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(only_me & filters.COMMAND, on_unknown_command))
    app.add_handler(MessageHandler(only_me & ~filters.TEXT, on_media))
    app.add_handler(MessageHandler(~only_me, on_unauthorized))
    app.add_error_handler(on_error)
    log.info("%s bot starting (long-poll); serving user id %s", AGENT_NAME, OWNER_ID)
    # Keep pending updates: messages sent while the bot was down must survive a restart.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
