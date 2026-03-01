"""
email_cache.py — SQLite-backed email cache for instant inbox loading.

Stores message summaries and full bodies from IMAP syncs.
Background sync task populates the cache; frontend reads from it first.
DB lives in KUKUIBOT_HOME/email_cache.db (local only, not pushed to GitHub).
"""

import json
import logging
import sqlite3
import time

from email.utils import parsedate_to_datetime

from config import KUKUIBOT_HOME

logger = logging.getLogger("kukuibot.email_cache")

DB_PATH = KUKUIBOT_HOME / "email_cache.db"


def _get_db() -> sqlite3.Connection:
    """Get cache DB connection with WAL mode, NORMAL sync, Row factory."""
    try:
        db = sqlite3.connect(str(DB_PATH), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(db)
        return db
    except Exception as e:
        logger.warning(f"email_cache: failed to open DB: {e}")
        raise


def _ensure_schema(db: sqlite3.Connection):
    """Create table and indexes if not exists."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            folder TEXT NOT NULL,
            message_id TEXT,
            from_addr TEXT,
            to_addr TEXT,
            subject TEXT,
            date TEXT,
            date_ts INTEGER,
            snippet TEXT,
            body_text TEXT,
            body_html TEXT,
            is_read INTEGER DEFAULT 0,
            has_attachments INTEGER DEFAULT 0,
            attachment_info TEXT,
            synced_at INTEGER,
            UNIQUE(uid, folder)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_folder_date ON messages(folder, date_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_folder_uid ON messages(folder, uid);
        CREATE INDEX IF NOT EXISTS idx_messages_synced ON messages(synced_at);
    """)


def _parse_date_ts(date_str: str) -> int:
    """Parse an email date header to unix timestamp. Returns 0 on failure."""
    if not date_str:
        return 0
    try:
        dt = parsedate_to_datetime(date_str)
        return int(dt.timestamp())
    except Exception:
        pass
    # Fallback: try generic parsing
    try:
        from datetime import datetime
        # Try common formats
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z"):
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                return int(dt.timestamp())
            except ValueError:
                continue
    except Exception:
        pass
    return 0


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a dict, parsing attachment_info JSON."""
    d = dict(row)
    d["is_read"] = bool(d.get("is_read"))
    d["has_attachments"] = bool(d.get("has_attachments"))
    if d.get("attachment_info"):
        try:
            d["attachment_info"] = json.loads(d["attachment_info"])
        except (json.JSONDecodeError, TypeError):
            d["attachment_info"] = []
    else:
        d["attachment_info"] = []
    return d


def get_cached_messages(folder: str, max_results: int = 50, search: str = "") -> list[dict]:
    """Get messages from cache. If search provided, LIKE match on from/to/subject/snippet."""
    try:
        db = _get_db()
        try:
            if search:
                like = f"%{search}%"
                rows = db.execute(
                    """SELECT uid, folder, message_id, from_addr, to_addr, subject, date,
                              date_ts, snippet, is_read, has_attachments, attachment_info, synced_at
                       FROM messages
                       WHERE folder = ? AND (from_addr LIKE ? OR to_addr LIKE ? OR subject LIKE ? OR snippet LIKE ?)
                       ORDER BY date_ts DESC LIMIT ?""",
                    (folder, like, like, like, like, max_results),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT uid, folder, message_id, from_addr, to_addr, subject, date,
                              date_ts, snippet, is_read, has_attachments, attachment_info, synced_at
                       FROM messages
                       WHERE folder = ?
                       ORDER BY date_ts DESC LIMIT ?""",
                    (folder, max_results),
                ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"get_cached_messages error: {e}")
        return []


def get_cached_message(folder: str, uid: str) -> dict | None:
    """Get full cached message by folder+uid including body. Returns None if not cached or no body."""
    try:
        db = _get_db()
        try:
            row = db.execute(
                """SELECT * FROM messages WHERE folder = ? AND uid = ?""",
                (folder, uid),
            ).fetchone()
            if not row:
                return None
            d = _row_to_dict(row)
            # Only return if we have body content cached
            if not d.get("body_text"):
                return None
            return d
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"get_cached_message error: {e}")
        return None


