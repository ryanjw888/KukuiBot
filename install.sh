#!/bin/bash
# KukuiBot — One-line installer for macOS
# Usage: curl -fsSL <url>/install.sh | bash
#   or:  bash install.sh --port 8080 --dir ~/my-kukuibot
#
# Options:
#   --port PORT   Server port (default: 443)
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

PORT="${CUSTOM_PORT:-${KUKUIBOT_PORT:-443}}"
KUKUIBOT_HOME="${CUSTOM_DIR:-${KUKUIBOT_HOME:-$HOME/.kukuibot}}"

# --- Interactive port selection (if not specified via flags) ---
# Always prompt for port (uses /dev/tty so it works even when piped via curl | bash)
if [ -z "$CUSTOM_PORT" ]; then
  echo "🧪 KukuiBot Installation"
  echo ""
  echo "Select HTTPS port for KukuiBot:"
  echo "  443   - Standard HTTPS (recommended)"
  echo "  8443  - Alternative HTTPS"
  echo "  7000  - Legacy default"
  echo "  Other - Custom port (1-65535)"
  echo ""
  read -p "Enter port [443]: " USER_PORT </dev/tty

  if [ -n "$USER_PORT" ]; then
    PORT="$USER_PORT"
  fi
  echo ""
fi

# --- Pre-flight validation ---
if ! echo "$PORT" | grep -qE '^[0-9]+$' || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "❌ Invalid port: $PORT (must be 1-65535)"
  exit 1
fi

# Note about privileged ports (< 1024) — handled via pfctl forwarding
if [ "$PORT" -lt 1024 ]; then
  echo "ℹ️  Port $PORT is privileged — will use pfctl forwarding (server runs as your user)"
fi

if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
  EXISTING_PROC=$(lsof -nP -iTCP:${PORT} -sTCP:LISTEN | tail -1 | awk '{print $1, $2}')
  echo "⚠️  Port $PORT is already in use by: $EXISTING_PROC"
  echo "   Choose a different port with --port or stop the conflicting process"
  exit 1
fi

if [ ! -d "$(dirname "$KUKUIBOT_HOME")" ]; then
  echo "❌ Parent directory does not exist: $(dirname "$KUKUIBOT_HOME")"
  exit 1
fi

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

# find_claude: search everywhere for the claude binary
find_claude() {
  # 1. Already on PATH
  command -v claude 2>/dev/null && return 0

  # 2. Common locations
  local candidates=(
    "$HOME/.local/bin/claude"
    "/opt/homebrew/bin/claude"
    "/usr/local/bin/claude"
    "$HOME/.npm-global/bin/claude"
  )

  # 3. npm global prefix (if npm is available)
  local npm_prefix
  npm_prefix="$(npm prefix -g 2>/dev/null)" && [ -n "$npm_prefix" ] && \
    candidates=("$npm_prefix/bin/claude" "${candidates[@]}")

  # 4. nvm-managed node versions (newest first)
  if [ -d "$HOME/.nvm/versions/node" ]; then
    for ndir in $(ls -1dr "$HOME/.nvm/versions/node/"* 2>/dev/null); do
      candidates+=("$ndir/bin/claude")
    done
  fi

  # 5. Check each candidate
  for c in "${candidates[@]}"; do
    if [ -x "$c" ]; then
      echo "$c"
      return 0
    fi
  done

  # 6. Last resort: find in common trees (fast — max depth 6)
  for search_dir in "$HOME/.local" "$HOME/.npm-global" "$HOME/.nvm" /opt/homebrew /usr/local; do
    [ -d "$search_dir" ] || continue
    local found
    found="$(find "$search_dir" -maxdepth 6 -name claude -type f -perm +111 2>/dev/null | head -1)"
    if [ -n "$found" ]; then
      echo "$found"
      return 0
    fi
  done

  return 1
}

# Prepend common npm bin dirs to PATH so both node and claude are findable
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

# --- Check/install Node.js with version requirement ---
# Claude Code CLI requires Node.js >= 18.0.0
NEED_NODE=false
if ! command -v node &>/dev/null; then
  NEED_NODE=true
