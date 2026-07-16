#!/usr/bin/env bash
# =============================================================================
# agenticore AI Services - Aliases Installer
# =============================================================================
# Usage: ./alias_setup.sh
#
# Installs shell aliases for the agenticore CLI.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Target the profile the shell actually sources on login, which differs by
# platform/shell: macOS Terminal.app spawns login shells (zsh default since
# Catalina, ~/.zshrc; bash falls back to ~/.bash_profile), Linux spawns
# interactive non-login shells (~/.bashrc).
if [[ "$(uname -s)" == "Darwin" ]]; then
    if [[ "${SHELL:-}" == */zsh ]]; then
        BASH_PROFILE="$HOME/.zshrc"
    else
        BASH_PROFILE="$HOME/.bash_profile"
    fi
else
    BASH_PROFILE="$HOME/.bashrc"
fi

# Use single quotes for the block so variables are NOT expanded at install time
# shellcheck disable=SC2016
ALIASES_BLOCK='
# =============================================================================
# agenticore AI Services Aliases
# =============================================================================

# Agent container management
alias ac_build_agent='"'"'agenticore agent --build'"'"'
alias ac_run_agent='"'"'agenticore agent --run'"'"'
alias ac_enter_agent='"'"'agenticore agent --enter'"'"'
alias ac_stop_agent='"'"'agenticore agent --stop'"'"'
alias ac_logs_agent='"'"'agenticore agent --logs'"'"'

# Dev compose stack
alias ac_compose_up='"'"'agenticore agent --compose-up'"'"'
alias ac_compose_down='"'"'agenticore agent --compose-down'"'"'
alias ac_compose_enter='"'"'agenticore agent --compose-enter'"'"'
alias ac_compose_logs='"'"'agenticore agent --compose-logs'"'"'

# Registry image push
alias ac_push_main='"'"'agenticore push --main'"'"'

# Help - show all available aliases
agenticore_help() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "                    AGENTICORE DEV ALIASES & TOOLS"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  CONTAINER MANAGEMENT:"
    echo "    ac_build_agent       Build agent Docker image"
    echo "    ac_run_agent         Run container in detached mode"
    echo "    ac_enter_agent       Shell into running container"
    echo "    ac_stop_agent        Stop running container"
    echo "    ac_logs_agent        View container logs"
    echo ""
    echo "  DEV COMPOSE STACK:"
    echo "    ac_compose_up        Compose up dev stack (docker-compose.dev.yml)"
    echo "    ac_compose_down      Compose down dev stack"
    echo "    ac_compose_enter     Shell into compose service"
    echo "    ac_compose_logs      Follow compose service logs"
    echo ""
    echo "  REGISTRY PUSH:"
    echo "    ac_push_main         Build and push main image to registry"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
}

# =============================================================================
# End agenticore Aliases
# =============================================================================
'

# Check if already installed
if grep -q "# agenticore AI Services Aliases" "$BASH_PROFILE" 2>/dev/null; then
    echo "agenticore aliases found in $BASH_PROFILE"
    echo ""
    read -p "Reinstall/update aliases? [y/N] " -n 1 -r || true
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled."
        exit 0
    fi
    # Remove existing block. GNU sed accepts a bare -i; BSD sed (macOS)
    # requires a backup-suffix argument, so pass one and delete it after.
    sed -i.bak '/# agenticore AI Services Aliases/,/# End agenticore Aliases/d' "$BASH_PROFILE" && rm -f "$BASH_PROFILE.bak"
    echo "Removed existing aliases."
fi

# Append to .bashrc
echo "$ALIASES_BLOCK" >> "$BASH_PROFILE"

echo "✓ Aliases installed to $BASH_PROFILE"
echo ""
echo "Run 'source ~/.bashrc' or open a new terminal to use them."
echo ""
echo "Quick start:"
echo "  ac_build_agent         # Build agent image"
echo "  ac_run_agent           # Run container"
echo "  ac_enter_agent         # Enter container"
echo "  ac_logs_agent          # View container logs"
echo "  ac_stop_agent          # Stop container"
echo "  ac_compose_up          # Start dev stack"
echo "  ac_compose_down        # Stop dev stack"
echo "  ac_compose_enter       # Shell into compose service"
echo "  ac_compose_logs        # Follow compose logs"
echo ""
echo "Registry push:"
echo "  ac_push_main           # Build and push main image"
echo ""
echo "Run 'agenticore_help' for full list of available commands"
