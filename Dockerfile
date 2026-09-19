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
# uv is used once, to turn uv.lock into a pinned requirements file. Same reason it is pinned.
ARG UV_VERSION=0.11.28

# git, curl and ripgrep are here because a turn's Bash/Grep tools are most of what the agent
# actually does; without them claude reports a missing binary instead of doing the work.
# The node tarball is checked against the SHASUMS256.txt of its own release before it is
# unpacked, so a mirror that hands back a different archive fails the build.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git ripgrep xz-utils; \
    rm -rf /var/lib/apt/lists/*; \
    case "$(dpkg --print-architecture)" in \
      amd64) node_arch=x64 ;; \
      arm64) node_arch=arm64 ;; \
      *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    tarball="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${tarball}"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"; \
    grep " ${tarball}\$" SHASUMS256.txt | sha256sum -c -; \
    tar -xJf "${tarball}" -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f "${tarball}" SHASUMS256.txt; \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"; \
    npm cache clean --force; \
    rm -rf /usr/local/include/node /usr/local/share/doc /usr/local/share/man /root/.npm; \
    claude --version

WORKDIR /app

# Dependencies come from uv.lock, the same file `uv run` uses, exported to a hash pinned
# requirements file and installed with --require-hashes: the same commit builds the same
# python-telegram-bot and the same mcp, today and in six months. --no-dev leaves the test
# only group (PyYAML) out of the image.
COPY pyproject.toml uv.lock ./
RUN set -eux; \
    pip install --no-cache-dir "uv==${UV_VERSION}"; \
    uv export --frozen --no-dev --no-emit-project --no-annotate --no-header \
      -o /tmp/requirements.txt; \
    pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt; \
    pip uninstall -y -q uv; \
    rm -f /tmp/requirements.txt

# The project itself, installed (not editable) and without resolving again: src/agent ends up
# in site-packages, so `python -m agent` works from any cwd. AGENT_HOME below, not a __file__
# climb, is what points the bot at SOUL.md and run/, see config.load_settings.
COPY src ./src
COPY SOUL.md ./
RUN pip install --no-cache-dir --no-deps .

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