else
  NODE_MAJOR=$(node -v 2>/dev/null | sed 's/v\([0-9]*\).*/\1/')
  if [ -z "$NODE_MAJOR" ] || [ "$NODE_MAJOR" -lt 18 ]; then
    echo "→ Node.js $(node -v 2>/dev/null || echo 'unknown') is too old (need >=18.0.0), upgrading via Homebrew..."
    NEED_NODE=true
  fi
fi

if [ "$NEED_NODE" = true ]; then
  echo "→ Installing Node.js 18+ (required for Claude Code)..."
  brew install node </dev/null
  # Refresh brew env to pick up new node
  eval "$(brew shellenv)"
fi

NODE_VER=$(node --version 2>/dev/null || echo '(pending)')
echo "✓ Node.js $NODE_VER"

# --- Install Claude Code CLI ---
CLAUDE_BIN_PATH=""
CLAUDE_BIN_PATH="$(find_claude)" || true

if [ -z "$CLAUDE_BIN_PATH" ]; then
  echo "→ Installing Claude Code CLI..."

  # Try with sudo first (handles permission issues on macOS)
  # We already primed sudo credentials at the start, so this is safe
  if sudo npm install -g @anthropic-ai/claude-code </dev/null 2>&1 | tail -1; then
    echo "  Installed with sudo"
  else
    echo "  ⚠️  Global install with sudo failed, trying user-local npm config..."
    # Fallback: configure npm to use user-local prefix
    mkdir -p "$HOME/.npm-global"
    npm config set prefix "$HOME/.npm-global" 2>/dev/null
    export PATH="$HOME/.npm-global/bin:$PATH"
    npm install -g @anthropic-ai/claude-code </dev/null 2>&1 | tail -1 || {
      echo "  ⚠️  npm install failed. You may need to install manually after this script completes."
    }
  fi

  # Re-search after install
  CLAUDE_BIN_PATH="$(find_claude)" || true
fi

if [ -n "$CLAUDE_BIN_PATH" ]; then
  # Add its directory to PATH for the rest of the install
  export PATH="$(dirname "$CLAUDE_BIN_PATH"):$PATH"
  echo "✓ Claude Code CLI $("$CLAUDE_BIN_PATH" --version 2>/dev/null | head -1) ($CLAUDE_BIN_PATH)"
else
  echo ""
  echo "⚠️  Claude Code CLI not found after installation attempt."
  echo "   Manual install steps:"
  echo "     1. Ensure Node.js >= 18:  node --version"
  echo "     2. Upgrade if needed:     brew upgrade node"
  echo "     3. Install Claude CLI:    sudo npm install -g @anthropic-ai/claude-code"
  echo "   Or use user-local install:"
  echo "     npm config set prefix ~/.npm-global"
  echo "     npm install -g @anthropic-ai/claude-code"
  echo "     export PATH=\"\$HOME/.npm-global/bin:\$PATH\""
  echo ""
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
PATH_ENV="${PYTHON_BIN_DIR}:${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Add claude's directory if it's not already covered
if [ -n "$CLAUDE_BIN_PATH" ]; then
  CLAUDE_DIR="$(dirname "$CLAUDE_BIN_PATH")"
  if ! echo "$PATH_ENV" | grep -qF "$CLAUDE_DIR"; then
    PATH_ENV="${CLAUDE_DIR}:${PATH_ENV}"
  fi
fi
# Set explicit CLAUDE_BIN env var for launchd — absolute path so it works
# even if launchd's PATH doesn't include the right directory
CLAUDE_BIN_PLIST_ENTRY=""
if [ -n "$CLAUDE_BIN_PATH" ]; then
  CLAUDE_BIN_PLIST_ENTRY="
        <key>CLAUDE_BIN</key>
        <string>${CLAUDE_BIN_PATH}</string>"
  echo "  Claude binary: $CLAUDE_BIN_PATH"
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

# --- Install Playwright browser (needed for web browsing tool) ---
if ! "$PYTHON_BIN" -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.stop()" 2>/dev/null; then
  echo "→ Installing Playwright Chromium browser..."
  "$PYTHON_BIN" -m playwright install chromium </dev/null 2>&1 | tail -1
  echo "✓ Playwright Chromium installed"
else
  echo "✓ Playwright Chromium already installed"
