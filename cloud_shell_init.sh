#!/usr/bin/env bash
# ==============================================================================
# CloudStream WebDAV Bridge - Google Cloud Shell 1-Click Bootstrap Initializer
# Usage in Google Cloud Shell terminal:
#   curl -sSL https://raw.githubusercontent.com/a48sahilrahman-create/cloud-stream-bridge/main/cloud_shell_init.sh | bash
# ==============================================================================

set -e

BOLD="\033[1m"
GREEN="\033[1;32m"
CYAN="\033[1;36m"
YELLOW="\033[1;33m"
RESET="\033[0m"

echo -e "${CYAN}================================================================${RESET}"
echo -e "${GREEN}${BOLD}  🎬 CloudStream WebDAV Bridge - Google Cloud Shell Initializer ${RESET}"
echo -e "${CYAN}================================================================${RESET}"

REPO_URL="https://github.com/a48sahilrahman-create/cloud-stream-bridge.git"
INSTALL_DIR="$HOME/cloud-stream-bridge"

# 1. Clone or update repository in user's persistent 5GB home directory
if [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "${CYAN}[*] Updating existing installation at ${INSTALL_DIR}...${RESET}"
    cd "$INSTALL_DIR"
    git fetch origin main --quiet || true
    git reset --hard origin/main --quiet || git pull --quiet || true
else
    echo -e "${CYAN}[*] Cloning CloudStream Bridge to ${INSTALL_DIR}...${RESET}"
    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
    fi
    git clone "$REPO_URL" "$INSTALL_DIR" --quiet
    cd "$INSTALL_DIR"
fi

# 2. Check for python3
if ! command -v python3 &>/dev/null; then
    echo -e "${YELLOW}[!] Python3 not found in PATH. Please ensure Python is installed.${RESET}"
    exit 1
fi

# 3. Launch persistent runner with anti-idle heartbeat
echo -e "${GREEN}[*] Launching CloudStream Bridge persistent runner...${RESET}"
exec python3 cloud_shell_runner.py
