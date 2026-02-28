#!/usr/bin/env bash
# Auto-push to GitHub repository
# This script is triggered by cron every hour to sync local commits to GitHub

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(dirname "$SCRIPT_DIR")"

cd "$SRC_DIR"

# Check if we have commits to push
if git rev-parse --git-dir > /dev/null 2>&1; then
    # Fetch to check remote state
    git fetch origin main 2>/dev/null || true

    # Check if we have unpushed commits
    LOCAL=$(git rev-parse @)
    REMOTE=$(git rev-parse @{u} 2>/dev/null || echo "")

    if [ -n "$REMOTE" ] && [ "$LOCAL" != "$REMOTE" ]; then
        # We have unpushed commits
        echo "[$(date)] Pushing commits to GitHub..."
        git push origin main 2>&1 || {
            echo "[$(date)] Failed to push to GitHub" >&2
            exit 1
        }
        echo "[$(date)] Successfully pushed to GitHub"
    else
        echo "[$(date)] No unpushed commits"
    fi
else
    echo "[$(date)] Not a git repository" >&2
    exit 1
fi
