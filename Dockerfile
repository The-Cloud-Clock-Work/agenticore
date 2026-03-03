# 3-stage build for agenticore — minimises CVE surface by keeping
# npm, pip, setuptools and gnupg out of the runtime image.
#
# Stage 1: node-builder  — install Claude CLI via npm
# Stage 2: python-builder — install Python deps into a venv
# Stage 3: runtime        — lean python:3.13-slim + copied artefacts
#
# Build:
#   docker build -t agenticore .

# ── pinned tool versions (easy to bump) ──────────────────────────
ARG GH_VERSION=2.87.3

# ── Stage 1: Node builder ────────────────────────────────────────
FROM node:22-slim AS node-builder

RUN npm install -g @anthropic-ai/claude-code && claude --version

# ── Stage 2: Python builder ──────────────────────────────────────
FROM python:3.13-slim AS python-builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml .
COPY agenticore/ agenticore/

RUN pip install --no-cache-dir . && \
    pip install --no-cache-dir "agentihooks @ git+https://github.com/The-Cloud-Clock-Work/agentihooks.git"

# ── Stage 3: Runtime ─────────────────────────────────────────────
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

# Node.js binary only (no npm) — needed by Claude CLI at runtime
COPY --from=node-builder /usr/local/bin/node /usr/local/bin/node

# Claude CLI only (exclude npm/corepack to avoid their CVEs)
COPY --from=node-builder /usr/local/lib/node_modules/@anthropic-ai /usr/local/lib/node_modules/@anthropic-ai
COPY --from=node-builder /usr/local/bin/claude /usr/local/bin/claude

# Claude looks for ripgrep at /usr/local/bin/vendor/ (relative to its install location)
RUN ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/vendor /usr/local/bin/vendor

# Python venv with all dependencies
COPY --from=python-builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copy application source
COPY agenticore/ agenticore/

# Non-root user (Claude CLI refuses bypassPermissions as root)
RUN useradd -m -s /bin/bash agenticore && \
    mkdir -p /home/agenticore/.agenticore/jobs \
             /home/agenticore/.agenticore/profiles \
             /home/agenticore/agenticore-repos \
             /opt/agenticore \
             /app/logs && \
    chown -R agenticore:agenticore /app /home/agenticore /opt/venv /opt/agenticore

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
