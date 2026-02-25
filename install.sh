#!/bin/bash
# KukuiBot — One-line installer for macOS
# Usage: curl -fsSL <url>/install.sh | bash
#   or:  bash install.sh --port 8080 --dir ~/my-kukuibot
#
# Options:
#   --port PORT   Server port (default: 7000)
#   --dir  DIR    Data directory (default: ~/.kukuibot)
#
# Architecture:
#   KukuiBot Server (single process) — unified server (auth, chat, tools, settings, API)
#
# Layout:
#   <dir>/src/              — source code (git repo)
#   <dir>/                  — data dir (db, memory, config, logs)
#   ~/Library/LaunchAgents/ — com.kukuibot.server plist

set -e

# --- Parse flags ---
CUSTOM_PORT=""
CUSTOM_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) CUSTOM_PORT="$2"; shift 2 ;;
    --dir)  CUSTOM_DIR="$2";  shift 2 ;;
    *)      echo "Unknown option: $1"; echo "Usage: install.sh [--port PORT] [--dir DIR]"; exit 1 ;;
  esac
done

PORT="${CUSTOM_PORT:-${KUKUIBOT_PORT:-7000}}"
KUKUIBOT_HOME="${CUSTOM_DIR:-${KUKUIBOT_HOME:-$HOME/.kukuibot}}"

echo "🧪 Installing KukuiBot..."
echo "   Port: $PORT"
echo "   Data: $KUKUIBOT_HOME"
echo ""

# --- Check Python ---
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 not found. Install it first:"
  echo "   brew install python3"
  exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python $PY_VER"

# --- Check/install Homebrew deps ---
if ! command -v brew &>/dev/null; then
  echo "❌ Homebrew not found. Install it first: https://brew.sh"
  exit 1
fi

for dep in mkcert ripgrep; do
  cmd="$dep"
  [ "$dep" = "ripgrep" ] && cmd="rg"
  if ! command -v "$cmd" &>/dev/null; then
    echo "→ Installing $dep..."
    brew install "$dep"
  fi
  echo "✓ $dep"
done

# --- Install root CA (one-time) ---
mkcert -install 2>/dev/null || true
echo "✓ Root CA trusted"

# --- Set up directories ---
SRC_DIR="$KUKUIBOT_HOME/src"
REPO_URL="${KUKUIBOT_REPO:-https://github.com/ryanjw888/KukuiBot.git}"
PYTHON_BIN=$(command -v python3)
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PATH_ENV="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$KUKUIBOT_HOME" "$LAUNCH_AGENTS"

# --- Clone or update source ---
if [ -d "$SRC_DIR/.git" ]; then
  echo "→ Updating existing source at $SRC_DIR"
  cd "$SRC_DIR" && git pull --ff-only 2>/dev/null || true
else
  echo "→ Cloning KukuiBot to $SRC_DIR"
  git clone "$REPO_URL" "$SRC_DIR" 2>/dev/null || {
    echo "❌ Git clone failed. Please clone the repo manually:"
    echo "   git clone $REPO_URL $SRC_DIR"
    exit 1
  }
fi
cd "$SRC_DIR"

# --- Install Python deps ---
echo "→ Installing Python dependencies..."
pip3 install -q -r requirements.txt 2>/dev/null || pip install -q -r requirements.txt

# --- Generate HTTPS certs ---
if [ ! -f certs/kukuibot.pem ]; then
  echo "→ Generating HTTPS certificates..."
  mkdir -p certs
  LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "")
  CERT_NAMES="localhost 127.0.0.1"
  [ -n "$LAN_IP" ] && CERT_NAMES="$CERT_NAMES $LAN_IP"
  mkcert -cert-file certs/kukuibot.pem -key-file certs/kukuibot-key.pem $CERT_NAMES
  CAROOT=$(mkcert -CAROOT)
  cp "$CAROOT/rootCA.pem" certs/rootCA.pem 2>/dev/null || true
fi
echo "✓ HTTPS certs ready"

# =============================================
# KukuiBot Server launchd service
# =============================================

echo "→ Setting up services..."

# Unload old plists (including legacy worker plist)
for svc in com.kukuibot.server com.kukuibot.worker; do
  launchctl unload "$LAUNCH_AGENTS/${svc}.plist" 2>/dev/null || true
