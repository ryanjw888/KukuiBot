#!/usr/bin/env bash
# Auto-push to GitHub repository
# This script is triggered by cron every hour to sync local commits to GitHub

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(dirname "$SCRIPT_DIR")"

cd "$SRC_DIR"

# Pre-push safety check: Scan for sensitive data patterns
check_sensitive_data() {
    local staged_files
    staged_files=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || echo "")

    if [ -z "$staged_files" ]; then
        return 0
    fi

    # Patterns that should never be pushed
    local sensitive_patterns=(
        "reports/"
        "audits/"
        "memory/"
        "*.nmap"
        "*.gnmap"
        "rustscan_"
        ".claude_session_"
        "*.eml"
        "*.msg"
        "session-*.md"
        "notes-*.md"
    )

    for pattern in "${sensitive_patterns[@]}"; do
        if echo "$staged_files" | grep -q "$pattern"; then
            echo "[$(date)] ERROR: Sensitive files staged for commit: $pattern" >&2
            echo "[$(date)] Aborting push. Review staged files." >&2
            return 1
        fi
    done

    # Check for common PII patterns in staged file content
    for file in $staged_files; do
        if [ -f "$file" ]; then
            # Check for IP addresses in non-documentation files
            if [[ ! "$file" =~ \.(md|rst|txt)$ ]] && grep -qE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' "$file" 2>/dev/null; then
                echo "[$(date)] WARNING: IP address detected in $file" >&2
            fi

            # Check for common credential patterns
            if grep -qiE '(password|secret|token|api[_-]?key)\s*[=:]\s*["\047][^"\047]{8,}["\047]' "$file" 2>/dev/null; then
                echo "[$(date)] ERROR: Potential credential detected in $file" >&2
                return 1
            fi
        fi
    done

    return 0
}

# Check if we have commits to push
if git rev-parse --git-dir > /dev/null 2>&1; then
    # Fetch to check remote state
    git fetch origin main 2>/dev/null || true

    # Check if we have unpushed commits
    LOCAL=$(git rev-parse @)
    REMOTE=$(git rev-parse @{u} 2>/dev/null || echo "")

    if [ -n "$REMOTE" ] && [ "$LOCAL" != "$REMOTE" ]; then
        # Run safety check before pushing
        if ! check_sensitive_data; then
            echo "[$(date)] Pre-push safety check FAILED. Not pushing." >&2
            exit 1
        fi

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
