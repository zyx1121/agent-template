"""Telegram update handlers, access control, and the app wiring / entrypoint.

Access control (the bot token is NOT access control — every update is gated here):
  • The owner (OWNER_USER_ID) is always served, in DM or any group.
  • A group is served only if its chat id is in ALLOWED_GROUP_IDS, and only when the bot is
    @-mentioned or replied to (so it stays quiet in normal group chatter).
  • Everyone else is logged and ignored.
Anyone who can address the bot in an allow-listed group can drive claude on this host — keep
the allow-list to groups whose members you trust with that.

One message = one claude turn (`claude.run_turn`), serialized by an asyncio.Lock (a resumed
session can't run concurrently). `_serve_turn` is the single place the turn lifecycle lives:
ack reaction → streamed run (live progress bubble) + reply → drain OUTBOX → outcome reaction.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
import subprocess
import time

from telegram import BotCommand, ReactionTypeEmoji, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent.claude import run_turn
from agent.config import Settings, load_settings, safe_name
from agent.messaging import send_file, send_message

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs each request URL at INFO — and PTB embeds the bot token in it. Pin to WARNING so
# the token never lands in the journal.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("agent")

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Telegram Bot API getFile download cap

_lock = asyncio.Lock()  # one claude turn at a time (a resumed session can't run concurrently)

# "got it" ack reactions (from Telegram's allowed bot set); a random one is set when a message
# arrives, then overwritten with the outcome (👍/👎) when the turn ends.
REACTIONS = ["👍", "🔥", "🎉", "👀", "🤔", "🙏", "💯", "⚡", "🤩", "👌", "🫡", "✍️", "🤝", "👏", "🤓"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


async def _set_reaction(msg, emoji: str) -> None:
    try:
        await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji=emoji)])
    except Exception as e:  # reactions can be unavailable (older chats / API hiccup); non-fatal
        log.warning("set_reaction failed: %s", e)


def _addressed_to_bot(msg, bot_username: str, bot_id: int) -> bool:
    """In a group, only act when the bot is explicitly addressed: @-mentioned (in the text OR
    a media caption), or the message is a reply to one of the bot's own messages."""
    r = msg.reply_to_message
    if r and r.from_user and r.from_user.id == bot_id:
        return True
    handle = f"@{bot_username}".lower()
    for e in (msg.entities or []):
        # parse_entity handles UTF-16 offsets correctly — raw text[offset:length] slicing
        # breaks when the message contains emoji/astral chars, silently missing a real mention.
        if e.type == "mention" and msg.parse_entity(e).lower() == handle:
            return True
    for e in (msg.caption_entities or []):
        if e.type == "mention" and msg.parse_caption_entity(e).lower() == handle:
            return True
    return False


def _strip_mention(text: str, bot_username: str) -> str:
    return re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.I).strip()


def _group_prompt(user, text: str, bot_username: str) -> str:
    sender = (user.full_name if user else "") or (user.username if user else "") or "someone"
    return f"[{sender}]: {_strip_mention(text, bot_username)}"


def _resolve_attachment(msg):
    """The message's primary downloadable file as (file_id, filename, size, kind), or None.
    One file per Telegram message — albums arrive as separate updates."""
    if msg.document:
        d = msg.document
        return d.file_id, safe_name(d.file_name, f"file_{d.file_unique_id}"), d.file_size, "文件"
    if msg.photo:
        p = msg.photo[-1]  # largest rendition
        return p.file_id, f"photo_{p.file_unique_id}.jpg", p.file_size, "圖片"
    if msg.voice:
        v = msg.voice
        return v.file_id, f"voice_{v.file_unique_id}.ogg", v.file_size, "語音"
    if msg.audio:
        a = msg.audio
        return a.file_id, safe_name(a.file_name, f"audio_{a.file_unique_id}.mp3"), a.file_size, "音訊"
    if msg.video:
        v = msg.video
        return v.file_id, safe_name(v.file_name, f"video_{v.file_unique_id}.mp4"), v.file_size, "影片"
    if msg.animation:
        a = msg.animation
        return a.file_id, safe_name(a.file_name, f"anim_{a.file_unique_id}.mp4"), a.file_size, "動圖"
    if msg.video_note:
        v = msg.video_note
        return v.file_id, f"videonote_{v.file_unique_id}.mp4", v.file_size, "視訊留言"
    if msg.sticker:
        s = msg.sticker
        return s.file_id, f"sticker_{s.file_unique_id}.webp", s.file_size, "貼圖"
    return None


