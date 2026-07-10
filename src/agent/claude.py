"""One `claude -p` streaming turn, plus the live progress bubble that mirrors its tool use.

`run_turn` spawns claude as an async subprocess (stream-json), relays each tool_use block to
a single in-place-updating Telegram message ("progress bubble") as it happens, and returns
the final reply text. Persona via --append-system-prompt (SOUL.md); per-chat rolling session
via --resume, retried once with a fresh session if a stale id fails to resume.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import subprocess
import time

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from agent.config import Settings

log = logging.getLogger("agent.claude")


def _tool_line(name: str, inp) -> str:
    """One step-log line for a tool_use block (mirrors noir's claude-stream-progress.py)."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        cmd = " ".join((inp.get("command") or "").split())
        return f"⚡️ {cmd[:130]}"
    if name in ("Edit", "Write", "NotebookEdit"):
        return f"📝 {name} {inp.get('file_path', '')}"
    if name == "Read":
        return f"📖 {inp.get('file_path', '')}"
    if name.startswith("mcp__"):
        return f"🔧 {name[len('mcp__'):].replace('__', '.')}"
    return f"🔧 {name}"


class ProgressBubble:
    """One live-updating Telegram message showing the tool steps of an in-flight claude turn —
    the progress-bubble equivalent of noir's claude-stream-progress.py, via PTB's bot API
    instead of hand-rolled urllib. Cosmetic: any Telegram API hiccup here must never break
    the turn itself."""

    _MAX_STEPS = 15
    _THROTTLE = 3.0

    def __init__(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        self._context = context
        self._chat_id = chat_id
        self._steps: list = []
        self._msg_id = None
        self._sends = 0
        self._last_edit = 0.0
        self._last_text = ""

    def _render(self) -> str:
        return "\n".join(f"<code>{html.escape(s)}</code>" for s in self._steps[-self._MAX_STEPS:])

    async def add(self, line: str) -> None:
        self._steps.append(line[:140])
        await self._flush()

    async def _flush(self, force: bool = False) -> None:
        if not self._steps:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < self._THROTTLE:
            return
        text = self._render()
        if text == self._last_text:
            return
        try:
            if self._msg_id is None:
                if self._sends >= 2:  # an unconfirmed send may have landed; one resend, then quiet
                    return
                self._sends += 1
                sent = await self._context.bot.send_message(
                    chat_id=self._chat_id, text=text, parse_mode=ParseMode.HTML)
                self._msg_id = sent.message_id
            else:
                await self._context.bot.edit_message_text(
                    chat_id=self._chat_id, message_id=self._msg_id, text=text, parse_mode=ParseMode.HTML)
            self._last_edit = now
            self._last_text = text
        except Exception as e:
            log.warning("progress bubble flush failed: %s", e)

    async def finish(self) -> None:
        await self._flush(force=True)


async def run_turn(prompt: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, settings: Settings) -> str:
    """Run ONE claude turn as an async streaming subprocess. Returns reply text, relaying
    tool-use steps to a live-updating progress bubble as they happen. Retries once with a
    fresh session if a stale/expired session id fails to resume."""
    persona = settings.soul_file.read_text() if settings.soul_file.exists() else ""
    sf = settings.session_file(chat_id)
    bubble = ProgressBubble(context, chat_id)

    async def _invoke(sid: str):
        cmd = [settings.claude_bin, "-p", prompt,
               "--output-format", "stream-json", "--include-partial-messages", "--verbose",
               "--permission-mode", "bypassPermissions"]
        if persona.strip():
            cmd += ["--append-system-prompt", persona]
        if sid:
            cmd += ["--resume", sid]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(settings.home),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # stream-json emits one JSON object per line; a big result / tool_use event easily
            # exceeds asyncio's default 64 KiB StreamReader line limit ("chunk is longer than
            # limit"). Raise it well past any realistic single event.
            limit=16 * 1024 * 1024)
        result_event = None
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") == "assistant":
                    for block in (ev.get("message", {}).get("content") or []):
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name", "")
                            # StructuredOutput is the --json-schema turn-output mechanism, not
                            # a work step.
                            if name == "StructuredOutput":
                                continue
                            await bubble.add(_tool_line(name, block.get("input")))
                elif ev.get("type") == "result":
                    result_event = ev
            stderr = (await proc.stderr.read()).decode("utf-8", "replace")
            returncode = await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        return returncode, result_event, stderr

    async def _invoke_timed(sid: str):
        try:
            return await asyncio.wait_for(_invoke(sid), timeout=settings.turn_timeout)
        except asyncio.TimeoutError:
            raise subprocess.TimeoutExpired(settings.claude_bin, settings.turn_timeout)

    sid = sf.read_text().strip() if sf.exists() else ""
    returncode, result_event, stderr = await _invoke_timed(sid)
    if returncode != 0 and sid and re.search(r"no (conversation|rollout) found", stderr, re.I):
        sf.unlink(missing_ok=True)  # stale session — start fresh
        returncode, result_event, stderr = await _invoke_timed("")
    await bubble.finish()
    if returncode != 0:
        raise RuntimeError(stderr.strip()[:500] or f"claude exited {returncode}")
    if result_event is None:
        raise RuntimeError("claude stream ended without a result event")
    new_sid = result_event.get("session_id", "")
    if new_sid:
        sf.write_text(new_sid)
    return result_event.get("result") or "(claude 回了空訊息)"
