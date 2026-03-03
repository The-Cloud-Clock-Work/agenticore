# 2-stage build for agenticore — minimises CVE surface.
#
# Stage 1: python-builder — install Python deps into a venv
# Stage 2: runtime        — lean python:3.13-slim + copied artefacts
#
# Claude Code installed via native installer (no Node.js required).
#
# Build:
#   docker build -t agenticore .

# ── pinned tool versions (easy to bump) ──────────────────────────
ARG GH_VERSION=2.87.3

# ── Stage 1: Python builder ──────────────────────────────────────
FROM python:3.13-slim AS python-builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN uv venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml .
COPY agenticore/ agenticore/

RUN uv pip install --no-cache . && \
    uv pip install --no-cache "agentihooks @ git+https://github.com/The-Cloud-Clock-Work/agentihooks.git"

# ── Stage 2: Runtime ─────────────────────────────────────────────
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/The-Cloud-Clock-Work/agenticore"
LABEL org.opencontainers.image.description="Claude Code runner and orchestrator"
LABEL org.opencontainers.image.licenses="MIT"

ARG GH_VERSION

# Runtime packages — dev/debug tools + AWS CLI deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      git curl wget jq vim less \
      unzip groff \
      netcat-openbsd iputils-ping dnsutils procps && \
    rm -rf /var/lib/apt/lists/*

# uv — fast Python package installer (used by entrypoint for runtime installs)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# AWS CLI
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws && \
    aws --version

# gh CLI — direct tarball, no apt key needed
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL --retry 3 --retry-delay 2 -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${ARCH}.tar.gz" && \
    tar xzf /tmp/gh.tar.gz -C /tmp && \
    mv "/tmp/gh_${GH_VERSION}_linux_${ARCH}/bin/gh" /usr/local/bin/gh && \
    rm -rf /tmp/gh* && \
    gh --version

# Claude Code — native binary install (no Node.js required)
RUN curl -fsSL https://claude.ai/install.sh | bash && \
    cp /root/.local/bin/claude /usr/local/bin/claude && \
    rm -rf /root/.local/bin/claude && \
    claude --version

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
             /app/package \
             /app/evaluation \
             /shared && \
    chown -R agenticore:agenticore /app /home/agenticore /opt/venv /opt/agenticore /shared

# Pod-specific shell functions + entrypoint
COPY docker/bashrc /opt/agenticore/bashrc
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV AGENTICORE_TRANSPORT=sse \
    AGENTICORE_HOST=0.0.0.0 \
    AGENTICORE_PORT=8200 \
    AGENTICORE_REPOS_ROOT=/home/agenticore/agenticore-repos

USER agenticore

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8200/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "agenticore"]
