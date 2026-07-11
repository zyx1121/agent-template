# agent-template

Minimal Telegram ↔ Claude Code bridge: one owner, one bot, one rolling `claude -p`
conversation. Long-polling only (no webhook, no open port), **single-owner** (the owner is
always served; allow-listed groups are opt-in), single process (`python -m agent`). This repo
is a **GitHub template** — fork it, swap the persona, deploy.

## How this template is meant to be used

There's deliberately no `create-agent` CLI or skill. Spinning up a new agent is a
handful of one-off steps — fork, write a persona, collect three tokens, deploy — that
don't recur often enough to justify freezing into a tool, and the deploy step differs
per target anyway.

The intended flow: hand this repo to a coding agent (Claude Code, etc.) and say *"open
me a new agent from this template."* The sections below double as a **runbook for that
agent** — it drives the mechanical parts (fork, edit `SOUL.md`, fill `.env`, run the
deploy) and pauses for the parts only a human can do (approve the bot in @BotFather,
run `claude setup-token` in a browser).

## Project layout

```
pyproject.toml       project metadata + the single dependency (python-telegram-bot)
SOUL.md              the persona — the ONLY file you change to make a different agent
.env                 secrets + config (copied from .env.example, gitignored)
src/agent/
  config.py          Settings dataclass + env parsing, in one place
  messaging.py       outbound Telegram sender + markdown→HTML rendering (sync, urllib)
  claude.py          one streaming `claude -p` turn + the live progress bubble
  handlers.py        Telegram handlers, access control, app wiring / entrypoint
  __main__.py        `python -m agent`
tests/               pure-function characterization tests (no network, no token)
deploy/              systemd unit + idempotent install script
```

## Open a new agent

1. **Fork from the template.**
   ```
   gh repo create <your-new-agent-name> --template <owner>/agent-template --private --clone
   cd <your-new-agent-name>
   ```
   (Replace `<owner>/agent-template` with this template's actual `owner/repo` once it's pushed
   to GitHub.)

2. **Write the persona.** Edit `SOUL.md` — it's the *only* file you're expected to
   change to make this a different agent. Everything under "你是 / 語氣 / 你會做的事 /
   你不會做的事" gets injected as the Claude system prompt on every turn
   (`claude -p --append-system-prompt "$(cat SOUL.md)"`). The `src/agent/` package stays as-is.

