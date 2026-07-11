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
    def mcp_config_file(self) -> Path:
        # Optional — extra user-defined MCP servers. When present, its mcpServers are merged
        # into the per-turn runtime config claude.py always mounts (which also carries the
        # builtin `schedule` server; a user entry named `schedule` never wins). Absent by
        # default: forks without extra servers still get scheduling, nothing else.
        return self.home / "mcp-config.json"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def schedules_file(self) -> Path:
        # The one file schedule_store.py reads/writes — shared by the bot's own JobQueue tick
        # and every `python -m agent.mcp_schedule` subprocess claude spawns per turn.
        return self.run_dir / "schedules.json"

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
    # Path.cwd(), NOT Path(__file__).resolve().parents[N] — __file__ points into
    # .venv/lib/pythonX/site-packages/agent/ for the --no-editable install deploy/install.sh
    # actually uses (uv sync --no-editable), so a parents[N] climb lands inside the venv, not
    # the repo. SOUL.md/mcp-config.json/run/ then silently resolve to nonexistent paths there:
    # persona and MCP servers never load, session files write to a location `uv sync
    # --reinstall` wipes on every redeploy — no crash, no log, just quietly not doing what the
    # file on disk says it should. Both real invocations (deploy/agent.service's
    # WorkingDirectory=, and `uv run python -m agent` per the README) already guarantee cwd
    # is the repo root, so Path.cwd() is a default that's actually true in both editable and
    # non-editable installs, instead of one that only happens to be true in editable mode.
    home = Path(os.environ.get("AGENT_HOME") or Path.cwd())
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
