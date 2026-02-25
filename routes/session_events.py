"""
session_events.py — Session Event System extracted from server.py.

Provides the SessionEventStore class, DB run helpers, schema migration,
and the _emit_event SSE emission function.
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

from auth import db_connection

logger = logging.getLogger("kukuibot.session_events")


# One-time schema backfill guard for session event cursors.
_session_event_schema_backfilled: bool = False


def _ensure_chat_event_schema(db):
    """Durable chat run/event journal for stream resume across refresh/reconnect."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL,
            last_seq INTEGER DEFAULT 0,
            last_event_at REAL,
            final_text TEXT,
            usage_json TEXT,
            error_message TEXT,
            updated_at REAL NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_chat_runs_session_started ON chat_runs(session_id, started_at DESC)")

    # Legacy per-run stream events (kept for backwards compatibility / historical rows).
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_events (
            run_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (run_id, seq)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_chat_events_session_seq ON chat_events(session_id, seq)")

    # New per-session monotonic event journal for persistent replay.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_events (
            session_id TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            timestamp REAL NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (session_id, event_id)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_session_events_session_event ON session_events(session_id, event_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_session_events_session_time ON session_events(session_id, timestamp)")

    # Persistent high-water mark per session so event_id never rewinds, even after TTL pruning.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_event_cursors (
            session_id TEXT PRIMARY KEY,
            last_event_id INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )

    global _session_event_schema_backfilled
    if not _session_event_schema_backfilled:
        # Backfill cursor table from existing event history (best effort, idempotent).
        db.execute(
            """
            INSERT INTO session_event_cursors (session_id, last_event_id, updated_at)
            SELECT se.session_id, MAX(se.event_id), ?
            FROM session_events se
            LEFT JOIN session_event_cursors c ON c.session_id = se.session_id
            WHERE c.session_id IS NULL
            GROUP BY se.session_id
            """,
            (time.time(),),
        )
        _session_event_schema_backfilled = True


class SessionEventStore:
    """Per-session monotonic event journal with in-memory ring + SQLite durability."""

    def __init__(
        self,
        *,
        ring_max_events: int,
        ring_max_bytes: int,
        db_max_events_per_session: int,
        ttl_seconds: int,
    ):
        self.ring_max_events = max(50, int(ring_max_events or 500))
        self.ring_max_bytes = max(128 * 1024, int(ring_max_bytes or (2 * 1024 * 1024)))
        self.db_max_events_per_session = max(200, int(db_max_events_per_session or 5000))
        self.ttl_seconds = max(300, int(ttl_seconds or 86400))

        self._rings: dict[str, deque[tuple[dict, int]]] = {}
        self._ring_bytes: dict[str, int] = {}
        self._next_event_id: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_prune_at: dict[str, float] = {}

    def _lock_for_session(self, session_id: str) -> asyncio.Lock:
        sid = str(session_id or "default")
        lock = self._locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[sid] = lock
        return lock

    def _seed_next_event_id(self, session_id: str) -> int:
        sid = str(session_id or "default")
        cached = self._next_event_id.get(sid)
        if cached is not None:
            return cached

        nxt = 1
        try:
            with db_connection() as db:
                _ensure_chat_event_schema(db)
                row = db.execute(
                    "SELECT COALESCE(MAX(event_id), 0) FROM session_events WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                max_event_id = int((row[0] if row else 0) or 0)

                cursor = db.execute(
                    "SELECT COALESCE(last_event_id, 0) FROM session_event_cursors WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                max_event_id = max(max_event_id, int((cursor[0] if cursor else 0) or 0))

                if max_event_id <= 0:
                    # Migration fallback from legacy rows where only seq existed.
                    legacy = db.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM chat_events WHERE session_id = ?",
                        (sid,),
                    ).fetchone()
                    max_event_id = max(max_event_id, int((legacy[0] if legacy else 0) or 0))

                if max_event_id > 0:
                    db.execute(
                        """
                        INSERT INTO session_event_cursors (session_id, last_event_id, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(session_id) DO UPDATE SET
                          last_event_id = CASE WHEN excluded.last_event_id > session_event_cursors.last_event_id THEN excluded.last_event_id ELSE session_event_cursors.last_event_id END,
                          updated_at = excluded.updated_at
                        """,
                        (sid, max_event_id, time.time()),
                    )
                    db.commit()

                nxt = max_event_id + 1
        except Exception as e:
            logger.warning(f"session_events next-id seed failed for {sid}: {e}")

        self._next_event_id[sid] = max(1, int(nxt))
        return self._next_event_id[sid]

    def peek_next_event_id(self, session_id: str) -> int:
        return self._seed_next_event_id(session_id)

    def latest_event_id(self, session_id: str) -> int:
        return max(0, self.peek_next_event_id(session_id) - 1)

    @staticmethod
    def _normalize_payload(event: dict) -> dict:
        payload: dict[str, Any] = {}
        for k, v in (event or {}).items():
            if k in {"event_id", "session_id", "run_id", "type", "payload", "seq"}:
                continue
            payload[k] = v
        return payload

    @staticmethod
    def _legacy_event_from_envelope(envelope: dict) -> dict:
        payload = dict(envelope.get("payload") or {})
        evt = {
            **payload,
            "type": str(envelope.get("type") or ""),
            "event_id": int(envelope.get("event_id") or 0),
            "seq": int(envelope.get("event_id") or 0),  # backwards-compatible field used by frontend
            "session_id": str(envelope.get("session_id") or ""),
            "run_id": str(envelope.get("run_id") or ""),
        }
        if "ts" not in evt:
            evt["ts"] = float(envelope.get("ts") or time.time())
        return evt

    def _ring_append(self, session_id: str, envelope: dict):
        sid = str(session_id or "default")
        ring = self._rings.setdefault(sid, deque())
        ring_bytes = int(self._ring_bytes.get(sid, 0) or 0)
        encoded = json.dumps(envelope, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        ring.append((envelope, size))
        ring_bytes += size

        while ring and (len(ring) > self.ring_max_events or ring_bytes > self.ring_max_bytes):
            _old_evt, old_size = ring.popleft()
            ring_bytes = max(0, ring_bytes - int(old_size or 0))

        self._ring_bytes[sid] = ring_bytes

    def _persist_event(self, envelope: dict, legacy_evt: dict):
        sid = str(envelope.get("session_id") or "default")
        run_id = str(envelope.get("run_id") or "")
        event_id = int(envelope.get("event_id") or 0)
        ts = float(envelope.get("ts") or time.time())
        evt_type = str(envelope.get("type") or "")
        payload_json = json.dumps(envelope.get("payload") or {})

        with db_connection() as db:
            try:
                _ensure_chat_event_schema(db)
                db.execute(
                    """
                    INSERT OR REPLACE INTO session_events
                      (session_id, event_id, run_id, event_type, timestamp, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (sid, event_id, run_id, evt_type, ts, payload_json),
                )
                db.execute(
                    """
                    INSERT INTO session_event_cursors (session_id, last_event_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                      last_event_id = CASE WHEN excluded.last_event_id > session_event_cursors.last_event_id THEN excluded.last_event_id ELSE session_event_cursors.last_event_id END,
                      updated_at = excluded.updated_at
                    """,
                    (sid, event_id, ts),
                )

                if run_id:
                    db.execute(
                        "INSERT OR REPLACE INTO chat_events (run_id, session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?, ?)",
                        (run_id, sid, event_id, json.dumps(legacy_evt), ts),
                    )

                    final_text = None
                    usage_json = None
                    error_message = None
                    status = "running"
                    ended_at = None
                    if evt_type == "done":
                        status = "done"
                        ended_at = ts
                        final_text = str(legacy_evt.get("text") or "")
                        usage_json = json.dumps(legacy_evt.get("usage") or {})
                    elif evt_type == "error":
                        error_message = str(legacy_evt.get("message") or "")[:2000]

                    db.execute(
                        """
                        UPDATE chat_runs SET
                            status = CASE WHEN ? = 'done' THEN 'done' ELSE status END,
                            ended_at = CASE WHEN ? = 'done' THEN ? ELSE ended_at END,
                            last_seq = CASE WHEN last_seq < ? THEN ? ELSE last_seq END,
                            last_event_at = ?,
                            final_text = COALESCE(?, final_text),
                            usage_json = COALESCE(?, usage_json),
                            error_message = COALESCE(?, error_message),
                            updated_at = ?
                        WHERE run_id = ?
                        """,
                        (
                            status,
                            status,
                            ended_at,
                            event_id,
                            event_id,
                            ts,
                            final_text,
                            usage_json,
                            error_message,
                            ts,
                            run_id,
                        ),
                    )

                db.commit()
            except Exception:
                db.rollback()
                raise

    def _prune_db(self, session_id: str, now_ts: float):
        sid = str(session_id or "default")
        last = float(self._last_prune_at.get(sid, 0) or 0)
        # Keep pruning amortized; we don't need this on every event.
        if now_ts - last < 60:
            return
        self._last_prune_at[sid] = now_ts

        cutoff = float(now_ts - float(self.ttl_seconds))
        with db_connection() as db:
            try:
                _ensure_chat_event_schema(db)
                db.execute(
                    "DELETE FROM session_events WHERE session_id = ? AND timestamp < ?",
                    (sid, cutoff),
                )

                keep = int(self.db_max_events_per_session)
                if keep > 0:
                    db.execute(
                        """
                        DELETE FROM session_events
                        WHERE session_id = ?
                          AND event_id < COALESCE(
                              (
                                  SELECT event_id FROM session_events
                                  WHERE session_id = ?
                                  ORDER BY event_id DESC
                                  LIMIT 1 OFFSET ?
                              ),
                              -1
                          )
                        """,
                        (sid, sid, keep - 1),
                    )

                    db.execute(
                        """
                        DELETE FROM chat_events
                        WHERE session_id = ?
                          AND (created_at < ? OR seq < COALESCE(
                              (
                                  SELECT event_id FROM session_events
                                  WHERE session_id = ?
                                  ORDER BY event_id DESC
                                  LIMIT 1 OFFSET ?
                              ),
                              -1
                          ))
                        """,
                        (sid, cutoff, sid, keep - 1),
                    )

                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"session_events prune failed for {sid}: {e}")

    async def append_event(self, session_id: str, run_id: str, event: dict, now_ts: float | None = None) -> tuple[dict, dict]:
        sid = str(session_id or "default")
        rid = str(run_id or "")
        ts = float(now_ts if now_ts is not None else time.time())

        async with self._lock_for_session(sid):
            event_id = int(self._seed_next_event_id(sid))
            self._next_event_id[sid] = event_id + 1

            evt_type = str((event or {}).get("type") or "")
            payload = self._normalize_payload(event or {})
            envelope = {
                "event_id": event_id,
                "session_id": sid,
                "run_id": rid,
                "type": evt_type,
                "ts": ts,
                "payload": payload,
            }
            legacy_evt = self._legacy_event_from_envelope(envelope)

            self._ring_append(sid, envelope)
            self._persist_event(envelope, legacy_evt)
            self._prune_db(sid, ts)

            return envelope, legacy_evt

    def _db_load_legacy_events(self, session_id: str, after_event_id: int = 0, limit: int = 15000) -> list[dict]:
        sid = str(session_id or "default")
        cap = max(1, min(int(limit or 15000), 50000))
        out: list[dict] = []
        try:
            with db_connection() as db:
                _ensure_chat_event_schema(db)
                rows = db.execute(
                    """
                    SELECT event_id, run_id, event_type, timestamp, payload_json
                    FROM session_events
                    WHERE session_id = ? AND event_id > ?
                    ORDER BY event_id ASC
                    LIMIT ?
                    """,
                    (sid, int(after_event_id or 0), cap),
                ).fetchall()

                for row in rows:
                    try:
                        payload = json.loads(row[4] or "{}")
                    except Exception:
                        payload = {}
                    envelope = {
                        "event_id": int(row[0] or 0),
                        "session_id": sid,
                        "run_id": str(row[1] or ""),
                        "type": str(row[2] or ""),
                        "ts": float(row[3] or 0),
                        "payload": payload if isinstance(payload, dict) else {},
                    }
                    out.append(self._legacy_event_from_envelope(envelope))
            return out
        except Exception as e:
            logger.warning(f"session_events replay load failed for {sid}: {e}")
            return []

    def replay_events(self, session_id: str, after_event_id: int = 0, limit: int = 15000) -> list[dict]:
        sid = str(session_id or "default")
        cap = max(1, min(int(limit or 15000), 50000))
        after = int(after_event_id or 0)

        ring = self._rings.get(sid)
        if ring:
            first_id = int(ring[0][0].get("event_id") or 0)
            # Ring can satisfy replay only when it fully covers requested cursor.
            if after >= (first_id - 1):
                out: list[dict] = []
                for envelope, _size in list(ring):
                    eid = int(envelope.get("event_id") or 0)
                    if eid > after:
                        out.append(self._legacy_event_from_envelope(envelope))
                    if len(out) >= cap:
                        break
                return out

        return self._db_load_legacy_events(sid, after_event_id=after, limit=cap)

    # Alias kept for API call-sites that refer to replay().
    def replay(self, session_id: str, after_event_id: int = 0, limit: int = 15000) -> list[dict]:
        return self.replay_events(session_id=session_id, after_event_id=after_event_id, limit=limit)

    def reap_stale_sessions(self, max_age_seconds: float = 3600) -> int:
        """Remove ring buffer entries for sessions with no activity in max_age_seconds.

        Returns the number of sessions reaped.
        """
        now = time.time()
        stale = []
        for sid, last_prune in self._last_prune_at.items():
            if (now - last_prune) > max_age_seconds:
                stale.append(sid)
        # Also reap sessions that have rings but no prune timestamp (orphaned)
        for sid in list(self._rings.keys()):
            if sid not in self._last_prune_at and sid not in stale:
                stale.append(sid)
        for sid in stale:
            self._rings.pop(sid, None)
            self._ring_bytes.pop(sid, None)
            self._next_event_id.pop(sid, None)
            self._locks.pop(sid, None)
            self._last_prune_at.pop(sid, None)
        if stale:
            logger.info(f"SessionEventStore: reaped {len(stale)} stale session(s) from ring buffers")
        return len(stale)


# --- DB Run Helpers ---


def _db_start_run(session_id: str, run_id: str, started_at: float):
    try:
        with db_connection() as db:
            _ensure_chat_event_schema(db)
            db.execute(
                """
                INSERT OR REPLACE INTO chat_runs
                  (run_id, session_id, status, started_at, ended_at, last_seq, last_event_at, final_text, usage_json, error_message, updated_at)
                VALUES (?, ?, 'running', ?, NULL, 0, ?, '', '', '', ?)
                """,
                (run_id, session_id, float(started_at), float(started_at), float(started_at)),
            )
            db.commit()
    except Exception as e:
        logger.warning(f"chat_runs start persist failed: {e}")


def _db_mark_run_done(run_id: str, status: str = "done"):
    if not run_id:
        return
    try:
        now = time.time()
        with db_connection() as db:
            _ensure_chat_event_schema(db)
            db.execute(
                "UPDATE chat_runs SET status = ?, ended_at = COALESCE(ended_at, ?), updated_at = ? WHERE run_id = ?",
                (str(status or "done"), float(now), float(now), run_id),
            )
            db.commit()
    except Exception as e:
        logger.warning(f"chat_runs finalize persist failed: {e}")


def _db_latest_run(session_id: str) -> dict | None:
    try:
        with db_connection() as db:
            _ensure_chat_event_schema(db)
            row = db.execute(
                """
                SELECT run_id, status, started_at, ended_at, last_seq, last_event_at, final_text, usage_json, error_message
                FROM chat_runs
                WHERE session_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "run_id": str(row[0] or ""),
            "status": str(row[1] or "idle"),
            "started_at": float(row[2] or 0),
            "ended_at": float(row[3] or 0) if row[3] is not None else None,
            "last_seq": int(row[4] or 0),
            "last_event_at": float(row[5] or row[2] or 0),
            "final_text": str(row[6] or ""),
            "usage_json": str(row[7] or ""),
            "error_message": str(row[8] or ""),
        }
    except Exception as e:
        logger.warning(f"chat_runs latest lookup failed: {e}")
        return None


def _db_load_run_events(run_id: str, after_seq: int = 0, limit: int = 15000) -> list[dict]:
    if not run_id:
        return []
    try:
        cap = max(1, min(int(limit or 15000), 50000))
        with db_connection() as db:
            _ensure_chat_event_schema(db)
            rows = db.execute(
                "SELECT event_json FROM chat_events WHERE run_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (run_id, int(after_seq or 0), cap),
            ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r[0]))
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning(f"chat_events replay load failed: {e}")
        return []


# --- SSE Event Emission ---

# Module-level references set by init_event_system() from server.py at startup.
_store: SessionEventStore | None = None
_active_tasks: dict[str, dict] | None = None
_resume_subscribers: dict[str, list[asyncio.Queue]] | None = None
_anthropic_event_subscribers: dict[str, list[asyncio.Queue]] | None = None
_ring_max_events: int = 500


def init_event_system(
    *,
    store: SessionEventStore,
    active_tasks: dict[str, dict],
    resume_subscribers: dict[str, list[asyncio.Queue]],
    anthropic_event_subscribers: dict[str, list[asyncio.Queue]],
    ring_max_events: int = 500,
):
    """Called by server.py at startup to wire module-level references."""
    global _store, _active_tasks, _resume_subscribers, _anthropic_event_subscribers, _ring_max_events
    _store = store
    _active_tasks = active_tasks
    _resume_subscribers = resume_subscribers
    _anthropic_event_subscribers = anthropic_event_subscribers
    _ring_max_events = ring_max_events


async def _emit_event(session_id: str, queue: asyncio.Queue | None, event: dict, run_id: str | None = None):
    now = time.time()
    task = _active_tasks.setdefault(
        session_id,
        {
            "status": "running",
            "started": now,
            "events": [],
            "next_seq": _store.peek_next_event_id(session_id),
            "last_event_at": now,
        },
    )
    task["last_event_at"] = now
    resolved_run_id = str(run_id or task.get("run_id") or "")

    try:
        _envelope, evt = await _store.append_event(
            session_id=session_id,
            run_id=resolved_run_id,
            event=event,
            now_ts=now,
        )
    except Exception as e:
        logger.warning(f"session_events append failed for {session_id}: {e}")
        # Fail open: keep stream alive even if persistence fails.
        fallback_seq = int(task.get("next_seq", 1) or 1)
        task["next_seq"] = fallback_seq + 1
        evt = dict(event or {})
        evt["seq"] = fallback_seq
        evt["event_id"] = fallback_seq
        evt["run_id"] = resolved_run_id
        evt["session_id"] = str(session_id)
        if "ts" not in evt:
            evt["ts"] = now
    else:
        evt_id = int(evt.get("event_id") or evt.get("seq") or 0)
        task["next_seq"] = max(int(task.get("next_seq", 1) or 1), evt_id + 1)
        task_events = task.setdefault("events", [])
        task_events.append(evt)
        if len(task_events) > _ring_max_events:
            del task_events[:len(task_events) - _ring_max_events]

        evt_type = str(evt.get("type") or "")
        if resolved_run_id and evt_type in {"done", "error"}:
            _db_mark_run_done(resolved_run_id, "done" if evt_type == "done" else "error")

    payload = f"data: {json.dumps(evt)}\n\n"
    if queue is not None:
        await queue.put(payload)
    for q in list(_resume_subscribers.get(session_id, [])):
        try:
            q.put_nowait(payload)
        except Exception:
            pass
    # Broadcast to persistent Anthropic EventSource subscribers
    for q in list(_anthropic_event_subscribers.get(session_id, [])):
        try:
            q.put_nowait(payload)
        except Exception:
            pass
