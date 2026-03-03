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
    pip install --no-cache-dir "agentihooks @ git+https://github.com/The-Cloud-Clock-Work/agentihooks.git" && \
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
             /app/logs && \
    chown -R agenticore:agenticore /app /home/agenticore

# Interactive shell functions for exec'd sessions
RUN cat >> /etc/bash.bashrc << 'EOF'
anton() { ANTHROPIC_API_KEY="${LITELLM_MCP_GATEWAY_KEY:-${ANTHROPIC_API_KEY:-}}" claude --dangerously-skip-permissions "$@"; }
wkt-get() {
  local query="${1:-}" projects_dir="$HOME/.claude/projects"
  local -a base_paths=()
  for proj_dir in "$projects_dir"/*/; do
    local dir_name="${proj_dir%/}"; dir_name="${dir_name##*/}"
    [[ "$dir_name" == *"--worktree"* ]] && continue
    local idx="$proj_dir/sessions-index.json"
    [ -f "$idx" ] || continue
    local orig_path
    orig_path=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('originalPath',''))" "$idx" 2>/dev/null)
    [ -n "$orig_path" ] && [ -d "$orig_path" ] || continue
    if [ -z "$query" ] || [[ "${orig_path,,}" == *"${query,,}"* ]]; then base_paths+=("$orig_path"); fi
  done
  [ ${#base_paths[@]} -gt 0 ] || { echo "No projects found${query:+ matching '$query'}"; return 1; }
  local i=1; for p in "${base_paths[@]}"; do printf "%d) %s\n" "$i" "$p"; ((i++)); done
  local sel; read -rp "Project [1-${#base_paths[@]}]: " sel
  [[ "$sel" =~ ^[0-9]+$ ]] && [ "$sel" -ge 1 ] && [ "$sel" -le "${#base_paths[@]}" ] || { echo "Invalid"; return 1; }
  echo "-> ${base_paths[$((sel-1))]}"; cd "${base_paths[$((sel-1))]}" || return 1
}
EOF

ENV AGENTICORE_TRANSPORT=sse \
    AGENTICORE_HOST=0.0.0.0 \
    AGENTICORE_PORT=8200 \
    AGENTICORE_REPOS_ROOT=/home/agenticore/agenticore-repos

USER agenticore

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8200/health || exit 1

CMD ["python", "-m", "agenticore"]
