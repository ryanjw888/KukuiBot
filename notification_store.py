"""
notification_store.py — Delegation notification persistence layer.

Owns ALL SQL for the `delegation_notifications` table. No direct SQL
for this table should exist anywhere else in the codebase.

State machine: pending → claimed → injected → consumed | failed

This module has ZERO imports from server.py — it is a data layer, not
an orchestrator.
"""

import json
import logging
import time
import uuid
from datetime import datetime

from auth import db_connection

logger = logging.getLogger("kukuibot.notification_store")

# Fast-path flag to avoid repeated PRAGMA checks after first init
_schema_initialized = False

# Delimiter used when prepending queued delegation notifications in front of a
# real user message. Intentionally unique to avoid accidental collisions with
# markdown content in delegated results (e.g. "\n\n---\n\n").
DELEGATION_PREPEND_BOUNDARY = "[[KUKUIBOT_DELEGATION_BOUNDARY_V1]]"


# ---------------------------------------------------------------------------
# Table DDL
# ---------------------------------------------------------------------------

_NEW_TABLE_DDL = """CREATE TABLE IF NOT EXISTS delegation_notifications (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'status_change',
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    delivered_at INTEGER DEFAULT 0,
    dedupe_key TEXT,
    state TEXT DEFAULT 'pending',
    attempt_count INTEGER DEFAULT 0,
    last_error TEXT,
    claimed_at TEXT,
    injected_at TEXT,
    UNIQUE(session_id, dedupe_key)
)"""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_schema(db=None) -> None:
    """Create/migrate the delegation_notifications table. Idempotent.

    Schema v2 changes (from v1):
    - UNIQUE(dedupe_key) → UNIQUE(session_id, dedupe_key) — session-scoped deduplication
    - Added columns: state, attempt_count, last_error, claimed_at, injected_at
    - Added index: idx_deleg_notif_state(session_id, state)

    Migration from v1: detects old single-column unique index on dedupe_key,
    renames to _dn_old, creates new table, migrates data, drops old table.
    Idempotent — handles crash mid-migration (_dn_old exists but main doesn't).
    """
    global _schema_initialized
    if _schema_initialized and db is not None:
        return

    owns_db = db is None
    if owns_db:
        with db_connection() as _db:
            ensure_schema(_db)
        return

    try:
        # Check if _dn_old exists from a crashed migration — finish it
        old_exists = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_dn_old'"
        ).fetchone()
        main_exists = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='delegation_notifications'"
        ).fetchone()

        if old_exists and not main_exists:
            # Crashed mid-migration: _dn_old exists but main doesn't — create new and migrate
            logger.info("delegation_notification schema: resuming crashed migration from _dn_old")
            db.execute(_NEW_TABLE_DDL)
            db.execute("""INSERT OR IGNORE INTO delegation_notifications
                (id, session_id, task_id, event_type, from_status, to_status,
                 payload_json, created_at, delivered_at, dedupe_key, state)
                SELECT id, session_id, task_id, event_type, from_status, to_status,
                       payload_json, created_at, delivered_at, dedupe_key,
                       CASE WHEN delivered_at > 0 THEN 'consumed' ELSE 'pending' END
                FROM _dn_old""")
            db.execute("DROP TABLE _dn_old")
            db.commit()
            logger.info("delegation_notification schema: migration from _dn_old completed")

        elif old_exists and main_exists:
            # Both exist — migration completed but _dn_old wasn't dropped
            db.execute("DROP TABLE _dn_old")
            db.commit()

        elif main_exists:
            # Table exists — check if it needs migration (old single-column unique on dedupe_key)
            needs_migration = False
            indexes = db.execute("PRAGMA index_list(delegation_notifications)").fetchall()
            for idx_row in indexes:
                idx_name = idx_row[1]
                is_unique = idx_row[2]
                if is_unique:
                    cols = db.execute(f"PRAGMA index_info({idx_name})").fetchall()
                    col_names = [c[2] for c in cols]
                    if col_names == ["dedupe_key"]:
                        needs_migration = True
                        break

            # Also check if state column exists
            col_info = db.execute("PRAGMA table_info(delegation_notifications)").fetchall()
            col_names_set = {c[1] for c in col_info}
            if "state" not in col_names_set:
                needs_migration = True

            if needs_migration:
                logger.info("delegation_notification schema: migrating v1 → v2 (session-scoped dedupe + state machine)")
                db.execute("ALTER TABLE delegation_notifications RENAME TO _dn_old")
                db.execute(_NEW_TABLE_DDL)
                db.execute("""INSERT OR IGNORE INTO delegation_notifications
                    (id, session_id, task_id, event_type, from_status, to_status,
                     payload_json, created_at, delivered_at, dedupe_key, state)
                    SELECT id, session_id, task_id, event_type, from_status, to_status,
                           payload_json, created_at, delivered_at, dedupe_key,
                           CASE WHEN delivered_at > 0 THEN 'consumed' ELSE 'pending' END
                    FROM _dn_old""")
                db.execute("DROP TABLE _dn_old")
                db.commit()
                logger.info("delegation_notification schema: v1 → v2 migration complete")
        else:
            # Fresh install — create new table
            db.execute(_NEW_TABLE_DDL)
            db.commit()

        # Ensure indexes exist (idempotent)
        db.execute("CREATE INDEX IF NOT EXISTS idx_deleg_notif_session ON delegation_notifications(session_id, delivered_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_deleg_notif_state ON delegation_notifications(session_id, state)")
        db.commit()
        _schema_initialized = True
    except Exception as e:
        logger.warning(f"delegation_notification schema init: {e}")


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def enqueue(session_id: str, payload: dict, dedupe_key: str = "", *, cap: int = 50) -> bool:
    """Insert a pending notification. Returns True if inserted."""
    with db_connection() as db:
        ensure_schema(db)
        try:
            # Cap per-session active (pending/claimed) at `cap` — drop oldest if exceeded
            count = db.execute(
                "SELECT COUNT(*) FROM delegation_notifications WHERE session_id=? AND state IN ('pending','claimed')",
                (session_id,),
            ).fetchone()[0]
            if count >= cap:
                oldest = db.execute(
                    "SELECT id FROM delegation_notifications WHERE session_id=? AND state IN ('pending','claimed') ORDER BY created_at ASC LIMIT ?",
                    (session_id, count - cap + 1),
                ).fetchall()
                if oldest:
                    ids = [r[0] for r in oldest]
                    db.execute(f"DELETE FROM delegation_notifications WHERE id IN ({','.join('?' * len(ids))})", ids)
                    logger.info(f"Delegation inbox cap: dropped {len(ids)} oldest notifications for {session_id}")

            notif_id = f"dn-{uuid.uuid4().hex[:12]}"
            dk = dedupe_key or notif_id
            db.execute(
                "INSERT OR IGNORE INTO delegation_notifications "
                "(id, session_id, task_id, event_type, from_status, to_status, payload_json, created_at, delivered_at, dedupe_key, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'pending')",
                (
                    notif_id,
                    session_id,
                    payload.get("task_id", ""),
                    payload.get("event_type", "status_change"),
                    payload.get("from_status", ""),
                    payload.get("to_status", ""),
                    json.dumps(payload),
                    int(time.time()),
                    dk,
                ),
            )
            db.commit()
            inserted = db.execute("SELECT changes()").fetchone()[0] > 0
            return inserted
        except Exception as e:
            logger.warning(f"Enqueue delegation notification failed: {e}")
            return False


