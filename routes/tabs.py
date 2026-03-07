"""
routes/tabs.py — Tabs, history, and session-state routes extracted from server.py.
"""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth import (
    db_connection,
    load_history,
    save_history,  # imported per extraction contract
    clear_history,
    get_request_user,
    is_localhost,  # imported per extraction contract
)
from security import clear_session_security
from server_helpers import model_key_from_session, resolve_profile, profile_limits, MODEL_PROFILES
from routes.session_events import _db_latest_run
from config import MODEL
from app_state import get_app_state

logger = logging.getLogger("kukuibot.tabs")

router = APIRouter()


@router.get("/api/history")
async def api_history(session_id: str = "default", limit: int = 200):
    items, _, _ = load_history(session_id)
    display = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "")
        if role == "user":
            content = item.get("content", "")
            if isinstance(content, str) and not content.startswith("[COMPACTION SUMMARY"):
                display.append({"role": "user", "content": content})
        elif role == "assistant":
            content = item.get("content", "")
            if isinstance(content, str) and content:
                display.append({"role": "assistant", "content": content})

    # Keep payload bounded for fast page reloads
    if limit > 0:
        display = display[-min(limit, 500):]

    return {"messages": display}


def _resolve_owner_username(request: Request) -> str:
    user = get_request_user(request) or {}
    owner = str(user.get("user") or user.get("username") or "").strip().lower()
    if owner and owner != "localhost":
        return owner
    try:
        with db_connection() as db:
            row = db.execute("SELECT username FROM users WHERE role = 'admin' LIMIT 1").fetchone()
            if row and row[0]:
                return str(row[0]).strip().lower()
    except Exception:
        pass
    return owner


def _ensure_tab_meta_schema(db):
    """Ensure tab_meta exists and supports label timestamp conflict resolution."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tab_meta (
            owner TEXT NOT NULL,
            session_id TEXT NOT NULL,
            tab_id TEXT,
            model_key TEXT,
            label TEXT,
            label_updated_at INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            updated_at INTEGER,
            PRIMARY KEY (owner, session_id)
        )
        """
    )
    try:
        cols = {str(r[1]) for r in db.execute("PRAGMA table_info(tab_meta)").fetchall()}
    except Exception:
        cols = set()
    if "label_updated_at" not in cols:
        try:
            db.execute("ALTER TABLE tab_meta ADD COLUMN label_updated_at INTEGER DEFAULT 0")
        except Exception:
            pass
    if "sort_order" not in cols:
        try:
            db.execute("ALTER TABLE tab_meta ADD COLUMN sort_order INTEGER DEFAULT 0")
        except Exception:
            pass
    if "worker_identity" not in cols:
        try:
            db.execute("ALTER TABLE tab_meta ADD COLUMN worker_identity TEXT DEFAULT ''")
        except Exception:
            pass

    if "created_explicitly" not in cols:
        try:
            db.execute("ALTER TABLE tab_meta ADD COLUMN created_explicitly INTEGER DEFAULT 0")
        except Exception:
            pass

    if "project_id" not in cols:
        try:
            db.execute("ALTER TABLE tab_meta ADD COLUMN project_id TEXT DEFAULT ''")
        except Exception:
            pass

    # Ensure projects table exists (project context registry)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL,
            description TEXT DEFAULT '',
            key_files TEXT DEFAULT '[]',
            context_budget INTEGER DEFAULT 8000,
            auto_scan INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active',
            created_at INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0
        )
        """
    )

    # Migration: add status column to projects if missing
    try:
        proj_cols = {r[1] for r in db.execute("PRAGMA table_info(projects)").fetchall()}
        if "status" not in proj_cols:
            db.execute("ALTER TABLE projects ADD COLUMN status TEXT DEFAULT 'active'")
    except Exception:
        pass

    # Ensure tab_tombstones table exists (tracks deleted tabs for cross-device sync)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tab_tombstones (
            owner TEXT NOT NULL,
            session_id TEXT NOT NULL,
            deleted_at INTEGER DEFAULT 0,
            PRIMARY KEY (owner, session_id)
        )
        """
    )

    # Add index for fast lookups on (owner, session_id)
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_tab_meta_owner_session ON tab_meta(owner, session_id)")
    except Exception:
        pass

    # Migrate 'localhost' owner rows to the real admin username so tabs are
    # consistent regardless of whether the user accesses via localhost or remote login.
    try:
        localhost_count = db.execute("SELECT COUNT(*) FROM tab_meta WHERE owner = 'localhost'").fetchone()
        if localhost_count and localhost_count[0] > 0:
            admin_row = db.execute("SELECT username FROM users WHERE role = 'admin' LIMIT 1").fetchone()
            if admin_row and admin_row[0] and str(admin_row[0]).strip().lower() != "localhost":
                admin_user = str(admin_row[0]).strip().lower()
                # Delete localhost rows that conflict with existing admin rows (same session_id)
                db.execute(
                    "DELETE FROM tab_meta WHERE owner = 'localhost' AND session_id IN "
                    "(SELECT session_id FROM tab_meta WHERE owner = ?)",
                    (admin_user,),
                )
                # Reassign remaining localhost rows to the admin user
                db.execute(
                    "UPDATE tab_meta SET owner = ? WHERE owner = 'localhost'",
                    (admin_user,),
                )
                db.commit()
    except Exception:
        pass


