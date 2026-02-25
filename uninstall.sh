#!/bin/bash
# KukuiBot — Uninstaller for macOS
# Usage: ~/.kukuibot/src/uninstall.sh
#    or: curl -fsSL <url>/uninstall.sh | bash
#    or: bash uninstall.sh --dir ~/my-kukuibot

set +e  # Don't bail on errors — uninstall should keep going

# --- Parse flags ---
CUSTOM_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) CUSTOM_DIR="$2"; shift 2 ;;
    *)     echo "Unknown option: $1"; echo "Usage: uninstall.sh [--dir DIR]"; exit 1 ;;
  esac
done

KUKUIBOT_HOME="${CUSTOM_DIR:-${KUKUIBOT_HOME:-$HOME/.kukuibot}}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

echo "🧪 Uninstalling KukuiBot..."
echo ""

# --- Confirm ---
read -p "This will remove KukuiBot, all data, and services. Continue? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
  echo "Cancelled."
  exit 0
fi

# --- Prime sudo ---
if ! sudo -n true 2>/dev/null; then
  echo "→ This uninstaller needs admin access. Enter your password:"
  sudo -v
fi

# --- Stop and unload services ---
echo "→ Stopping services..."
UID_VAL=$(id -u)
for svc in com.kukuibot.server com.kukuibot.worker; do
  launchctl bootout "gui/${UID_VAL}/${svc}" 2>/dev/null || true
  launchctl stop "$svc" 2>/dev/null || true
  launchctl unload "$LAUNCH_AGENTS/${svc}.plist" 2>/dev/null || true
  rm -f "$LAUNCH_AGENTS/${svc}.plist"
  echo "  ✓ $svc removed"
done

# Root LaunchDaemon (privileged helper)
echo "→ Removing privileged helper daemon..."
sudo launchctl bootout system/com.kukuibot.privhelper 2>/dev/null || true
sudo rm -f /Library/LaunchDaemons/com.kukuibot.privhelper.plist
sudo rm -f /tmp/kukuibot-priv.sock /tmp/kukuibot-privhelper.log /tmp/kukuibot-privileged.log
echo "  ✓ com.kukuibot.privhelper removed"

# --- Remove cron jobs ---
echo "→ Removing cron jobs..."
CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
if printf "%s\n" "$CURRENT_CRON" | grep -q "# kukuibot-"; then
  printf "%s\n" "$CURRENT_CRON" | grep -v "# kukuibot-" | sed '/^$/d' | crontab - 2>/dev/null || true
  echo "  ✓ Cron jobs removed"
else
  echo "  ✓ No cron jobs found"
fi

# --- Remove sudoers ---
echo "→ Removing sudoers rules..."
for sf in /etc/sudoers.d/kukuibot-*; do
  if [ -f "$sf" ]; then
    sudo rm -f "$sf"
    echo "  ✓ Removed $sf"
  fi
done

# --- Remove data directory ---
echo "→ Removing $KUKUIBOT_HOME..."
if [ -d "$KUKUIBOT_HOME" ]; then
  # Some files are root-owned, need sudo
  sudo rm -rf "$KUKUIBOT_HOME"
  echo "  ✓ Data directory removed"
else
  echo "  ✓ Already gone"
fi

# --- Clean up logs ---
echo "→ Cleaning up logs..."
sudo rm -f /tmp/kukuibot-*.log /tmp/kukuibot-priv.sock
echo "  ✓ Logs removed"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✅ KukuiBot uninstalled completely."
echo ""
echo "  What was removed:"
echo "    • LaunchAgents (com.kukuibot.server, com.kukuibot.worker)"
echo "    • Cron jobs (backup, orphan cleanup)"
echo "    • Sudoers rules (/etc/sudoers.d/kukuibot-*)"
echo "    • All data ($KUKUIBOT_HOME)"
echo "    • Log files (/tmp/kukuibot-*.log)"
echo ""
echo "  NOT removed (manual cleanup if desired):"
echo "    • Homebrew packages (mkcert, ripgrep)"
echo "    • Python packages (pip3 uninstall fastapi uvicorn ...)"
echo "    • mkcert root CA (mkcert -uninstall)"
echo "═══════════════════════════════════════════════════"
