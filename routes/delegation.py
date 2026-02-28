"""
routes/delegation.py — Delegation routes and notification delivery system.

Extracted from server.py Phase 9. Contains:
- API endpoints for delegating tasks, checking status, listing, dismissing
- Delegation monitor (reconciler background task)
- Notification formatting, routing, and delivery
- Proactive wake system (waking idle Claude models for notifications)
- System wake (post-restart recovery)
- Delegation cleanup helper
"""

import asyncio
import json
import logging
import time
import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app_state import get_app_state
from auth import db_connection, load_history, save_history, clear_history
from config import WORKER_PORT
from log_store import log_write
from server_helpers import (
    is_claude_session as _is_claude_session,
    claude_model_for_session as _claude_model_for_session,
    worker_identity_for_session as _worker_identity_for_session,
)
import notification_store

logger = logging.getLogger("kukuibot.delegation_routes")

router = APIRouter()

# --- Module-level callback for firing Claude chat runs (set via init_delegation_routes) ---
# _process_chat_claude_fn is called by the proactive wake system to start an internal
# chat run on an idle Claude subprocess. Set during server startup to avoid circular imports.
_process_chat_claude_fn = None


def init_delegation_routes(*, process_chat_claude_fn):
    """Initialize module callbacks. Called from server.py startup.

    Args:
        process_chat_claude_fn: async callable matching _process_chat_claude signature:
            async def(queue, session_id, message, run_id, *, is_internal=False)
    """
    global _process_chat_claude_fn
    _process_chat_claude_fn = process_chat_claude_fn


# ============================================================================
# API Route Handlers
# ============================================================================


@router.get("/api/delegated-tasks")
async def api_delegated_tasks(session_id: str = "", compact: str = ""):
    """Return delegated tasks for a session (as parent) and tasks targeting any session.

    ?compact=1 — omit result_full and truncate result_summary/prompt to 200 chars.
    """
    is_compact = compact in ("1", "true", "yes")
    try:
        from delegation import _load_tasks_for_session, _deleg_db_connection

        result = {"ok": True, "outgoing": [], "incoming": []}

        # Outgoing: tasks this session delegated
        if session_id:
            tasks = _load_tasks_for_session(session_id)
            if is_compact:
                for t in tasks:
                    t.pop("result_full", None)
                    if t.get("result_summary"):
                        t["result_summary"] = t["result_summary"][:200]
                    if t.get("prompt"):
                        t["prompt"] = t["prompt"][:200]
            result["outgoing"] = tasks

        # Incoming: tasks targeting any active session (for sidebar badges)
        with _deleg_db_connection() as db:
            rows = db.execute(
                "SELECT task_id, parent_session_id, target_session_id, target_base_session_id, target_worker, target_model, status, prompt, created_at, updated_at FROM delegated_tasks WHERE status IN ('pending', 'running', 'dispatched') ORDER BY created_at DESC LIMIT 50",
            ).fetchall()
        result["incoming"] = [
            {
                "task_id": r[0], "parent_session_id": r[1], "target_session_id": r[2],
                "target_base_session_id": r[3],
                "target_worker": r[4], "target_model": r[5], "status": r[6],
                "prompt_preview": (r[7] or "")[:120], "created_at": r[8], "updated_at": r[9],
            }
            for r in rows
        ]
        return result
    except Exception as e:
        return {"ok": False, "error": str(e), "outgoing": [], "incoming": []}


