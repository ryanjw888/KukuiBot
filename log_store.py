"""
log_store.py — SQLite-backed log storage for KukuiBot.

Replaces file-based logging with a single indexed SQLite table.
Thread-safe via thread-local connections. WAL mode for concurrent reads.

Usage:
    from log_store import log_write, log_query, log_purge, init_log_db

    # Write
    log_write("chat", "Hello world", role="user", session_id="tab-codex-abc")

    # Query
    rows = log_query(category="chat", session_id="tab-codex-abc", limit=50)

    # Purge old entries
    log_purge(max_age_days=30)
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from threading import local as _thread_local

from config import KUKUIBOT_HOME

LOG_DB_PATH = KUKUIBOT_HOME / "logs" / "kukuibot-logs.db"
MAX_MESSAGE_CHARS = 10_000
MAX_DB_SIZE_MB = 500  # Auto-purge oldest entries when DB exceeds this size

_log_db_local = _thread_local()
_write_count = 0  # Track writes for periodic size check
_SIZE_CHECK_INTERVAL = 500  # Check DB size every N writes

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime')),
    ts_unix REAL NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    category TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'INFO',
    source TEXT NOT NULL DEFAULT '',
    session_id TEXT DEFAULT '',
    worker TEXT DEFAULT '',
    role TEXT DEFAULT '',
    message TEXT NOT NULL,
    metadata TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts_unix);
CREATE INDEX IF NOT EXISTS idx_logs_category_ts ON logs(category, ts_unix);
CREATE INDEX IF NOT EXISTS idx_logs_session ON logs(session_id, ts_unix) WHERE session_id != '';
CREATE INDEX IF NOT EXISTS idx_logs_worker ON logs(worker, ts_unix) WHERE worker != '';
CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level, ts_unix) WHERE level IN ('WARNING', 'ERROR');
CREATE INDEX IF NOT EXISTS idx_logs_role_ts ON logs(role, ts_unix) WHERE role != '';
"""


def _get_log_db() -> sqlite3.Connection:
    """Thread-local SQLite connection with WAL mode."""
    conn = getattr(_log_db_local, "conn", None)
    if conn is None:
        LOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(LOG_DB_PATH), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA wal_autocheckpoint=500")  # checkpoint every 500 pages (~2MB)
        _log_db_local.conn = conn
        # Restrict DB file permissions to owner-only (contains user messages)
        try:
            os.chmod(str(LOG_DB_PATH), 0o600)
        except Exception:
            pass
    return conn


def init_log_db() -> None:
    """Create the logs table and indexes if they don't exist. Idempotent."""
    conn = _get_log_db()
    conn.executescript(_SCHEMA_SQL)
    conn.commit()