# ---------------------------------------------------------------------------
# Claim / Drain
# ---------------------------------------------------------------------------

def claim(session_id: str, *, limit: int = 20) -> tuple[list[str], list[dict]]:
    """Claim pending notifications for drain. Returns (ids, payloads).

    State: pending -> claimed. Caller must call mark_injected() after use.
    """
    with db_connection() as db:
        ensure_schema(db)
        try:
            rows = db.execute(
                "SELECT id, payload_json FROM delegation_notifications "
                "WHERE session_id=? AND state='pending' ORDER BY created_at ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            if not rows:
                return ([], [])
            ids = [r[0] for r in rows]
            now_iso = datetime.utcnow().isoformat()
            # Claim: pending → claimed (only if still pending — prevents double-claiming)
            db.execute(
                f"UPDATE delegation_notifications SET state='claimed', claimed_at=?, attempt_count=attempt_count+1 "
                f"WHERE id IN ({','.join('?' * len(ids))}) AND state='pending'",
                [now_iso] + ids,
            )
            db.commit()
            payloads = []
            for r in rows:
                try:
                    payloads.append(json.loads(r[1]))
                except Exception:
                    # Mark unparseable rows as failed
                    try:
                        db.execute(
                            "UPDATE delegation_notifications SET state='failed', last_error='JSON parse error' WHERE id=?",
                            (r[0],),
                        )
                        db.commit()
                    except Exception:
                        pass
                    ids.remove(r[0])
            return (ids, payloads)
        except Exception as e:
            logger.warning(f"Drain delegation notifications failed: {e}")
            return ([], [])


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def mark_injected(ids: list[str]) -> int:
    """Mark claimed notifications as injected (prepended to user message). Returns rows updated."""
    if not ids:
        return 0
    with db_connection() as db:
        try:
            now_iso = datetime.utcnow().isoformat()
            now_epoch = int(time.time())
            cursor = db.execute(
                f"UPDATE delegation_notifications SET state='injected', injected_at=?, delivered_at=? "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                [now_iso, now_epoch] + ids,
            )
            db.commit()
            return cursor.rowcount
        except Exception as e:
            logger.warning(f"Mark notifications injected failed: {e}")
            return 0


def mark_consumed(ids: list[str]) -> int:
    """Mark notifications as fully consumed (model processed them). Returns rows updated."""
    if not ids:
        return 0
    with db_connection() as db:
        try:
            cursor = db.execute(
                f"UPDATE delegation_notifications SET state='consumed' "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
            db.commit()
            return cursor.rowcount
        except Exception as e:
            logger.warning(f"Mark notifications consumed failed: {e}")
            return 0


def mark_consumed_by_dedupe(session_id: str, dedupe_key: str) -> int:
    """Mark a notification as consumed by session_id + dedupe_key. Returns rows updated.

    Used when proactive wake succeeds and we need to mark the DB notification
    consumed without knowing the row ID.
    """
    if not session_id or not dedupe_key:
        return 0
    with db_connection() as db:
        try:
            now_iso = datetime.utcnow().isoformat()
            now_epoch = int(time.time())
            cursor = db.execute(
                "UPDATE delegation_notifications SET state='consumed', injected_at=?, delivered_at=? "
                "WHERE session_id=? AND dedupe_key=? AND state IN ('pending','claimed','injected','failed')",
                (now_iso, now_epoch, session_id, dedupe_key),
            )
            db.commit()
            return cursor.rowcount
        except Exception as e:
            logger.warning(f"Mark notification consumed by dedupe failed: {e}")
            return 0


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_pending_count(session_id: str) -> int:
    """Count pending+claimed notifications for a session."""
    with db_connection() as db:
        ensure_schema(db)
        try:
            row = db.execute(
                "SELECT COUNT(*) FROM delegation_notifications WHERE session_id=? AND state IN ('pending','claimed')",
                (session_id,),
            ).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0


def list_sessions_with_pending(limit: int = 500) -> list[str]:
    """List session IDs that have pending notifications. For dispatcher startup fan-out (Phase 3)."""
    with db_connection() as db:
        ensure_schema(db)
        try:
            rows = db.execute(
                "SELECT DISTINCT session_id FROM delegation_notifications WHERE state='pending' LIMIT ?",
                (limit,),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def recover(*, max_attempts: int = 3, retention_seconds: int = 86400) -> dict[str, int]:
    """Startup recovery: claimed->pending (< max_attempts), claimed->failed (>= max_attempts).
    Also resets stale injected->pending. Cleans up consumed/failed older than retention_seconds.
    Returns counts dict.
    """
    counts = {"reset_pending": 0, "exhausted_failed": 0, "injected_reset": 0, "cleaned": 0}
    with db_connection() as db:
        try:
            # Reset retryable claimed → pending
            reset = db.execute(
                "UPDATE delegation_notifications SET state='pending', claimed_at=NULL "
                "WHERE state='claimed' AND attempt_count < ?",
                (max_attempts,),
            )
            counts["reset_pending"] = reset.rowcount

            # Exhaust non-retryable claimed → failed
            exhausted = db.execute(
                "UPDATE delegation_notifications SET state='failed', last_error='max attempts exceeded' "
                "WHERE state='claimed' AND attempt_count >= ?",
                (max_attempts,),
            )
            counts["exhausted_failed"] = exhausted.rowcount

            # Reset stale injected → pending (injected but never consumed means
            # the model run crashed after injection — retry).
            # Exclude fire-and-forget notifications (system_wake) — these have no
            # TASK_DONE marker and should NOT be retried; mark them consumed instead.
            db.execute(
                "UPDATE delegation_notifications SET state='consumed' "
                "WHERE state='injected' AND task_id='system_wake'",
            )
            injected_reset = db.execute(
                "UPDATE delegation_notifications SET state='pending', claimed_at=NULL, injected_at=NULL "
                "WHERE state='injected' AND attempt_count < ? AND task_id != 'system_wake'",
                (max_attempts,),
            )
            counts["injected_reset"] = injected_reset.rowcount

            # Clean up old consumed/failed rows
            cutoff = int(time.time()) - retention_seconds
            cleaned = db.execute(
                "DELETE FROM delegation_notifications WHERE state IN ('consumed','failed') AND created_at < ?",
                (cutoff,),
            )
            counts["cleaned"] = cleaned.rowcount

            db.commit()
            total = sum(counts.values())
            if total:
                logger.info(
                    f"Delegation notification recovery: reset={counts['reset_pending']} claimed→pending, "
                    f"exhausted={counts['exhausted_failed']} claimed→failed, "
                    f"injected_reset={counts['injected_reset']} injected→pending, "
                    f"cleaned={counts['cleaned']} old rows"
                )
        except Exception as e:
            logger.warning(f"Delegation notification recovery failed: {e}")
    return counts


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_notification(payload: dict) -> str:
    """Render a stored DB notification payload into human-readable text.

    Used when draining notifications from the DB inbox (delegation_notifications
    table). If the payload already has a pre-formatted message, returns it directly.
    Otherwise reconstructs from structured fields.
    """
    msg = payload.get("message", "")
    if msg:
        return msg
    # Fallback: reconstruct from fields — minimal format when no pre-rendered message
    task_id = payload.get("task_id", "unknown")
    to_status = payload.get("to_status", "?")
    worker = payload.get("worker", "unknown")
    model = payload.get("model", "unknown")
    summary = payload.get("summary", "")
    lines = [
        "[DELEGATION UPDATE]",
        f"Task: {task_id}",
        f"Worker: {worker} ({model})",
        f"Status: {payload.get('from_status', '?')} → {to_status}",
    ]
    elapsed = payload.get("elapsed_seconds", 0)
    if elapsed:
        lines.append(f"Elapsed: {int(elapsed)}s")
    if summary:
        safe_summary = str(summary).replace(DELEGATION_PREPEND_BOUNDARY, "[delegation-boundary]")
        lines.append(f"Result: {safe_summary}")
    return "\n".join(lines)
