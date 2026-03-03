#!/bin/bash
# =============================================================================
# agenticore AI Services - Aliases Installer
# =============================================================================
# Usage: ./alias_setup.sh
#
# Installs shell aliases for the agenticore CLI.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASH_PROFILE="$HOME/.bashrc"

# Use single quotes for the block so variables are NOT expanded at install time
# shellcheck disable=SC2016
ALIASES_BLOCK='
# =============================================================================
# agenticore AI Services Aliases
# =============================================================================

# Agent container management
alias build_agent='"'"'agenticore agent --build'"'"'
alias run_agent='"'"'agenticore agent --run'"'"'
alias enter_agent='"'"'agenticore agent --enter'"'"'
alias stop_agent='"'"'agenticore agent --stop'"'"'
alias logs_agent='"'"'agenticore agent --logs'"'"'

# Registry image push
alias push_main='"'"'agenticore push --main'"'"'

# Environment helpers
alias loadenv='"'"'set -a; source .env; set +a'"'"'
alias sourcebash='"'"'source ~/.bashrc'"'"'

# Help - show all available aliases
agenticore_help() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "                    AGENTICORE DEV ALIASES & TOOLS"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  CONTAINER MANAGEMENT:"
    echo "    build_agent          Build agent Docker image"
    echo "    run_agent            Run container in detached mode"
    echo "    enter_agent          Shell into running container"
    echo "    stop_agent           Stop running container"
    echo "    logs_agent           View container logs"
    echo ""
    echo "  REGISTRY PUSH:"
    echo "    push_main            Build and push main image to registry"
    echo ""
    echo "  ENVIRONMENT:"
    echo "    loadenv              Load .env file into current shell"
    echo "    sourcebash           Reload ~/.bashrc"
    echo "    agenticore_help      Show this help message"
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
    read -p "Reinstall/update aliases? [y/N] " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled."
        exit 0
    fi
    # Remove existing block
    sed -i '/# agenticore AI Services Aliases/,/# End agenticore Aliases/d' "$BASH_PROFILE"
    echo "Removed existing aliases."
fi

# Append to .bashrc
echo "$ALIASES_BLOCK" >> "$BASH_PROFILE"

echo "✓ Aliases installed to $BASH_PROFILE"
echo ""
echo "Run 'source ~/.bashrc' or open a new terminal to use them."
echo ""
echo "Quick start:"
echo "  build_agent            # Build agent image"
echo "  run_agent              # Run container"
echo "  enter_agent            # Enter container"
echo "  logs_agent             # View container logs"
echo "  stop_agent             # Stop container"
echo ""
echo "Registry push:"
echo "  push_main              # Build and push main image"
echo ""
echo "Run 'agenticore_help' for full list of available commands"