def _clear_outbox(settings: Settings) -> None:
    """Blocking — call via asyncio.to_thread. Wipes stale outbox contents before a turn so a
    file left over from a crashed prior turn never gets (re)sent."""
    outbox = settings.outbox_dir
    outbox.mkdir(exist_ok=True)
    for p in outbox.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def _flush_outbox(settings: Settings, chat_id: int) -> list:
    """Blocking — call via asyncio.to_thread. Sends everything claude dropped in the outbox
    during the just-finished turn. A file that fails to send is KEPT (not silently lost) and its
    name returned so the caller can tell the user. Serialized by _lock, so the outbox's contents
    belong entirely to the turn that just ran."""
    outbox = settings.outbox_dir
    if not outbox.exists():
        return []
    failed = []
    for p in sorted(outbox.iterdir()):
        if not p.is_file():
            continue
        if send_file(settings.token, chat_id, str(p)):
            p.unlink(missing_ok=True)
        else:
            failed.append(p.name)
            log.warning("outbox: failed to send %s — kept for retry", p.name)
    return failed


async def _serve_turn(msg, chat, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    """One claude turn end-to-end: ack reaction, streamed run (live progress bubble) + reply,
    drain any files claude dropped in OUTBOX, outcome reaction. Shared by on_message and
    on_media so the turn lifecycle lives in exactly one place."""
    settings = _settings(context)
    async with _lock:  # serialize turns
        await _set_reaction(msg, random.choice(REACTIONS))  # "got it" ack
        ok = True
        await asyncio.to_thread(_clear_outbox, settings)  # drop stale files from a crashed prior turn
        try:
            reply = await run_turn(prompt, chat.id, context, settings)  # streams progress bubble
        except subprocess.TimeoutExpired:
            reply = f"⚠️ claude 逾時({settings.turn_timeout}s)"
            ok = False
        except Exception as e:
            log.exception("claude turn failed")
            reply = f"⚠️ claude 失敗:{e}"
            ok = False
        await asyncio.to_thread(send_message, settings.token, chat.id, reply)
        failed = await asyncio.to_thread(_flush_outbox, settings, chat.id)
        if failed:
            await asyncio.to_thread(send_message, settings.token, chat.id,
                                    f"⚠️ {len(failed)} 個附件沒送成功(保留在 outbox,可稍後重試):{', '.join(failed)}")
        await _set_reaction(msg, "👍" if ok else "👎")  # overwrite the ack with the outcome


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    # effective_message (not message): an edited text passes the TEXT filter but has
    # update.message=None — treat it as a fresh turn.
    msg = update.effective_message
    chat = update.effective_chat
    user = msg.from_user
    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    if is_group:
        if chat.id not in settings.allowed_groups:
            # The filter only lets an unlisted group through when the sender is the owner, so
            # this is the owner probing for the group id to add to the allow-list.
            if user and user.id == settings.owner_id and _addressed_to_bot(msg, context.bot.username, context.bot.id):
                await msg.reply_text(
                    f"這個群組還沒授權。group id = {chat.id}\n"
                    "填進 .env 的 ALLOWED_GROUP_IDS 再 restart 就能用。"
                )
            return
        if not _addressed_to_bot(msg, context.bot.username, context.bot.id):
            return  # allow-listed group, but not addressed to the bot — stay quiet

    text = msg.text or ""
    prompt = _group_prompt(user, text, context.bot.username) if is_group else text
    await _serve_turn(msg, chat, context, prompt)


async def on_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    settings.session_file(update.effective_chat.id).unlink(missing_ok=True)  # fresh session for THIS chat
    await update.effective_message.reply_text("🆕 開了新對話(清掉這個 chat 的 session 記憶)。")


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    await update.effective_message.reply_text(
        f"{settings.agent_name} 上線。傳訊息給我就是一次對話。\n/new 開新對話。"
    )


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Only in DM — in a group an unrecognized command isn't ours to answer (avoid noise).
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.effective_message.reply_text("未知指令。可用:/new(開新對話)、/start。")


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # A file/photo/voice/etc.: download it so claude can actually open it, then run a turn
    # whose prompt = caption (the task) + the saved path. Group gate mirrors on_message
    # (same allow-list + @-mention check); media doesn't get the owner group-id-probe hint
    # since that's a text-only discovery aid.
    settings = _settings(context)
    msg = update.effective_message
    chat = update.effective_chat
    user = msg.from_user
    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    if is_group:
        if chat.id not in settings.allowed_groups:
            return
        if not _addressed_to_bot(msg, context.bot.username, context.bot.id):
            return  # allow-listed group, but not addressed to the bot — stay quiet

    caption = (msg.caption or "").strip()
    resolved = _resolve_attachment(msg)
    if resolved is None:
        # location / contact / poll / dice … nothing downloadable
        if caption:
            prompt = _group_prompt(user, caption, context.bot.username) if is_group else caption
            await _serve_turn(msg, chat, context, prompt)
            return
        await msg.reply_text("這則訊息我抓不到內容(沒有可下載的檔案)。")
        return

    file_id, fname, size, kind = resolved
    if size and size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text(f"📎 {fname} 太大({size // 1024 // 1024} MB)— Telegram bot 最多下載 20 MB。")
        return

    dest_dir = settings.attach_dir / f"{int(time.time())}-{chat.id}"
    path = dest_dir / fname
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        tgfile = await context.bot.get_file(file_id)  # raises if >20 MB even when size was None
        await tgfile.download_to_drive(str(path))
        path.chmod(0o600)
    except Exception as e:
        log.error("download failed: %s", e)
        await msg.reply_text(f"⚠️ 檔案下載失敗:{e}")
        return

    kb = path.stat().st_size // 1024
    head = caption if caption else "使用者傳了一個檔案,看內容並判斷要不要動作。"
    if caption and is_group:
        head = _group_prompt(user, caption, context.bot.username)
    prompt = (
        f"{head}\n\n--- 附件 ({kind}) ---\n{fname} ({kb} KB) → {path}\n"
        "(檔案已存到上面路徑,要看就讀檔/解析 — 圖片、文件、音訊用你的工具開。)"
    )
    await _serve_turn(msg, chat, context, prompt)


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
        owner_id = context.bot_data["settings"].owner_id
        await context.bot.send_message(chat_id=owner_id, text=f"⚠️ bot handler 掛了:{context.error}")
    except Exception:
        pass


async def post_init(app: Application) -> None:
    # Sync the bot's Telegram-side presentation on every start. Idempotent; wrapped so a
    # transient API hiccup can't crash startup.
    try:
        await app.bot.set_my_commands([BotCommand("new", "開新對話(清掉 session 記憶)")])
        await app.bot.set_my_name(app.bot_data["settings"].agent_name)
    except Exception as e:
        log.warning("post_init presentation failed (non-fatal): %s", e)


def main() -> None:
    settings = load_settings()
    app = Application.builder().token(settings.token).post_init(post_init).build()
    app.bot_data["settings"] = settings
    # served = owner (DM or any group) OR any member of an allow-listed group. Everything else
    # falls to on_unauthorized. Group messages are further gated to @-mentions inside on_message.
    served = filters.User(user_id=settings.owner_id)
    if settings.allowed_groups:
        served = served | filters.Chat(chat_id=list(settings.allowed_groups))
    app.add_handler(CommandHandler("start", on_start, filters=served))
    app.add_handler(CommandHandler("new", on_new, filters=served))
    app.add_handler(MessageHandler(served & filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(served & filters.COMMAND, on_unknown_command))
    app.add_handler(MessageHandler(served & ~filters.TEXT, on_media))
    app.add_handler(MessageHandler(~served, on_unauthorized))
    app.add_error_handler(on_error)
    log.info("%s bot starting (long-poll); owner=%s, allowed groups=%s",
             settings.agent_name, settings.owner_id, sorted(settings.allowed_groups) or "(none)")
    # Keep pending updates: messages sent while the bot was down must survive a restart.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
