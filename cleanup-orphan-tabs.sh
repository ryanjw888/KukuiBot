#!/bin/bash
# Cleanup orphan tab metadata rows (tabs with no history).
#
# Default behavior: dry run, all models, keep tabs newer than 2 hours.
# Usage:
#   ./cleanup-orphan-tabs.sh                 # dry run
#   ./cleanup-orphan-tabs.sh --apply         # delete candidates
#   ./cleanup-orphan-tabs.sh --apply --model spark --min-age-seconds 0

set -euo pipefail

KUKUIBOT_HOME="${KUKUIBOT_HOME:-$HOME/.kukuibot}"
DB_PATH="${KUKUIBOT_DB:-$KUKUIBOT_HOME/kukuibot.db}"
MODEL_FILTER=""
MIN_AGE_SECONDS="${MIN_AGE_SECONDS:-7200}"
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --model)
      MODEL_FILTER="${2:-}"
      shift 2
      ;;
    --min-age-seconds)
      MIN_AGE_SECONDS="${2:-7200}"
      shift 2
      ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--apply] [--model spark|codex] [--min-age-seconds N]

Deletes orphan tab_meta rows where:
  - session_id starts with tab-
  - no matching history row exists
  - updated_at <= now - min_age_seconds

Default: dry-run only.
EOF
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$DB_PATH" ]]; then
  echo "DB not found: $DB_PATH"
  exit 1
fi

if ! [[ "$MIN_AGE_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "Invalid --min-age-seconds: $MIN_AGE_SECONDS" >&2
  exit 2
fi

NOW="$(date +%s)"
CUTOFF=$((NOW - MIN_AGE_SECONDS))

MODEL_WHERE="1=1"
if [[ -n "$MODEL_FILTER" ]]; then
  MODEL_WHERE="COALESCE(m.model_key,'') = '${MODEL_FILTER//\'/\'\'}'"
fi

read -r -d '' CANDIDATE_SQL <<SQL || true
SELECT m.owner, m.session_id, COALESCE(m.tab_id,''), COALESCE(m.model_key,''), COALESCE(m.label,''), COALESCE(m.updated_at,0)
FROM tab_meta m
WHERE m.session_id LIKE 'tab-%'
  AND ${MODEL_WHERE}
  AND COALESCE(m.updated_at,0) <= ${CUTOFF}
  AND NOT EXISTS (
    SELECT 1 FROM history h WHERE h.session_id = m.session_id
  )
ORDER BY COALESCE(m.updated_at,0) ASC;
SQL

ROWS_FILE="$(mktemp)"
sqlite3 -separator $'\t' "$DB_PATH" "$CANDIDATE_SQL" > "$ROWS_FILE"
count="$(wc -l < "$ROWS_FILE" | tr -d ' ')"

echo "[orphan-tab-cleanup] db=$DB_PATH model=${MODEL_FILTER:-all} min_age_seconds=$MIN_AGE_SECONDS cutoff=$CUTOFF candidates=$count apply=$APPLY"

if [[ "$count" -eq 0 ]]; then
  rm -f "$ROWS_FILE"
  exit 0
fi

while IFS=$'\t' read -r owner sid tab_id model label updated_at; do
  [[ -z "${sid:-}" ]] && continue
  echo "  - owner=$owner session_id=$sid tab_id=$tab_id model=$model updated_at=$updated_at label=$label"
done < "$ROWS_FILE"

if [[ "$APPLY" -ne 1 ]]; then
  echo "Dry-run only. Re-run with --apply to delete these rows."
  rm -f "$ROWS_FILE"
  exit 0
fi

# Use one DELETE statement (with busy timeout) to avoid lock churn.
read -r -d '' DELETE_SQL <<SQL || true
PRAGMA busy_timeout=5000;
DELETE FROM tab_meta
WHERE session_id LIKE 'tab-%'
  AND ${MODEL_WHERE}
  AND COALESCE(updated_at,0) <= ${CUTOFF}
  AND NOT EXISTS (
    SELECT 1 FROM history h WHERE h.session_id = tab_meta.session_id
  );
SELECT changes();
SQL

deleted="$(sqlite3 "$DB_PATH" "$DELETE_SQL" | tail -n 1 | tr -d '[:space:]')"
rm -f "$ROWS_FILE"
echo "Deleted ${deleted:-0} orphan tab_meta row(s)."