@router.get("/api/history/sessions")
async def api_history_sessions(request: Request, limit: int = 20):
    """List recent sessions with persisted history plus saved tab labels for this user."""
    try:
        cap = max(1, min(limit, 100))
        owner = _resolve_owner_username(request)

        with db_connection() as db:
            _ensure_tab_meta_schema(db)

            history_rows = db.execute(
                "SELECT session_id, items, updated_at FROM history ORDER BY updated_at DESC LIMIT ?",
                (cap,),
            ).fetchall()

            meta_rows = []
            if owner:
                meta_rows = db.execute(
                    "SELECT session_id, tab_id, model_key, label, label_updated_at, updated_at, COALESCE(sort_order, 0), COALESCE(worker_identity, ''), COALESCE(created_explicitly, 0), COALESCE(project_id, '') FROM tab_meta WHERE owner = ?",
                    (owner,),
                ).fetchall()

        sessions_map: dict[str, dict] = {}

        for session_id, items_json, updated_at in history_rows:
            msg_count = 0
            if items_json:
                try:
                    items = json.loads(items_json)
                    for item in items:
                        if isinstance(item, dict) and item.get("role") in {"user", "assistant"}:
                            msg_count += 1
                except Exception:
                    msg_count = 0

            sid = str(session_id)
            sessions_map[sid] = {
                "session_id": sid,
                "updated_at": int(updated_at or 0),
                "message_count": msg_count,
                "label": "",
                "tab_id": "",
                "model_key": "",
                "meta_updated_at": 0,
                "label_updated_at": 0,
                "sort_order": 0,
            }

        for row in meta_rows:
            sid = str(row[0])
            tab_id = row[1]
            model_key = row[2]
            label = row[3]
            label_updated_at = row[4]
            meta_updated = row[5]
            sort_order_val = int(row[6]) if len(row) > 6 else 0
            worker_identity_val = str(row[7]) if len(row) > 7 else ""
            created_explicitly_val = bool(int(row[8])) if len(row) > 8 else False
            project_id_val = str(row[9]) if len(row) > 9 else ""
            key = sid
            entry = sessions_map.get(key)
            if not entry:
                entry = {
                    "session_id": key,
                    "updated_at": int(meta_updated or 0),
                    "message_count": 0,
                    "label": "",
                    "tab_id": "",
                    "model_key": "",
                    "meta_updated_at": 0,
                    "label_updated_at": 0,
                    "sort_order": 0,
                    "worker_identity": "",
                    "created_explicitly": False,
                    "project_id": "",
                }
                sessions_map[key] = entry

            entry["label"] = str(label or "").strip()
            entry["tab_id"] = str(tab_id or "").strip()
            entry["model_key"] = str(model_key or "").strip()
            entry["label_updated_at"] = int(label_updated_at or 0)
            entry["meta_updated_at"] = int(meta_updated or 0)
            entry["sort_order"] = sort_order_val
            entry["worker_identity"] = worker_identity_val
            entry["created_explicitly"] = created_explicitly_val
            entry["project_id"] = project_id_val
            entry["updated_at"] = max(int(entry.get("updated_at", 0)), int(meta_updated or 0))

        sessions = sorted(sessions_map.values(), key=lambda x: int(x.get("updated_at", 0)), reverse=True)[:cap]

        # Fetch tombstones so clients can remove deleted tabs from localStorage
        deleted_sids = []
        try:
            with db_connection() as db2:
                _ensure_tab_meta_schema(db2)
                if owner:
                    tomb_rows = db2.execute(
                        "SELECT session_id FROM tab_tombstones WHERE owner = ?", (owner,)
                    ).fetchall()
                    deleted_sids = [str(r[0]) for r in tomb_rows]
        except Exception:
            pass

        # Filter out tombstoned sessions from results
        if deleted_sids:
            deleted_set = set(deleted_sids)
            sessions = [s for s in sessions if s["session_id"] not in deleted_set]

        return {"sessions": sessions, "deleted": deleted_sids}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/tabs/sync")
