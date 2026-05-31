#!/usr/bin/env bash
# =============================================================================
# xui-limiter installer
# =============================================================================
#
# Installs xui-limiter as a systemd service that monitors 3x-ui clients and
# disables those exceeding the configured IP / traffic limits.
#
# Layout after install:
#   /opt/xui-limiter/            - code + virtualenv (idempotent reinstall)
#   /etc/xui-limiter/config.yaml - configuration (preserved on reinstall)
#   /var/log/xui-limiter.log     - log file
#   systemd unit: xui-limiter.service
#
# Source layout (this script reads from):
#   <repo>/xui_limiter.py
#   <repo>/requirements.txt
#   <repo>/config/config.example.yaml
#   <repo>/systemd/xui-limiter.service
#
# Usage: sudo bash scripts/install.sh
#
# Powered By Piriz  |  Telegram: @ThePiriz
# =============================================================================

set -euo pipefail

INSTALL_DIR="/opt/xui-limiter"
CONFIG_DIR="/etc/xui-limiter"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
SERVICE_NAME="xui-limiter"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
# scripts/install.sh sits one level below the repo root
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

red()    { printf '\033[1;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
blue()   { printf '\033[1;34m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

die() { red "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# detect_access_log
#
# Tries hard to find where xray actually writes its access log, in order:
#   1. Read xray's access path from 3x-ui's DB (xrayTemplateConfig.log.access),
#      then resolve relative paths against xray's real cwd from /proc/PID/cwd.
#   2. Scan xray's open file descriptors in /proc/PID/fd/ for *.log files.
#   3. Probe common known locations.
#   4. Filesystem-wide search filtered to xray/x-ui paths (slow, last resort).
#
# Prints the resolved absolute path on stdout, exits 0 on success.
# Exits 1 (with empty stdout) if nothing can be located.
# ---------------------------------------------------------------------------
detect_access_log() {
    # Init all locals to empty - we run under `set -u` so any later test on
    # an unset local (e.g. when xray isn't running yet) would abort the script.
    local xray_pid="" xray_cwd="" cfg_value="" resolved="" target="" hit="" fd=""

    xray_pid="$(pidof xray 2>/dev/null | awk '{print $1}' || true)"

    # --- Method 1: read configured path from x-ui DB ---
    if [[ -f /etc/x-ui/x-ui.db ]]; then
        cfg_value="$(python3 - <<'PYEOF' 2>/dev/null
import sqlite3, json, sys
try:
    conn = sqlite3.connect('/etc/x-ui/x-ui.db')
    row = conn.execute(
        "SELECT value FROM settings WHERE key='xrayTemplateConfig'"
    ).fetchone()
    if row:
        cfg = json.loads(row[0])
        access = (cfg.get('log') or {}).get('access') or ''
        sys.stdout.write(access)
except Exception:
    pass
PYEOF
)"
        if [[ -n "$cfg_value" ]]; then
            if [[ "$cfg_value" == /* ]]; then
                resolved="$cfg_value"
            else
                # Relative path - resolve against xray's real cwd
                if [[ -n "$xray_pid" ]] && [[ -L "/proc/${xray_pid}/cwd" ]]; then
                    xray_cwd="$(readlink "/proc/${xray_pid}/cwd" 2>/dev/null || true)"
                fi
                if [[ -z "$xray_cwd" ]]; then
                    xray_cwd="/usr/local/x-ui"
                fi
                # Strip leading "./" if present
                resolved="${xray_cwd%/}/${cfg_value#./}"
            fi
            if [[ -n "$resolved" ]]; then
                echo "$resolved"
                return 0
            fi
        fi
    fi

    # --- Method 2: scan xray's open fds for an access.log file ---
    if [[ -n "$xray_pid" ]] && [[ -d "/proc/${xray_pid}/fd" ]]; then
        for fd in "/proc/${xray_pid}/fd/"*; do
            [[ -L "$fd" ]] || continue
            target="$(readlink "$fd" 2>/dev/null || true)"
            if [[ "$target" == *access.log ]] || [[ "$target" == *access*.log ]]; then
                echo "$target"
                return 0
            fi
        done
    fi

    # --- Method 3: probe well-known locations ---
    local candidates=(
        "/usr/local/x-ui/access.log"
        "/usr/local/x-ui/bin/access.log"
        "/etc/x-ui/access.log"
        "/var/log/xray/access.log"
        "/var/log/3x-ui/access.log"
        "/var/log/x-ui/access.log"
        "/opt/x-ui/access.log"
    )
    for path in "${candidates[@]}"; do
        if [[ -f "$path" ]]; then
            echo "$path"
            return 0
        fi
    done

    # --- Method 4: filesystem search, filtered to xray/x-ui dirs ---
    hit="$(find / -xdev \( -path /proc -o -path /sys -o -path /run \) -prune -o \
                  -type f -name 'access.log' -print 2>/dev/null \
          | grep -iE 'x-ui|xray' | head -1 || true)"
    if [[ -n "$hit" ]]; then
        echo "$hit"
        return 0
    fi

    return 1
}

[[ $EUID -eq 0 ]] || die "must run as root: sudo bash $0"

bold ""
bold "============================================================"
bold "  xui-limiter installer"
bold "  Powered By Piriz  |  Telegram: @ThePiriz"
bold "============================================================"
bold ""

blue "==> Checking prerequisites"

if ! command -v systemctl >/dev/null 2>&1; then
    die "systemctl not found - this installer expects a systemd-based Linux"
fi

# Detect package manager
if command -v apt-get >/dev/null 2>&1; then
    PM=apt
elif command -v dnf >/dev/null 2>&1; then
    PM=dnf
elif command -v yum >/dev/null 2>&1; then
    PM=yum
else
    PM=
fi

ensure_python() {
    if command -v python3 >/dev/null 2>&1; then
        local ver
        ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        local major="${ver%%.*}"
        local minor="${ver##*.}"
        if (( major > 3 )) || { (( major == 3 )) && (( minor >= 9 )); }; then
            return 0
        fi
        yellow "python3 found but version ${ver} - need >= 3.9"
    fi
    case "$PM" in
        apt) apt-get update && apt-get install -y python3 python3-venv python3-pip ;;
        dnf) dnf install -y python3 python3-pip ;;
        yum) yum install -y python3 python3-pip ;;
        *)   die "Could not detect package manager - install python3 (>= 3.9) manually then re-run" ;;
    esac
}

ensure_python

# python3-venv is its own package on Debian/Ubuntu
if [[ "$PM" == "apt" ]]; then
    apt-get install -y python3-venv >/dev/null 2>&1 || true
fi

# curl is needed for the Telegram test message
if ! command -v curl >/dev/null 2>&1; then
    case "$PM" in
        apt) apt-get install -y curl >/dev/null 2>&1 || true ;;
        dnf) dnf install -y curl >/dev/null 2>&1 || true ;;
        yum) yum install -y curl >/dev/null 2>&1 || true ;;
    esac
fi

if [[ ! -f "/etc/x-ui/x-ui.db" ]]; then
    yellow "WARNING: /etc/x-ui/x-ui.db not found"
    yellow "         If 3x-ui is installed elsewhere, edit database_path in ${CONFIG_FILE} after install."
fi

blue "==> Installing files to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
install -m 0644 "${SRC_DIR}/xui_limiter.py"   "${INSTALL_DIR}/xui_limiter.py"
install -m 0644 "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"

blue "==> Creating virtualenv and installing python deps"
if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

blue "==> Setting up ${CONFIG_DIR}"
mkdir -p "${CONFIG_DIR}"
if [[ -f "${CONFIG_FILE}" ]]; then
    yellow "config already exists at ${CONFIG_FILE} - leaving it alone"
    CONFIG_IS_NEW=0
else
    install -m 0640 "${SRC_DIR}/config/config.example.yaml" "${CONFIG_FILE}"
    # Generate a random API token automatically so we don't ship with "CHANGE_ME"
    TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
    sed -i "s|CHANGE_ME_TO_A_RANDOM_LONG_STRING|${TOKEN}|" "${CONFIG_FILE}"
    green "wrote new config to ${CONFIG_FILE} (API token auto-generated)"
    CONFIG_IS_NEW=1
fi

# ---------------------------------------------------------------------------
# Detect and patch xray access log path
# ---------------------------------------------------------------------------
blue "==> Detecting xray access log path"
DETECTED_ACCESS_LOG="$(detect_access_log || true)"
if [[ -n "$DETECTED_ACCESS_LOG" ]]; then
    green "    detected: ${DETECTED_ACCESS_LOG}"
    export DETECTED_ACCESS_LOG CONFIG_FILE
    python3 - <<'PYEOF'
import os, re
path     = os.environ["CONFIG_FILE"]
detected = os.environ["DETECTED_ACCESS_LOG"]
src = open(path, encoding="utf-8").read()
new = re.sub(
    r"^(\s*access_log:).*$",
    lambda m: m.group(1) + " " + detected,
    src,
    count=1,
    flags=re.MULTILINE,
)
if new != src:
    open(path, "w", encoding="utf-8").write(new)
    print("    patched config: ip_limit.access_log = " + detected)
else:
    print("    config already matches; no change needed")
PYEOF
else
    yellow "    Could not auto-detect xray access log on this server."
    yellow "    Leaving default in ${CONFIG_FILE}:"
    yellow "      ip_limit.access_log: /usr/local/x-ui/access.log"
    yellow "    If your xray writes somewhere else, edit that line manually."
fi

# ---------------------------------------------------------------------------
# Interactive Telegram setup
# ---------------------------------------------------------------------------
blue "==> Telegram notifications setup"

TG_CONFIGURED=0
if [[ ! -t 0 ]]; then
    yellow "    Non-interactive install detected (no TTY)."
    yellow "    Skipping Telegram setup. Edit ${CONFIG_FILE} later to enable."
else
    cat <<EOF

  When a client is limited (IP abuse or traffic abuse), xui-limiter can
  push a notification to a Telegram chat / group / channel.

  - Bot token: create a bot via @BotFather, copy the token.
  - Chat ID:   private chat = positive number, group = negative number,
               supergroup = starts with -100.
               Tip: send a message to your bot, then visit
               https://api.telegram.org/bot<TOKEN>/getUpdates to find chat.id

  Press Enter on the bot token to skip and disable Telegram notifications.

EOF

    TG_BOT_TOKEN=""
    while true; do
        read -r -p "  Telegram bot token (or Enter to skip): " TG_BOT_TOKEN
        TG_BOT_TOKEN="${TG_BOT_TOKEN// /}"
        if [[ -z "$TG_BOT_TOKEN" ]]; then
            yellow "    Skipped. Telegram notifications will be disabled."
            break
        fi
        if [[ "$TG_BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
            break
        fi
        red "    Invalid format. Expected: <digits>:<letters/digits/_/-> (e.g. 1234567890:AAEx...)"
    done

    TG_CHAT_ID=""
    if [[ -n "$TG_BOT_TOKEN" ]]; then
        while true; do
            read -r -p "  Telegram chat ID: " TG_CHAT_ID
            TG_CHAT_ID="${TG_CHAT_ID// /}"
            if [[ "$TG_CHAT_ID" =~ ^-?[0-9]+$ ]]; then
                break
            fi
            red "    Invalid chat ID. Expected: digits, optionally starting with - for groups."
        done

        # Optional message thread id (for forum topics inside supergroups)
        read -r -p "  Forum topic thread ID (only for supergroup topics, Enter to skip): " TG_THREAD_ID
        TG_THREAD_ID="${TG_THREAD_ID// /}"
        if [[ -n "$TG_THREAD_ID" && ! "$TG_THREAD_ID" =~ ^[0-9]+$ ]]; then
            yellow "    Ignoring non-numeric thread ID"
            TG_THREAD_ID=""
        fi

        blue "==> Testing Telegram delivery"
        TEST_RESP="$(curl -sS --max-time 10 -X POST \
            "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            -H "Content-Type: application/json" \
            -d "{\"chat_id\":\"${TG_CHAT_ID}\",\"text\":\"<b>xui-limiter</b> installer test\\nPowered By Piriz | @ThePiriz\",\"parse_mode\":\"HTML\"}" \
            2>&1 || true)"
        if echo "$TEST_RESP" | grep -q '"ok":true'; then
            green "    Test message delivered to your Telegram chat."
        else
            red "    Test FAILED. Response: ${TEST_RESP:0:300}"
            echo ""
            read -r -p "  Save these credentials anyway? [y/N]: " keep
            if [[ ! "$keep" =~ ^[Yy]$ ]]; then
                yellow "    Telegram setup aborted. You can re-run the installer to retry."
                TG_BOT_TOKEN=""
                TG_CHAT_ID=""
            fi
        fi
    fi

    if [[ -n "$TG_BOT_TOKEN" && -n "$TG_CHAT_ID" ]]; then
        blue "==> Writing Telegram credentials into ${CONFIG_FILE}"
        export TG_BOT_TOKEN TG_CHAT_ID TG_THREAD_ID CONFIG_FILE
        "${INSTALL_DIR}/venv/bin/python" - <<'PYEOF'
import os, re

PATH      = os.environ["CONFIG_FILE"]
TOKEN     = os.environ["TG_BOT_TOKEN"]
CHAT      = os.environ["TG_CHAT_ID"]
THREAD    = os.environ.get("TG_THREAD_ID", "")

with open(PATH, encoding="utf-8") as f:
    src = f.read()

# Also flip webhook.enabled to false so we don't double-notify.
def flip_in_webhook(m):
    return m.group(0).replace("enabled: true", "enabled: false", 1)

src = re.sub(
    r"^webhook:\n(?:[ \t].*\n)+",
    flip_in_webhook,
    src,
    count=1,
    flags=re.MULTILINE,
)

tg_block = (
    "telegram:\n"
    "  enabled: true\n"
    '  bot_token: "' + TOKEN + '"\n'
    '  chat_id: "' + CHAT + '"\n'
    '  message_thread_id: "' + THREAD + '"\n'
    "  timeout_seconds: 10\n"
    "  retries: 3\n"
    "  parse_mode: HTML\n"
)

if re.search(r"^telegram:\s*$", src, flags=re.MULTILINE):
    src = re.sub(
        r"^telegram:\n(?:[ \t].*\n?)*",
        tg_block,
        src,
        count=1,
        flags=re.MULTILINE,
    )
else:
    if not src.endswith("\n"):
        src += "\n"
    src += "\n# Telegram notifications (configured by installer)\n" + tg_block

with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)
print("    config updated")
PYEOF
        TG_CONFIGURED=1
    fi
fi

blue "==> Installing systemd unit"
install -m 0644 "${SRC_DIR}/systemd/xui-limiter.service" "${SERVICE_FILE}"
systemctl daemon-reload

blue "==> Validating config"
if ! "${INSTALL_DIR}/venv/bin/python" "${INSTALL_DIR}/xui_limiter.py" --config "${CONFIG_FILE}" --validate; then
    red "config validation failed - fix ${CONFIG_FILE} then run:"
    red "    systemctl enable --now ${SERVICE_NAME}"
    exit 1
fi

blue "==> Enabling and starting service"
systemctl enable --now "${SERVICE_NAME}"
sleep 2

if systemctl is-active --quiet "${SERVICE_NAME}"; then
    green "==> xui-limiter is RUNNING"
else
    red "==> xui-limiter failed to start. Last logs:"
    journalctl -u "${SERVICE_NAME}" -n 30 --no-pager
    exit 1
fi

# ---------------------------------------------------------------------------
# Final summary - the access-log reminder must be impossible to miss
# ---------------------------------------------------------------------------
cat <<EOF

$(green '============================================================')
$(green ' Install complete.')
$(green '============================================================')

  Config file : ${CONFIG_FILE}
  Log file    : /var/log/xui-limiter.log
  Service     : systemctl {status|restart|stop} ${SERVICE_NAME}
  Live logs   : journalctl -u ${SERVICE_NAME} -f
EOF

if [[ "${TG_CONFIGURED}" -eq 1 ]]; then
    green ""
    green " Telegram notifications: ENABLED"
    green " A startup ping was delivered to your chat."
else
    yellow ""
    yellow " Telegram notifications: DISABLED"
    yellow " To enable later: edit the 'telegram:' section in ${CONFIG_FILE}"
    yellow " then run: systemctl restart ${SERVICE_NAME}"
fi

cat <<EOF

$(red '############################################################')
$(red '##                                                        ##')
$(red '##   IMPORTANT - one-time 3x-ui panel setup REQUIRED:     ##')
$(red '##                                                        ##')
$(red '##   Be sure to enable your Xray ACCESS LOG in the        ##')
$(red '##   3x-ui panel at the following path:                   ##')
$(red '##                                                        ##')
$(red '##       /usr/local/x-ui/access.log                       ##')
$(red '##                                                        ##')
$(red '##   Panel path:                                          ##')
$(red '##     Xray Configurations -> Log -> Access Log           ##')
$(red '##                                                        ##')
$(red '##   Then click "Save" and "Restart Xray Service".        ##')
$(red '##                                                        ##')
$(red '##   Without this, IP-based limiting will NOT detect      ##')
$(red '##   connections. (Traffic-based limiting still works.)   ##')
$(red '##                                                        ##')
$(red '############################################################')

$(blue '------------------------------------------------------------')
$(bold ' Powered By Piriz   |   Telegram: @ThePiriz')
$(blue '------------------------------------------------------------')

EOF
