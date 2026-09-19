# The kitbash deploy target: one image, one long-poll process, no open port.
#
# This file sits at the repo root, not in deploy/kitbash/, because a kitbash Package builds
# only from inside its own folder (PLAN.md 2.5 rule 4, enforced by the daemon) and the
# containerfile has to sit at the root of that build context. The context has to hold src/,
# SOUL.md and pyproject.toml, so the Package folder IS the repo root: kitbash.yaml next to
# this file is its manifest, and the rest of the target lives in deploy/kitbash/.
FROM python:3.12-slim-bookworm

# The `claude` CLI, pinned. A rolling `latest` would change the agent's behaviour on a
# rebuild the manifest never mentions.
ARG CLAUDE_CODE_VERSION=2.1.278
# Node is installed from nodejs.org, not from a distro package, so this line is the version.
ARG NODE_VERSION=22.22.0

# git, curl and ripgrep are here because a turn's Bash/Grep tools are most of what the agent
# actually does; without them claude reports a missing binary instead of doing the work.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git ripgrep xz-utils; \
    rm -rf /var/lib/apt/lists/*; \
    case "$(dpkg --print-architecture)" in \
      amd64) node_arch=x64 ;; \
      arm64) node_arch=arm64 ;; \
      *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
      | tar -xJ -C /usr/local --strip-components=1 --no-same-owner; \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"; \
    npm cache clean --force; \
    rm -rf /usr/local/include/node /usr/local/share/doc /usr/local/share/man /root/.npm; \
    claude --version

WORKDIR /app

# Dependencies and the package itself, installed (not editable): src/agent ends up in
# site-packages, so `python -m agent` works from any cwd. AGENT_HOME below, not a __file__
# climb, is what points the bot at SOUL.md and run/, see config.load_settings.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY SOUL.md ./
COPY deploy/kitbash/entrypoint.sh /usr/local/bin/agent-entrypoint
RUN chmod +x /usr/local/bin/agent-entrypoint

# AGENT_HOME is the image's /app, so the persona a fork edits travels in the image and is
# never shadowed by whatever is on the mount. Only mutable state lives on the mount, which
# is run/ (schedules.json, the per-chat session ids, attachments, outbox) plus HOME, so
# claude's own session rollouts under $HOME/.claude survive a restart too.
ENV AGENT_HOME=/app \
    HOME=/app/run/home \
    CLAUDE_BIN=/usr/local/bin/claude \
    IS_SANDBOX=1 \
    PYTHONUNBUFFERED=1

# The entrypoint checks its environment, writes the kitbash MCP server config, then execs
# the bot, so `python -m agent` is PID 1 and PTB's own SIGTERM handler runs on proc_stop.
ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
