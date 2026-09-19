#!/bin/sh
# PID 1 of the kitbash Process: check the environment, hand claude the kitbash MCP server,
# then exec the bot. Everything here is about the two things a container start knows that a
# systemd start does not: that the config came from a manifest instead of .env, and that
# kitbashd handed this Process a one-session MCP endpoint and token.
set -eu

# A start that cannot work until a person changes a value stops with status 0, so the
# on-failure restart policy in kitbash.yaml leaves the Process stopped with the reason in
# proc_logs instead of reprinting the same line every few seconds forever. A crash that a
# restart might actually fix still exits non zero and still comes back.
fail() {
  echo "$*" >&2
  exit 0
}

# Fail fast and by name. A missing token would otherwise surface as a KeyError traceback
# from config.load_settings, three restarts deep in proc_logs, which reads like a bug in the
# bot rather than a secret the member has not set yet.
missing=""
for name in TELEGRAM_BOT_TOKEN OWNER_USER_ID CLAUDE_CODE_OAUTH_TOKEN; do
  eval "value=\${$name:-}"
  [ -n "$value" ] || missing="$missing $name"
done
if [ -n "$missing" ]; then
  echo "agent: not starting, these variables are empty or unset:$missing" >&2
  echo "agent: TELEGRAM_BOT_TOKEN and CLAUDE_CODE_OAUTH_TOKEN are secrets, set them with" >&2
  echo "agent:   secrets_set, then proc_run again. OWNER_USER_ID is deploy.units[0].env in" >&2
  echo "agent:   kitbash.yaml: replace CHANGE_ME with your Telegram user id and pkg_build." >&2
  exit 0
fi

case "$OWNER_USER_ID" in
  ''|*[!0-9]*) fail "agent: OWNER_USER_ID is \"$OWNER_USER_ID\", want the numeric Telegram user id @userinfobot gives you (deploy.units[0].env in kitbash.yaml)" ;;
esac

# The mount lands here empty on the first start: run/ holds schedules.json and the per-chat
# session ids, and HOME under it holds claude's own ~/.claude rollouts, which is what
# `claude -p --resume` reads. Both have to exist before anything writes to them.
mkdir -p "${AGENT_HOME:-/app}/run" "${HOME:-/app/run/home}"

# kitbash as the agent's own MCP server. It is written into mcp-config.json rather than
# registered with `claude mcp add`, because every turn runs with --strict-mcp-config (see
# claude.py): a server in ~/.claude.json is invisible to a headless `claude -p`, and
# mcp-config.json is the seam this repo already has for extra servers (README, "Extra MCP
# servers"). The token is read from the environment by the writer below and never echoed,
# never on a command line, never in a log line. The file is created 0600 by the open itself
# rather than chmodded after the fact, it lives in the image layer and not on the mount, and
# it is rewritten from scratch at every start because kitbashd issues a fresh token per
# Process.
if [ -n "${KITBASH_MCP_ENDPOINT:-}" ] && [ -n "${KITBASH_TELEMETRY_TOKEN:-}" ]; then
  python3 - <<'PY'
import json, os

home = os.environ.get("AGENT_HOME", "/app")
path = os.path.join(home, "mcp-config.json")
# KITBASH_MCP_ENDPOINT already ends in /mcp (spec/kitbashd-api.yaml, environment); the
# suffix is added only if a future daemon stops spelling it that way.
url = os.environ["KITBASH_MCP_ENDPOINT"].rstrip("/")
if not url.endswith("/mcp"):
    url += "/mcp"

config = {"mcpServers": {"kitbash": {
    "type": "http",
    "url": url,
    "headers": {"Authorization": "Bearer " + os.environ["KITBASH_TELEMETRY_TOKEN"]},
}}}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(config, fh, indent=2)
print("agent: kitbash MCP server registered at", url, flush=True)
PY
else
  echo "agent: no KITBASH_MCP_ENDPOINT/KITBASH_TELEMETRY_TOKEN in the environment, starting without the kitbash surface" >&2
fi

# exec, so the bot is PID 1: proc_stop sends SIGTERM to it directly and python-telegram-bot
# stops polling and finishes the turn in flight instead of being killed by the shell's
# default handling.
exec python -m agent
