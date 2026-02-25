#!/bin/bash
# Sync KukuiBot source to standalone GitHub repo
set -e

SOURCE_ROOT="${KUKUIBOT_SOURCE_ROOT:-~/.kukuibot}"
SOURCE_DIR="$SOURCE_ROOT/src"
STANDALONE="${KUKUIBOT_STANDALONE_REPO:-/tmp/kukuibot-repo}"

if [ ! -d "$SOURCE_DIR" ]; then
  echo "Source dir not found: $SOURCE_DIR"
  exit 1
fi

# Ensure standalone repo exists
if [ ! -d "$STANDALONE/.git" ]; then
  git clone "${KUKUIBOT_GITHUB_REPO:-git@github.com:ryanjw888/KukuiBot.git}" "$STANDALONE"
fi

cd "$STANDALONE"
git pull --ff-only origin main 2>/dev/null || true

# Sync files (exclude local/runtime artifacts)
rsync -av --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='certs/' \
  --exclude='.DS_Store' \
  --exclude='*.log' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='config/security-policy.json' \
  --exclude='sync-to-github.sh' \
  "$SOURCE_DIR/" "$STANDALONE/"

# Keep standalone .gitignore stable
cat > "$STANDALONE/.gitignore" << 'EOF'
__pycache__/
*.pyc
.DS_Store
*.log
certs/
config/security-policy.json
.env
EOF

cd "$STANDALONE"
git add -A
if git diff --cached --quiet; then
  echo "No changes to sync"
else
  git commit -m "sync: kukuibot $(date +%Y-%m-%d_%H:%M)"
  git push origin main
  echo "✅ Pushed to GitHub"
fi
