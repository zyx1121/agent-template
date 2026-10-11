# agent-template

Bun service that connects a Telegram bot to headless Claude Code. Forks reuse it through `FROM ghcr.io/zyx1121/agent-template:<major>`, so a change here reaches every fork on its next upgrade. Read [README.md](README.md) before you change anything.

## Rules

- Keep the image generic. Persona text goes in `SOUL.md`, and fork-specific tools go in the fork's Dockerfile, not here.
- Tokens reach the process only as environment variables. Never log them, and never write them into the image or `run/`.
- Keep one turn at a time. Concurrency changes need a design first.
- A breaking change to `.env`, `compose.yaml` or the image layout needs a major version, because forks pin the major tag.
- Repository documents and code comments are English.

## Where to run things

In Loki's environment, the MacBook is for editing and git only. Run `bun install`, tests, `docker build` and live turns on `ssh sandbox` in a login shell (`bash -lc`). Do not install dependencies in the iCloud clone.

## Verify

```sh
bun install
bunx tsc --noEmit
bun test
docker build -t agent-template:dev .
```

For a change to turns, streaming or schedules, also run `bun test/live-turn.ts` with a real `CLAUDE_CODE_OAUTH_TOKEN`, then send a message to a test bot.

## Release

Each push to main publishes `ghcr.io/zyx1121/agent-template:sha-<commit>` and `:main`. A `v*` tag publishes the SemVer tags and `:latest`. Forks move when they change their `FROM` tag.
