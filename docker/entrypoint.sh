#!/bin/bash
set -e

# Purge stale gh credential helpers from persistent gitconfig (gh CLI removed in v0.10.2)
git config --global --unset-all credential.https://github.com.helper 2>/dev/null || true
git config --global --unset-all credential.https://gist.github.com.helper 2>/dev/null || true

# Clear stale NFS locks in uv tools cache (NFS .nfs files prevent uv from replacing dirs on restart)
rm -rf "${HOME:?}/.local/share/uv/tools/agentihooks" 2>/dev/null || true

# Resolve agentihooks source (priority: local mount > git URL > installed venv)
if [ -d "/opt/agentihooks-src" ]; then
  # Local dev: ~/dev/agentihooks mounted at /opt/agentihooks-src
  uv pip install --quiet -e /opt/agentihooks-src
elif [ -n "${AGENTICORE_AGENTIHOOKS_URL:-}" ]; then
  # K8s / CI: clone to shared FS, then editable install.
  # Editable install means hot-reload (git fetch in background) updates the
  # live code and profile files without needing a re-install or pod restart.
  _hooks_dest="${AGENTICORE_SHARED_FS_ROOT:-$HOME/.agenticore}/agentihooks"
  if [ ! -d "$_hooks_dest/.git" ]; then
    git clone --quiet "$AGENTICORE_AGENTIHOOKS_URL" "$_hooks_dest"
  else
    git -C "$_hooks_dest" fetch --all --prune --quiet
    git -C "$_hooks_dest" reset --hard origin/HEAD --quiet
  fi
  uv pip install --quiet -e "$_hooks_dest"
fi
# else: use package already installed in /opt/venv at build time

# Wire hooks, skills, agents, CLAUDE.md into ~/.claude
# HOME determines where: /home/agenticore (local) or /shared (K8s)
# agentihooks reads AGENTIHOOKS_PROFILE and AGENTIHOOKS_MCP_FILE env vars directly
agentihooks init

# Install pod-specific shell functions into ~/.bashrc (for exec sessions)
# Always replace the block so new builds update the PVC copy
[ -f "$HOME/.bashrc" ] && [ ! -w "$HOME/.bashrc" ] && chmod u+w "$HOME/.bashrc" 2>/dev/null || true
if [ -f "/opt/agenticore/bashrc" ]; then
  # Strip old block if present, then append fresh copy
  sed -i '/# agenticore-shell/,$d' "$HOME/.bashrc" 2>/dev/null || true
  { echo "# agenticore-shell"; cat /opt/agenticore/bashrc; } >> "$HOME/.bashrc" 2>/dev/null || true
fi

export CLAUDE_CODE_HOME_DIR="${CLAUDE_CODE_HOME_DIR:-$HOME}"

exec "$@"
