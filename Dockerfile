FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/The-Cloud-Clock-Work/agenticore"
LABEL org.opencontainers.image.description="Claude Code runner and orchestrator"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# System deps for git operations + Node.js for Claude CLI
RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install gh CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Install Claude CLI
RUN npm install -g @anthropic-ai/claude-code && claude --version

COPY pyproject.toml .
COPY agenticore/ agenticore/
COPY defaults/ defaults/

RUN pip install --no-cache-dir -e .

# Create non-root user (Claude CLI refuses bypassPermissions as root)
RUN useradd -m -s /bin/bash agenticore && \
    mkdir -p /home/agenticore/.agenticore/jobs \
             /home/agenticore/.agenticore/profiles \
             /home/agenticore/agenticore-repos \
             /app/logs && \
    chown -R agenticore:agenticore /app /home/agenticore

ENV AGENTICORE_TRANSPORT=sse
ENV AGENTICORE_HOST=0.0.0.0
ENV AGENTICORE_PORT=8200
ENV AGENTICORE_REPOS_ROOT=/home/agenticore/agenticore-repos

USER agenticore

EXPOSE 8200

CMD ["python", "-m", "agenticore"]
