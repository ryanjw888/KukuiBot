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

# --- Prime sudo credentials ---
# When run via `curl | bash`, stdin is the pipe, so sudo can't prompt normally.
# We use /dev/tty to read the password from the actual terminal.
# This caches credentials for the rest of the install (~15 min timeout).
if ! sudo -n true 2>/dev/null; then
  echo "→ This installer needs admin access. Enter your password:"
  sudo -v </dev/tty
fi
# Keep sudo alive in the background
(while true; do sudo -n true; sleep 50; done) &
SUDO_KEEPALIVE_PID=$!
trap 'kill $SUDO_KEEPALIVE_PID 2>/dev/null' EXIT

# --- Check/install Xcode Command Line Tools ---
# On a fresh Mac, `python3` is a shim that triggers the Xcode CLT install dialog.
# Install CLT automatically and wait for it to finish.
if ! xcode-select -p &>/dev/null; then
  echo "→ Xcode Command Line Tools not found. Installing..."
  echo ""
  echo "  ⚠️  A system dialog will appear — it may be BEHIND other windows."
  echo "     Look for 'Install Command Line Developer Tools' and click 'Install'."
  echo ""
  # Trigger the installer (async — spawns a GUI dialog and returns)
  xcode-select --install 2>/dev/null || true
  # Wait up to 30 minutes, printing dots so the user knows we're alive.
  # Disable set -e for this block — xcode-select -p returns non-zero until done.
  set +e
  CLT_WAIT=0
  CLT_MAX=1800
  while true; do
    xcode-select -p &>/dev/null && break
    if [ "$CLT_WAIT" -ge "$CLT_MAX" ]; then
      echo ""
      echo "❌ Timed out waiting for Xcode Command Line Tools."
      echo "   Install them manually:  xcode-select --install"
      echo "   Then re-run this installer."
      exit 1
    fi
    printf "." 2>/dev/null
    sleep 10
    CLT_WAIT=$((CLT_WAIT + 10))
  done
  set -e
  echo ""
  echo "✓ Xcode Command Line Tools installed"
fi