fi

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
# Use absolute path to ensure certs are created in the correct location
CERT_DIR="$SRC_DIR/certs"
if [ ! -f "$CERT_DIR/kukuibot.pem" ]; then
  echo "→ Generating HTTPS certificates..."
  mkdir -p "$CERT_DIR"
  LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "")
  CERT_NAMES="localhost 127.0.0.1"
  [ -n "$LAN_IP" ] && CERT_NAMES="$CERT_NAMES $LAN_IP"
  mkcert -cert-file "$CERT_DIR/kukuibot.pem" -key-file "$CERT_DIR/kukuibot-key.pem" $CERT_NAMES
  CAROOT=$(mkcert -CAROOT)
  cp "$CAROOT/rootCA.pem" "$CERT_DIR/rootCA.pem" 2>/dev/null || true
  echo "  Certificates: $CERT_DIR"
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
# Always run as user LaunchAgent (never root). For privileged ports (<1024),
# the server binds to an internal high port and pfctl forwards the external
# port to it. This avoids all root-ownership issues (Claude CLI, git, file
# permissions).

BIND_PORT="$PORT"
BIND_PORT_PLIST_ENTRY=""
PFCTL_ANCHOR="com.kukuibot"
PFCTL_ANCHOR_DIR="/etc/pf.anchors"
PFCTL_DAEMON_PLIST="/Library/LaunchDaemons/com.kukuibot.portfwd.plist"

# Clean up any legacy root LaunchDaemon for the server (from older installs)
sudo launchctl bootout system/com.kukuibot.server 2>/dev/null || true
sudo rm -f /Library/LaunchDaemons/com.kukuibot.server.plist 2>/dev/null || true

if [ "$PORT" -lt 1024 ]; then
  BIND_PORT=8443
  echo "  Port $PORT → server binds to $BIND_PORT (pfctl forwards $PORT → $BIND_PORT)"
  BIND_PORT_PLIST_ENTRY="
        <key>KUKUIBOT_BIND_PORT</key>
        <string>${BIND_PORT}</string>"

  # --- pfctl port-forwarding anchor ---
  sudo mkdir -p "$PFCTL_ANCHOR_DIR"
  sudo tee "$PFCTL_ANCHOR_DIR/$PFCTL_ANCHOR" > /dev/null << PF
# KukuiBot port forwarding — redirect privileged port to user-space server
rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port ${PORT} -> 127.0.0.1 port ${BIND_PORT}
rdr pass on lo0 inet proto tcp from any to any port ${PORT} -> 127.0.0.1 port ${BIND_PORT}
PF

  # Load the anchor into pf.conf if not already present
  if ! grep -qF "com.kukuibot" /etc/pf.conf 2>/dev/null; then
    # Insert anchor lines before the last line (which is usually a trailing newline or rule)
    sudo cp /etc/pf.conf /etc/pf.conf.kukuibot-backup
    # Append anchor references
    printf '\n# KukuiBot port forwarding\nrdr-anchor "%s"\nload anchor "%s" from "%s/%s"\n' \
      "$PFCTL_ANCHOR" "$PFCTL_ANCHOR" "$PFCTL_ANCHOR_DIR" "$PFCTL_ANCHOR" \
      | sudo tee -a /etc/pf.conf > /dev/null
  fi

  # Apply the rules now
  sudo pfctl -ef /etc/pf.conf 2>/dev/null || sudo pfctl -f /etc/pf.conf 2>/dev/null || true

  # --- LaunchDaemon to re-apply pfctl rules on boot ---
  sudo tee "$PFCTL_DAEMON_PLIST" > /dev/null << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kukuibot.portfwd</string>
    <key>ProgramArguments</key>
    <array>
        <string>/sbin/pfctl</string>
        <string>-ef</string>
        <string>/etc/pf.conf</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/kukuibot-portfwd.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/kukuibot-portfwd.log</string>
</dict>
</plist>
PLIST

  sudo chown root:wheel "$PFCTL_DAEMON_PLIST"
  sudo chmod 644 "$PFCTL_DAEMON_PLIST"
  sudo launchctl bootout system/com.kukuibot.portfwd 2>/dev/null || true
  sudo launchctl bootstrap system "$PFCTL_DAEMON_PLIST"

  echo "✓ Port forwarding: $PORT → $BIND_PORT (pfctl)"
