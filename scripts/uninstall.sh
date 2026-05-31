#!/usr/bin/env bash
# =============================================================================
# xui-limiter uninstaller
# =============================================================================
# Removes the systemd service and binaries. Config + logs are kept by default
# unless --purge is passed.
#
# Usage: sudo bash scripts/uninstall.sh [--purge]
#
# Powered By Piriz  |  Telegram: @ThePiriz
# =============================================================================

set -euo pipefail

INSTALL_DIR="/opt/xui-limiter"
CONFIG_DIR="/etc/xui-limiter"
LOG_FILE="/var/log/xui-limiter.log"
SERVICE_NAME="xui-limiter"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then
    PURGE=1
fi

red()    { printf '\033[1;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { red "must run as root"; exit 1; }

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}\.service"; then
    systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
fi

rm -f "${SERVICE_FILE}"
systemctl daemon-reload

rm -rf "${INSTALL_DIR}"

if [[ "${PURGE}" -eq 1 ]]; then
    rm -rf "${CONFIG_DIR}"
    rm -f  "${LOG_FILE}"*
    green "uninstalled and purged config + logs"
else
    yellow "left config (${CONFIG_DIR}) and logs (${LOG_FILE}) in place"
    yellow "pass --purge to remove those too"
fi

green "done"
echo ""
echo "------------------------------------------------------------"
printf '\033[1m%s\033[0m\n' " Powered By Piriz   |   Telegram: @ThePiriz"
echo "------------------------------------------------------------"