# --- Check/install Homebrew ---
if ! command -v brew &>/dev/null; then
  echo "→ Installing Homebrew (this may take a few minutes)..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/null
  # Add Homebrew to PATH for Apple Silicon Macs
  if [ -f /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -f /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  if ! command -v brew &>/dev/null; then
    echo "❌ Homebrew install failed. Install manually:"
    echo "   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "   Then re-run this installer."
    exit 1
  fi
fi
echo "✓ Homebrew"

# --- Check/install Python 3.11+ ---
# Always check if brew Python exists and prepend it to PATH first,
# because /usr/bin/python3 (Xcode CLT 3.9) wins by default even after
# brew install. This handles both fresh installs AND re-runs.
BREW_PY_PREFIX="$(brew --prefix python@3.13 2>/dev/null || true)"
if [ -n "$BREW_PY_PREFIX" ] && [ -d "$BREW_PY_PREFIX/libexec/bin" ]; then
  export PATH="$BREW_PY_PREFIX/libexec/bin:$PATH"
fi

NEED_PYTHON=false
if ! command -v python3 &>/dev/null; then
  NEED_PYTHON=true
else
  PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
  if [ "$PY_MINOR" -lt 11 ]; then
    echo "→ System Python is 3.$PY_MINOR (need 3.11+), installing via Homebrew..."
    NEED_PYTHON=true
  fi
fi
if [ "$NEED_PYTHON" = true ]; then
  brew install python@3.13 </dev/null
  # Re-check prefix after install
  BREW_PY_PREFIX="$(brew --prefix python@3.13 2>/dev/null)"
  if [ -n "$BREW_PY_PREFIX" ] && [ -d "$BREW_PY_PREFIX/libexec/bin" ]; then
    export PATH="$BREW_PY_PREFIX/libexec/bin:$PATH"
  fi
  eval "$(brew shellenv)"
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python $PY_VER"

for dep in mkcert ripgrep; do
  cmd="$dep"
  [ "$dep" = "ripgrep" ] && cmd="rg"
  if ! command -v "$cmd" &>/dev/null; then
    echo "→ Installing $dep..."
    brew install "$dep" </dev/null
  fi
  echo "✓ $dep"
done

# --- Check/install Node.js + Claude Code CLI ---
# npm may install to ~/.local/bin — ensure it's on PATH for detection
export PATH="$HOME/.local/bin:$PATH"

if ! command -v node &>/dev/null; then
  echo "→ Installing Node.js (required for Claude Code)..."
  brew install node </dev/null
fi
echo "✓ Node.js $(node --version 2>/dev/null || echo '(pending)')"

if ! command -v claude &>/dev/null; then
  echo "→ Installing Claude Code CLI..."
  npm install -g @anthropic-ai/claude-code </dev/null 2>&1 | tail -1
  # npm may have installed to ~/.local/bin — re-check PATH
  if [ -f "$HOME/.local/bin/claude" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
if command -v claude &>/dev/null; then
  CLAUDE_BIN_PATH="$(command -v claude)"
  echo "✓ Claude Code CLI $(claude --version 2>/dev/null | head -1) ($CLAUDE_BIN_PATH)"
else
  echo "⚠️  Claude Code CLI install failed — install manually: npm install -g @anthropic-ai/claude-code"
fi

# --- Install root CA (one-time) ---
mkcert -install </dev/null 2>/dev/null || true
echo "✓ Root CA trusted"

# --- Set up directories ---
SRC_DIR="$KUKUIBOT_HOME/src"
VENV_DIR="$KUKUIBOT_HOME/venv"
REPO_URL="${KUKUIBOT_REPO:-https://github.com/ryanjw888/KukuiBot.git}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

mkdir -p "$KUKUIBOT_HOME" "$LAUNCH_AGENTS"

# --- Create/update virtual environment ---
# PEP 668 (Python 3.12+) blocks system-wide pip installs.
# A venv avoids this and keeps deps isolated.
if [ ! -d "$VENV_DIR" ]; then
  echo "→ Creating virtual environment..."
  python3 -m venv "$VENV_DIR"
fi
# Use the venv's python for everything from here on
PYTHON_BIN="$VENV_DIR/bin/python3"
PYTHON_BIN_DIR="$(dirname "$PYTHON_BIN")"
# Build PATH for launchd — include the directory where claude was found
CLAUDE_DIR=""
if command -v claude &>/dev/null; then
  CLAUDE_DIR="$(dirname "$(command -v claude)")"
fi
PATH_ENV="${PYTHON_BIN_DIR}:${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Add claude's directory if it's not already in the PATH
if [ -n "$CLAUDE_DIR" ] && ! echo "$PATH_ENV" | grep -qF "$CLAUDE_DIR"; then
  PATH_ENV="${CLAUDE_DIR}:${PATH_ENV}"
fi
# Set explicit CLAUDE_BIN env var for launchd if claude was found
CLAUDE_BIN_PLIST_ENTRY=""
if command -v claude &>/dev/null; then
  CLAUDE_FULL_PATH="$(command -v claude)"
  CLAUDE_BIN_PLIST_ENTRY="
        <key>CLAUDE_BIN</key>
        <string>${CLAUDE_FULL_PATH}</string>"
  echo "  Claude binary: $CLAUDE_FULL_PATH"
fi

# --- Accept Xcode license (required before git works after fresh CLT install) ---
sudo xcodebuild -license accept </dev/null 2>/dev/null || true

# --- Clone or update source ---
if [ -d "$SRC_DIR/.git" ]; then
  echo "→ Updating existing source at $SRC_DIR"
  cd "$SRC_DIR" && git pull --ff-only 2>/dev/null || true
else
  echo "→ Cloning KukuiBot to $SRC_DIR"
  git clone "$REPO_URL" "$SRC_DIR" </dev/null || {
    echo "❌ Git clone failed. Check your network connection and try again:"
    echo "   git clone $REPO_URL $SRC_DIR"
    exit 1
  }
fi
cd "$SRC_DIR"

# --- Install Python deps ---
echo "→ Installing Python dependencies..."
"$PYTHON_BIN" -m pip install -q -r requirements.txt </dev/null

# --- Seed default data files (only if missing — won't overwrite user customizations) ---
echo "→ Seeding default configuration files..."
for f in SOUL.md USER.md TOOLS.md MEMORY.md; do
  if [ ! -f "$KUKUIBOT_HOME/$f" ] && [ -f "$SRC_DIR/agent/$f" ]; then
    cp "$SRC_DIR/agent/$f" "$KUKUIBOT_HOME/$f"
  fi
done
mkdir -p "$KUKUIBOT_HOME/workers" "$KUKUIBOT_HOME/models"
for f in "$SRC_DIR/workers/"*.md; do
  [ -f "$f" ] || continue
  base="$(basename "$f")"
  [ -f "$KUKUIBOT_HOME/workers/$base" ] || cp "$f" "$KUKUIBOT_HOME/workers/$base"
done
for f in "$SRC_DIR/models/"*.md; do
  [ -f "$f" ] || continue
  base="$(basename "$f")"
  [ -f "$KUKUIBOT_HOME/models/$base" ] || cp "$f" "$KUKUIBOT_HOME/models/$base"
done
echo "✓ Configuration files ready"

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

# Clear old server log so stale errors don't persist
: > /tmp/kukuibot-server.log 2>/dev/null || true

# Stop and remove any existing services (use both old and new launchctl API)
UID_VAL=$(id -u)
for svc in com.kukuibot.server com.kukuibot.worker; do
  launchctl bootout "gui/${UID_VAL}/${svc}" 2>/dev/null || true
  launchctl unload "$LAUNCH_AGENTS/${svc}.plist" 2>/dev/null || true
done
# Remove legacy worker plist if it exists
rm -f "$LAUNCH_AGENTS/com.kukuibot.worker.plist"

echo "  Using Python: $PYTHON_BIN"

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
        <string>${PORT}</string>${CLAUDE_BIN_PLIST_ENTRY}
    </dict>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/${UID_VAL}" "$LAUNCH_AGENTS/com.kukuibot.server.plist" 2>/dev/null || \
  launchctl load "$LAUNCH_AGENTS/com.kukuibot.server.plist" 2>/dev/null || true
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

# Give the server more time on first launch (imports take a while)
echo "→ Waiting for server to start..."
SERVER_OK=false
for i in 1 2 3 4 5 6; do
  if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    SERVER_OK=true
    break
  fi
  sleep 2
done

if [ "$SERVER_OK" = true ]; then
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
echo ""
echo "→ Opening KukuiBot in your browser..."
open "https://localhost:${PORT}"
