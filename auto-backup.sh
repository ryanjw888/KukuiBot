#!/bin/bash
# KukuiBot auto-backup — commits and pushes to GitHub
# Run via cron or launchd
set -e

REPO_DIR="${KUKUIBOT_REPO_DIR:-$KUKUIBOT_HOME/src}"
cd "$REPO_DIR" || exit 1

if [ ! -d .git ]; then
  echo "Not a git repo: $REPO_DIR"
  exit 0
fi

# Only if there are changes
if git diff --quiet HEAD -- . && git diff --cached --quiet -- .; then
  exit 0  # Nothing to commit
fi

git add -A
git commit -m "auto-backup: kukuibot $(date +%Y-%m-%d_%H:%M)" --no-verify 2>/dev/null || true
git push origin main 2>/dev/null || git push origin master 2>/dev/null || true
