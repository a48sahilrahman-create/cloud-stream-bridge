#!/usr/bin/env bash
# ==============================================================================
# CloudStream WebDAV Bridge - Step 2: Google Cloud Shell Launcher (1-Click Init)
# Routes via unique hardware-anchored device username (e.g. rmx3031-4f9a2e81c0d5)
# with zero Google Sign-In dependencies.
#
# Step Workflow:
#   Step 1: Device Identity & Pointer Hub (CloudStream Android App)
#   Step 2: Google Cloud Shell Launcher (this script)
#   Step 3: Permanent CX File Explorer Setup (Android TV & Phone)
#
# Usage in Google Cloud Shell terminal:
#   curl -sSL https://raw.githubusercontent.com/a48sahilrahman-create/cloud-stream-bridge/main/cloud_shell_init.sh | bash -s <deviceUsername> <hubUrl>
#   or: bash cloud_shell_init.sh <deviceUsername> <hubUrl>
# ==============================================================================

set -e

BOLD="\033[1m"
GREEN="\033[1;32m"
CYAN="\033[1;36m"
YELLOW="\033[1;33m"
WHITE="\033[1;37m"
RESET="\033[0m"

USER_ID="${1:-${CLOUDSTREAM_USER_ID:-}}"
HUB_URL="${2:-${CLOUDSTREAM_HUB_URL:-https://cloudstream-hub.onrender.com}}"

# Prompt for unique hardware device username if not supplied via argument or env var
if [ -z "$USER_ID" ]; then
    if [ -t 0 ]; then
        read -r -p "Enter unique device username from Step 1 (e.g. rmx3031-4f9a2e81c0d5, zero Google Sign-In) [default]: " INPUT_USER
        USER_ID="${INPUT_USER:-default}"
    elif [ -e /dev/tty ]; then
        read -r -p "Enter unique device username from Step 1 (e.g. rmx3031-4f9a2e81c0d5, zero Google Sign-In) [default]: " INPUT_USER </dev/tty 2>/dev/null || true
        USER_ID="${INPUT_USER:-default}"
    else
        USER_ID="default"
    fi
fi

echo -e "${CYAN}================================================================${RESET}"
echo -e "${GREEN}${BOLD}  🎬 Step 2: Google Cloud Shell Launcher (CloudStream Bridge)   ${RESET}"
echo -e "${CYAN}================================================================${RESET}"
echo -e "${CYAN}[*] Unique Device Username: ${BOLD}${GREEN}${USER_ID}${RESET} (Zero Google Sign-In)"
echo -e "${CYAN}[*] Central Pointer Hub:    ${BOLD}${WHITE}${HUB_URL}${RESET}"

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

# 3. Launch persistent runner with anti-idle heartbeat and multi-user arguments
echo -e "${GREEN}[*] Launching Step 2 persistent runner (preparing Step 3 CX File Explorer setup)...${RESET}"
exec python3 cloud_shell_runner.py --user "${USER_ID}" --hub "${HUB_URL}"
