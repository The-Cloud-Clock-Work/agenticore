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

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml .
COPY agenticore/ agenticore/

RUN pip install --no-cache-dir . && \
    pip uninstall -y pip setuptools wheel 2>/dev/null; true

# ── Stage 3: Runtime ─────────────────────────────────────────────
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/The-Cloud-Clock-Work/agenticore"
LABEL org.opencontainers.image.description="Claude Code runner and orchestrator"
LABEL org.opencontainers.image.licenses="MIT"

ARG GH_VERSION

# Minimal runtime packages — no gnupg, no npm
RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/* && \
    pip uninstall -y pip setuptools 2>/dev/null; true

# gh CLI — direct tarball, no apt key needed
RUN ARCH=$(dpkg --print-architecture) && \
    curl -sL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${ARCH}.tar.gz" \
    | tar xz -C /tmp && \
    mv "/tmp/gh_${GH_VERSION}_linux_${ARCH}/bin/gh" /usr/local/bin/gh && \
    rm -rf /tmp/gh_* && \
    gh --version

# Node.js binary only (no npm) — needed by Claude CLI at runtime
COPY --from=node-builder /usr/local/bin/node /usr/local/bin/node

# Claude CLI (node_modules + launcher)
COPY --from=node-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node-builder /usr/local/bin/claude /usr/local/bin/claude

# Python venv with all dependencies
COPY --from=python-builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copy application source + defaults
COPY agenticore/ agenticore/
COPY defaults/ defaults/

# Non-root user (Claude CLI refuses bypassPermissions as root)
RUN useradd -m -s /bin/bash agenticore && \
    mkdir -p /home/agenticore/.agenticore/jobs \
             /home/agenticore/.agenticore/profiles \
             /home/agenticore/agenticore-repos \
             /app/logs && \
    chown -R agenticore:agenticore /app /home/agenticore

ENV AGENTICORE_TRANSPORT=sse \
    AGENTICORE_HOST=0.0.0.0 \
    AGENTICORE_PORT=8200 \
    AGENTICORE_REPOS_ROOT=/home/agenticore/agenticore-repos

USER agenticore

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8200/health || exit 1

CMD ["python", "-m", "agenticore"]
