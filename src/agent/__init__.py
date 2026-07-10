"""agent — a minimal single-owner Telegram ↔ Claude Code bridge.

One owner is the trust anchor (always served, in DM or any group); the owner may optionally
share the bot with allow-listed groups, where any member can drive it via @-mention. Fork
this template, rewrite SOUL.md with a persona, fill .env, deploy — the bot itself is generic
and env-driven; SOUL.md is the only file you change to make a different agent.

Layout:
  config     — everything read from the environment, in one Settings dataclass
  messaging  — outbound Telegram HTTP sender + markdown→HTML rendering (sync, urllib)
  claude     — one `claude -p` streaming turn + the live progress bubble
  handlers   — Telegram update handlers, access control, and the app wiring / entrypoint
"""