def log_write(
    category: str,
    message: str,
    *,
    level: str = "INFO",
    source: str = "",
    session_id: str = "",
    worker: str = "",
    role: str = "",
    metadata: dict | None = None,
) -> None:
    """Write a log entry to SQLite."""
    try:
        conn = _get_log_db()
        conn.execute(
            """INSERT INTO logs (category, level, source, session_id, worker, role, message, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                category,
                level,
                source,
                session_id or "",
                worker or "",
                role or "",
                message[:MAX_MESSAGE_CHARS] if len(message) > MAX_MESSAGE_CHARS else message,
                json.dumps(metadata) if metadata else None,
            ),
        )
        conn.commit()
        _maybe_auto_purge()
    except Exception as e:
        print(f"LOG_WRITE_FAILED: {category} {level} {message[:200]} — {e}", file=sys.stderr)


def log_write_batch(entries: list[dict]) -> int:
    """Write multiple log entries in a single transaction. Returns count written."""
    if not entries:
        return 0
    try:
        conn = _get_log_db()
        rows = []
        for e in entries:
            msg = e.get("message", "")
            rows.append((
                e.get("category", "system"),
                e.get("level", "INFO"),
                e.get("source", ""),
                e.get("session_id", ""),
                e.get("worker", ""),
                e.get("role", ""),
                msg[:MAX_MESSAGE_CHARS] if len(msg) > MAX_MESSAGE_CHARS else msg,
                json.dumps(e["metadata"]) if e.get("metadata") else None,
            ))
        conn.executemany(
            """INSERT INTO logs (category, level, source, session_id, worker, role, message, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    except Exception as e:
        print(f"LOG_WRITE_BATCH_FAILED: {len(entries)} entries — {e}", file=sys.stderr)
        return 0


def log_query(
    *,
    category: str | None = None,
    session_id: str | None = None,
    worker: str | None = None,
    level: str | None = None,
    role: str | None = None,
    source: str | None = None,
    since_unix: float | None = None,
    until_unix: float | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
    order: str = "DESC",
) -> list[dict]:
    """Query logs with filters. Returns list of log entry dicts."""
    conn = _get_log_db()
    clauses: list[str] = []
    params: list = []

    if category:
        clauses.append("category = ?")
        params.append(category)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if worker:
        clauses.append("worker = ?")
        params.append(worker)
    if level:
        clauses.append("level = ?")
        params.append(level)
    if role:
        clauses.append("role = ?")
        params.append(role)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if since_unix is not None:
        clauses.append("ts_unix >= ?")
        params.append(since_unix)
    if until_unix is not None:
        clauses.append("ts_unix < ?")
        params.append(until_unix)
    if search:
        clauses.append("message LIKE ?")
        params.append(f"%{search}%")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    direction = "ASC" if order.upper() == "ASC" else "DESC"
    params.extend([max(1, min(limit, 10000)), max(0, offset)])

    rows = conn.execute(
        f"SELECT id, ts, ts_unix, category, level, source, session_id, worker, role, message, metadata "
        f"FROM logs {where} ORDER BY ts_unix {direction} LIMIT ? OFFSET ?",
        params,
    ).fetchall()

    return [
        {
            "id": r[0],
            "ts": r[1],
            "ts_unix": r[2],
            "category": r[3],
            "level": r[4],
            "source": r[5],
            "session_id": r[6],
            "worker": r[7],
            "role": r[8],
            "message": r[9],
            "metadata": json.loads(r[10]) if r[10] else None,
        }
        for r in rows
    ]


def log_count(
    *,
    category: str | None = None,
    session_id: str | None = None,
    since_unix: float | None = None,
    until_unix: float | None = None,
    level: str | None = None,
) -> int:
    """Count log entries matching filters."""
    conn = _get_log_db()
    clauses: list[str] = []
    params: list = []

    if category:
        clauses.append("category = ?")
        params.append(category)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if since_unix is not None:
        clauses.append("ts_unix >= ?")
        params.append(since_unix)
    if until_unix is not None:
        clauses.append("ts_unix < ?")
        params.append(until_unix)
    if level:
        clauses.append("level = ?")
        params.append(level)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = conn.execute(f"SELECT COUNT(*) FROM logs {where}", params).fetchone()
    return row[0] if row else 0


def log_stats() -> list[dict]:
    """Return per-category stats: count, oldest, newest."""
    conn = _get_log_db()
    rows = conn.execute(
        "SELECT category, COUNT(*), MIN(ts), MAX(ts), MIN(ts_unix), MAX(ts_unix) "
        "FROM logs GROUP BY category ORDER BY category"
    ).fetchall()
    return [
        {
            "category": r[0],
            "count": r[1],
            "oldest": r[2],
            "newest": r[3],
            "oldest_unix": r[4],
            "newest_unix": r[5],
        }
        for r in rows
    ]


def log_purge(max_age_days: int = 30) -> dict:
    """Delete logs older than max_age_days and reclaim space."""
    conn = _get_log_db()
    cutoff = time.time() - (max_age_days * 86400)

    cursor = conn.execute("SELECT COUNT(*) FROM logs WHERE ts_unix < ?", (cutoff,))
    count = cursor.fetchone()[0]

    if count == 0:
        return {"deleted": 0, "cutoff_days": max_age_days}

    conn.execute("DELETE FROM logs WHERE ts_unix < ?", (cutoff,))
    conn.commit()

    # Reclaim disk space only if significant deletion
    if count > 1000:
        conn.execute("VACUUM")

    return {"deleted": count, "cutoff_days": max_age_days}


def log_export_chatlog_format(
    since_unix: float,
    until_unix: float,
    *,
    session_id: str | None = None,
) -> str:
    """Export chat logs in the old file format for backward compatibility.

    Produces lines like: [ROLE YYYY-MM-DD HH:MM:SS session_id]: content
    """
    rows = log_query(
        category="chat",
        session_id=session_id,
        since_unix=since_unix,
        until_unix=until_unix,
        limit=100000,
        order="ASC",
    )
    lines = []
    for r in rows:
        ts = r["ts"][:19]  # Trim to YYYY-MM-DD HH:MM:SS (remove fractional seconds)
        # Normalize the T separator to space for legacy compatibility
        ts = ts.replace("T", " ")
        role = (r["role"] or "SYSTEM").upper()
        sid = r["session_id"] or ""
        msg = r["message"].replace("\n", "\\n")
        lines.append(f"[{role} {ts} {sid}]: {msg}")
    return "\n".join(lines)


def log_db_size() -> int:
    """Return the log database file size in bytes."""
    try:
        return LOG_DB_PATH.stat().st_size
    except Exception:
        return 0


def _maybe_auto_purge() -> None:
    """Periodically check DB size and delete oldest entries if over limit.

    Called after every log_write(). Only actually checks disk every
    _SIZE_CHECK_INTERVAL writes to avoid stat() on every insert.
    """
    global _write_count
    _write_count += 1
    if _write_count % _SIZE_CHECK_INTERVAL != 0:
        return

    try:
        size = LOG_DB_PATH.stat().st_size
        max_bytes = MAX_DB_SIZE_MB * 1024 * 1024
        if size <= max_bytes:
            return

        conn = _get_log_db()
        total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        if total == 0:
            return

        # Delete oldest 20% of entries
        delete_count = max(1, total // 5)
        conn.execute(
            "DELETE FROM logs WHERE id IN ("
            "  SELECT id FROM logs ORDER BY ts_unix ASC LIMIT ?"
            ")",
            (delete_count,),
        )
        conn.commit()
        print(f"LOG_AUTO_PURGE: deleted {delete_count}/{total} oldest entries "
              f"(db was {size // (1024*1024)}MB, limit {MAX_DB_SIZE_MB}MB)",
              file=sys.stderr)
    except Exception as e:
        print(f"LOG_AUTO_PURGE_FAILED: {e}", file=sys.stderr)
