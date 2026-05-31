# xui-limiter

Production-ready abuse limiter for [3x-ui](https://github.com/MHSanaei/3x-ui)
panels. When a client's config is shared with too many people (IP abuse) or
hammered by torrents / a botnet (traffic abuse), xui-limiter detects it within
seconds, disables the client, rotates its credential, renames it, and pushes a
notification to Telegram — all without deleting anything from the 3x-ui
database.

> **Powered By Piriz** &nbsp;|&nbsp; Telegram: [@ThePiriz](https://t.me/ThePiriz)

---

## What it does

Two enforcement modes run side-by-side as a single `systemd` service.

| Mode | Trigger | Action |
|---|---|---|
| **IP limit** | A client has more than `max_ips` (default **5**) unique source IPs at the same time, sustained for `sustain_seconds` (default **20s**), counted over a sliding `window_seconds` (default **60s**) window. | Rename email to `name-iplimited`, rotate UUID/password, set `enable: false`, push Telegram event. |
| **Traffic limit** | A client moves more than `threshold_bytes` (default **5 GiB**) of `up + down` inside a `comparison_window_seconds` (default **300s** = 5 minutes) window. | Rename email to `name-trafficlimited`, rotate UUID/password, set `enable: false`, push Telegram event. |

After applying limits the installer triggers `systemctl restart x-ui` (debounced
— multiple limits in a 5-second window cause exactly one restart). xray reloads
its config and drops the abuser. The original row stays in the database with
its traffic counters intact, only renamed and disabled.

A read-only HTTP API (`http://127.0.0.1:9999/events`, Bearer-token auth) keeps
the last 500 events in memory for polling by external systems.

---

## Requirements

- A Linux server with **3x-ui** installed (`/etc/x-ui/x-ui.db` present).
- `systemd` and Python ≥ 3.9 (the installer installs Python if missing on
  Debian / Ubuntu / RHEL / Fedora).
- Root access (the installer must write to `/opt`, `/etc`, and reload systemd).

---

## Install

### 1. Get the source onto the server

From your local machine:

```powershell
# Windows PowerShell — adjust the IP
scp -r "C:\Users\amirm\OneDrive\Desktop\xui-script" root@YOUR_SERVER_IP:~/
```

```bash
# Linux / macOS / WSL
scp -r ./xui-script root@YOUR_SERVER_IP:~/
```

### 2. Run the installer on the server

```bash
ssh root@YOUR_SERVER_IP
cd ~/xui-script
sudo bash scripts/install.sh
```

That's it. The installer:

1. Installs Python + venv + dependencies into `/opt/xui-limiter/venv`.
2. Writes the default config to `/etc/xui-limiter/config.yaml` and generates
   a random API token.
3. **Auto-detects your xray access-log path** (reads it from the 3x-ui DB,
   resolves relative paths via `/proc/$(pidof xray)/cwd`, scans xray's open
   file descriptors, probes well-known locations, and finally does a
   filesystem search filtered to xray/x-ui dirs).
4. Prompts you for an optional **Telegram bot token + chat ID**, sends a live
   test message, and writes the credentials into the config if the test
   succeeds.
5. Installs and enables the systemd unit, validates the config, starts the
   service, and prints a big red reminder about enabling the xray access log
   in your 3x-ui panel.

### 3. One-time 3x-ui panel setup (required for IP limiting)

In the 3x-ui panel, go to:

> **Xray Configurations → Log → Access Log**

and set it to:

```
/usr/local/x-ui/access.log
```

(or whatever the installer auto-detected and wrote into
`/etc/xui-limiter/config.yaml`). Click **Save**, then **Restart Xray Service**.

Without this, IP-based limiting cannot see client connections. Traffic-based
limiting works regardless.

---

## Daily operations

```bash
# Live logs (most useful)
journalctl -u xui-limiter -f

# Service control
systemctl status  xui-limiter
systemctl restart xui-limiter
systemctl stop    xui-limiter

# See queued limit events (Bearer-token auth)
TOKEN=$(grep '^  token:' /etc/xui-limiter/config.yaml | awk -F'"' '{print $2}')
curl -sH "Authorization: Bearer $TOKEN" http://127.0.0.1:9999/events | python3 -m json.tool

# Liveness probe (no auth)
curl http://127.0.0.1:9999/healthz
```

---

## Configuration

Everything lives in `/etc/xui-limiter/config.yaml`. After editing, run
`systemctl restart xui-limiter`. See [`config/config.example.yaml`](config/config.example.yaml)
for every option and its default. Highlights:

```yaml
ip_limit:
  enabled: true
  max_ips: 5
  window_seconds: 60
  sustain_seconds: 20
  access_log: /usr/local/x-ui/access.log

traffic_limit:
  enabled: true
  snapshot_interval_seconds: 10
  comparison_window_seconds: 300
  threshold_bytes: 5368709120     # 5 GiB

whitelist:
  # Any client whose email starts with this prefix is exempt from BOTH limits.
  # Rename a client to `vip-myfriend` in the 3x-ui panel to whitelist them.
  name_prefixes: ["vip-"]

telegram:
  enabled: true
  bot_token: "<your bot token>"
  chat_id:   "<your chat id>"
```

---

## Telegram message format

Each limit produces a message like:

```
🚷 Service limited due to IP sharing

👤 Previous email: ali
🔄 New email:      ali-iplimited
⛔ Type:           IP limit
📡 Inbound:       5 (vless)
🔑 ID rotated:    abcd...wxyz → 1234...5678

Details:
• Unique IPs:      47
• Window:          60s
• Sustained:       23s
• Sample IPs:      1.2.3.4, 5.6.7.8, ...

Reason: Exceeded 5 concurrent IPs ...

⏰ 2026-05-28T10:30:00+00:00
```

---

## Undo a limit (false positive)

If a genuine user gets caught:

1. In the 3x-ui panel, find the client (now named `<original>-iplimited` or
   `<original>-trafficlimited`).
2. Rename it back to `<original>`.
3. Set `enable: true`.
4. The credential (UUID / password) has been rotated — generate a new client
   config in the panel and re-send it to the user.

To prevent re-triggering, prefix their name with `vip-` (whitelist).

---

## Uninstall

```bash
# keep config + logs
sudo bash scripts/uninstall.sh

# wipe everything
sudo bash scripts/uninstall.sh --purge
```

---

## Project layout

```
xui-script/
├── README.md                       # this file
├── xui_limiter.py                  # main Python service
├── requirements.txt                # pyyaml + aiohttp
│
├── config/
│   └── config.example.yaml         # commented config template
│
├── systemd/
│   └── xui-limiter.service         # hardened systemd unit
│
└── scripts/
    ├── install.sh                  # interactive installer (Telegram + access-log auto-detect)
    ├── uninstall.sh                # idempotent uninstaller
    └── upgrade-telegram.sh         # legacy: add Telegram to an old install
```

---

## Troubleshooting

**`access log /usr/local/x-ui/access.log missing - waiting`**
The xray access log isn't being written there. Either it isn't enabled in the
3x-ui panel, or it's at a different path. Find it:

```bash
# What does xray's config say?
sqlite3 /etc/x-ui/x-ui.db \
  "SELECT value FROM settings WHERE key='xrayTemplateConfig'" \
  | python3 -c "import sys, json; print(json.loads(sys.stdin.read())['log'])"

# Where does xray actually have open file descriptors?
ls -la /proc/$(pidof xray)/fd | grep -i log

# Search the filesystem
find / -xdev -name 'access.log' -type f 2>/dev/null
```

Then edit `ip_limit.access_log` in `/etc/xui-limiter/config.yaml` and run
`systemctl restart xui-limiter`.

**`telegram 400` in logs**
The bot token or chat ID is wrong. Re-run `bash scripts/install.sh` (it
preserves the existing config but re-prompts for Telegram).

**Service starts but never limits anyone**
Maybe nobody is actually abusing yet — that's good. To force a test, lower
the threshold temporarily:

```bash
sed -i 's/max_ips: 5/max_ips: 2/' /etc/xui-limiter/config.yaml
sed -i 's/sustain_seconds: 20/sustain_seconds: 5/' /etc/xui-limiter/config.yaml
systemctl restart xui-limiter

# Now connect from 3 devices to one config and watch:
journalctl -u xui-limiter -f

# Restore when done:
sed -i 's/max_ips: 2/max_ips: 5/' /etc/xui-limiter/config.yaml
sed -i 's/sustain_seconds: 5/sustain_seconds: 20/' /etc/xui-limiter/config.yaml
systemctl restart xui-limiter
```

---

## License & credit

> **Powered By Piriz** &nbsp;|&nbsp; Telegram: [@ThePiriz](https://t.me/ThePiriz)
