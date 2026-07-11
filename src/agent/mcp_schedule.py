"""Standalone stdio MCP server exposing schedule CRUD to claude — the ONLY sanctioned way a
turn creates a persistent, guaranteed-to-fire reminder or scheduled task. `claude.py` spawns
one fresh `python -m agent.mcp_schedule` subprocess per turn (wired in via the runtime
mcp-config it writes to `run/mcp-runtime-<chat_id>.json`), passing `AGENT_HOME` (where
schedules.json lives) and `AGENT_CHAT_ID` (which chat schedule_add should target) as env vars
— never argv or cwd, since claude controls the exact command/args/env from that config file.

This module is a thin protocol adapter. The actual schedule storage lives in
schedule_store.py (shared with the bot's own long-running process), and the actual firing
happens in handlers.py's JobQueue tick — nothing in this process ever executes a schedule,
it only reads/writes the JSON file the tick polls.

Run directly for a smoke test: `AGENT_HOME=. AGENT_CHAT_ID=123 python -m agent.mcp_schedule`
(stdio JSON-RPC on stdin/stdout, per the MCP spec).
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from agent.cron import validate_cron
from agent.schedule_store import add_schedule, edit_schedule, list_schedules, remove_schedule

mcp = FastMCP("schedule")


def _schedules_file() -> Path:
    home = Path(os.environ.get("AGENT_HOME") or Path.cwd())
    return home / "run" / "schedules.json"


def _chat_id() -> int:
    raw = os.environ.get("AGENT_CHAT_ID")
    if not raw:
        raise RuntimeError(
            "AGENT_CHAT_ID not set — this server must be launched per-turn by claude.py, "
            "not run standalone against a live chat"
        )
    return int(raw)


@mcp.tool()
def schedule_add(cron: str, prompt: str, note: str = "", once: bool = False) -> str:
    """Create a persistent schedule — when it fires, it automatically runs a claude turn in
    "this" chat and sends the result back here. This is the only reliable mechanism for
    reminders/scheduled tasks. Schedules are stored in the bot's long-running process,
    unaffected by this conversation ending.

    Args:
        cron: standard 5-field cron expression (minute hour day month weekday), e.g.
            "0 8 * * *" (daily at 8:00), "*/30 * * * *" (every 30 minutes), "0 9 * * 1-5"
            (weekdays at 9am). Both 0 and 7 mean Sunday in the weekday field.
        prompt: the task to hand to claude when it fires — describe it in complete natural
            language; there's no context from this conversation at execution time, so the
            more self-contained the better.
        note: a short human-readable note (shown by schedule_list); doesn't affect execution.
        once: True means fire exactly once and auto-delete afterward; False (default) repeats
            per the cron expression.

    Returns:
        On success, the schedule id + summary; on an invalid cron expression, a clear error
        message (nothing is created).
    """
    err = validate_cron(cron)
    if err:
        return f"❌ invalid cron expression: {err}"
    sched = add_schedule(
        _schedules_file(), cron=cron, prompt=prompt, chat_id=_chat_id(), note=note, once=once
    )
    return f"✅ created schedule id={sched['id']} cron={cron} → {prompt[:60]}"


@mcp.tool()
def schedule_list() -> str:
    """List all schedules, including ones created from other chats (single-host,
    single-user deployment — no per-chat isolation). Look up a schedule's id here before
    editing (schedule_edit) or deleting (schedule_remove) it.

    Returns:
        One summary line per schedule: id, whether enabled, whether one-off, cron, its
        chat_id, note, and the start of its prompt. Says so explicitly when there are none,
        never returns an empty string.
    """
    schedules = list_schedules(_schedules_file())
    if not schedules:
        return "No schedules yet."
    lines = []
    for s in schedules:
        flag = "" if s.get("enabled", True) else "(disabled)"
        once_flag = " [once]" if s.get("once") else ""
        note = f" — {s['note']}" if s.get("note") else ""
        lines.append(
            f"{s['id']}{flag}{once_flag} · {s['cron']} · chat {s['chat_id']}{note}\n"
            f"  {s['prompt'][:80]}"
        )
    return "\n".join(lines)


@mcp.tool()
def schedule_edit(
    id: str,
    cron: str | None = None,
    prompt: str | None = None,
    note: str | None = None,
    enabled: bool | None = None,
    once: bool | None = None,
) -> str:
    """Edit an existing schedule — only pass the fields you want to change, leave the rest
    None. Common uses: enabled=False to pause (without deleting and recreating), changing
    cron to retime it, changing prompt to retask it. Look up id via schedule_list().

    Returns:
        The updated schedule summary; if the id doesn't exist or the cron expression is
        invalid, a clear error message (nothing is applied).
    """
    if cron is not None:
        err = validate_cron(cron)
        if err:
            return f"❌ invalid cron expression: {err}"
    sched = edit_schedule(
        _schedules_file(), id, cron=cron, prompt=prompt, note=note, enabled=enabled, once=once
    )
    if sched is None:
        return f"❌ schedule not found: id={id}"
    return f"✅ updated schedule id={sched['id']} cron={sched['cron']} → {sched['prompt'][:60]}"


@mcp.tool()
def schedule_remove(id: str) -> str:
    """Delete a schedule — effective immediately, it will never fire again. Look up id via
    schedule_list()."""
    ok = remove_schedule(_schedules_file(), id)
    return f"✅ deleted schedule {id}" if ok else f"❌ schedule not found: id={id}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
