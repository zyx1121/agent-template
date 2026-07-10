# agent-template

Minimal Telegram ↔ Claude Code bridge: one owner, one bot, one rolling `claude -p`
conversation. Long-polling only (no webhook, no open port), single-user (only
`OWNER_USER_ID` is served), single process (`bot.py`). This repo is a **GitHub
template** — fork it, swap the persona, deploy.

## How this template is meant to be used

There's deliberately no `create-agent` CLI or skill. Spinning up a new agent is a
handful of one-off steps — fork, write a persona, collect three tokens, deploy — that
don't recur often enough to justify freezing into a tool, and the deploy step differs
per target anyway.

The intended flow: hand this repo to a coding agent (Claude Code, etc.) and say *"open
me a new agent from this template."* The sections below double as a **runbook for that
agent** — it drives the mechanical parts (fork, edit `AGENT.md`, fill `.env`, run the
deploy) and pauses for the parts only a human can do (approve the bot in @BotFather,
run `claude setup-token` in a browser).

## Open a new agent

1. **Fork from the template.**
   ```
   gh repo create <your-new-agent-name> --template <owner>/agent-template --private --clone
   cd <your-new-agent-name>
   ```
   (Replace `<owner>/agent-template` with this template's actual `owner/repo` once it's pushed
   to GitHub.)

2. **Write the persona.** Edit `AGENT.md` — it's the *only* file you're expected to
   change to make this a different agent. Everything under "你是 / 語氣 / 你會做的事 /
   你不會做的事" gets injected as the Claude system prompt on every turn
   (`claude -p --append-system-prompt "$(cat AGENT.md)"`). `bot.py` / `tg_send.py` stay as-is.

3. **Get a Telegram bot token.** Talk to [@BotFather](https://t.me/BotFather),
   `/newbot`, copy the token → `TELEGRAM_BOT_TOKEN`.

4. **Get your Telegram user id.** Talk to [@userinfobot](https://t.me/userinfobot) →
   `OWNER_USER_ID`. Only messages from this id are served; everyone else is logged and
   ignored.

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
   to this repo's directory — only needed if you run `bot.py` from elsewhere).

## Run it locally

Requires Python 3.10+ and the [`claude` CLI](https://docs.claude.com/en/docs/claude-code)
installed and logged in (or `CLAUDE_CODE_OAUTH_TOKEN` set, per above).

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

`bot.py` auto-loads `.env` from `AGENT_HOME` on startup (a minimal loader, not
`python-dotenv` — already-set env vars always win), so this is the entire local flow.
Message your bot on Telegram; it replies via a resumed `claude -p` session.

## Run it on a VM / LXC (systemd, always-on)

For a PVE VM or keel LXC where the bot should survive reboots and crash-restart:

```
bash deploy/install.sh
```

This is idempotent — safe to re-run after `git pull` to pick up code changes. It:

- creates `.venv/` and installs `requirements.txt` if missing
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
macOS launchd, a container platform, a process manager — wrap `.venv/bin/python bot.py`
however that target expects; the bot itself is just a long-poll process with no open port.

## Commands

- `/start` — greet, confirm the bot is up.
- `/new` — clear the rolling session (`run/session`); the next message starts a fresh
  `claude` conversation with no prior turn history.

## Extending

This skeleton is **text-only** — `on_media` in `bot.py` just replies "目前這個骨架只收
文字訊息" for photos/voice/documents/etc. To wire media in: in `on_media`, download the
Telegram file to a local path, then build the prompt with that path so `claude -p` can
open it with its own file tools — same turn model as text, just with a download step first.