async def api_tabs_sync(req: Request):
    """Persist per-user tab labels/session mapping for cross-device consistency."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    tabs = body.get("tabs") or []
    if not isinstance(tabs, list):
        return JSONResponse({"error": "tabs must be a list"}, status_code=400)

    owner = _resolve_owner_username(req)
    if not owner:
        return JSONResponse({"error": "Unable to resolve user"}, status_code=400)

    now = int(time.time())
    saved = 0
    try:
        # Lazy import to avoid circular dependency at module load.
        from server import _load_max_policy, _max_counts_for_owner

        with db_connection() as db:
            _ensure_tab_meta_schema(db)

            rejected_session_ids: list[str] = []

            # Phase C enforcement helpers
            limits = _load_max_policy()
            # Seed counts from persisted view
            _counts0 = _max_counts_for_owner(owner)
            active_total = int(_counts0.get("active_total", 0) or 0)
            active_codex = int(_counts0.get("active_codex", 0) or 0)
            active_spark = int(_counts0.get("active_spark", 0) or 0)

            # Snapshot existing persisted sessions for this owner so we can detect *new* ones.
            existing_session_ids = set(
                str(r[0]) for r in db.execute(
                    "SELECT session_id FROM tab_meta WHERE owner = ?",
                    (owner,),
                ).fetchall() if r and r[0]
            )

            for row in tabs[:200]:
                if not isinstance(row, dict):
                    continue
                session_id = str(row.get("session_id") or "").strip()
                if not session_id:
                    continue

                tab_id = str(row.get("id") or "").strip()
                model_key = str(row.get("model_key") or "").strip().lower()
                label = str(row.get("label") or "").strip()
                try:
                    label_updated_at = int(row.get("label_updated_at") or 0)
                except Exception:
                    label_updated_at = 0
                if label_updated_at < 0:
                    label_updated_at = 0
                try:
                    sort_order = int(row.get("sort_order") or 0)
                except Exception:
                    sort_order = 0
                worker_identity = str(row.get("worker_identity") or "").strip()
                try:
                    created_explicitly = 1 if row.get("created_explicitly") else 0
                except Exception:
                    created_explicitly = 0

                # Skip if this session was tombstoned (deleted on another device)
                tomb = db.execute(
                    "SELECT deleted_at FROM tab_tombstones WHERE owner = ? AND session_id = ?",
                    (owner, session_id),
                ).fetchone()
                if tomb:
                    continue  # Don't re-create deleted tabs

                # Phase C: enforce max-session limits on *new* sessions.
                # Existing sessions should always sync.
                # Note: we must account for multiple new sessions in a single sync request,
                # so we use locally tracked counts (seeded before the loop) rather than
                # a fresh DB query per-row (which won't see uncommitted inserts).
                exists = (session_id in existing_session_ids)
                if not exists and model_key in {"codex", "spark"}:
                    lt = int(limits.get("max_total_sessions", 0) or 0)
                    lc = int(limits.get("max_codex_sessions", 0) or 0)
                    ls = int(limits.get("max_spark_sessions", 0) or 0)

                    # Total cap
                    if lt == 0 or active_total >= lt:
                        rejected_session_ids.append(session_id)
                        continue

                    # Per-model cap
                    if model_key == "codex" and (lc == 0 or active_codex >= lc):
                        rejected_session_ids.append(session_id)
                        continue
                    if model_key == "spark" and (ls == 0 or active_spark >= ls):
                        rejected_session_ids.append(session_id)
                        continue

                # Guard against phantom default-tab proliferation from cache-miss boots.
                # If a brand-new tab arrives as default "Codex 1"/"Spark 1" with no history,
                # keep at most one empty row per owner+model+label.
                if model_key in {"codex", "spark"} and label == f"{model_key.title()} 1":
                    has_history = db.execute(
                        "SELECT 1 FROM history WHERE session_id = ? LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    if not has_history:
                        dup_rows = db.execute(
                            "SELECT session_id FROM tab_meta WHERE owner = ? AND model_key = ? AND label = ? AND session_id != ?",
                            (owner, model_key, label, session_id),
                        ).fetchall()
                        keep_existing = False
                        for dr in dup_rows:
                            existing_sid = str(dr[0] or "").strip()
                            if not existing_sid:
                                continue
                            existing_has_history = db.execute(
                                "SELECT 1 FROM history WHERE session_id = ? LIMIT 1",
                                (existing_sid,),
                            ).fetchone()
                            if not existing_has_history:
                                keep_existing = True
                                break
                        if keep_existing:
                            continue

                db.execute(
                    """
                    INSERT INTO tab_meta (owner, session_id, tab_id, model_key, label, label_updated_at, sort_order, worker_identity, created_explicitly, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner, session_id) DO UPDATE SET
                        tab_id = excluded.tab_id,
                        model_key = excluded.model_key,
                        label = CASE
                            WHEN excluded.label_updated_at >= COALESCE(tab_meta.label_updated_at, 0)
                                THEN excluded.label
                            ELSE tab_meta.label
                        END,
                        label_updated_at = CASE
                            WHEN excluded.label_updated_at >= COALESCE(tab_meta.label_updated_at, 0)
                                THEN excluded.label_updated_at
                            ELSE COALESCE(tab_meta.label_updated_at, 0)
                        END,
                        sort_order = excluded.sort_order,
                        worker_identity = excluded.worker_identity,
                        created_explicitly = MAX(COALESCE(tab_meta.created_explicitly, 0), excluded.created_explicitly),
                        updated_at = excluded.updated_at
                    """,
                    (owner, session_id, tab_id, model_key, label, label_updated_at, sort_order, worker_identity, created_explicitly, now),
                )
                saved += 1

                # If this was a brand-new session, update local enforcement counters
                # so multiple new sessions in a single sync request can't exceed limits.
                if not exists and model_key in {"codex", "spark"}:
                    existing_session_ids.add(session_id)
                    active_total += 1
                    if model_key == "codex":
                        active_codex += 1
                    elif model_key == "spark":
                        active_spark += 1

            # Garbage-collect orphaned tab_meta entries: no worker_identity, no history,
            # older than 24h. Prevents phantom "Other" tabs on fresh browser loads.
            gc_cutoff = now - 86400
            db.execute(
                """
                DELETE FROM tab_meta
                WHERE owner = ? AND (worker_identity IS NULL OR worker_identity = '')
                  AND updated_at < ?
                  AND session_id NOT IN (SELECT session_id FROM history)
                """,
                (owner, gc_cutoff),
            )

            # Clean up old tombstones (>7 days)
            cutoff = now - (7 * 86400)
            db.execute("DELETE FROM tab_tombstones WHERE deleted_at < ? AND deleted_at > 0", (cutoff,))

            db.commit()

        # Broadcast tab sync to all other browsers for cross-device consistency
        if saved > 0:
            try:
                from server import _broadcast_global_event
                _broadcast_global_event({"type": "tabs_updated", "saved": saved, "ts": int(time.time() * 1000)})
            except Exception:
                pass
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return {"ok": True, "saved": saved, "rejected_session_ids": rejected_session_ids}


async def _cleanup_tab_session(session_id: str, owner: str = "", tab_id: str = "", state=None):
    sid = str(session_id or "").strip()
    if not sid:
        return

    # Use AppState if provided, fall back to lazy import for background-task callers
    if state is None:
        from server import _app_state as state

    # 1) Conversation/session state
    try:
        clear_history(sid)
    except Exception:
        pass
    state.last_api_usage.pop(sid, None)

    # 2) Runtime stream/task state
    state.active_tasks.pop(sid, None)
    state.active_docs.pop(sid, None)
    subs = state.resume_subscribers.pop(sid, [])
    for q in subs:
        try:
            q.put_nowait(None)
        except Exception:
            pass

    # 3) Security/session approvals
    try:
        clear_session_security(sid)
    except Exception:
        pass

    # 4) Persisted tab metadata + tombstone
    try:
        with db_connection() as db:
            _ensure_tab_meta_schema(db)
            now = int(time.time())
            if owner:
                db.execute("DELETE FROM tab_meta WHERE owner = ? AND session_id = ?", (owner, sid))
                if tab_id:
                    db.execute("DELETE FROM tab_meta WHERE owner = ? AND tab_id = ?", (owner, str(tab_id).strip()))
                # Write tombstone so other devices know this tab was deleted
                db.execute(
                    "INSERT OR REPLACE INTO tab_tombstones (owner, session_id, deleted_at) VALUES (?, ?, ?)",
                    (owner, sid, now),
                )
            else:
                db.execute("DELETE FROM tab_meta WHERE session_id = ?", (sid,))
            db.commit()
    except Exception:
        pass


@router.post("/api/tab-delete")
async def api_tab_delete(req: Request):
    """Delete tab/session data in background (history, metadata, runtime + security state)."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    session_id = str(body.get("session_id") or "").strip()
    tab_id = str(body.get("tab_id") or body.get("id") or "").strip()
    if not session_id:
        return JSONResponse({"error": "session_id required"}, status_code=400)

    owner = _resolve_owner_username(req)
    asyncio.create_task(_cleanup_tab_session(session_id, owner=owner, tab_id=tab_id))
    return {"ok": True, "queued": True}


