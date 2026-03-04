"""
delegation.py — Cross-agent task delegation for coordinator workers (Dev Manager, etc.).

Provides delegate_task, check_task, list_tasks for dispatching work to other
KukuiBot worker sessions via the local API. All delegation activity is logged
to both the delegator's and delegate's worker chat logs for full visibility.
"""

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime

import urllib3
import requests as http_requests

# Suppress SSL warnings for loopback calls — server is HTTPS-only, even on localhost
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from auth import clear_history, db_connection
except ImportError:
    def clear_history(session_id: str):
        """Fallback: clear_history not available."""
        pass
    db_connection = None  # type: ignore

from config import DB_PATH, DELEGATION_MAX_SLOTS, MAX_CLAUDE_PROCESSES
from log_store import log_write, log_query

logger = logging.getLogger("kukuibot.delegation")

# ---------------------------------------------------------------------------
# DB — delegated_tasks table
# ---------------------------------------------------------------------------

_deleg_schema_initialized = False


def _ensure_deleg_schema(db: sqlite3.Connection):
    """Create delegated_tasks table + migrations. Idempotent."""
    global _deleg_schema_initialized
    if _deleg_schema_initialized:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS delegated_tasks (
        task_id TEXT PRIMARY KEY,
        parent_session_id TEXT NOT NULL,
        target_session_id TEXT NOT NULL,
        target_base_session_id TEXT NOT NULL DEFAULT '',
        target_worker TEXT NOT NULL,
        target_model TEXT NOT NULL,
        prompt TEXT NOT NULL,
        dispatched_prompt TEXT NOT NULL DEFAULT '',
        task_token TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        result_summary TEXT DEFAULT '',
        result_full TEXT DEFAULT '',
        correlation_status TEXT DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        completed_at INTEGER DEFAULT 0
    )""")
    # Lightweight migration for existing installs
    try:
        cols = {str(r[1]) for r in db.execute("PRAGMA table_info(delegated_tasks)").fetchall()}
    except Exception:
        cols = set()
    for col, ddl in (
        ("target_base_session_id", "ALTER TABLE delegated_tasks ADD COLUMN target_base_session_id TEXT NOT NULL DEFAULT ''"),
        ("dispatched_prompt", "ALTER TABLE delegated_tasks ADD COLUMN dispatched_prompt TEXT NOT NULL DEFAULT ''"),
        ("task_token", "ALTER TABLE delegated_tasks ADD COLUMN task_token TEXT NOT NULL DEFAULT ''"),
        ("result_full", "ALTER TABLE delegated_tasks ADD COLUMN result_full TEXT DEFAULT ''"),
        ("correlation_status", "ALTER TABLE delegated_tasks ADD COLUMN correlation_status TEXT DEFAULT ''"),
    ):
        if col not in cols:
            try:
                db.execute(ddl)
            except Exception:
                pass
    db.execute("""CREATE INDEX IF NOT EXISTS idx_delegated_tasks_parent_status
        ON delegated_tasks(parent_session_id, status)""")
    db.commit()
    _deleg_schema_initialized = True


def _deleg_db_connection():
    """Return a context manager for DB access.

    Uses auth.db_connection() (unified pragmas + schema) when available,
    otherwise falls back to a local connection with the same pragma set.
    """
    if db_connection is not None:
        return db_connection()
    # Fallback: local context manager with proper pragmas
    from contextlib import contextmanager

    @contextmanager
    def _local_db():
        db = sqlite3.connect(str(DB_PATH), timeout=5.0)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA wal_autocheckpoint=500")
        try:
            yield db
        finally:
            db.close()
    return _local_db()


def _get_db() -> sqlite3.Connection:
    """Open a DB connection with delegation schema init. Caller MUST close.

    Deprecated: prefer ``with _deleg_db_connection() as db:`` for automatic close.
    """
    global _deleg_schema_initialized
    db = sqlite3.connect(str(DB_PATH), timeout=5.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA wal_autocheckpoint=500")
    _ensure_deleg_schema(db)
    return db


def _save_task(task: dict):
    with _deleg_db_connection() as db:
        _ensure_deleg_schema(db)
        db.execute(
            """INSERT OR REPLACE INTO delegated_tasks
               (task_id, parent_session_id, target_session_id, target_base_session_id, target_worker, target_model,
                prompt, dispatched_prompt, task_token, status, result_summary, result_full, correlation_status,
                created_at, updated_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task["task_id"], task["parent_session_id"], task["target_session_id"], task.get("target_base_session_id", ""),
                task["target_worker"], task["target_model"], task["prompt"],
                task.get("dispatched_prompt", ""), task.get("task_token", ""),
                task["status"], task.get("result_summary", ""), task.get("result_full", ""), task.get("correlation_status", ""),
                task["created_at"], task["updated_at"], task.get("completed_at", 0),
            ),
        )
        db.commit()


