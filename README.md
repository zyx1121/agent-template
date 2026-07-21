# agent-template

> Wiring a new Telegram bot to Claude Code is the same afternoon every time. Fork this once, and it's already done.

`telegram` · `claude-code` · `python` · `template` · `automation`

[![version](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fzyx1121%2Fagent-template%2Fmain%2Fpyproject.toml&query=%24.project.version&label=version&color=111111)](pyproject.toml) &nbsp;[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](#license)

```
> "check the deploy log and tell me if it's healthy"
  ⚡️ systemctl status agent
  📖 deploy/agent.service
✓ Service active, restarted 2 hours ago
```

<sub>One Telegram message, live-updated with every tool call, then the final reply.</sub>

Every new bot idea starts with the same three chores: wire up Telegram, wire up Claude Code headlessly, wire up something that remembers reminders after the process exits. This repo is that wiring, done once, with the fiddly parts (session resumption, group @-mention gating, a live progress bubble, persistent scheduling) already handled. Reskinning it into a new agent means editing one file (`SOUL.md`) and collecting three tokens; the rest of `src/agent/` stays untouched.

## Fork it

There's deliberately no `create-agent` CLI: opening a new agent is a handful of one-off steps that don't recur often enough to freeze into a tool, and the deploy step differs per target anyway. The steps below also work as a runbook you can hand straight to a coding agent ("open me a new agent from this template"): it drives every mechanical part and only pauses for the two steps a human has to do (approving the bot in @BotFather, running `claude setup-token` in a browser).

```bash
gh repo create <your-agent-name> --template zyx1121/agent-template --private --clone
cd <your-agent-name>
cp .env.example .env
```

1. **Write the persona** in `SOUL.md`, the only file a fork is expected to touch (injected as the system prompt every turn via `claude -p --append-system-prompt`). `src/agent/` stays as-is.
2. **Get a bot token**: [@BotFather](https://t.me/BotFather) → `/newbot` → `TELEGRAM_BOT_TOKEN`. While there, `/setprivacy` → select the bot → **Disable** (skip this and group @-mentions silently never reach the bot, see **Groups**).
3. **Get your user id**: [@userinfobot](https://t.me/userinfobot) → `OWNER_USER_ID`, the trust anchor: always served, everyone else ignored unless allow-listed.
4. **Get a Claude Code OAuth token**: on a machine with a browser and `claude` logged in, run `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` (~1 year validity; an interactive login token won't auto-refresh headlessly).
5. Fill in the rest of `.env`:

| Var | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | - | from @BotFather |
| `OWNER_USER_ID` | yes | - | from @userinfobot |
| `CLAUDE_CODE_OAUTH_TOKEN` | yes | - | from `claude setup-token` |
| `AGENT_NAME` | no | `Agent` | display name |
| `CLAUDE_BIN` | no | `~/.local/bin/claude` | path to the `claude` CLI |
| `AGENT_TURN_TIMEOUT` | no | `1800` | seconds before a turn is killed |
| `ALLOWED_GROUP_IDS` | no | (none) | comma-separated group chat ids, see **Groups** |
| `AGENT_HOME` | no | repo dir | only needed running the bot from elsewhere |
| `IS_SANDBOX` | no | (unset) | set `1` if the bot runs as root, see **Deploy** |

## Run it

```bash
uv run python -m agent
```

Requires the [`claude` CLI](https://docs.claude.com/en/docs/claude-code) logged in (or `CLAUDE_CODE_OAUTH_TOKEN` set) and [uv](https://docs.astral.sh/uv/), Python 3.10+. `uv` creates the venv and installs the one dependency on first run, and `.env` auto-loads from `AGENT_HOME` on startup (a minimal loader, not `python-dotenv`; already-set env vars always win). Without uv: `pip install .` then `python -m agent`.

Tests (pure functions, no network, no bot token): `uv run python -m unittest discover -s tests`.

## What it gives you

- **Bridges Telegram and Claude Code** headlessly: one long-poll process (no webhook, no open port), one rolling `claude -p` session per chat.
- **Gates access to one owner**: DMs always served; groups are opt-in via allow-list + @-mention.
- **Streams a live progress bubble**: one Telegram message updates in place with every tool call (`⚡️` Bash, `📖` Read, `📝` Edit/Write, `🔧` MCP).
- **Persists reminders past the turn**: a builtin `schedule` MCP server + a `JobQueue` tick, since claude's own `CronCreate` dies the moment the turn's process exits.
- **Deploys as a systemd service**: one idempotent install script, safe to re-run after `git pull`.

## Project layout

```
pyproject.toml       project metadata + dependencies (python-telegram-bot, mcp)
SOUL.md              the persona — the ONLY file you change to make a different agent
.env                 secrets + config (copied from .env.example, gitignored)
src/agent/
  config.py          Settings dataclass + env parsing, in one place
  messaging.py       outbound Telegram sender + markdown→HTML rendering (sync, urllib)
  claude.py          one streaming `claude -p` turn + the live progress bubble
  handlers.py        Telegram handlers, access control, app wiring / entrypoint, JobQueue tick
  cron.py            dependency-free 5-field cron matcher
  schedule_store.py  schedules.json read/write layer (shared by bot + mcp_schedule.py)
  mcp_schedule.py    stdio MCP server exposing schedule CRUD to claude
  __main__.py        `python -m agent`
tests/               pure-function characterization tests (no network, no token)
deploy/              systemd unit + idempotent install script
```

## Commands

- `/start`: greet, confirm the bot is up.
- `/new`: clear this chat's rolling session; the next message starts a fresh `claude` conversation with no prior history.

## Groups

Off by default (owner-only DM). To let a group in:

0. **Disable privacy mode first** (skip if already done in step 2 of **Fork it**): [@BotFather](https://t.me/BotFather) → `/setprivacy` → select the bot → **Disable**. With privacy ON, a plain `@bot hi` text mention is silently dropped by Telegram before it ever reaches the bot (only commands and replies get through); if the bot is already in the group, remove and re-add it after disabling.
1. Add the bot to the group.
2. @-mention it once, it replies with that group's id.
3. Put the id in `ALLOWED_GROUP_IDS` (comma-separated for several) and restart.

In a group the bot only responds when @-mentioned or replied to, each chat keeps its own `claude` session, and messages are tagged with the sender's name so `claude` knows who's talking. Anyone who can address the bot in an allow-listed group can drive `claude` on the host: only add groups you trust.

## What's built in

- **Media in / files out**: photos, documents, voice, etc. download to `run/telegram/…` with the local path passed into the prompt; anything `claude` drops in `run/outbox/` is sent back at the end of the turn (a failed send is kept and reported, never silently lost).
- **Reply / quote context**: replying to (or Telegram-quoting part of) an earlier message threads that text into the prompt, so `claude` knows what "that" refers to, even hours later in a fresh session.
- **Reactions + typing indicator**: a random emoji acks receipt, swapped for 👍/👎 on completion; a "typing…" indicator runs until the turn's first outbound message appears.
- **Graceful failure handling**: a failed turn surfaces the real reason (usage limit, auth error, API 5xx) instead of a bare exit code, classified so a usage-limit or transient blip stays quiet on a scheduled tick (it self-heals) while an auth failure ("token likely needs refreshing") always surfaces.

## Scheduling

`claude` sets up persistent reminders via a builtin `mcp__schedule__*` MCP tool, always mounted regardless of persona. This exists because claude's own `CronCreate`/`CronList` live only in the current `claude -p` process's memory, and this bot spawns a fresh process per message: any `CronCreate` job evaporates the instant that turn ends. Real persistence lives in the bot itself: schedules sit in `run/schedules.json`, and a `JobQueue` tick every 60 seconds fires any schedule whose cron expression matches the current minute.

- **Downtime isn't backfilled**: a missed firing while the bot was down is skipped, not caught up (small in-process ticking delays ARE caught up, capped at 5 minutes).
- **`once: true` schedules self-delete** after firing: use these for one-off reminders instead of a cron matching one specific minute.
- **A same-minute restart can double-fire** (the "last processed minute" isn't persisted to disk): an accepted tradeoff for a single-owner bot.
- **No timezone field**: cron expressions evaluate against the bot process's local clock.
- **`NO_REPLY` sentinel**: for a monitoring-style schedule, tell claude to reply with exactly `NO_REPLY` when there's nothing to report; that tick sends nothing and deletes its own progress bubble. Only honored on a scheduled firing, never in a normal conversation.
- Ask claude to list/edit/remove schedules in plain language: it drives `mcp__schedule__*` itself, no separate command needed.

## Deploy (systemd)

```bash
bash deploy/install.sh
```

Idempotent, safe to re-run after `git pull`. It creates `.venv/` (`uv sync`, or a `pip install` fallback), fails fast if `.env` is missing, warns if the `claude` CLI isn't found, renders `deploy/agent.service` (placeholders filled from the script's own location and the invoking user) into `/etc/systemd/system/`, then `daemon-reload` + `enable --now` + `restart` (so a re-run actually applies new code, not a no-op on an already-running unit). The unit name comes from the repo directory's basename, so forked agents run side by side on one host; override with `SERVICE_NAME=foo bash deploy/install.sh`.

- A restart doesn't kill an in-flight turn: `KillMode=mixed` lets the running handler finish before systemd sends SIGKILL, capped by `TimeoutStopSec=300`.
- If the bot runs as **root** (e.g. a bare LXC container), `claude` refuses `--permission-mode bypassPermissions` unless `IS_SANDBOX=1` is set in `.env`. A non-root user avoids this entirely (recommended).
- Any Linux host with systemd works (VM, container, cloud box). Elsewhere (macOS launchd, a container platform), wrap `.venv/bin/python -m agent` directly: the bot itself is just a long-poll process with no open port.

Afterwards: `sudo systemctl status <name>` · `sudo journalctl -u <name> -f`.

## Skills

Recurring workflows (a daily standup, a weekly report) are better expressed as Claude Code skills than as ever-longer schedule prompts:

- Keep skills in their own repo, cloned somewhere on the host, and **symlink each one** into `~/.claude/skills/<name>` (headless `claude -p` picks up symlinks fine).
- **Link every skill a schedule references**, or claude silently improvises with no clue why. Verify with `claude -p 'List your available skills, names only.'`.
- No auto-pull: updating means `git pull` on the host (or have the schedule prompt do it as its first step).
- A skill shelling out to `gh api` for reads should put filters in the URL query string, not `-f` (which silently turns a GET into a POST, and GET-only endpoints answer with a 404).

## Extra MCP servers

Drop an `mcp-config.json` (gitignored, same class as `.env`) in the repo root and every turn mounts its servers alongside the builtin `schedule` one:

```json
{
  "mcpServers": {
    "sensorium": {
      "type": "http",
      "url": "https://sensorium.example.com/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

A `schedule` entry here is ignored in favor of the builtin (a warning is logged). `--strict-mcp-config` means these are the *only* MCP servers the bot sees: a `claude mcp add --scope user` done interactively on the box is invisible to it, same as to any other headless `claude -p`. No `--allowedTools` needed: `--permission-mode bypassPermissions` already trusts MCP tools the same way it trusts `Bash`/`Read`/`Write`.

To verify a server by hand: `set -a && . ./.env && set +a`, then `claude -p 'Use the <server> <tool> tool and report the result.' --mcp-config mcp-config.json --strict-mcp-config --permission-mode bypassPermissions`. Don't drop `bypassPermissions` here: without it every MCP call stops at a permission prompt headless mode auto-denies, which looks exactly like a broken server or a bad token when it's neither.

## Contributing

A template repo, but issues and PRs are welcome: ground rules in [CONTRIBUTING.md](https://github.com/zyx1121/.github/blob/main/CONTRIBUTING.md).

## License

[MIT](LICENSE) · fork it, rename the bot, make it yours.