@router.post("/api/reset")
async def api_reset(req: Request):
    body = await req.json()
    session_id = body.get("session_id", "default")
    clear_history(session_id)

    state = get_app_state(req)
    state.last_api_usage.pop(session_id, None)
    return {"ok": True}


def _resolve_session_model_label(session_id: str) -> str:
    sid = str(session_id or "default")
    profile = resolve_profile(sid)
    if profile == "openrouter":
        from server import _openrouter_model

        return f"openrouter ({_openrouter_model(sid)})"
    if profile in {"anthropic", "anthropic_sonnet45", "anthropic_haiku", "anthropic_opus"}:
        from server import _anthropic_model

        return f"anthropic ({_anthropic_model(sid)})"
    if profile in {"claude", "claude_sonnet"}:
        from server_helpers import claude_model_for_session

        return f"claude-code ({claude_model_for_session(sid)})"
    return str(MODEL_PROFILES.get(profile, MODEL_PROFILES["codex"]).get("ui_model") or MODEL)


def _session_status_from_task(task: dict | None) -> str:
    if not task:
        return "idle"
    raw = str(task.get("status") or "idle").strip().lower()
    if raw in {"running", "thinking", "idle"}:
        return raw
    if raw in {"done", "cancelled", "error"}:
        return "idle"
    return "running" if raw else "idle"