def upsert_messages(messages: list[dict], folder: str):
    """Insert or update message summaries from list_messages."""
    if not messages:
        return
    try:
        db = _get_db()
        try:
            now = int(time.time())
            for m in messages:
                date_ts = _parse_date_ts(m.get("date", ""))
                db.execute(
                    """INSERT INTO messages (uid, folder, message_id, from_addr, to_addr, subject,
                                            date, date_ts, snippet, is_read, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(uid, folder) DO UPDATE SET
                           message_id = excluded.message_id,
                           from_addr = excluded.from_addr,
                           to_addr = excluded.to_addr,
                           subject = excluded.subject,
                           date = excluded.date,
                           date_ts = excluded.date_ts,
                           snippet = excluded.snippet,
                           is_read = excluded.is_read,
                           synced_at = excluded.synced_at""",
                    (
                        str(m.get("uid", "")),
                        folder,
                        m.get("message_id", ""),
                        m.get("from", ""),
                        m.get("to", ""),
                        m.get("subject", ""),
                        m.get("date", ""),
                        date_ts,
                        m.get("snippet", ""),
                        1 if m.get("is_read") else 0,
                        now,
                    ),
                )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"upsert_messages error: {e}")


def upsert_full_message(msg: dict, folder: str):
    """Update cache with full message body from get_message. Also stores attachment_info."""
    try:
        db = _get_db()
        try:
            now = int(time.time())
            uid = str(msg.get("uid", ""))
            date_ts = _parse_date_ts(msg.get("date", ""))
            attachments = msg.get("attachments", [])
            has_attachments = 1 if attachments else 0
            att_json = json.dumps(attachments) if attachments else None

            db.execute(
                """INSERT INTO messages (uid, folder, message_id, from_addr, to_addr, subject,
                                        date, date_ts, snippet, body_text, body_html,
                                        is_read, has_attachments, attachment_info, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(uid, folder) DO UPDATE SET
                       body_text = excluded.body_text,
                       body_html = excluded.body_html,
                       has_attachments = excluded.has_attachments,
                       attachment_info = excluded.attachment_info,
                       synced_at = excluded.synced_at""",
                (
                    uid,
                    folder,
                    msg.get("message_id", ""),
                    msg.get("from", ""),
                    msg.get("to", ""),
                    msg.get("subject", ""),
                    msg.get("date", ""),
                    date_ts,
                    (msg.get("body", "") or "")[:120].replace("\n", " ").strip(),
                    msg.get("body", ""),
                    msg.get("body_html"),
                    1,  # if we fetched full message, mark as read
                    has_attachments,
                    att_json,
                    now,
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"upsert_full_message error: {e}")


def delete_message(folder: str, uid: str):
    """Remove a message from cache."""
    try:
        db = _get_db()
        try:
            db.execute("DELETE FROM messages WHERE folder = ? AND uid = ?", (folder, uid))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"delete_message error: {e}")


def update_message_flag(folder: str, uid: str, is_read: bool):
    """Update the is_read flag for a cached message."""
    try:
        db = _get_db()
        try:
            db.execute(
                "UPDATE messages SET is_read = ? WHERE folder = ? AND uid = ?",
                (1 if is_read else 0, folder, uid),
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"update_message_flag error: {e}")


def get_sync_status() -> dict:
    """Return sync status: total cached, last sync time, per-folder counts."""
    try:
        db = _get_db()
        try:
            total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            last_sync_row = db.execute("SELECT MAX(synced_at) FROM messages").fetchone()
            last_sync_ts = last_sync_row[0] if last_sync_row and last_sync_row[0] else 0

            folders_rows = db.execute(
                "SELECT folder, COUNT(*) as cnt FROM messages GROUP BY folder"
            ).fetchall()
            folders = {r["folder"]: r["cnt"] for r in folders_rows}

            last_sync_ago = ""
            if last_sync_ts:
                ago = int(time.time()) - last_sync_ts
                if ago < 60:
                    last_sync_ago = "just now"
                elif ago < 3600:
                    last_sync_ago = f"{ago // 60}m ago"
                elif ago < 86400:
                    last_sync_ago = f"{ago // 3600}h ago"
                else:
                    last_sync_ago = f"{ago // 86400}d ago"

            return {
                "total_cached": total,
                "last_sync_ts": last_sync_ts,
                "last_sync_ago_str": last_sync_ago,
                "folders": folders,
            }
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"get_sync_status error: {e}")
        return {"total_cached": 0, "last_sync_ts": 0, "last_sync_ago_str": "error", "folders": {}}


def cleanup_old_messages(days: int = 30):
    """Delete messages older than N days."""
    try:
        cutoff = int(time.time()) - days * 86400
        db = _get_db()
        try:
            result = db.execute("DELETE FROM messages WHERE date_ts > 0 AND date_ts < ?", (cutoff,))
            db.commit()
            if result.rowcount:
                logger.info(f"email_cache: cleaned up {result.rowcount} messages older than {days} days")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"cleanup_old_messages error: {e}")


def clear_cache():
    """Drop and recreate the messages table."""
    try:
        db = _get_db()
        try:
            db.execute("DROP TABLE IF EXISTS messages")
            _ensure_schema(db)
            db.commit()
            logger.info("email_cache: cache cleared")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"clear_cache error: {e}")
