#!/usr/bin/env bash
# Install this agent as a systemd service (system-scope, long-poll bot).
# Idempotent: safe to re-run after `git pull` — venv/deps are refreshed, the unit is
# re-rendered from deploy/agent.service, and the service is restarted so the new code
# actually takes effect (enable --now alone would NOT restart an already-running unit).
#
# Usage: bash deploy/install.sh   (run as the user the bot should run as; sudo is invoked
# only for the systemd-affecting lines below — no need to run the whole script as root)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

[ -f "$REPO_DIR/pyproject.toml" ] || { echo "!!! $REPO_DIR/pyproject.toml not found — deploy/install.sh must live in <repo>/deploy/"; exit 1; }
command -v python3 >/dev/null || { echo "!!! python3 not found — install it first"; exit 1; }

# venv: the bot runs via `.venv/bin/python -m agent` (see ExecStart in deploy/agent.service),
# and .venv/ is gitignored — without this a fresh clone has nothing to execute. Prefer uv
# (fast, respects uv.lock); fall back to stdlib venv + a pip install of the project itself,
# which works because pyproject.toml is a standard PEP 621 build. --no-editable so the unit
# runs the installed package, not a path-dependent editable shim.
if command -v uv >/dev/null; then
  (cd "$REPO_DIR" && uv sync --no-editable)
else
  [ -d "$REPO_DIR/.venv" ] || python3 -m venv "$REPO_DIR/.venv"
  "$REPO_DIR/.venv/bin/pip" install -q "$REPO_DIR"
fi

# .env is required at systemd load time (EnvironmentFile=, no leading '-'): fail fast with
# a clear pointer instead of installing a unit that will crash-loop on missing env vars.
if [ ! -f "$REPO_DIR/.env" ]; then
  echo "!!! $REPO_DIR/.env missing. Run:"
  echo "!!!   cp '$REPO_DIR/.env.example' '$REPO_DIR/.env'"
  echo "!!! then fill in TELEGRAM_BOT_TOKEN / OWNER_USER_ID / CLAUDE_CODE_OAUTH_TOKEN and re-run this script."
  exit 1
fi

# claude CLI: the bot shells out to it every turn. Non-fatal — CLAUDE_BIN in .env may point
# elsewhere on this box — but worth flagging before the service starts crash-looping.
CLAUDE_BIN_DEFAULT="$HOME/.local/bin/claude"
CLAUDE_BIN_CFG="$(grep -E '^CLAUDE_BIN=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
CLAUDE_BIN_CHECK="${CLAUDE_BIN_CFG:-$CLAUDE_BIN_DEFAULT}"
if [ ! -x "$CLAUDE_BIN_CHECK" ] && ! command -v claude >/dev/null; then
  echo "WARNING: claude CLI not found at '$CLAUDE_BIN_CHECK' or on PATH — install it before the bot can serve turns."
fi

# who the service runs as: if this script itself was invoked with sudo, don't let the
# service inherit root — run as the real login user instead.
RUN_USER="${SUDO_USER:-$(id -un)}"

# unit name: derived from the repo directory name (not hardcoded) so multiple forked
# agents can be installed side-by-side on the same host without colliding.
# (printf, not `basename | tr`, so basename's trailing newline never reaches tr — piping
# it straight through turns that newline into a stray trailing '-'.)
REPO_BASENAME="$(basename "$REPO_DIR")"
SERVICE_NAME="${SERVICE_NAME:-$(printf '%s' "$REPO_BASENAME" | tr -c 'A-Za-z0-9_-' '-')}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

# Unit Description shows the agent's display name (AGENT_NAME in .env, same value the bot
# presents on Telegram), falling back to the unit name when unset. The name is escaped for
# use in a sed replacement string (\, &, and the # delimiter), since it's arbitrary text.
AGENT_DISPLAY_NAME="$(sed -n 's/^AGENT_NAME=//p' "$REPO_DIR/.env" | tail -1 | sed -e 's/^"\(.*\)"$/\1/')"
AGENT_DISPLAY_NAME="${AGENT_DISPLAY_NAME:-$SERVICE_NAME}"
AGENT_DISPLAY_NAME_ESCAPED="$(printf '%s' "$AGENT_DISPLAY_NAME" | sed -e 's/[\\#&]/\\&/g')"

sed -e "s#__REPO_DIR__#$REPO_DIR#g" \
    -e "s#__RUN_USER__#$RUN_USER#g" \
    -e "s#__AGENT_DISPLAY_NAME__#$AGENT_DISPLAY_NAME_ESCAPED#g" \
    "$REPO_DIR/deploy/agent.service" | sudo tee "$UNIT_PATH" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"

echo "=== ${SERVICE_NAME}.service ==="
sudo systemctl --no-pager status "${SERVICE_NAME}.service" | head -6 || true
echo
echo "Logs: sudo journalctl -u ${SERVICE_NAME}.service -f"
