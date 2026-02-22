FROM python:3.12-slim

WORKDIR /app

# System deps for git operations
RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/*

# Install gh CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY agenticore/ agenticore/
COPY defaults/ defaults/

RUN pip install --no-cache-dir -e .

# Create dirs
RUN mkdir -p /app/logs /root/.agenticore/jobs /root/.agenticore/profiles /root/agenticore-repos

ENV AGENTICORE_TRANSPORT=sse
ENV AGENTICORE_HOST=0.0.0.0
ENV AGENTICORE_PORT=8200

EXPOSE 8200

CMD ["python", "-m", "agenticore"]
