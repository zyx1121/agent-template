"""One `claude -p` streaming turn, plus the live progress bubble that mirrors its tool use.

`run_turn` spawns claude as an async subprocess (stream-json), relays each tool_use block to
a single in-place-updating Telegram message ("progress bubble") as it happens, and returns
the final reply text (plus the bubble's message id, for a caller that wants to delete it —
see NO_REPLY_SENTINEL / is_no_reply below). Persona via --append-system-prompt (SOUL.md);
per-chat rolling session via --resume, retried once with a fresh session if a stale id fails
to resume.

Every turn also gets a *builtin* `schedule` MCP server (agent.mcp_schedule) — the only
sanctioned way claude creates persistent reminders/scheduled tasks, since claude's built-in
CronCreate is session-only and evaporates the instant this `-p` process exits. `_build_mcp_config`
merges that in with any user-supplied mcp-config.json (see README's "Extra MCP servers"), writes
the result to a per-chat runtime file, and --mcp-config/--strict-mcp-config is now always
passed (previously conditional on mcp-config.json existing).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import subprocess
import sys
import time
from typing import Awaitable, Callable

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from agent.config import Settings

log = logging.getLogger("agent.claude")

# The magic reply that silences a *scheduled* turn (see is_no_reply / SCHEDULING_NOTE below).
# handlers.py only honors this on the schedule-fired path — a real user turn always replies,
# even if claude emits this token literally.
NO_REPLY_SENTINEL = "NO_REPLY"

# Appended after the persona (or standalone if the persona is empty — a fork with no SOUL.md
# still must not use CronCreate) on every turn. Keeps "how to schedule" out of SOUL.md, which
# is the one file a fork is expected to touch — this instruction has to survive every fork
# untouched, since it's the only thing standing between claude and a promise it can't keep.
SCHEDULING_NOTE = (
    "Scheduling rule: for any 'remind me', 'at a set time', 'run this every day/week' type "
    "request, the only mechanism is the mcp__schedule__* tools (schedule_add / schedule_list / "
    "schedule_edit / schedule_remove). Schedules are persisted by the bot's long-running "
    "process — when one fires, it runs a full turn in the same chat and sends the result back, "
    "unaffected by this conversation ending. Never use the built-in CronCreate / CronList — "
    "those only live in this session's own memory, scoped to this single `claude -p` "
    "invocation; the moment this turn ends they evaporate and will never actually fire. Also "
    "never set up an OS-level cron / at / systemd timer yourself.\n\n"
    "Silent-monitoring rule: this only applies when the CURRENT turn is itself a scheduled "
    "firing (its prompt starts with '[schedule fired ...]') — for a monitoring-style schedule "
    "where most checks find nothing worth reporting, reply with exactly the single token "
    f"{NO_REPLY_SENTINEL} (nothing else in the message, no extra words or punctuation) and the "
    "chat stays completely silent for this run. This sentinel is recognized ONLY on a scheduled "
    "firing; in a normal conversation with a person, always reply normally — never send "
    f"{NO_REPLY_SENTINEL} to a user."
)


class ClaudeTurnError(RuntimeError):
    """A claude turn that failed. `message` is the best human-readable reason available — the
    stream-json result event's own text when present (e.g. "You've hit your session limit ·
    resets 1:50pm"), else stderr, else a bare exit-code line. Previously such a failure raised
    a plain `RuntimeError(stderr or "claude exited N")`, discarding the result event entirely —
    so an empty-stderr failure (a 429 usage/session limit, which reports only via the result
    event) surfaced to the user as the useless "claude exited 1". `is_usage_limit` flags an API
    429 / usage-or-session limit so callers can treat it as an expected transient condition (a
    calm notice, not an alarm; and a scheduled tick can stay quiet rather than repeat it every
    firing) instead of a crash."""

    def __init__(self, message: str, *, is_usage_limit: bool = False):
        super().__init__(message)
        self.message = message
        self.is_usage_limit = is_usage_limit


def _result_message(result_event: dict | None, stderr: str, returncode: int) -> str:
    """Best available failure reason: the result event's own text first (that's where claude
    puts a usage-limit / auth / api error), then stderr, then a bare exit line."""
    if result_event:
        msg = (result_event.get("result") or "").strip()
        if msg:
            return msg[:500]
    return stderr.strip()[:500] or f"claude exited {returncode}"


def _is_usage_limit(result_event: dict | None, message: str) -> bool:
    """A 429 (api_error_status) or a message that names a usage/session/rate limit."""
    if result_event and result_event.get("api_error_status") == 429:
        return True
    return bool(re.search(r"(usage|session|rate)[ -]?limit", message, re.I))


def is_no_reply(reply: str) -> bool:
    """True iff `reply` signals suppression: the whole message (after stripping surrounding
    whitespace) is exactly the NO_REPLY sentinel, or it *ends* with the bare sentinel at a word
    boundary — models occasionally pad a summary sentence before the sentinel despite the
    prompt, and a trailing bare token still unambiguously means "do not deliver". A reply that
    merely mentions the token mid-text, or has it followed by punctuation, is a normal reply
    and must still be sent. Callers that suppress a padded reply should log the dropped text."""
    stripped = reply.strip()
    if stripped == NO_REPLY_SENTINEL:
        return True
    if not stripped.endswith(NO_REPLY_SENTINEL):
        return False
    boundary = stripped[-len(NO_REPLY_SENTINEL) - 1]
    return not (boundary.isalnum() or boundary == "_")


def _build_mcp_config(settings: Settings, chat_id: int) -> dict:
    """The `--mcp-config` payload for one turn: the builtin `schedule` server, merged with any
    user-supplied mcp-config.json. The builtin entry is applied AFTER the user's servers so it
    always wins on a name collision — a fork's mcp-config.json can't accidentally (or
    deliberately) shadow the one thing every fork must keep working."""
    servers: dict = {}
    if settings.mcp_config_file.exists():
        try:
            user_cfg = json.loads(settings.mcp_config_file.read_text())
            servers.update(user_cfg.get("mcpServers", {}))
        except Exception as e:
            log.warning("mcp-config.json unreadable, ignoring: %s", e)
    if "schedule" in servers:
        log.warning("mcp-config.json defines a 'schedule' server — overridden by the builtin scheduling MCP")
    servers["schedule"] = {
        "command": sys.executable,
        "args": ["-m", "agent.mcp_schedule"],
        "env": {"AGENT_HOME": str(settings.home), "AGENT_CHAT_ID": str(chat_id)},
    }
    return {"mcpServers": servers}


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
    the turn itself.

    `on_first_send` (optional) fires exactly once, the moment this bubble's message actually
    lands for the first time — i.e. the first outbound Telegram message of the turn. This
    class has no opinion on what a caller does with that signal (it's a plain notification
    hook, not a typing-indicator concept); `run_turn` just forwards it through from its own
    caller. A hook failure is swallowed like every other cosmetic failure here."""

    _MAX_STEPS = 15
    _THROTTLE = 3.0

    def __init__(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                 *, on_first_send: Callable[[], Awaitable[None]] | None = None):
        self._context = context
        self._chat_id = chat_id
        self._steps: list = []
        self._msg_id = None
        self._sends = 0
        self._last_edit = 0.0
        self._last_text = ""
        self._on_first_send = on_first_send

    @property
    def message_id(self) -> int | None:
        """The bubble's Telegram message id, or None if it never actually sent (e.g. a turn
        with zero tool calls). Exposed so a caller can delete it after the turn — used by the
        scheduled-firing NO_REPLY path (see handlers._delete_progress_bubble)."""
        return self._msg_id

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
        first_send = self._msg_id is None
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
            return
        if first_send and self._msg_id is not None and self._on_first_send:
            try:
                await self._on_first_send()
            except Exception as e:
                log.warning("progress bubble on_first_send hook failed: %s", e)

    async def finish(self) -> None:
        await self._flush(force=True)


