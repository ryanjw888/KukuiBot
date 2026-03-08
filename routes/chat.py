"""
chat.py — Chat API routes extracted from server.py (Phase 10b).

Contains /api/chat, /api/chat/cancel, and the SSE stream dispatch logic.
"""

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from app_state import get_app_state
from auth import get_request_user
from claude_bridge import get_claude_pool
from log_store import log_write
from routes.session_events import _emit_event, _db_start_run, _db_mark_run_done
from server_helpers import (
    is_claude_session,
    is_anthropic_session,
    is_openrouter_session,
    validate_attachments,
    cleanup_old_attachments,
)
import notification_store

logger = logging.getLogger("kukuibot.routes.chat")

router = APIRouter()


@router.post("/api/chat")
async def api_chat(req: Request):
    state = get_app_state(req)
    _active_tasks = state.active_tasks
    _session_event_store = state.session_event_store

    raw = await req.body()
    if not raw or not raw.strip():
        return JSONResponse({"error": "Empty request body"}, status_code=400)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    message = body.get("message", "").strip()
    session_id = body.get("session_id", "default")
    is_internal = bool(body.get("_internal"))
    attachments = validate_attachments(body.get("attachments", []))
    if not message and not attachments:
        return JSONResponse({"error": "No message"}, status_code=400)
    if not message and attachments:
        message = "[attached files]"

    cleanup_old_attachments()

    now = time.time()
    current = _active_tasks.get(session_id) or {}
    current_status = str(current.get("status") or "")
    current_task = current.get("task")
    if current_status == "running" and (current_task is None or not current_task.done()):
        if current.get("_internal") and not is_internal:
            if current_task and not current_task.done():
                current_task.cancel()
            current["status"] = "done"
            wake_msg = current.get("_wake_message", "")
            if wake_msg:
                pool = get_claude_pool()
                if pool:
                    proc = pool.get(session_id)
                    if proc:
                        proc.queue_notification(wake_msg)
            logger.info(f"User message preempted internal wake for {session_id} (run={current.get('run_id', '')})")
        else:
            # Mid-turn injection for Claude sessions — write to subprocess stdin
            if is_claude_session(session_id):
                pool = get_claude_pool()
                proc = pool.get(session_id) if pool else None
                if proc:
                    injected = await proc.inject_user_message(message)
                    if injected:
                        existing_queue = current.get("queue")
                        if existing_queue:
                            user_evt = {"type": "user_message", "text": message, "ts": int(time.time() * 1000)}
                            await _emit_event(session_id, existing_queue, user_evt, run_id=str(current.get("run_id", "")))
                        return JSONResponse({"ok": True, "injected": True, "run_id": str(current.get("run_id", ""))})
            # Non-Claude or injection failed — return 409 as before
            return JSONResponse(
                {
                    "error": "run_in_progress",
                    "run": {
                        "run_id": str(current.get("run_id") or ""),
                        "status": current_status,
                        "started": float(current.get("started") or now),
                        "last_event_at": float(current.get("last_event_at") or current.get("started") or now),
                    },
                },
                status_code=409,
            )

    queue: asyncio.Queue = asyncio.Queue()
    run_id = str(uuid.uuid4())
    _active_tasks[session_id] = {
        "status": "running",
        "started": now,
        "events": [],
        "next_seq": _session_event_store.peek_next_event_id(session_id),
        "last_event_at": now,
        "run_id": run_id,
        "queue": queue,
        "task": None,
    }
    _db_start_run(session_id, run_id, now)

    # Drain pending delegation notifications from DB inbox
    notif_ids, queued_notifs = notification_store.claim(session_id, limit=20)
    if queued_notifs:
        block = "\n\n".join(notification_store.render_notification(n) for n in queued_notifs)
        message = f"{block}\n\n{notification_store.DELEGATION_PREPEND_BOUNDARY}\n\n{message}"
        notification_store.mark_injected(notif_ids)
        logger.info(f"Prepended {len(queued_notifs)} delegation notification(s) to message for {session_id}")
    _active_tasks[session_id]["_notif_ids"] = notif_ids

    if not is_internal:
        user_evt = {"type": "user_message", "text": message, "ts": int(now * 1000)}
        await _emit_event(session_id, None, user_evt, run_id=run_id)

    if session_id.startswith("deleg-"):
        try:
            log_write(
                "chat", message[:5000],
                role="user", session_id=session_id,
                worker="delegation", source="dispatch_receive",
            )
            logger.info(f"Delegation dispatch received: session={session_id}, msg_len={len(message)}")
        except Exception as _lr_err:
            logger.warning(f"Failed to write delegation receive log for {session_id}: {_lr_err}")

    # Import providers lazily to avoid circular imports at module load
    from chat_providers.claude_provider import process_chat_claude
    from chat_providers.anthropic_provider import process_chat_anthropic
    from chat_providers.openrouter_provider import process_chat_openrouter
    from chat_providers.codex_provider import process_chat_codex

    # Route to provider-specific engine
    if is_claude_session(session_id):
        task = asyncio.create_task(process_chat_claude(
            queue, session_id, message, run_id,
            attachments=attachments, is_internal=is_internal,
            active_tasks=_active_tasks,
            runtime_config=state.runtime_config,
            app_state=state,
        ))
    elif is_anthropic_session(session_id):
        task = asyncio.create_task(process_chat_anthropic(
            queue, session_id, message, run_id,
            attachments=attachments,
            active_tasks=_active_tasks,
            runtime_config=state.runtime_config,
            last_api_usage=state.last_api_usage,
            anthropic_containers=state.anthropic_containers,
            app_state=state,
        ))
    elif is_openrouter_session(session_id):
        task = asyncio.create_task(process_chat_openrouter(
            queue, session_id, message, run_id,
            attachments=attachments,
            active_tasks=_active_tasks,
            runtime_config=state.runtime_config,
            last_api_usage=state.last_api_usage,
            openrouter_tools_unsupported_until=state.openrouter_tools_unsupported_until,
            app_state=state,
        ))
    else:
        task = asyncio.create_task(process_chat_codex(
            queue, session_id, message, run_id,
            attachments=attachments,
            active_tasks=_active_tasks,
            runtime_config=state.runtime_config,
            last_api_usage=state.last_api_usage,
            active_docs=state.active_docs,
            app_state=state,
        ))
    _active_tasks[session_id]["task"] = task

    # Import SSE keepalive interval from server
    import server as _srv
    _sse_keepalive = _srv._SSE_KEEPALIVE_SECONDS

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=float(_sse_keepalive))
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            pass

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/chat/cancel")
async def api_chat_cancel(session_id: str = "default"):
    # Import active_tasks from server since we need the live reference
    import server as _srv
    _active_tasks = _srv._active_tasks

    task = _active_tasks.get(session_id)
    if not task:
        return {"ok": True, "status": "idle"}

    run_id = str(task.get("run_id") or "")
    bg = task.get("task")
    queue = task.get("queue")

    if bg and not bg.done():
        bg.cancel()

    task["status"] = "cancelled"
    _active_tasks[session_id] = task

    cancel_evt = {"type": "done", "text": "⚠️ Cancelled.", "model": "system", "cancelled": True}
    await _emit_event(session_id, queue, cancel_evt, run_id=run_id)

    if run_id:
        _db_mark_run_done(run_id, "cancelled")

    if queue is not None:
        try:
            await queue.put(None)
        except Exception:
            pass

    # For Claude sessions: kill and restart the subprocess so it stops
    # thinking immediately and reloads context on next message
    restarted = False
    try:
        from claude_bridge import get_claude_pool
        from server_helpers import model_key_from_session
        mk = model_key_from_session(session_id)
        if mk in ("claude_opus", "claude_sonnet"):
            pool = get_claude_pool()
            if pool:
                proc = pool.get(session_id)
                if proc:
                    import logging
                    logging.getLogger("kukuibot.chat").info(
                        f"Cancel: restarting Claude subprocess for {session_id}"
                    )
                    await proc.restart()
                    restarted = True
    except Exception as e:
        import logging
        logging.getLogger("kukuibot.chat").warning(
            f"Cancel: failed to restart Claude subprocess for {session_id}: {e}"
        )

    return {"ok": True, "status": "cancelled", "run_id": run_id, "restarted": restarted}