done
# Remove legacy worker plist if it exists
rm -f "$LAUNCH_AGENTS/com.kukuibot.worker.plist"

# --- KukuiBot Server ---
cat > "$LAUNCH_AGENTS/com.kukuibot.server.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kukuibot.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${SRC_DIR}/server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SRC_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>/tmp/kukuibot-server.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/kukuibot-server.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>PATH</key>
        <string>${PATH_ENV}</string>
        <key>KUKUIBOT_HOME</key>
        <string>${KUKUIBOT_HOME}</string>
        <key>KUKUIBOT_PORT</key>
        <string>${PORT}</string>
    </dict>
</dict>
</plist>
PLIST

launchctl load "$LAUNCH_AGENTS/com.kukuibot.server.plist"
echo "✓ KukuiBot server (port $PORT) installed"

# =============================================
# Privileged helper daemon (root launchd)
# =============================================

echo "→ Setting up privileged helper daemon..."
PRIV_DAEMON_PLIST="/Library/LaunchDaemons/com.kukuibot.privhelper.plist"

sudo tee "$PRIV_DAEMON_PLIST" > /dev/null << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kukuibot.privhelper</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${SRC_DIR}/kukuibot_privileged_helper.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SRC_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/kukuibot-privhelper.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/kukuibot-privhelper.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${PATH_ENV}</string>
        <key>KUKUIBOT_PRIV_SOCKET</key>
        <string>/tmp/kukuibot-priv.sock</string>
        <key>KUKUIBOT_PRIV_LOG</key>
        <string>/tmp/kukuibot-privileged.log</string>
    </dict>
</dict>
</plist>
PLIST

sudo chown root:wheel "$PRIV_DAEMON_PLIST"
sudo chmod 644 "$PRIV_DAEMON_PLIST"
sudo launchctl bootout system/com.kukuibot.privhelper 2>/dev/null || true
sudo launchctl bootstrap system "$PRIV_DAEMON_PLIST"
sudo launchctl kickstart -k system/com.kukuibot.privhelper

echo "✓ Privileged helper daemon installed"

# =============================================
# Cron jobs
# =============================================

CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
NEW_CRON="$CURRENT_CRON"

BACKUP_CRON="0 * * * * ${SRC_DIR}/backup.sh >> ${KUKUIBOT_HOME}/logs/backup-cron.log 2>&1 # kukuibot-backup"
ORPHAN_CRON="17 * * * * ${SRC_DIR}/cleanup-orphan-tabs.sh --apply --min-age-seconds 7200 >> ${KUKUIBOT_HOME}/logs/orphan-tabs-cron.log 2>&1 # kukuibot-orphan-tabs"

if ! printf "%s\n" "$CURRENT_CRON" | grep -qF "# kukuibot-backup"; then
  NEW_CRON="$NEW_CRON
$BACKUP_CRON"
  echo "✓ Hourly backup cron installed"
else
  echo "✓ Backup cron already present"
fi

if ! printf "%s\n" "$CURRENT_CRON" | grep -qF "# kukuibot-orphan-tabs"; then
  NEW_CRON="$NEW_CRON
$ORPHAN_CRON"
  echo "✓ Orphan-tab cleanup cron installed"
else
  echo "✓ Orphan-tab cleanup cron already present"
fi

if [ "$NEW_CRON" != "$CURRENT_CRON" ]; then
  printf "%s\n" "$NEW_CRON" | sed '/^$/d' | crontab -
fi

# =============================================
# Verify
# =============================================

sleep 3
SERVER_OK=false

if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
  SERVER_OK=true
  echo "✓ KukuiBot server running on port $PORT"
else
  echo "⚠️  Server didn't start — check /tmp/kukuibot-server.log"
fi

LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "<your-ip>")

echo ""
echo "═══════════════════════════════════════════════════"
echo "  🧪 KukuiBot installed successfully!"
echo ""
echo "  Open:    https://localhost:${PORT}"
echo "  LAN:     https://${LAN_IP}:${PORT}"
echo ""
echo "  Manage:"
echo "    Restart server:   launchctl stop com.kukuibot.server"
echo "    Server logs:      tail -f /tmp/kukuibot-server.log"
echo "═══════════════════════════════════════════════════"

# Open in default browser
if [ "$SERVER_OK" = true ]; then
  echo ""
  echo "→ Opening KukuiBot in your browser..."
  open "https://localhost:${PORT}"
fi
