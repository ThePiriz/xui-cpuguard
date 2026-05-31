#!/usr/bin/env bash
# =============================================================================
# upgrade-telegram.sh - add Telegram notifications to a deployed xui-limiter.
# Idempotent: safe to re-run.
# =============================================================================
set -euo pipefail

red()    { printf '\033[1;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
blue()   { printf '\033[1;34m%s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { red "must run as root"; exit 1; }

INSTALL_DIR="/opt/xui-limiter"
CONFIG_FILE="/etc/xui-limiter/config.yaml"

[[ -f "$INSTALL_DIR/xui_limiter.py" ]] || { red "xui-limiter not installed at $INSTALL_DIR"; exit 1; }
[[ -f "$CONFIG_FILE" ]]                || { red "config not found at $CONFIG_FILE"; exit 1; }

TS=$(date +%s)
blue "==> Stopping service"
systemctl stop xui-limiter || true

blue "==> Backing up current files (.bak.$TS)"
cp "$INSTALL_DIR/xui_limiter.py" "$INSTALL_DIR/xui_limiter.py.bak.$TS"
cp "$CONFIG_FILE"                "$CONFIG_FILE.bak.$TS"

blue "==> Writing new xui_limiter.py with Telegram support"
base64 -d <<'B64END' | gunzip > "$INSTALL_DIR/xui_limiter.py"
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
COCrAAA=
B64END

blue "==> Verifying Python syntax"
"$INSTALL_DIR/venv/bin/python" -c "import ast; ast.parse(open('$INSTALL_DIR/xui_limiter.py').read())"
green "    syntax OK"

blue "==> Updating config (disable webhook, set telegram block)"
cat > /tmp/_xui_update_config.py <<'PYEOF'
import re
PATH = "/etc/xui-limiter/config.yaml"
TOKEN = "8991121718:AAEWkrPb1adKuMyxsmRxKnkdRDcMfaCAm_o"
CHAT  = "-5179084042"

with open(PATH, encoding="utf-8") as f:
    src = f.read()

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
    '  message_thread_id: ""\n'
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
    src += "\n# Telegram notifications (managed by upgrade-telegram.sh)\n" + tg_block

with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)

print("    config updated")
PYEOF

"$INSTALL_DIR/venv/bin/python" /tmp/_xui_update_config.py
rm -f /tmp/_xui_update_config.py

blue "==> Validating new config"
"$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/xui_limiter.py" --config "$CONFIG_FILE" --validate

blue "==> Starting service"
systemctl start xui-limiter
sleep 3

if systemctl is-active --quiet xui-limiter; then
    green "==> xui-limiter is RUNNING"
else
    red "==> xui-limiter failed to start. Recent logs:"
    journalctl -u xui-limiter -n 40 --no-pager
    yellow "Rollback:"
    yellow "  cp $INSTALL_DIR/xui_limiter.py.bak.$TS $INSTALL_DIR/xui_limiter.py"
    yellow "  cp $CONFIG_FILE.bak.$TS $CONFIG_FILE"
    yellow "  systemctl restart xui-limiter"
    exit 1
fi

blue "==> Last 15 log lines:"
journalctl -u xui-limiter -n 15 --no-pager

green ""
green "----------------------------------------------------------------"
green " Telegram support deployed."
green "----------------------------------------------------------------"
echo ""
echo "  You should see a startup ping in your Telegram chat now."
echo ""
echo "  Test a real limit event later:"
echo "    1. Lower ip_limit.max_ips to 2 in $CONFIG_FILE"
echo "    2. systemctl restart xui-limiter"
echo "    3. Connect from 3 devices to one config"
echo "    4. Restore max_ips to 5 after"
echo ""
echo "  Backups: *.bak.$TS"