def _extract_last_assistant_message(session_id: str) -> str:
    try:
        items, _, _ = load_history(session_id)
    except Exception:
        return ""
    for item in reversed(items or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") != "assistant":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return ""


def _token_usage_for_session(session_id: str, state=None) -> dict:
    if state is None:
        from server import _app_state as state
    usage = state.last_api_usage.get(session_id) or {}
    if isinstance(usage, dict) and usage:
        return usage
    latest = _db_latest_run(session_id)
    if latest:
        raw = str(latest.get("usage_json") or "").strip()
        if raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    return decoded
            except Exception:
                pass
    return {}


@router.get("/api/session/state")
async def api_session_state(request: Request, session_id: str = "", max_messages: int = 50):
    sid = str(session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)

    state = get_app_state(request)
    from server_helpers import worker_identity_for_session as _worker_identity_for_session

    task = state.active_tasks.get(sid)
    run_id = ""
    if task:
        run_id = str(task.get("run_id") or "").strip()
    if not run_id:
        latest_run = _db_latest_run(sid)
        if latest_run:
            run_id = str(latest_run.get("run_id") or "").strip()

    # Fetch last N messages from session history for quick UI restore
    msg_cap = max(1, min(int(max_messages or 50), 200))
    messages: list[dict] = []
    try:
        items, _, _ = load_history(sid)
        if items:
            # Return only the tail — enough to render the chat on reconnect
            tail = items[-msg_cap:] if len(items) > msg_cap else items
            for item in tail:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "")
                if role not in ("user", "assistant", "system"):
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    messages.append({"role": role, "content": content})
                elif isinstance(content, list):
                    # Multi-part content — extract text parts only
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(str(part.get("text") or ""))
                        elif isinstance(part, str):
                            text_parts.append(part)
                    if text_parts:
                        messages.append({"role": role, "content": "\n".join(text_parts)})
    except Exception:
        pass

    # Fetch tab metadata (worker, label, project_id) from tab_meta table
    worker = ""
    tab_label = ""
    project_id = ""
    try:
        with db_connection() as db:
            _ensure_tab_meta_schema(db)
            row = db.execute(
                "SELECT label, COALESCE(worker_identity, ''), COALESCE(project_id, '') FROM tab_meta WHERE session_id = ?",
                (sid,),
            ).fetchone()
            if row:
                tab_label = str(row[0] or "")
                worker = str(row[1] or "")
                project_id = str(row[2] or "")
    except Exception:
        pass
    # Fallback: derive worker from session_id for delegated sessions
    if not worker:
        worker = _worker_identity_for_session(sid)

    return {
        "ok": True,
        "session_id": sid,
        "model": _resolve_session_model_label(sid),
        "status": _session_status_from_task(task),
        "last_event_id": int(state.session_event_store.latest_event_id(sid)),
        "last_message": _extract_last_assistant_message(sid),
        "run_id": run_id,
        "token_usage": _token_usage_for_session(sid, state=state),
        "messages": messages,
        "worker": worker,
        "tab_label": tab_label,
        "project_id": project_id,
    }


__all__ = [
    "router",
    "_cleanup_tab_session",
    "_resolve_owner_username",
    "_ensure_tab_meta_schema",
]
