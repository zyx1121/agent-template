# agent-template: Bun + the Claude Code CLI + the tools turns commonly need.
# A fork builds FROM this image and adds SOUL.md, .claude/skills and its own CLIs.
FROM oven/bun:1.3.1-debian

ARG CLAUDE_VERSION=2.1.316
ENV DEBIAN_FRONTEND=noninteractive TZ=Asia/Taipei

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git jq ripgrep tini tzdata unzip openssh-client gh \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 -s /bin/bash agent 2>/dev/null || usermod -l agent -d /home/agent -m bun

# Guest turns (anyone but the owner) run as this uid; see agent-entrypoint.
RUN useradd -m -u 1001 -s /bin/bash guest

USER agent
WORKDIR /home/agent
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_VERSION}
ENV PATH=/home/agent/.local/bin:$PATH

# The guest uid runs the same claude binary, so it must be able to reach it.
USER root
RUN chmod 711 /home/agent

WORKDIR /app
COPY --chown=agent package.json tsconfig.json ./
COPY --chown=agent src ./src
COPY --chown=agent SOUL.md ./
COPY agent-entrypoint /usr/local/bin/agent-entrypoint
# State (sessions, schedules, files) and claude's own session rollouts live on the volume.
ENV AGENT_HOME=/app CLAUDE_CONFIG_DIR=/app/run/claude
VOLUME /app/run

# Starts as root to prepare run/ and drop to agent; a fork that sets USER keeps working, without the guest uid.
ENTRYPOINT ["tini", "--", "agent-entrypoint"]
CMD ["bun", "src/main.ts"]
