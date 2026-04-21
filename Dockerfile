# 2-stage build for agenticore — minimises CVE surface.
#
# Stage 1: python-builder — install Python deps into a venv
# Stage 2: runtime        — lean python:3.13-slim + copied artefacts
#
# Claude Code installed via native installer (no Node.js required).
#
# Build:
#   docker build -t agenticore .

# ── Stage 1: Python builder ──────────────────────────────────────
FROM python:3.13-slim AS python-builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN uv venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml .
COPY agenticore/ agenticore/

ARG AGENTIHOOKS_BUST_CACHE=1
RUN uv pip install --no-cache ".[telegram]" && \
    uv pip install --no-cache "agentihooks @ git+https://github.com/The-Cloud-Clock-Work/agentihooks.git"

# ── Stage 2: Runtime ─────────────────────────────────────────────
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/The-Cloud-Clock-Work/agenticore"
LABEL org.opencontainers.image.description="Claude Code runner and orchestrator"
LABEL org.opencontainers.image.licenses="MIT"

# Runtime packages — dev/debug tools + AWS CLI deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      git openssh-client curl wget jq vim less \
      unzip groff \
      tini \
      netcat-openbsd iputils-ping dnsutils procps && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /etc/ssh && ssh-keyscan github.com >> /etc/ssh/ssh_known_hosts 2>/dev/null

# uv — fast Python package installer
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# AWS CLI
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws && \
    aws --version

# Bun (required by Claude Code channel plugins: Telegram, Discord, etc.)
RUN curl -fsSL https://bun.sh/install | bash && \
    cp --dereference /root/.bun/bin/bun /usr/local/bin/bun

# Claude Code — native binary install (no Node.js required)
RUN curl -fsSL https://claude.ai/install.sh | bash && \
    cp /root/.local/bin/claude /usr/local/bin/claude && \
    rm -rf /root/.local/bin/claude && \
    claude --version

# Telegram channel plugin — baked in so agents don't need runtime install
RUN claude plugin marketplace add anthropics/claude-plugins-official && \
    claude plugin install telegram@claude-plugins-official

# Python venv with all dependencies
COPY --from=python-builder /opt/venv /opt/venv
ENV PATH="/home/agenticore/.local/bin:/opt/venv/bin:$PATH"

WORKDIR /app

# Copy application source
COPY agenticore/ agenticore/

# Non-root user (Claude CLI refuses bypassPermissions as root)
RUN useradd -m -s /bin/bash agenticore && \
    mkdir -p /home/agenticore/.agenticore/jobs \
             /home/agenticore/.agenticore/profiles \
             /home/agenticore/agenticore-repos \
             /opt/agenticore \
             /app/logs \
             /app/worktrees \
             /app/package \
             /app/evaluation \
             /shared && \
    chown -R agenticore:agenticore /app /home/agenticore /opt/venv /opt/agenticore /shared

# Copy baked-in Claude config (plugins, marketplace) to agenticore user
# Then pre-install Telegram plugin deps so they're baked into the image
RUN cp -r /root/.claude /home/agenticore/.claude && \
    chown -R agenticore:agenticore /home/agenticore/.claude && \
    cd /home/agenticore/.claude/plugins/cache/claude-plugins-official/telegram/*/  && \
    bun install --no-summary

# Default .agentihooks.json with brain+amygdala channels (overridden by agentihub if AGENTIHUB_AGENT set)
RUN echo '{"profile":"agenticore","channels":["brain","amygdala"]}' > /app/package/.agentihooks.json && \
    chown agenticore:agenticore /app/package/.agentihooks.json

# Pod-specific shell functions
COPY docker/bashrc /opt/agenticore/bashrc

ENV AGENTICORE_TRANSPORT=sse \
    AGENTICORE_HOST=0.0.0.0 \
    AGENTICORE_PORT=8200 \
    AGENTICORE_REPOS_ROOT=/home/agenticore/agenticore-repos

USER agenticore

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8200/health || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "agenticore", "serve"]