@router.post("/api/delegate")
async def api_delegate(req: Request):
    """POST /api/delegate — Delegate a task to another worker session.

    Body: {"worker": "developer", "prompt": "...", "model": "codex", "parent_session_id": "..."}
    parent_session_id is optional — defaults to a synthetic Claude Code coordinator session.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Body must be a JSON object"}, status_code=400)

    worker = (body.get("worker") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    model = (body.get("model") or "").strip()
    force = bool(body.get("force", False))
    parent_session_id = (body.get("parent_session_id") or "").strip()

    if not worker:
        return JSONResponse({"ok": False, "error": "worker is required"}, status_code=400)
    if not prompt:
        return JSONResponse({"ok": False, "error": "prompt is required"}, status_code=400)

    # Default parent session for Claude Code callers
    if not parent_session_id:
        parent_session_id = "claude-code-api"

    from delegation import delegate_task
    # Run in executor to avoid blocking the event loop — delegate_task()
    # spawns a thread that POSTs back to /api/chat on this server, which
    # would deadlock if the event loop is blocked.
    loop = asyncio.get_event_loop()
    result_json = await loop.run_in_executor(
        None,
        lambda: delegate_task(
            parent_session_id=parent_session_id,
            worker=worker,
            prompt=prompt,
            model=model,
            force=force,
        ),
    )
    result = json.loads(result_json)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status_code)


@router.get("/api/delegate/check")
async def api_delegate_check(task_id: str = ""):
    """GET /api/delegate/check?task_id=task-xxx — Check status of a delegated task."""
    if not task_id:
        return JSONResponse({"ok": False, "error": "task_id is required"}, status_code=400)

    from delegation import check_task
    result_json = check_task(task_id=task_id)
    return JSONResponse(json.loads(result_json))


@router.get("/api/delegate/list")
async def api_delegate_list(parent_session_id: str = "claude-code-api"):
    """GET /api/delegate/list?parent_session_id=xxx — List delegated tasks for a session."""
    from delegation import list_tasks
    result_json = list_tasks(parent_session_id=parent_session_id)
    return JSONResponse(json.loads(result_json))


@router.get("/api/delegate/activity")
async def api_delegate_activity(req: Request):
    """Return current tool activity for all active (dispatched/running) delegated tasks.

    Lightweight endpoint designed for fast polling (2-3s) — one small DB query
    for active tasks, then reads tool state from in-memory ring buffer only.
    """
    try:
        from delegation import _deleg_db_connection

        state = get_app_state(req)
        _active_tasks = state.active_tasks
        _session_event_store = state.session_event_store

        with _deleg_db_connection() as db:
            rows = db.execute(
                "SELECT task_id, parent_session_id, target_session_id, target_worker, target_model, status, created_at FROM delegated_tasks WHERE status IN ('dispatched', 'running') ORDER BY created_at DESC LIMIT 20",
            ).fetchall()

        activities = []
        for r in rows:
            task_id, parent_sid, target_sid, worker, model, status, created_at = r
            activity = {
                "task_id": task_id,
                "parent_session_id": parent_sid,
                "target_session_id": target_sid,
                "worker": worker,
                "model": model,
                "status": status,
                "created_at": created_at,
                "tool_name": None,
                "tool_detail": None,
                "is_active": False,
            }

            # Check _active_tasks for live status
            task_state = _active_tasks.get(target_sid)
            if isinstance(task_state, dict) and task_state.get("status") == "running":
                activity["is_active"] = True

            # Scan ring buffer backwards for latest tool event
            ring = _session_event_store._rings.get(target_sid)
            if ring:
                for envelope, _size in reversed(list(ring)):
                    evt_type = str(envelope.get("type") or "")
                    payload = envelope.get("payload") or {}
                    if evt_type == "tool_use":
                        activity["tool_name"] = str(payload.get("name") or "")
                        activity["tool_detail"] = str(payload.get("input") or "")[:180]
                        break
                    elif evt_type == "ping" and payload.get("tool"):
                        activity["tool_name"] = str(payload.get("tool") or "")
                        activity["tool_detail"] = str(payload.get("detail") or "")[:180]
                        break
                    elif evt_type == "subagent_tool_use":
                        activity["tool_name"] = f"agent:{payload.get('name', '')}"
                        activity["tool_detail"] = str(payload.get("input") or "")[:180]
                        break
                    elif evt_type in ("text", "chunk", "thinking", "thinking_start"):
                        # Model is generating text — show that instead
                        activity["tool_name"] = "_thinking" if "thinking" in evt_type else "_generating"
                        activity["tool_detail"] = None
                        break
                    elif evt_type == "done":
                        # Task just finished — no active tool
                        break

            activities.append(activity)

        return {"ok": True, "activities": activities}
    except Exception as e:
        return {"ok": False, "error": str(e), "activities": []}


@router.post("/api/delegate/dismiss")
async def api_delegate_dismiss(req: Request):
    """Dismiss a delegated task — marks it as 'dismissed' so it drops off the activity bar."""
    try:
        state = get_app_state(req)
        _active_tasks = state.active_tasks

        body = await req.json()
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            return JSONResponse({"ok": False, "error": "task_id required"}, status_code=400)
        from delegation import _deleg_db_connection
        with _deleg_db_connection() as db:
            # Look up target session before updating status
            row = db.execute("SELECT target_session_id FROM delegated_tasks WHERE task_id = ?", (task_id,)).fetchone()
            db.execute(
                "UPDATE delegated_tasks SET status = 'dismissed', updated_at = ? WHERE task_id = ? AND status IN ('dispatched', 'running')",
                (int(time.time()), task_id),
            )
            db.commit()
        # Cancel the sub-worker's active generation to stop burning tokens
        target_session = row[0] if row else None
        if target_session:
            task_state = _active_tasks.get(target_session)
            if isinstance(task_state, dict):
                bg = task_state.get("task")
                if bg and not bg.done():
                    bg.cancel()
                    logger.info(f"Delegation dismiss: cancelled active task for session {target_session}")
                task_state["status"] = "cancelled"
        logger.info(f"Delegation dismiss: {task_id}")
        return {"ok": True, "task_id": task_id}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ============================================================================
# Delegation Status Notification Helpers
# ============================================================================
# These functions format the text that models receive when a delegated task
# changes status. The formatted text is passed through the full 4-step
# delivery pipeline (_deliver_or_queue_parent_notification) and ultimately
# injected into the model's context (either prepended to a user message
# via drain_notifications or sent directly via proactive wake).


def _format_delegation_status_notification(
    task_id: str,
    worker: str,
    model: str,
    from_status: str,
    to_status: str,
    elapsed: int | float,
    summary: str = "",
) -> str:
    """Format a human-readable delegation status notification.

    Produces the [DELEGATION UPDATE] block that models recognize as a
    task status change. Includes task ID, worker, status transition,
    elapsed time, and next-action guidance.

    IMPORTANT: summary text is sanitized to avoid accidental boundary collisions
    when notifications are prepended to user messages.
    """
    safe_summary = str(summary or "")
    if safe_summary:
        safe_summary = safe_summary.replace(notification_store.DELEGATION_PREPEND_BOUNDARY, "[delegation-boundary]")

    lines = [
        "[DELEGATION UPDATE]",
        f"Task: {task_id}",
        f"Worker: {worker} ({model})",
        f"Status: {from_status} → {to_status}",
        f"Elapsed: {int(elapsed)}s",
    ]
    if safe_summary:
        lines.append(f"Summary:\n{safe_summary}")
    if to_status == "completed":
        lines.append(f"Next: Use check_task('{task_id}') only if full raw output is needed.")
    elif to_status in ("dispatch_failed", "timed_out", "failed"):
        lines.append("Action: Report failure to user and decide whether to retry.")
    return "\n".join(lines)


# ============================================================================
# Delegation Cleanup Helper
# ============================================================================

async def _do_delegation_cleanup(task_id: str):
    """Clean up delegated session after result has been captured: clear history and kill subprocess.

    Also writes a summary log entry to the worker's base tab session so
    delegation results are visible in the developer tab UI.
    """
    try:
        from delegation import _load_task
        from claude_bridge import get_claude_pool

        t = _load_task(task_id)
        if not t:
            return
        target_sid = t.get("target_session_id", "")
        base_sid = t.get("target_base_session_id", "")
        task_status = t.get("status", "completed")
        worker = t.get("target_worker", "")
        model = t.get("target_model", "")

        # Write a summary to the worker's base tab chatlog so delegation
        # results appear in the developer tab history.
        if base_sid and base_sid != target_sid:
            try:
                prompt_preview = (t.get("prompt") or "")[:200]
                result_preview = (t.get("result_summary") or t.get("result_full") or "")[:10000]
                elapsed = int(t.get("completed_at") or 0) - int(t.get("created_at") or 0)
                elapsed_str = f"{elapsed}s" if elapsed > 0 else "unknown"

                summary_lines = [
                    f"[DELEGATED TASK {task_status.upper()}]",
                    f"Task: {task_id}",
                    f"Worker: {worker} ({model})",
                    f"Elapsed: {elapsed_str}",
                    f"Prompt: {prompt_preview}",
                ]
                if result_preview:
                    summary_lines.append(f"Result:\n{result_preview}")
                summary_text = "\n".join(summary_lines)

                # 1) Persist to chatlog (source of truth used by loadTabHistory)
                log_write(
                    "chat",
                    summary_text,
                    role="system",
                    session_id=base_sid,
                    worker=worker,
                    source=f"delegation.{model}" if model else "delegation",
                )

                # 2) Also append to tab history so users currently in that tab see
                # the summary immediately (without waiting for chatlog reload).
                try:
                    _hist_items, _last_resp_id, _last_usage = load_history(base_sid)
                    _hist_items.append({"role": "system", "content": summary_text})
                    if len(_hist_items) > 50:
                        _hist_items = _hist_items[-50:]
                    save_history(base_sid, _hist_items, _last_resp_id, _last_usage or {})
                except Exception as hist_err:
                    logger.warning(f"Delegation cleanup: failed to append base tab history: {hist_err}")

                # 3) Broadcast SSE event to the base tab so the frontend refreshes
                try:
                    pool = get_claude_pool()
                    if pool:
                        base_proc = pool.get(base_sid)
                        if base_proc:
                            base_proc._broadcast({
                                "type": "delegation_notification",
                                "task_id": task_id,
                                "status": task_status,
                                "message": summary_text,
                            })
                except Exception as sse_err:
                    logger.debug(f"Delegation cleanup: SSE broadcast to base tab skipped: {sse_err}")

                logger.info(f"Delegation cleanup: wrote summary to base tab {base_sid} for {task_id}")
            except Exception as log_err:
                logger.warning(f"Delegation cleanup: failed to write summary to base tab: {log_err}")

        if target_sid:
            clear_history(target_sid)
            logger.info(f"Delegation cleanup: cleared history for {target_sid}")
            # Kill the delegation subprocess to free pool slot and resources
            pool = get_claude_pool()
            if pool:
                await pool.kill_session(target_sid)
                logger.info(f"Delegation cleanup: killed subprocess for {target_sid}")
    except Exception as ce:
        logger.warning(f"Delegation cleanup failed for {task_id}: {ce}")


# ============================================================================
# Direct Completion Hook
# ============================================================================

async def _check_delegation_completion(session_id: str):
    """Direct completion hook — called from _process_chat_* finally blocks.

    Detects TASK_DONE immediately when a deleg-* session finishes, without
    waiting for the 120s reconciler. If completion is detected, sends
    notifications to parent + base tab and runs cleanup.
    """
    try:
        from delegation import on_target_run_finished
        result = on_target_run_finished(session_id)
        if result is None:
            return

        task_id = result["task_id"]
        parent_sid = result["parent_session_id"]
        worker = result["target_worker"]
        model = result["target_model"]
        base_sid = result.get("target_base_session_id", "")
        summary = result["result_summary"]
        elapsed = result["elapsed"]

        # Notify developer's base tab (where the work happened)
        if base_sid and base_sid != parent_sid:
            full_msg = _format_delegation_status_notification(
                task_id=task_id, worker=worker, model=model,
                from_status="running", to_status="completed",
                elapsed=elapsed, summary=summary,
            )
            await _deliver_or_queue_parent_notification(base_sid, full_msg, task_id, "completed")

        # Notify parent (dev manager) with lightweight message
        brief = f"Task completed. Use check_task('{task_id}') for details if needed."
        parent_msg = _format_delegation_status_notification(
            task_id=task_id, worker=worker, model=model,
            from_status="running", to_status="completed",
            elapsed=elapsed, summary=brief,
        )
        await _deliver_or_queue_parent_notification(parent_sid, parent_msg, task_id, "completed")

        await _do_delegation_cleanup(task_id)
        logger.info(f"Direct completion hook: {task_id} notified and cleaned up")

    except Exception as e:
        logger.warning(f"Direct completion hook error for {session_id}: {e}")


# ============================================================================
# Proactive Wake System
# ============================================================================
#
# The proactive wake system allows the delegation monitor to "wake up" idle
# Claude models when a delegation task changes status (e.g. completed, failed).
# Without this, notifications would sit in the pending queue until the next
# user message arrives — which could be minutes or hours.
#
# Architecture:
#   1. Delegation monitor detects status change → calls _deliver_or_queue_parent_notification()
#   2. Notification is persisted to DB + queued in-memory on the subprocess
#   3. If model is idle, _try_proactive_wake() fires _process_chat_claude() as
#      an internal run with the notification text as the "message"
#   4. The model processes the notification and responds (e.g. "Task completed, here's the result")
#   5. If user sends a message while wake is in progress, the wake is cancelled
#      and its content is re-queued to prepend to the user's message
#
# Per-session locks prevent duplicate concurrent wakes (e.g. if two tasks
# complete at the same time for the same parent session).
#
# Wake locks are cleaned up by the _memory_reaper() background task when
# sessions become inactive.


def _get_wake_lock(session_id: str, _app_state=None) -> asyncio.Lock:
    """Get or create a per-session wake lock."""
    if _app_state is None:
        # Fallback: access via module-level (set during init)
        raise RuntimeError("_get_wake_lock requires _app_state")
    locks = _app_state.proactive_wake_locks
    if session_id not in locks:
        locks[session_id] = asyncio.Lock()
    return locks[session_id]


async def _try_proactive_wake(
    session_id: str,
    proc,  # PersistentClaudeProcess
    notify_msg: str,
    task_id: str,
    to_status: str,
    label: str = "",
    _retry_count: int = 0,
    _app_state=None,
) -> bool:
    """Try to proactively wake an idle Claude model to process a notification.

    This is the core of the proactive wake system. It checks whether the model
    is idle, and if so, starts a new internal _process_chat_claude run with the
    notification text as the message. The model then processes the notification
    and generates a response (visible in the UI via SSE).

    Uses a per-session lock to prevent concurrent duplicate wakes — if two
    delegation tasks complete simultaneously, only one wake fires; the second
    notification is queued and will be drained by the first wake's run.

    The run is marked _internal=True in _active_tasks so that:
      - User messages can preempt it (cancel the wake, re-queue the notification)
      - The UI knows this is a system-initiated run, not a user message

    _retry_count tracks deferred retry attempts — capped at 3 to prevent
    infinite retry loops when the subprocess is persistently broken.

    Returns True if wake was fired, False if model was busy or lock was held.
    """
    from claude_bridge import get_claude_pool
    from routes.session_events import _db_start_run

    if _app_state is None:
        raise RuntimeError("_try_proactive_wake requires _app_state")

    _active_tasks = _app_state.active_tasks
    _session_event_store = _app_state.session_event_store

    if _retry_count >= 3:
        logger.warning(
            f"Proactive wake: max retries ({_retry_count}) reached for {session_id} "
            f"({task_id}:{to_status}) — notification stays queued for next user message"
        )
        return False
    lock = _get_wake_lock(session_id, _app_state=_app_state)
    if lock.locked():
        # Another wake is already in progress for this session — the notification
        # was already queued in-memory and will be drained by that wake's run.
        logger.info(
            f"Delegation notify: wake lock held for {session_id}, "
            f"notification queued for next turn ({task_id}:{to_status})"
        )
        return False

    async with lock:
        # Verify subprocess is actually alive before checking idle state
        if proc.proc is None or proc.proc.returncode is not None:
            logger.warning(f"Proactive wake: subprocess dead for {session_id} — attempting respawn")
            try:
                pool = get_claude_pool()
                if pool:
                    proc = await _ensure_claude_subprocess(pool, session_id)
                else:
                    proc = None
                if proc is None or proc.proc is None or proc.proc.returncode is not None:
                    logger.error(f"Proactive wake: respawn failed for {session_id} — notification stays queued")
                    return False
            except Exception as e:
                logger.error(f"Proactive wake: respawn error for {session_id}: {e}")
                return False

        # Re-check idle state under lock — model may have become busy between
        # the lock.locked() check above and acquiring the lock.
        _current = _active_tasks.get(session_id) or {}
        _cur_status = str(_current.get("status") or "")
        _cur_task = _current.get("task")
        _has_active_run = (
            _cur_status == "running"
            and _cur_task is not None
            and not _cur_task.done()
        )

        if proc.is_busy or _has_active_run:
            # Model is processing a user message or another wake — notification
            # stays in proc._pending_notifications and will be prepended to the
            # next message when send_message() calls drain_notifications().
            logger.info(
                f"Delegation notify: {label}model busy, notification queued for next turn on {session_id} "
                f"({task_id}:{to_status}, is_busy={proc.is_busy}, has_active_run={_has_active_run})"
            )
            return False

        # Drain all pending notifications from the subprocess queue so they become
        # the message text directly. This avoids double-prepend: if we left them
        # in _pending_notifications, send_message() would drain them again and
        # the notification would appear twice.
        drained = proc.drain_notifications()
        proactive_text = "\n\n".join(drained) if drained else notify_msg

        # Set up the internal run — same structure as a user-initiated run but
        # with _internal=True and _wake_message storing the notification text
        # (used for re-queuing if preempted by a user message).
        _wake_queue: asyncio.Queue = asyncio.Queue()
        _wake_run_id = str(uuid.uuid4())
        _wake_now = time.time()
        _active_tasks[session_id] = {
            "status": "running",
            "_internal": True,              # Marks this as a wake-initiated run
            "_wake_message": proactive_text, # Preserved for re-queuing if preempted
            "started": _wake_now,
            "events": [],
            "next_seq": _session_event_store.peek_next_event_id(session_id),
            "last_event_at": _wake_now,
            "run_id": _wake_run_id,
            "queue": _wake_queue,
            "task": None,
        }
        _db_start_run(session_id, _wake_run_id, _wake_now)

        if _process_chat_claude_fn is None:
            logger.error("Proactive wake: _process_chat_claude_fn not initialized")
            return False

        # Fire _process_chat_claude as a background task — this sends the
        # notification text to the Claude subprocess and streams the response.
        # is_internal=True tells the chat handler this is a system wake, not
        # a user message (affects context injection and notification draining).
        _wake_task = asyncio.create_task(
            _process_chat_claude_fn(
                _wake_queue, session_id, proactive_text, _wake_run_id,
                is_internal=True,
            )
        )
        _active_tasks[session_id]["task"] = _wake_task

        # Add done-callback: if wake failed quickly (exception, cancel), schedule
        # a deferred retry so the notification doesn't sit forever.
        _current_retry = _retry_count  # capture for closure
        def _wake_done_callback(task, sid=session_id, cur_retry=_current_retry, app_state=_app_state):
            """If proactive wake failed quickly, schedule a deferred retry (max 3)."""
            try:
                exc = task.exception() if not task.cancelled() else None
            except Exception:
                exc = None
            if task.cancelled() or exc is not None:
                logger.warning(f"Proactive wake failed for {sid}: cancelled={task.cancelled()}, exc={exc} — scheduling deferred retry ({cur_retry + 1}/3)")
                async def _deferred_wake_retry():
                    await asyncio.sleep(5)
                    try:
                        _retry_pool = get_claude_pool()
                        _retry_proc = (await _ensure_claude_subprocess(_retry_pool, sid)) if _retry_pool else None
                        if _retry_proc:
                            # Re-queue: proc still has pending notifications from earlier drain
                            _retry_drained = _retry_proc.drain_notifications()
                            if _retry_drained:
                                _retry_proc.queue_notification("\n\n".join(_retry_drained))
                            await _try_proactive_wake(sid, _retry_proc, notify_msg, task_id, to_status, label="retry ", _retry_count=cur_retry + 1, _app_state=app_state)
                    except Exception as e2:
                        logger.warning(f"Deferred wake retry failed for {sid}: {e2}")
                try:
                    asyncio.get_running_loop().create_task(_deferred_wake_retry())
                except RuntimeError:
                    pass  # Event loop closed

        _wake_task.add_done_callback(_wake_done_callback)

        logger.info(
            f"Delegation notify: {label}proactive model wake fired for {session_id} "
            f"(run={_wake_run_id}, {task_id}:{to_status})"
        )
        return True


async def _ensure_claude_subprocess(pool, session_id: str):
    """Ensure a Claude subprocess exists AND is running for session_id.

    This is critical for the wake system: after a server restart, Claude
    subprocesses don't exist yet (they're spawned lazily on first message).
    When the system wake or delegation monitor needs to deliver a notification
    to a session, it must first ensure the subprocess is running.

    For real tab sessions (tab-claude-*), spawns the subprocess via
    get_or_create() if it doesn't exist yet, then calls ensure_running()
    to actually start the OS process. This enables proactive wake delivery
    even when no user has connected since the last restart.

    For virtual sessions (e.g. claude-code-api), returns None — these don't
    have real subprocesses and are handled via fallback routing to dev-manager.
    """
    proc = pool.get(session_id)
    if proc:
        # Existing wrapper — make sure the OS process is actually alive
        if proc.proc is None or proc.proc.returncode is not None:
            try:
                await proc.ensure_running()
                logger.info(f"Delegation notify: respawned dead subprocess for {session_id}")
            except Exception as e:
                logger.warning(f"Delegation notify: ensure_running() failed for existing proc {session_id}: {e}")
                return None
        return proc

    # Only auto-spawn for real tab sessions, not for virtual session IDs
    if not session_id.startswith("tab-claude"):
        return None

    # Look up worker identity and model for this session
    try:
        wi = _worker_identity_for_session(session_id)
        cm = _claude_model_for_session(session_id)
        proc = pool.get_or_create(session_id, worker_identity=wi, model=cm)
        if proc and (proc.proc is None or proc.proc.returncode is not None):
            await proc.ensure_running()
        logger.info(f"Delegation notify: spawned subprocess for {session_id} (worker={wi}, model={cm})")
        return proc
    except RuntimeError as e:
        logger.warning(f"Delegation notify: failed to spawn subprocess for {session_id}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Delegation notify: unexpected error spawning subprocess for {session_id}: {e}")
        return None


# ============================================================================
# Notification Routing
# ============================================================================

async def _deliver_or_queue_parent_notification(
    parent_sid: str,
    notify_msg: str,
    task_id: str,
    to_status: str,
) -> bool:
    """Notification routing for delegation status changes.

    Enqueues the notification to the DB inbox (source of truth) and triggers
    the event-driven NotificationDispatcher for immediate delivery.

    Routing logic:
    - Claude sessions: enqueue to target session, dispatcher handles wake
    - claude-code-api: resolve to dev-manager tab session via fallback
    - Non-Claude sessions: enqueue to DB + fire-and-forget POST to /api/chat

    Returns True if notification was enqueued successfully.
    """
    from claude_bridge import get_claude_pool
    from routes.tabs import _ensure_tab_meta_schema

    dedupe_key = f"{task_id}:{to_status}"
    payload = {
        "task_id": task_id,
        "event_type": "status_change",
        "from_status": "",
        "to_status": to_status,
        "message": notify_msg,
    }

    # Access _app_state via deferred import to avoid circular import at module load
    from server import _app_state

    if _is_claude_session(parent_sid):
        # --- Resolve target session: parent directly, or fallback for claude-code-api ---
        target_sid = parent_sid

        if parent_sid == "claude-code-api":
            # REST API caller — route to dev-manager tab if available
            pool = get_claude_pool()
            resolved = False
            if pool:
                try:
                    with db_connection() as _fb_db:
                        _ensure_tab_meta_schema(_fb_db)
                        _fb_rows = _fb_db.execute(
                            "SELECT session_id FROM tab_meta WHERE worker_identity = ? ORDER BY rowid DESC",
                            ("dev-manager",)
                        ).fetchall()
                    for (_fb_sid,) in _fb_rows:
                        if _fb_sid.startswith("tab-claude"):
                            target_sid = _fb_sid
                            resolved = True
                            logger.info(
                                f"Delegation notify: fallback routed {parent_sid} → {_fb_sid} (dev-manager) "
                                f"({task_id}:{to_status})"
                            )
                            break
                except Exception as e:
                    logger.warning(f"Delegation notify: dev-manager fallback lookup failed: {e}")
            if not resolved:
                # Enqueue under claude-code-api — will drain when next user message arrives
                target_sid = parent_sid

        # Persist to DB inbox (source of truth)
        notification_store.enqueue(target_sid, payload, dedupe_key)
        logger.info(f"Delegation notify: enqueued for Claude session {target_sid} ({task_id}:{to_status})")

        # Trigger event-driven dispatcher for immediate delivery attempt
        if _app_state.notification_dispatcher:
            _app_state.notification_dispatcher.trigger_enqueue(target_sid)

        return True

    # --- Non-Claude Session Delivery ---
    # Non-Claude sessions (Codex, OpenRouter) don't have a subprocess with in-memory
    # notification queues or proactive wake. Instead:
    #   1. Persist to DB inbox (source of truth, drained on next /api/chat call)
    #   2. Fire-and-forget POST to /api/chat for immediate delivery attempt
    notification_store.enqueue(parent_sid, payload, dedupe_key)
    logger.info(f"Delegation notify: enqueued for non-Claude session {parent_sid} ({task_id}:{to_status})")

    # Fire-and-forget immediate delivery attempt (ignore 409 = run_in_progress)
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(
                f"https://127.0.0.1:{WORKER_PORT}/api/chat",
                json={"session_id": parent_sid, "message": notify_msg, "_internal": True},
                timeout=15.0,
            )
            if resp.status_code == 200:
                logger.info(f"Delegation notify: also POSTed immediately to {parent_sid} ({task_id}:{to_status})")
    except Exception:
        pass  # Inbox has it — will be drained on next message
    return True


# ============================================================================
# Delegation Monitor (Reconciler Background Task)
# ============================================================================
# Background task demoted to 120s reconciler (was 3s poller). Event-driven
# NotificationDispatcher handles immediate delivery. This monitor now:
#   - Detects tasks stuck in dispatched state
#   - Timeout tasks older than 2h
#   - Catches completions missed by the event-driven path
#   - Promotes dispatched → running on delivery confirmation
# On each status transition, calls _deliver_or_queue_parent_notification()
# which enqueues to DB and triggers the dispatcher.

async def _delegation_monitor(poll_interval: float = 120.0, _app_state=None):
    """Reconciler: poll delegated tasks for status changes at low frequency.

    Direct completion hooks in _process_chat_* finally blocks handle immediate
    detection. Event-driven NotificationDispatcher handles delivery. This
    monitor is a safety net that catches completions missed by both paths,
    detects stale dispatches, and enforces 2h timeouts.
    """
    from claude_bridge import get_claude_pool

    if _app_state is None:
        raise RuntimeError("_delegation_monitor requires _app_state")

    _active_tasks = _app_state.active_tasks

    logger.info("Delegation monitor (reconciler) started (poll every %.0fs)", poll_interval)
    # Track task_ids that have already received a dispatched→running or stale notification
    # to prevent repeated notifications on every monitor cycle.
    _notified_running: set[str] = set()
    _first_cycle_logged = False
    while True:
        try:
            tasks_checked = 0

            # Find all running/dispatched tasks
            from delegation import _deleg_db_connection, check_task as _deleg_check
            with _deleg_db_connection() as db:
                rows = db.execute(
                    "SELECT task_id, parent_session_id, target_worker, target_model, status, created_at "
                    "FROM delegated_tasks WHERE status IN ('running', 'dispatched', 'dispatch_failed') "
                    "ORDER BY created_at ASC"
                ).fetchall()

            for row in rows:
                task_id, parent_sid, worker, model, old_status, created_at = row
                tasks_checked += 1
                try:
                    # Skip tasks older than 2 hours (stale)
                    if time.time() - created_at > 7200:
                        try:
                            with _deleg_db_connection() as db2:
                                db2.execute(
                                    "UPDATE delegated_tasks SET status='timed_out', updated_at=? WHERE task_id=?",
                                    (int(time.time()), task_id),
                                )
                                db2.commit()
                        except Exception:
                            pass
                        _notified_running.discard(task_id)
                        continue

                    # For 'dispatched' tasks, verify delivery via SQLite log + pool/active checks
                    if old_status == "dispatched":
                        try:
                            from delegation import _verify_delivery_via_log, _load_task as _deleg_load
                            t = _deleg_load(task_id)
                            if t and t.get("task_token") and t.get("target_session_id"):
                                delivered = _verify_delivery_via_log(
                                    t["target_session_id"], t["task_token"], max_wait=1,
                                )
                                if delivered:
                                    logger.info(f"Delegation monitor: {task_id} token_found=true (SQLite)")
                                else:
                                    logger.debug(f"Delegation monitor: {task_id} token_found=false")

                                target_active = False
                                task_state = _active_tasks.get(t["target_session_id"])
                                if isinstance(task_state, dict) and task_state.get("status") in {"running", "queued", "pending"}:
                                    target_active = True
                                if not target_active:
                                    try:
                                        pool = get_claude_pool()
                                        if pool:
                                            status = pool.get_all_status()
                                            sessions = status.get("processes", {}) if isinstance(status, dict) else {}
                                            if t["target_session_id"] in sessions:
                                                target_active = True
                                    except Exception:
                                        pass

                                elapsed_s = time.time() - created_at
                                if delivered or target_active:
                                    reason = "delivery confirmed" if delivered else "active target session fallback"
                                    with _deleg_db_connection() as db3:
                                        db3.execute(
                                            "UPDATE delegated_tasks SET status='running', updated_at=? WHERE task_id=?",
                                            (int(time.time()), task_id),
                                        )
                                        db3.commit()
                                    logger.info(f"Delegation monitor: {task_id} dispatched → running ({reason})")
                                    # Delay notification until 30s after dispatch — immediate pings are low value
                                    if elapsed_s >= 30 and task_id not in _notified_running:
                                        running_msg = _format_delegation_status_notification(
                                            task_id=task_id, worker=worker, model=model,
                                            from_status="dispatched", to_status="running",
                                            elapsed=elapsed_s, summary=f"Delivery confirmed ({reason}).",
                                        )
                                        await _deliver_or_queue_parent_notification(parent_sid, running_msg, task_id, "running")
                                        _notified_running.add(task_id)
                                    old_status = "running"
                                elif elapsed_s >= 30 and task_id not in _notified_running:
                                    # Still dispatched after 30s with no delivery confirmation — warn parent
                                    warn_msg = _format_delegation_status_notification(
                                        task_id=task_id, worker=worker, model=model,
                                        from_status="dispatched", to_status="dispatched",
                                        elapsed=elapsed_s,
                                        summary="Task has been in dispatched state for 30s — delivery may have failed. Investigate with check_task().",
                                    )
                                    await _deliver_or_queue_parent_notification(parent_sid, warn_msg, task_id, "dispatched_stale")
                                    _notified_running.add(task_id)
                        except Exception as e:
                            logger.warning(f"Delegation monitor: delivery check for {task_id}: {e}")

                    # Check task status (this updates the DB internally)
                    try:
                        result_json = _deleg_check(task_id, parent_sid)
                        result = json.loads(result_json)
                    except Exception as e:
                        logger.warning(f"Delegation monitor: check_task({task_id}) failed: {e}")
                        continue

                    new_status = result.get("status", "")
                    if new_status in ("completed", "failed", "dispatch_failed", "timed_out") and old_status != new_status:
                        # Task reached terminal state — notify BOTH developer base tab AND parent session
                        elapsed = result.get("elapsed_seconds", 0)
                        summary = result.get("result_summary", "")[:10000]

                        # Load task to get target_base_session_id
                        from delegation import _load_task
                        task_data = _load_task(task_id)
                        base_sid = task_data.get("target_base_session_id", "") if task_data else ""

                        # 1. Send FULL summary to developer's base tab (where the work happened)
                        if base_sid and parent_sid != base_sid:
                            full_notify_msg = _format_delegation_status_notification(
                                task_id=task_id, worker=worker, model=model,
                                from_status=old_status, to_status=new_status,
                                elapsed=elapsed, summary=summary,
                            )
                            await _deliver_or_queue_parent_notification(base_sid, full_notify_msg, task_id, new_status)

                        # 2. Send LIGHTWEIGHT notification to dev manager (parent who dispatched)
                        # Include status and brief note, but NOT the verbose summary
                        brief_summary = f"Task {new_status}. Use check_task('{task_id}') for details if needed."
                        parent_notify_msg = _format_delegation_status_notification(
                            task_id=task_id, worker=worker, model=model,
                            from_status=old_status, to_status=new_status,
                            elapsed=elapsed, summary=brief_summary,
                        )
                        await _deliver_or_queue_parent_notification(parent_sid, parent_notify_msg, task_id, new_status)

                        await _do_delegation_cleanup(task_id)
                        _notified_running.discard(task_id)
                except asyncio.CancelledError:
                    logger.info("Delegation monitor cancelled while processing task %s", task_id)
                    raise

            if not _first_cycle_logged:
                logger.info("Delegation monitor: first cycle complete, %d tasks checked", tasks_checked)
                _first_cycle_logged = True

            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            logger.info("Delegation monitor stopped")
            return
        except Exception as e:
            logger.warning(f"Delegation monitor error: {e}")
            await asyncio.sleep(10)  # Back off on errors


# ============================================================================
# System Wake (Post-Restart Recovery)
# ============================================================================

async def _system_wake():
    """Post-startup: reconcile stale tasks and wake coordinator sessions.

    Called once as an asyncio.create_task() during server startup.
    Waits 5 seconds for the server to finish binding before running.
    """
    # Brief delay to let the server finish binding and accept requests
    await asyncio.sleep(5)
    logger.info("System wake: checking for in-flight tasks and coordinator sessions")

    try:
        from delegation import _deleg_db_connection
        from claude_bridge import get_claude_pool
        from routes.tabs import _ensure_tab_meta_schema

        # --- 1. Reconcile stale delegated tasks ---
        with _deleg_db_connection() as db:
            rows = db.execute(
                "SELECT task_id, parent_session_id, target_worker, target_model, status, created_at "
                "FROM delegated_tasks WHERE status IN ('running', 'dispatched', 'dispatch_failed') "
                "ORDER BY created_at ASC"
            ).fetchall()

        stale_count = 0
        active_count = 0
        parent_sessions: dict[str, list[str]] = {}  # parent_sid -> [task_ids]

        for task_id, parent_sid, worker, model, status, created_at in rows:
            age_s = time.time() - created_at
            if age_s > 7200:
                # Mark tasks older than 2h as timed out
                try:
                    with _deleg_db_connection() as db2:
                        db2.execute(
                            "UPDATE delegated_tasks SET status='timed_out', updated_at=? WHERE task_id=?",
                            (int(time.time()), task_id),
                        )
                        db2.commit()
                    stale_count += 1
                except Exception:
                    pass
            else:
                active_count += 1
                parent_sessions.setdefault(parent_sid, []).append(task_id)

        logger.info(f"System wake: {active_count} active tasks, {stale_count} timed out")

        # --- 2. Find coordinator/dev-manager sessions to wake ---
        # Two categories of sessions need wake notifications:
        #   a) Parent sessions with active delegated tasks — they need to know
        #      the monitor is tracking their tasks again after the restart
        #   b) Dev-manager/coordinator sessions — they orchestrate multi-worker
        #      workflows and need to resume coordination
        wake_sessions: set[str] = set()

        # (a) Wake sessions that have active delegated tasks
        wake_sessions.update(parent_sessions.keys())

        # (b) Also wake any dev-manager sessions (they coordinate work)
        mgr_rows = []
        try:
            with db_connection() as db3:
                _ensure_tab_meta_schema(db3)
                mgr_rows = db3.execute(
                    "SELECT session_id, worker_identity FROM tab_meta "
                    "WHERE LOWER(worker_identity) LIKE '%manager%' OR LOWER(worker_identity) LIKE '%coordinator%'"
                ).fetchall()
            for sid, wi in mgr_rows:
                wake_sessions.add(sid)
        except Exception as e:
            logger.warning(f"System wake: failed to query tab_meta: {e}")

        if not wake_sessions:
            logger.info("System wake: no sessions to notify")
            return

        # --- 3. Send [SYSTEM WAKE] notifications ---
        # Deduplication logic: Non-tab parent sessions (e.g. claude-code-api) are
        # fallback-routed to dev-manager tab sessions by _deliver_or_queue_parent_notification.
        # If that same dev-manager tab is ALSO in wake_sessions (because it has its own
        # active tasks), it would receive two notifications: one from the fallback route
        # and one from the direct wake below. To prevent this, we pre-compute which tab
        # sessions will be fallback targets and skip their direct wake.
        #
        # Build set of dev-manager tab session IDs that will receive fallback routing.
        fallback_targets: set[str] = set()
        mgr_tab_sids = {sid for sid, _ in mgr_rows} if mgr_rows else set()

        # Identify non-tab parent sessions that will fallback-route to a manager tab
        non_tab_parents = [
            sid for sid in wake_sessions
            if not sid.startswith("tab-") and sid in parent_sessions
        ]
        for ntp in non_tab_parents:
            # _deliver_or_queue_parent_notification routes claude-code-api to
            # dev-manager tabs. Pre-compute which tabs will be fallback targets.
            if _is_claude_session(ntp):
                pool = get_claude_pool()
                if pool and not pool.get(ntp):
                    # No direct subprocess — will fallback to dev-manager tab
                    fallback_targets.update(mgr_tab_sids & wake_sessions)

        # Process non-tab parents first (their fallback routing delivers to manager tabs).
        # Merge any tasks owned by the fallback target into the message so only one
        # consolidated notification is needed per real tab session.
        # Dedupe key is minute-granularity to prevent duplicate notifications if
        # _system_wake() somehow runs twice in the same minute.
        wake_dedupe = f"system_wake:{int(time.time()) // 60}"
        for sid in non_tab_parents:
            # Collect all task IDs: the non-tab parent's own tasks + any tasks owned
            # by tab sessions that will receive this via fallback (to consolidate).
            all_task_ids = list(parent_sessions.get(sid, []))
            for ft in fallback_targets:
                ft_tasks = parent_sessions.get(ft, [])
                if ft_tasks:
                    all_task_ids.extend(ft_tasks)

            task_list = ", ".join(all_task_ids)
            wake_msg = (
                f"[SYSTEM WAKE] Server restarted. Delegation monitor is active.\n"
                f"You have {len(all_task_ids)} in-flight task(s): {task_list}\n"
                f"The delegation monitor will track these automatically and notify you on completion.\n"
                f"Use check_task() or list_tasks() to review current status."
            )
            await _deliver_or_queue_parent_notification(sid, wake_msg, "system_wake", wake_dedupe)
            logger.info(f"System wake: notification delivered for {sid}")

        # Process remaining tab sessions, skipping those already notified via fallback
        for sid in wake_sessions:
            if sid in non_tab_parents:
                continue  # already processed above
            if sid in fallback_targets:
                logger.info(f"System wake: skipping {sid} (already notified via fallback routing)")
                continue

            task_ids = parent_sessions.get(sid, [])
            if task_ids:
                task_list = ", ".join(task_ids)
                wake_msg = (
                    f"[SYSTEM WAKE] Server restarted. Delegation monitor is active.\n"
                    f"You have {len(task_ids)} in-flight task(s): {task_list}\n"
                    f"The delegation monitor will track these automatically and notify you on completion.\n"
                    f"Context loading.\n"
                    f"Dev-Manager should respond within 60 seconds."
                )
            else:
                wake_msg = (
                    f"[SYSTEM WAKE] Server restarted. Delegation monitor is active.\n"
                    f"No in-flight delegated tasks. Context loading.\n"
                    f"Dev-Manager should respond within 60 seconds."
                )
            await _deliver_or_queue_parent_notification(sid, wake_msg, "system_wake", wake_dedupe)
            logger.info(f"System wake: notification delivered for {sid}")

    except Exception as e:
        logger.error(f"System wake failed: {e}")