3. **Get a Telegram bot token.** Talk to [@BotFather](https://t.me/BotFather),
   `/newbot`, copy the token → `TELEGRAM_BOT_TOKEN`. While you're there, also run
   `/setprivacy` → select the new bot → **Disable**. Skip this and group mentions silently
   never reach the bot (see **Groups** below for why) — cheaper to do it now than to debug it
   later.

4. **Get your Telegram user id.** Talk to [@userinfobot](https://t.me/userinfobot) →
   `OWNER_USER_ID`. This id is the trust anchor — always served, in DM or any group. Everyone
   else is ignored unless they're in an allow-listed group (see **Groups** below).

5. **Get a Claude Code OAuth token.** On any machine with a browser and the `claude`
   CLI logged in:
   ```
   claude setup-token
   ```
   This prints a token (`sk-ant-oat01-…`, ~1 year validity) → `CLAUDE_CODE_OAUTH_TOKEN`.
   This is what lets `claude -p` authenticate headlessly on a server with no browser —
   don't use an interactive login token here, it won't auto-refresh.

6. **Fill in `.env`.**
   ```
   cp .env.example .env
   ```
   then edit it. Required: `TELEGRAM_BOT_TOKEN`, `OWNER_USER_ID`,
   `CLAUDE_CODE_OAUTH_TOKEN`. Optional (defaults shown): `AGENT_NAME=Agent` (display
   name), `CLAUDE_BIN=~/.local/bin/claude` (path to the `claude` CLI),
   `AGENT_TURN_TIMEOUT=1800` (seconds before a turn is killed), `AGENT_HOME` (defaults
   to this repo's directory — only needed if you run the bot from elsewhere).

## Run it locally

Requires the [`claude` CLI](https://docs.claude.com/en/docs/claude-code) installed and logged
in (or `CLAUDE_CODE_OAUTH_TOKEN` set, per above), plus [uv](https://docs.astral.sh/uv/):

```
uv run python -m agent
```

`uv` creates the virtualenv and installs the dependency on first run (Python 3.10+). The bot
auto-loads `.env` from `AGENT_HOME` on startup (a minimal loader, not `python-dotenv` —
already-set env vars always win), so this is the entire local flow. Message your bot on
Telegram; it replies via a resumed `claude -p` session.

Without uv, a plain venv works too — `pip install .` then `python -m agent`.

## Tests

The fiddly pure functions (markdown→HTML rendering, message chunking, env parsing) have
characterization tests that run with no network and no bot token:

```
uv run python -m unittest discover -s tests
```

## Run it on a VM / LXC (systemd, always-on)

For a Linux host with systemd where the bot should survive reboots and crash-restart:

```
bash deploy/install.sh
```

This is idempotent — safe to re-run after `git pull` to pick up code changes. It:

- creates `.venv/` and installs the project + dependency (via `uv sync`, or a `pip install`
  fallback if uv isn't present)
- checks `.env` exists (fails fast with instructions if not — see step 6 above)
- warns if the `claude` CLI isn't found at `CLAUDE_BIN` / on `PATH`
- renders `deploy/agent.service` (a template — placeholders `__REPO_DIR__`,
  `__RUN_USER__`, `__AGENT_NAME__` are filled in from the script's own location and the
  invoking user, nothing hardcoded) and installs it to `/etc/systemd/system/`
- `systemctl daemon-reload`, `enable --now`, and `restart` (so a re-run actually
  applies new code, not just a no-op `enable` on an already-running unit)

The unit name is derived from the repo directory's basename (e.g. cloning as
`my-agent/` installs `my-agent.service`), so multiple forked agents can run side by
side on the same host. Override with `SERVICE_NAME=foo bash deploy/install.sh` if you
want a different unit name than the directory.

Useful commands afterwards:
```
sudo systemctl status <service-name>
sudo journalctl -u <service-name> -f
```

If you'd rather hand-install the unit yourself instead of running the script, copy
`deploy/agent.service`, replace `__REPO_DIR__` / `__RUN_USER__` / `__AGENT_NAME__`
manually, then `systemctl daemon-reload && systemctl enable --now <name>.service`.

Any Linux host with systemd works (a VM, a container, a cloud box). For other setups —
macOS launchd, a container platform, a process manager — wrap `.venv/bin/python -m agent`
however that target expects; the bot itself is just a long-poll process with no open port.

If the bot runs as **root** (e.g. a bare LXC container), `claude` refuses
`--permission-mode bypassPermissions` for safety — add `IS_SANDBOX=1` to `.env` to allow it
inside a sandboxed container. Running as a non-root user avoids this entirely (recommended).

## Commands

- `/start` — greet, confirm the bot is up.
- `/new` — clear this chat's rolling session (`run/session-<chat_id>`); the next message
  starts a fresh `claude` conversation with no prior history.

## Groups

By default the bot only serves the owner in DM. To let a group use it:

0. **Privacy mode must be Disabled first** (skip if you already did this in step 3 of
   *Open a new agent*). Telegram's default privacy mode ON only delivers commands
   (`/foo@bot`) and replies to the bot's own messages — a plain `@bot hi` text mention is
   **silently dropped by Telegram before it ever reaches the bot** (no error, nothing to log,
   nothing to debug on this side). Fix: [@BotFather](https://t.me/BotFather) → `/setprivacy` →
   select the bot → **Disable**. If the bot is already in the group, the setting doesn't apply
   retroactively — remove it and re-add it after disabling.
1. Add the bot to the group.
2. @-mention it once — it replies with that group's `id`.
3. Put the id in `.env`'s `ALLOWED_GROUP_IDS` (comma-separated for several) and restart.

In a group the bot only responds when @-mentioned or replied to (it stays quiet in normal
chatter), each chat keeps its own `claude` session, and group messages are tagged with the
sender's name so `claude` knows who's talking. Note: anyone who can address the bot in an
allow-listed group can drive `claude` on the host — only add groups whose members you trust.

## What's built in

- **Media in** — photos / documents / voice / etc. are downloaded to `run/telegram/…` and
  their local path is passed into the prompt, so `claude` reads the file with its own tools.
- **Files out** — anything `claude` drops in `run/outbox/` is sent back to the chat at the end
  of the turn (failed sends are kept and reported, never silently lost).
- **Live progress** — one Telegram message updates in place with each tool `claude` runs
  (`📖 Read`, `⚡️ Bash …`, `📝 Edit`), so a long turn visibly shows what it's doing.
- **Reactions** — a random emoji acks receipt, overwritten with 👍 / 👎 when the turn ends.
