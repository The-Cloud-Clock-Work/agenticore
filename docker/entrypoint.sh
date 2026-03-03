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
PROFILE="${AGENTICORE_DEFAULT_PROFILE:-default}"
agentihooks global --profile "$PROFILE"

# Install gateway MCP servers if file path is configured and exists on the volume
if [ -n "${AGENTIHOOKS_MCP_FILE:-}" ] && [ -f "$AGENTIHOOKS_MCP_FILE" ]; then
  agentihooks --mcp "$AGENTIHOOKS_MCP_FILE"
fi

# Append pod-specific shell functions to ~/.bashrc (for exec sessions)
[ -f "/opt/agenticore/bashrc" ] && cat /opt/agenticore/bashrc >> "$HOME/.bashrc"

# Install colt-tools (personal shell toolbelt)
COLT_TOOLS_DIR="$HOME/colt-tools"
if [ ! -d "$COLT_TOOLS_DIR" ]; then
  echo "Installing colt-tools..."
  gh repo clone nestorcolt/colt-tools "$COLT_TOOLS_DIR" 2>/dev/null || \
    echo "Warning: Could not clone colt-tools (check GH_TOKEN)"
fi
if [ -d "$COLT_TOOLS_DIR" ]; then
  cd "$COLT_TOOLS_DIR" && git pull --ff-only 2>/dev/null || true
  cd /app
  bash "$COLT_TOOLS_DIR/install.sh"
fi

exec "$@"