fi

# --- Server LaunchAgent (always runs as user, never root) ---
PLIST_PATH="$LAUNCH_AGENTS/com.kukuibot.server.plist"

# Stop any existing agent
launchctl bootout "gui/${UID_VAL}/com.kukuibot.server" 2>/dev/null || true
launchctl unload "$PLIST_PATH" 2>/dev/null || true

cat > "$PLIST_PATH" << PLIST
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
        <string>${PORT}</string>${BIND_PORT_PLIST_ENTRY}${CLAUDE_BIN_PLIST_ENTRY}
    </dict>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/${UID_VAL}" "$PLIST_PATH" 2>/dev/null || \
  launchctl load "$PLIST_PATH" 2>/dev/null || true

echo "✓ KukuiBot server (port $BIND_PORT) installed as user agent"

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

# Give the server time to start — first launch is slow (Python imports,
# DB schema init, TLS setup). Wait up to 45 seconds with progress dots.
echo -n "→ Waiting for server to start"
SERVER_OK=false
for i in $(seq 1 15); do
  if lsof -nP -iTCP:${BIND_PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    SERVER_OK=true
    break
  fi
  printf "."
  sleep 3
done
echo ""

if [ "$SERVER_OK" = true ]; then
  if [ "$BIND_PORT" != "$PORT" ]; then
    echo "✓ KukuiBot server running on port $BIND_PORT (accessible on port $PORT via pfctl)"
  else
    echo "✓ KukuiBot server running on port $PORT"
  fi
else
  echo "⚠️  Server didn't start within 45 seconds"
  echo ""
  # Show recent log output so the user doesn't have to go hunting
  if [ -s /tmp/kukuibot-server.log ]; then
    echo "  Recent server log output:"
    echo "  ─────────────────────────"
    tail -20 /tmp/kukuibot-server.log | sed 's/^/    /'
    echo "  ─────────────────────────"
    echo ""
  fi
  echo "  Diagnostic steps:"
  echo "    1. Full server log:       tail -50 /tmp/kukuibot-server.log"
  echo "    2. Check launchd status:  launchctl list | grep kukuibot"
  echo "    3. Verify database:       ls -la $KUKUIBOT_HOME/kukuibot.db"
  echo "    4. Test manually:         cd $SRC_DIR && $PYTHON_BIN server.py"
  echo ""
  echo "  Common issues:"
  echo "    - Port $BIND_PORT already in use: lsof -nP -iTCP:$BIND_PORT"
  echo "    - Certificate issues: Check $CERT_DIR/"
  echo "    - Python dependencies: $PYTHON_BIN -m pip check"
  echo ""
fi

LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "<your-ip>")

echo ""
echo "═══════════════════════════════════════════════════"
echo "  🧪 KukuiBot installation complete!"
echo ""
echo "  Access URLs:"
echo "    Local:  https://localhost:${PORT}"
echo "    LAN:    https://${LAN_IP}:${PORT}  (requires enabling Remote Access in Settings)"
echo ""
echo "  Configuration:"
echo "    Data dir:   $KUKUIBOT_HOME"
echo "    Source:     $SRC_DIR"
echo "    Python:     $PYTHON_BIN"
echo "    Node.js:    $(command -v node) ($NODE_VER)"
if [ -n "$CLAUDE_BIN_PATH" ]; then
echo "    Claude CLI: $CLAUDE_BIN_PATH"
fi
echo ""
echo "  Manage:"
echo "    Restart:    launchctl stop com.kukuibot.server"
echo "    Logs:       tail -f /tmp/kukuibot-server.log"
echo "    Uninstall:  cd $SRC_DIR && ./uninstall.sh"
echo "═══════════════════════════════════════════════════"

# Open in default browser only if server started successfully
if [ "$SERVER_OK" = true ]; then
  echo ""
  echo "→ Opening KukuiBot in your browser..."
  sleep 1
  open "https://localhost:${PORT}"
else
  echo ""
  echo "→ Server needs troubleshooting before accessing web interface."
  echo "  Review the diagnostic steps above and check logs."
fi
