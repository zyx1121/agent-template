"""The single read/write layer for `run/schedules.json` — shared by two different processes
that never share memory: the long-running bot (handlers.py's JobQueue tick reads it every
minute) and the short-lived `python -m agent.mcp_schedule` subprocess claude spawns each turn
(schedule_add/list/edit/remove write it). Every operation here does the full read-modify-write
under an `fcntl.flock`, and every write lands via temp-file + `os.replace` — so a tick reading
concurrently with an MCP edit never observes a half-written file, and two MCP calls racing
(unlikely — turns are serialized by handlers.py's `_lock`, but this file has no visibility into
that) never clobber each other's change.

Schema (one JSON object per schedule, in `schedules.json`'s top-level `schedules` list):
    id: 6 hex chars, assigned by add_schedule
    cron: 5-field cron expression (validated by callers via agent.cron.validate_cron — this
        module does no cron parsing, it just stores the string)
    prompt: what to hand claude when this fires
    chat_id: which Telegram chat the reply goes to
    note: free-text label for humans (schedule_list output), unused by the tick logic
    enabled: bool — disabled schedules are skipped by the tick but not deleted
    once: bool — fire once then self-delete (the tick, not this module, does the deleting)
    created_at: ISO-8601 UTC timestamp
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_EDITABLE_FIELDS = {"cron", "prompt", "note", "enabled", "once"}


@contextmanager
def _locked(path: Path):
    """Exclusive lock on a sibling `.lock` file — never itself replaced by the atomic rename
    in `_write`, so every caller (bot process, every MCP subprocess) contends on the same
    stable inode regardless of how many times schedules.json itself gets swapped out."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    if not path.exists():
        return {"schedules": []}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"schedules": []}
    data.setdefault("schedules", [])
    return data


def _write(path: Path, data: dict) -> None:
    """temp file in the same directory + os.replace: a reader (the tick, or a concurrent MCP
    call once the lock above is released) never sees a partially-written file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".schedules-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def list_schedules(path: Path) -> list[dict]:
    """All schedules, across every chat — single-host single-owner deployment, no per-chat
    isolation. `handlers.py`'s tick filters by `enabled`; `mcp__schedule__schedule_list`
    surfaces `chat_id` so claude can tell them apart in its reply."""
    with _locked(path):
        return _read(path)["schedules"]


def add_schedule(
    path: Path, cron: str, prompt: str, chat_id: int, note: str = "", once: bool = False
) -> dict:
    """Create and persist a new schedule, enabled by default. Caller is responsible for
    validating `cron` first (agent.cron.validate_cron) — this just stores whatever string it's
    given."""
    with _locked(path):
        data = _read(path)
        existing_ids = {s["id"] for s in data["schedules"]}
        new_id = secrets.token_hex(3)
        while new_id in existing_ids:  # 16.7M combos — collision is astronomically unlikely,
            new_id = secrets.token_hex(3)  # but a free retry costs nothing.
        sched = {
            "id": new_id,
            "cron": cron,
            "prompt": prompt,
            "chat_id": chat_id,
            "note": note,
            "enabled": True,
            "once": once,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data["schedules"].append(sched)
        _write(path, data)
        return sched


def edit_schedule(path: Path, id: str, **fields) -> dict | None:
    """Patch an existing schedule — only keys in `fields` that are both a recognized editable
    field AND not None are applied (so callers can pass every possible kwarg through untouched
    and rely on "didn't specify it" == None == no-op, matching the MCP tool's optional args).
    Returns the updated record, or None if `id` doesn't exist."""
    with _locked(path):
        data = _read(path)
        for sched in data["schedules"]:
            if sched["id"] == id:
                for k, v in fields.items():
                    if k in _EDITABLE_FIELDS and v is not None:
                        sched[k] = v
                _write(path, data)
                return sched
        return None


def remove_schedule(path: Path, id: str) -> bool:
    """Delete a schedule. Returns False (no-op, no error) if `id` doesn't exist."""
    with _locked(path):
        data = _read(path)
        kept = [s for s in data["schedules"] if s["id"] != id]
        if len(kept) == len(data["schedules"]):
            return False
        data["schedules"] = kept
        _write(path, data)
        return True