def _load_task(task_id: str) -> dict | None:
    with _deleg_db_connection() as db:
        _ensure_deleg_schema(db)
        row = db.execute(
            "SELECT task_id, parent_session_id, target_session_id, target_base_session_id, target_worker, target_model, prompt, dispatched_prompt, task_token, status, result_summary, result_full, correlation_status, created_at, updated_at, completed_at FROM delegated_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if not row:
        return None
    return dict(zip(
        ["task_id", "parent_session_id", "target_session_id", "target_base_session_id", "target_worker", "target_model",
         "prompt", "dispatched_prompt", "task_token", "status", "result_summary", "result_full", "correlation_status", "created_at", "updated_at", "completed_at"],
        row,
    ))


def _load_tasks_for_session(parent_session_id: str) -> list[dict]:
    with _deleg_db_connection() as db:
        _ensure_deleg_schema(db)
        rows = db.execute(
            "SELECT task_id, target_worker, target_model, status, prompt, result_summary, result_full, correlation_status, created_at, updated_at, completed_at FROM delegated_tasks WHERE parent_session_id = ? ORDER BY created_at DESC",
            (parent_session_id,),
        ).fetchall()
    return [
        dict(zip(
            ["task_id", "target_worker", "target_model", "status", "prompt",
             "result_summary", "result_full", "correlation_status", "created_at", "updated_at", "completed_at"],
            row,
        ))
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Cross-worker chat log writing
# ---------------------------------------------------------------------------

def _log_delegation(session_id: str, worker: str, model_key: str, role: str, message: str):
    """Write a delegation event to SQLite for visibility.

    Chat rendering APIs only surface roles user/assistant/system, so custom roles
    (e.g. "delegation") must be normalized to "system" for UI visibility.
    """
    try:
        role_norm = str(role or "").strip().lower()
        if role_norm not in {"user", "assistant", "system"}:
            role_norm = "system"
        log_write(
            "chat",
            message,
            role=role_norm,
            session_id=session_id,
            worker=worker,
            source=f"delegation.{model_key}" if model_key else "delegation",
        )
    except Exception as e:
        logger.warning(f"Failed to log delegation: {e}")


# ---------------------------------------------------------------------------
# Find target worker session
# ---------------------------------------------------------------------------

def _canonical_model_base(model_key: str) -> str:
    mk = str(model_key or "").strip().lower()
    if mk.startswith("openrouter"):
        return "openrouter"
    if mk.startswith("anthropic"):
        return "anthropic"
    if mk.startswith("claude_sonnet"):
        return "claude_sonnet"
    if mk.startswith("claude"):
        return "claude_opus"
    if mk.startswith("codex"):
        return "codex"
    if mk.startswith("spark"):
        return "spark"
    return mk


def _canonical_session_id(base_model: str, worker: str, slot: int = 1) -> str:
    bm = _canonical_model_base(base_model)
    wk = re.sub(r"[^a-z0-9_-]", "-", str(worker or "").strip().lower()) or "worker"
    return f"deleg-{bm}-{wk}-{int(slot)}"


def _find_available_slot(base_model: str, worker: str, max_slots: int | None = None) -> int | None:
    """Find the first slot (1..max_slots) with no in-flight task for this model+worker.

    Returns the slot number, or None if all slots are occupied.
    """
    cap = max(1, int(max_slots or DELEGATION_MAX_SLOTS))
    for slot in range(1, cap + 1):
        sid = _canonical_session_id(base_model, worker, slot)
        if _find_inflight_task_for_session(sid) is None:
            return slot
    return None


def _find_inflight_task_for_session(session_id: str) -> dict | None:
    """Check if there's an in-flight (running/dispatched) task for a delegated session."""
    with _deleg_db_connection() as db:
        _ensure_deleg_schema(db)
        row = db.execute(
            "SELECT task_id, status, created_at, task_token FROM delegated_tasks "
            "WHERE target_session_id = ? AND status IN ('running', 'dispatched') "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    return {"task_id": row[0], "status": row[1], "created_at": row[2], "task_token": row[3]}


def _find_worker_session(worker: str, model_key: str = "") -> dict | None:
    """Find an existing tab session for a given worker identity + optional model."""
    with _deleg_db_connection() as db:
        try:
            # Ensure tab_meta exists
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "tab_meta" not in tables:
                return None

            # Check if worker_identity column exists
            cols = {str(r[1]) for r in db.execute("PRAGMA table_info(tab_meta)").fetchall()}
            if "worker_identity" not in cols:
                return None

            w = (worker or "").strip().lower()
            if not w:
                return None

            if model_key:
                m = (model_key or "").strip().lower()
                row = db.execute(
                    "SELECT session_id, tab_id, model_key, label, worker_identity FROM tab_meta WHERE LOWER(worker_identity) = ? AND LOWER(model_key) = ? ORDER BY updated_at DESC LIMIT 1",
                    (w, m),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT session_id, tab_id, model_key, label, worker_identity FROM tab_meta WHERE LOWER(worker_identity) = ? ORDER BY updated_at DESC LIMIT 1",
                    (w,),
                ).fetchone()

            if not row:
                return None
            return {
                "session_id": row[0],
                "tab_id": row[1],
                "model_key": row[2],
                "label": row[3],
                "worker_identity": row[4],
            }
        except Exception as e:
            logger.error(f"Error finding worker session: {e}")
            return None


# ---------------------------------------------------------------------------
# Dispatch — two-phase: fast POST + async delivery verification
# ---------------------------------------------------------------------------

def _dispatch_message(session_id: str, message: str, task_token: str = "", task_id: str = "") -> dict:
    """Fire-and-forget POST to /api/chat. Returns as soon as the connection is accepted.

    Phase 1 (synchronous, <5s): POST with stream=True, wait only for HTTP status.
    Phase 2 (deferred): The delegation monitor verifies delivery on its next poll
    by checking the chat log for the task token.

    Returns {"ok": True} if the POST was accepted (HTTP 200) or if connection is
    still pending (optimistic — we mark as 'dispatched' and verify later).
    """
    result = {"ok": False, "error": ""}

    def _send():
        try:
            # Post directly to the local server
            from config import PORT
            resp = http_requests.post(
                f"https://localhost:{PORT}/api/chat",
                json={"session_id": session_id, "message": message},
                verify=False,
                timeout=(10, 120),  # generous read timeout — we close immediately anyway
                stream=True,
            )
            result["ok"] = resp.status_code == 200
            if not result["ok"]:
                result["error"] = f"HTTP {resp.status_code}"
            resp.close()
        except Exception as e:
            result["error"] = str(e)

        if not result["ok"] and task_id:
            import time
            time.sleep(1)  # allow main thread to save 'dispatched' state first
            try:
                task = _load_task(task_id)
                if task and task["status"] == "dispatched":
                    task["status"] = "dispatch_failed"
                    task["result_summary"] = f"Async dispatch failed: {result['error']}"
                    _save_task(task)
                    logger.warning(f"Async dispatch failed for {task_id}: {result['error']}")
            except Exception as e:
                logger.error(f"Failed to async update dispatch failure for {task_id}: {e}")

    t = threading.Thread(target=_send, daemon=True)
    t.start()
    # Wait up to 10s for the HTTP response — enough for connection + routing,
    # but not long enough to block the caller waiting for model startup.
    t.join(timeout=10)

    if not result["ok"] and not result["error"]:
        # Thread still running — POST was accepted by the TCP stack but
        # the server hasn't returned headers yet. This is normal for Claude CLI
        # which takes 30-40s to spawn. Mark as optimistic success.
        result["ok"] = True
        result["optimistic"] = True
        logger.info(f"Dispatch optimistic accept for {session_id} (HTTP still pending)")

    return result


def _verify_delivery_via_log(session_id: str, task_token: str, max_wait: int = 5) -> bool:
    """Check if a task token appears in the SQLite log, confirming delivery.

    Used by the delegation monitor to promote 'dispatched' → 'running'.
    """
    for _ in range(max_wait):
        try:
            rows = log_query(category="chat", search=task_token, limit=1)
            if rows:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _decorate_prompt(task_id: str, prompt: str) -> tuple[str, str]:
    token = f"TASK_TOKEN:{task_id}"
    tagged = (
        f"[TASK_ID:{task_id}]\n"
        f"[TASK_TOKEN:{token}]\n"
        f"IMPORTANT: Do NOT restart the KukuiBot server. Do NOT call /api/restart, os._exit(), sys.exit(), or any restart/kill command. "
        f"The Dev Manager will coordinate server restarts after your task is complete.\n\n"
        f"{prompt.rstrip()}\n\n"
        f"Return a final line exactly as: TASK_DONE {task_id}"
    )
    return tagged, token


def _response_has_task_marker(text: str, task_id: str, task_token: str) -> bool:
    t = str(text or "")
    return (f"TASK_DONE {task_id}" in t) or (f"[TASK_ID:{task_id}]" in t) or (task_token and task_token in t)


# Patterns that indicate a timeout or error response (not a real model answer).
_ERROR_RESPONSE_PATTERNS = [
    "No response from Claude process",
    "timed out or process died",
    "No response for ",            # "No response for 128s" / "No response for 300s"
    "Process killed",
    "⚠️ No response",
    "[ERROR] No OpenRouter API key configured",
    "[ERROR] OpenRouter returned empty response",
]


def _is_timeout_or_error_response(text: str) -> bool:
    """Return True if the response text looks like a timeout/error, not a real answer."""
    if not text:
        return False
    for pat in _ERROR_RESPONSE_PATTERNS:
        if pat in text:
            return True
    return False


def _extract_task_response(task: dict) -> tuple[str, bool]:
    """Find delegated response by marker in target history.

    Returns (response_text, correlated_bool).
    """
    try:
        from auth import load_history
        items, _, _ = load_history(task["target_session_id"])
        tid = str(task.get("task_id", "") or "")
        ttoken = str(task.get("task_token", "") or "")
        if not items:
            return "", False

        # Prefer first assistant after tagged user prompt in this task session.
        anchor = -1
        for i in range(len(items) - 1, -1, -1):
            it = items[i]
            if not isinstance(it, dict) or it.get("role") != "user":
                continue
            c = it.get("content", "")
            if isinstance(c, str) and (f"[TASK_ID:{tid}]" in c or (ttoken and ttoken in c)):
                anchor = i
                break

        if anchor >= 0:
            for j in range(anchor + 1, len(items)):
                it = items[j]
                if not isinstance(it, dict) or it.get("role") != "assistant":
                    continue
                c = it.get("content", "")
                if isinstance(c, str) and c.strip():
                    return c, _response_has_task_marker(c, tid, ttoken)
                if isinstance(c, list):
                    buf = []
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            txt = str(b.get("text", "") or "")
                            if txt:
                                buf.append(txt)
                    if buf:
                        joined = "\n".join(buf)
                        return joined, _response_has_task_marker(joined, tid, ttoken)

        # Final fallback: latest assistant (uncorrelated)
        for it in reversed(items):
            if not isinstance(it, dict) or it.get("role") != "assistant":
                continue
            c = it.get("content", "")
            if isinstance(c, str) and c.strip():
                return c, _response_has_task_marker(c, tid, ttoken)
            if isinstance(c, list):
                buf = []
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        txt = str(b.get("text", "") or "")
                        if txt:
                            buf.append(txt)
                if buf:
                    joined = "\n".join(buf)
                    return joined, _response_has_task_marker(joined, tid, ttoken)
    except Exception as e:
        logger.warning(f"extract task response failed: {e}")
    return "", False


# ---------------------------------------------------------------------------
# Public API — tool implementations
# ---------------------------------------------------------------------------

def delegate_task(
    parent_session_id: str,
    worker: str,
    prompt: str,
    model: str = "",
    force: bool = False,
) -> str:
    """Delegate a task to another worker. Returns task_id and dispatch status."""

    if not worker:
        return json.dumps({"ok": False, "error": "worker is required (e.g. 'developer', 'it-admin')"})
    if not prompt:
        return json.dumps({"ok": False, "error": "prompt is required"})

    # Find target session
    target = _find_worker_session(worker, model)
    if not target or not target.get("session_id"):
        available = _list_available_workers()
        return json.dumps({
            "ok": False,
            "error": f"No active session found for worker '{worker}'" + (f" with model '{model}'" if model else ""),
            "available_workers": available,
            "hint": "Open a tab for this worker in the UI first, or specify a different worker/model.",
        })

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    now = int(time.time())

    base_model = _canonical_model_base(target.get("model_key", model or ""))

    # --- Multi-slot selection ---
    # Try to find an available slot for this model+worker combo.
    # Auto-expire stale dispatched tasks (>10min, no delivery confirmation) first.
    max_slots = max(1, int(DELEGATION_MAX_SLOTS))
    selected_slot = None
    collision_info = None  # Track collision details for error reporting

    for slot in range(1, max_slots + 1):
        sid = _canonical_session_id(base_model, worker, slot)
        existing = _find_inflight_task_for_session(sid)

        if existing and existing.get("status") == "dispatched":
            # Auto-expire stale dispatched tasks older than 10 minutes
            elapsed = max(0, now - int(existing.get("created_at") or now))
            if elapsed > 600:
                stale_token = str(existing.get("task_token") or "")
                delivered = bool(stale_token) and _verify_delivery_via_log(
                    sid, stale_token, max_wait=5,
                )
                if not delivered:
                    try:
                        with _deleg_db_connection() as _expire_db:
                            _ensure_deleg_schema(_expire_db)
                            _expire_db.execute(
                                "UPDATE delegated_tasks SET status='failed', updated_at=?, completed_at=?, result_summary=? WHERE task_id=?",
                                (now, now,
                                 f"Auto-expired stale dispatched task ({elapsed}s with no delivery confirmation)",
                                 existing["task_id"]),
                            )
                            _expire_db.commit()
                    except Exception as e:
                        logger.warning(f"Failed to auto-expire stale task {existing['task_id']}: {e}")
                    else:
                        logger.info(
                            f"Collision guard: auto-expired stale task {existing['task_id']} (dispatched {elapsed}s ago, no delivery confirmation)"
                        )
                        existing = None

        if existing is None:
            selected_slot = slot
            break
        else:
            # Track the first collision for error reporting
            if collision_info is None:
                collision_info = {"session_id": sid, "task_id": existing["task_id"], "status": existing["status"]}

    # Force bypass: if all slots are occupied but force=True, use slot 1
    if selected_slot is None and force:
        selected_slot = 1
        forced_sid = _canonical_session_id(base_model, worker, selected_slot)
        logger.warning(
            "Collision guard bypassed with force for %s (all %d slots occupied)",
            forced_sid, max_slots,
        )

    if selected_slot is None:
        # All slots occupied, no force bypass
        ci = collision_info or {}
        return json.dumps({
            "ok": False,
            "error": f"All {max_slots} delegation slot(s) for {worker}/{base_model} are occupied. "
                     f"Example: {ci.get('session_id', '?')} has task {ci.get('task_id', '?')} (status={ci.get('status', '?')}). "
                     f"Wait for a task to complete or use force=true.",
            "existing_task_id": ci.get("task_id", ""),
            "existing_status": ci.get("status", ""),
            "slots_checked": max_slots,
        }, indent=2)

    isolated_session_id = _canonical_session_id(base_model, worker, selected_slot)
    dispatched_prompt, task_token = _decorate_prompt(task_id, prompt)

    task = {
        "task_id": task_id,
        "parent_session_id": parent_session_id,
        "target_session_id": isolated_session_id,
        "target_base_session_id": target["session_id"],
        "target_worker": worker,
        "target_model": target.get("model_key", model or ""),
        "prompt": prompt,
        "dispatched_prompt": dispatched_prompt,
        "task_token": task_token,
        "status": "dispatched",
        "result_summary": "",
        "result_full": "",
        "correlation_status": "pending",
        "created_at": now,
        "updated_at": now,
        "completed_at": 0,
    }

    # Log to delegator's worker log
    parent_worker = _worker_for_session(parent_session_id)
    parent_model = _model_for_session(parent_session_id)
    _log_delegation(
        parent_session_id, parent_worker, parent_model, "DELEGATION",
        f"[{task_id}] Delegating to {worker} ({target['model_key']}) via {isolated_session_id} (slot {selected_slot}): {prompt[:200]}",
    )

    # Log to target worker's log (base worker log for visibility)
    _log_delegation(
        target["session_id"], worker, target.get("model_key", ""), "DELEGATION",
        f"[{task_id}] Incoming task from {parent_worker or 'coordinator'} on isolated session {isolated_session_id} (slot {selected_slot}): {prompt[:200]}",
    )

    # Clear stale history from previous tasks on this delegated session
    try:
        clear_history(isolated_session_id)
    except Exception:
        pass

    # For OpenRouter sessions, copy the model config from the base tab session
    # so _openrouter_model() resolves the correct model (not the fallback default)
    if _canonical_model_base(target.get("model_key", model or "")) == "openrouter":
        try:
            from auth import get_config, set_config
            base_sid = target["session_id"]
            or_model = (get_config(f"openrouter.session_model.{base_sid}", "") or "").strip()
            if or_model:
                set_config(f"openrouter.session_model.{isolated_session_id}", or_model)
                logger.info(f"Copied OpenRouter model config '{or_model}' from {base_sid} to {isolated_session_id}")
        except Exception as e:
            logger.warning(f"Failed to copy OpenRouter model config for {isolated_session_id}: {e}")

    # Pre-check: if targeting a Claude session, verify pool has capacity
    if _canonical_model_base(target.get("model_key", model or "")).startswith("claude"):
        try:
            from claude_bridge import get_claude_pool
            pool = get_claude_pool()
            if pool:
                pool_status = pool.get_all_status()
                pool_size = pool_status.get("pool_size", 0)
                max_size = pool_status.get("max_size", MAX_CLAUDE_PROCESSES)
                if pool_size >= max_size:
                    # Check if any process can be evicted (not actively busy)
                    can_evict = False
                    for _sid, _pinfo in pool_status.get("processes", {}).items():
                        if isinstance(_pinfo, dict) and not _pinfo.get("busy", True):
                            can_evict = True
                            break
                    if not can_evict:
                        task["status"] = "dispatch_failed"
                        task["result_summary"] = f"Claude process pool is full ({pool_size}/{max_size}). All processes are busy."
                        _save_task(task)
                        logger.warning(f"Delegation {task_id}: pool full ({pool_size}/{max_size}), no idle processes to evict")
                        return json.dumps({
                            "ok": False,
                            "task_id": task_id,
                            "status": "dispatch_failed",
                            "error": task["result_summary"],
                            "pool_size": pool_size,
                            "max_size": max_size,
                        }, indent=2)
        except Exception as e:
            logger.debug(f"Pool capacity pre-check skipped: {e}")

    # Dispatch the tagged message to isolated delegated session
    dispatch_result = _dispatch_message(isolated_session_id, dispatched_prompt, task_token=task_token, task_id=task_id)

    if dispatch_result.get("error"):
        # Hard failure: connection refused, HTTP error, etc.
        task["status"] = "dispatch_failed"
        task["result_summary"] = dispatch_result["error"]
    else:
        # POST accepted (or optimistic) — mark as dispatched, monitor will verify
        task["status"] = "dispatched"

    _save_task(task)

    return json.dumps({
        "ok": not bool(dispatch_result.get("error")),
        "task_id": task_id,
        "target_worker": worker,
        "target_model": target.get("model_key", ""),
        "target_session_id": isolated_session_id,
        "target_base_session_id": target["session_id"],
        "target_label": target.get("label", ""),
        "slot": selected_slot,
        "status": task["status"],
        "task_token": task_token,
        "force": bool(force),
        "error": dispatch_result.get("error", ""),
    }, indent=2)


def check_task(task_id: str, parent_session_id: str = "") -> str:
    """Check delegated task status with marker-based correlation."""

    task = _load_task(task_id)
    if not task:
        return json.dumps({"ok": False, "error": f"Task '{task_id}' not found."})

    latest_response = ""
    correlated = False
    still_running = False

    try:
        from auth import load_history
        items, _, _usage = load_history(task["target_session_id"])
        latest_response, correlated = _extract_task_response(task)

        if items:
            last_item = items[-1]
            if isinstance(last_item, dict):
                role = last_item.get("role", "")
                if role == "tool" or (role == "assistant" and last_item.get("tool_calls")):
                    still_running = True
                content = last_item.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            still_running = True

        # Self-heal dispatch_failed/dispatched: if the message actually landed, promote to running
        if task["status"] in ("dispatch_failed", "dispatched"):
            if items or latest_response:
                logger.info(f"Self-healing task {task_id}: {task['status']} → running (message was delivered)")
                task["status"] = "running"
            elif task.get("task_token"):
                if _verify_delivery_via_log(
                    task.get("target_session_id", ""), task["task_token"], max_wait=5,
                ):
                    logger.info(f"Self-healing task {task_id}: {task['status']} → running (found in chat log)")
                    task["status"] = "running"

        if task["status"] in ("running", "dispatched"):
            if latest_response and not still_running:
                # Check if the response is a timeout/error (no TASK_DONE marker
                # and the response text matches known error patterns).
                # This catches runs that were killed by the silence watchdog.
                is_error_response = (
                    not correlated
                    and _is_timeout_or_error_response(latest_response)
                )
                if is_error_response:
                    task["status"] = "failed"
                    task["result_summary"] = f"[TIMEOUT] {latest_response[:10000]}"
                    task["result_full"] = latest_response
                    task["completed_at"] = int(time.time())
                    task["correlation_status"] = "error_detected"
                    logger.info(f"Task {task['task_id']}: detected timeout/error response, marking as failed")
                else:
                    task["status"] = "completed"
                    task["result_summary"] = latest_response[:10000]
                    task["result_full"] = latest_response
                    task["completed_at"] = int(time.time())
                    task["correlation_status"] = "matched" if correlated else "best_effort"

                # NOTE: Session history cleanup is deferred — the delegation monitor
                # clears it after the parent session has been notified.  Clearing here
                # would race with the monitor and wipe results before they're delivered.
            elif still_running:
                task["status"] = "running"
                task["correlation_status"] = "pending"

        task["updated_at"] = int(time.time())
        _save_task(task)

    except Exception as e:
        logger.warning(f"Error checking task history: {e}")

    elapsed = int(task.get("completed_at") or int(time.time())) - int(task["created_at"])

    return json.dumps({
        "ok": True,
        "task_id": task["task_id"],
        "status": task["status"],
        "target_worker": task["target_worker"],
        "target_model": task["target_model"],
        "elapsed_seconds": elapsed,
        "prompt_preview": task["prompt"][:200],
        "latest_response": latest_response if latest_response else "(no response yet)",
        "result_full": task.get("result_full", ""),
        "result_summary": task.get("result_summary", ""),
        "correlation_status": task.get("correlation_status", ""),
        "task_token": task.get("task_token", ""),
    }, indent=2)


def list_tasks(parent_session_id: str) -> str:
    """List all delegated tasks for the current session."""
    tasks = _load_tasks_for_session(parent_session_id)

    if not tasks:
        return json.dumps({"ok": True, "tasks": [], "message": "No delegated tasks yet."})

    summary = []
    for t in tasks:
        elapsed = int(t.get("completed_at") or int(time.time())) - int(t["created_at"])
        summary.append({
            "task_id": t["task_id"],
            "target_worker": t["target_worker"],
            "target_model": t["target_model"],
            "status": t["status"],
            "elapsed_seconds": elapsed,
            "prompt_preview": t["prompt"][:120],
            "result_preview": t.get("result_summary", "")[:200],
            "correlation_status": t.get("correlation_status", ""),
        })

    return json.dumps({"ok": True, "count": len(summary), "tasks": summary}, indent=2)


# ---------------------------------------------------------------------------
# Direct Completion Hook — called from _process_chat_* finally blocks
# ---------------------------------------------------------------------------

def on_target_run_finished(session_id: str) -> dict | None:
    """Check if a delegated task just completed on this session.

    Called from the finally block of _process_chat_* functions immediately
    after the model finishes a run. Detects TASK_DONE markers without
    waiting for the delegation monitor's reconciliation cycle.

    Returns a dict with task info if completion was detected and the task
    was successfully marked completed. Returns None if:
    - session_id is not a deleg-* session
    - no in-flight task found for this session
    - task is already completed/failed/timed_out
    - no TASK_DONE marker found in the response
    - response looks like a timeout/error (let monitor handle)
    """
    sid = str(session_id or "").strip()
    if not sid.startswith("deleg-"):
        return None

    try:
        # Find in-flight task for this delegation session
        inflight = _find_inflight_task_for_session(sid)
        if not inflight:
            return None

        task_id = inflight["task_id"]
        task = _load_task(task_id)
        if not task:
            return None

        # Only process running/dispatched tasks — skip terminal states
        if task["status"] not in ("running", "dispatched"):
            return None

        # Extract response and check for TASK_DONE marker
        response_text, correlated = _extract_task_response(task)
        if not response_text:
            return None

        # Skip timeout/error responses — let the monitor handle those
        if not correlated and _is_timeout_or_error_response(response_text):
            logger.debug(f"on_target_run_finished: {task_id} has error response, deferring to monitor")
            return None

        # Must have TASK_DONE marker for direct completion
        task_id_str = str(task.get("task_id", "") or "")
        if f"TASK_DONE {task_id_str}" not in str(response_text or ""):
            # No explicit TASK_DONE — don't mark complete from the hook.
            # The monitor will handle ambiguous completions.
            return None

        # Mark completed in DB
        now = int(time.time())
        with _deleg_db_connection() as db:
            _ensure_deleg_schema(db)
            # Guard: only update if still running/dispatched (prevents race with monitor)
            cursor = db.execute(
                "UPDATE delegated_tasks SET status='completed', result_summary=?, result_full=?, "
                "correlation_status='matched', completed_at=?, updated_at=? "
                "WHERE task_id=? AND status IN ('running', 'dispatched')",
                (response_text[:10000], response_text, now, now, task_id),
            )
            db.commit()
            if cursor.rowcount == 0:
                # Another path already marked it — skip
                logger.debug(f"on_target_run_finished: {task_id} already completed by another path")
                return None

        logger.info(
            f"on_target_run_finished: {task_id} completed (direct hook) "
            f"session={sid}, response_len={len(response_text)}"
        )

        return {
            "task_id": task_id,
            "parent_session_id": task["parent_session_id"],
            "target_worker": task["target_worker"],
            "target_model": task["target_model"],
            "target_base_session_id": task.get("target_base_session_id", ""),
            "result_summary": response_text[:10000],
            "elapsed": now - int(task["created_at"]),
        }

    except Exception as e:
        logger.warning(f"on_target_run_finished error for {sid}: {e}")
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_available_workers() -> list[dict]:
    """List all unique worker identities with active sessions."""
    try:
        with _deleg_db_connection() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "tab_meta" not in tables:
                return []
            cols = {str(r[1]) for r in db.execute("PRAGMA table_info(tab_meta)").fetchall()}
            if "worker_identity" not in cols:
                return []
            rows = db.execute(
                "SELECT DISTINCT worker_identity, model_key, label FROM tab_meta WHERE worker_identity IS NOT NULL AND worker_identity != '' ORDER BY worker_identity",
            ).fetchall()
            return [{"worker": r[0], "model": r[1], "label": r[2]} for r in rows]
    except Exception:
        return []


def _worker_for_session(session_id: str) -> str:
    """Look up worker identity for a session."""
    try:
        with _deleg_db_connection() as db:
            cols = {str(r[1]) for r in db.execute("PRAGMA table_info(tab_meta)").fetchall()}
            if "worker_identity" not in cols:
                return ""
            row = db.execute("SELECT worker_identity FROM tab_meta WHERE session_id = ?", (session_id,)).fetchone()
            return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _model_for_session(session_id: str) -> str:
    """Look up model_key for a session."""
    try:
        with _deleg_db_connection() as db:
            row = db.execute("SELECT model_key FROM tab_meta WHERE session_id = ?", (session_id,)).fetchone()
            return str(row[0] or "") if row else ""
    except Exception:
        return ""
