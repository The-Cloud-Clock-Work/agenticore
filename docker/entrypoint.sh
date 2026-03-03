#!/bin/bash
set -e

# Resolve agentihooks source (priority: local mount > git URL > installed venv)
if [ -d "/opt/agentihooks-src" ]; then
  # Local dev: ~/dev/agentihooks mounted at /opt/agentihooks-src
  uv pip install --quiet -e /opt/agentihooks-src
elif [ -n "${AGENTICORE_AGENTIHOOKS_URL:-}" ]; then
  # K8s / CI: install from git URL at runtime
  uv pip install --quiet "agentihooks @ git+${AGENTICORE_AGENTIHOOKS_URL}"
fi
# else: use package already installed in /opt/venv at build time

# Wire hooks, skills, agents, CLAUDE.md into ~/.claude
# HOME determines where: /home/agenticore (local) or /shared (K8s)
# agentihooks reads AGENTIHOOKS_PROFILE and AGENTIHOOKS_MCP_FILE env vars directly
agentihooks global

# Append pod-specific shell functions to ~/.bashrc (for exec sessions)
# /shared may have root-owned files from init job — fix ownership first
[ -f "$HOME/.bashrc" ] && [ ! -w "$HOME/.bashrc" ] && chmod u+w "$HOME/.bashrc" 2>/dev/null || true
if [ -f "/opt/agenticore/bashrc" ] && ! grep -q "# agenticore-shell" "$HOME/.bashrc" 2>/dev/null; then
  { echo "# agenticore-shell"; cat /opt/agenticore/bashrc; } >> "$HOME/.bashrc" 2>/dev/null || true
fi

exec "$@"