async def run_turn(prompt: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, settings: Settings,
                    *, on_first_send: Callable[[], Awaitable[None]] | None = None) -> tuple[str, int | None]:
    """Run ONE claude turn as an async streaming subprocess. Returns (reply text, progress
    bubble message id) — the bubble id lets a caller delete it afterward (the scheduled-firing
    NO_REPLY path does; every other caller just ignores it), relaying tool-use steps to a
    live-updating progress bubble as they happen. Retries once with a fresh session if a
    stale/expired session id fails to resume.

    `on_first_send` is forwarded verbatim to the ProgressBubble (see its docstring) — this
    function has no opinion on it either, purely a pass-through seam for a caller (e.g.
    handlers.py's typing indicator) that wants to know when the turn's first outbound message
    actually lands."""
    persona = settings.soul_file.read_text() if settings.soul_file.exists() else ""
    system_prompt = f"{persona.strip()}\n\n{SCHEDULING_NOTE}" if persona.strip() else SCHEDULING_NOTE
    sf = settings.session_file(chat_id)
    bubble = ProgressBubble(context, chat_id, on_first_send=on_first_send)

    # Written once per turn (not per retry attempt below — same chat_id, same config either
    # way), so both the fresh-session retry and the first attempt point at the same file.
    settings.run_dir.mkdir(exist_ok=True)
    mcp_config_path = settings.run_dir / f"mcp-runtime-{chat_id}.json"
    mcp_config_path.write_text(json.dumps(_build_mcp_config(settings, chat_id), ensure_ascii=False))

    async def _invoke(sid: str):
        cmd = [settings.claude_bin, "-p", prompt,
               "--output-format", "stream-json", "--include-partial-messages", "--verbose",
               "--permission-mode", "bypassPermissions",
               "--append-system-prompt", system_prompt,
               # --strict-mcp-config: this is a non-interactive/headless invocation, so
               # ~/.claude.json's user-scope mcpServers (if any exist on this box) must NOT
               # silently leak in — the only servers available are the ones listed in the
               # runtime config this turn just wrote (builtin `schedule` + mcp-config.json, if
               # any). No --allowedTools needed: --permission-mode bypassPermissions already
               # trusts every tool, MCP or built-in, the same way it already does for
               # Bash/Read/Write.
               "--mcp-config", str(mcp_config_path), "--strict-mcp-config"]
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

    def _is_stale(sderr: str, ev: dict | None) -> bool:
        # the "session expired, start fresh" signal used to land only in stderr; check the
        # result event's text too, since a failure's message now lives there just as often.
        hay = f"{sderr or ''} {(ev or {}).get('result') or ''}"
        return bool(re.search(r"no (conversation|rollout) found", hay, re.I))

    sid = sf.read_text().strip() if sf.exists() else ""
    returncode, result_event, stderr = await _invoke_timed(sid)
    if returncode != 0 and sid and _is_stale(stderr, result_event):
        sf.unlink(missing_ok=True)  # stale session — start fresh
        returncode, result_event, stderr = await _invoke_timed("")
    await bubble.finish()
    # A turn failed if claude exited non-zero OR the result event itself is flagged is_error
    # (a 429 usage/session limit reports subtype="success" but is_error=true — don't trust
    # returncode or subtype alone). Surface the result event's own message, not a bare exit code.
    if returncode != 0 or (result_event is not None and result_event.get("is_error")):
        message = _result_message(result_event, stderr, returncode)
        raise ClaudeTurnError(message, is_usage_limit=_is_usage_limit(result_event, message))
    if result_event is None:
        raise RuntimeError("claude stream ended without a result event")
    new_sid = result_event.get("session_id", "")
    if new_sid:
        sf.write_text(new_sid)
    reply = result_event.get("result") or "(claude returned an empty message)"
    return reply, bubble.message_id
