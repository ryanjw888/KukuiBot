#!/bin/bash
# KukuiBot — backup to GitHub (code) + local data snapshot
set -euo pipefail

REPO_DIR="${KUKUIBOT_REPO_DIR:-$(dirname "$(realpath "$0")")}"   # default: ~/.kukuibot/src
KUKUIBOT_HOME="${KUKUIBOT_HOME:-$HOME/.kukuibot}"
DATA_DIR="$KUKUIBOT_HOME"
LOG="$KUKUIBOT_HOME/logs/backup.log"

mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

failed=0

# --- Backup code repo ---
if git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if ! git -C "$REPO_DIR" diff --quiet HEAD 2>/dev/null || [ -n "$(git -C "$REPO_DIR" ls-files --others --exclude-standard)" ]; then
    git -C "$REPO_DIR" add -A
    git -C "$REPO_DIR" commit -m "auto-backup: $(date '+%Y-%m-%d %H:%M')" --no-verify >/dev/null 2>&1 || true
  fi

  if git -C "$REPO_DIR" remote get-url origin >/dev/null 2>&1; then
    branch="$(git -C "$REPO_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || echo master)"
    if git -C "$REPO_DIR" push origin "$branch" 2>>"$LOG"; then
      log "Code push succeeded: $REPO_DIR ($branch)"
    else
      log "ERROR: Code push failed: $REPO_DIR ($branch)"
      failed=1
    fi
  else
    log "ERROR: No git remote 'origin' configured for $REPO_DIR"
    failed=1
  fi
else
  log "ERROR: Not a git repo: $REPO_DIR"
  failed=1
fi

# --- Backup local data repo (optional local snapshot) ---
if [ ! -d "$DATA_DIR/.git" ]; then
  git -C "$DATA_DIR" init -q
  cat > "$DATA_DIR/.gitignore" << 'EOF'
kukuibot.db
kukuibot.db.bak
*.db-journal
logs/
__pycache__/
EOF
  git -C "$DATA_DIR" add -A
  git -C "$DATA_DIR" commit -m "init: kukuibot data backup" -q >/dev/null 2>&1 || true
  log "Initialized local data backup repo at $DATA_DIR"
else
  if ! git -C "$DATA_DIR" diff --quiet HEAD 2>/dev/null || [ -n "$(git -C "$DATA_DIR" ls-files --others --exclude-standard)" ]; then
    git -C "$DATA_DIR" add -A
    git -C "$DATA_DIR" commit -m "data-backup: $(date '+%Y-%m-%d %H:%M')" -q >/dev/null 2>&1 || true
    log "Data snapshot committed: $DATA_DIR"
  fi
fi

exit $failed
