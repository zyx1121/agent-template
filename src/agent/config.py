"""Runtime configuration — everything the agent reads from the environment, in one place.

A single frozen `Settings` is built once at startup by `load_settings()` and stashed in the
PTB Application's `bot_data`, so handlers pull it from `context.bot_data["settings"]` instead
of reaching into `os.environ` scattered across modules. Nothing here touches the environment
at import time — that keeps the pure parsers below testable without a populated `.env`.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("agent.config")


def load_env(path: Path) -> None:
    """Minimal .env loader so `python -m agent` works without python-dotenv. systemd uses
    EnvironmentFile instead; already-set env always wins (setdefault)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_group_ids(raw: str) -> set[int]:
    """Parse ALLOWED_GROUP_IDS (comma-separated). Skip malformed entries (no crash on a bad
    env value) and non-negative ids — group/supergroup chat ids are always negative; a
    non-negative id would be a user's DM, not a group, and would bypass the @-mention gate."""
    out: set[int] = set()
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


def safe_name(name: str | None, fallback: str) -> str:
    """Sanitize an attachment's declared filename into something safe to write to disk."""
    name = (name or "").replace("\x00", "").strip()
    name = re.sub(r"[^\w.\- ]", "_", name).strip("._ ")
    return (name or fallback)[:120]


@dataclass(frozen=True)
class Settings:
    """Immutable per-process configuration. Built by `load_settings()`; the path-derived
    locations (SOUL.md, run dirs, session files) hang off `home` so a fork that sets
    AGENT_HOME relocates everything at once."""

    token: str
    owner_id: int
    agent_name: str
    claude_bin: str
    turn_timeout: int
    allowed_groups: frozenset[int]
    home: Path

    @property
    def soul_file(self) -> Path:
        return self.home / "SOUL.md"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def outbox_dir(self) -> Path:
        # claude drops files here to have them sent back at the end of the turn.
        return self.run_dir / "outbox"

    @property
    def attach_dir(self) -> Path:
        # downloaded inbound Telegram attachments.
        return self.run_dir / "telegram"

    def session_file(self, chat_id: int) -> Path:
        # Per-chat rolling session: a group conversation and the owner's DM never share
        # claude context.
        return self.run_dir / f"session-{chat_id}"


def load_settings() -> Settings:
    """Load `.env`, then build Settings from the environment. Raises KeyError early if a
    required var (TELEGRAM_BOT_TOKEN / OWNER_USER_ID) is missing — fail fast at startup,
    not mid-turn."""
    home = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parents[2])
    load_env(home / ".env")
    settings = Settings(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        owner_id=int(os.environ["OWNER_USER_ID"]),
        agent_name=os.environ.get("AGENT_NAME", "Agent"),
        claude_bin=os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude")),
        turn_timeout=int(os.environ.get("AGENT_TURN_TIMEOUT", "1800")),
        allowed_groups=frozenset(parse_group_ids(os.environ.get("ALLOWED_GROUP_IDS", ""))),
        home=home,
    )
    settings.run_dir.mkdir(exist_ok=True)
    settings.outbox_dir.mkdir(exist_ok=True)
    return settings
