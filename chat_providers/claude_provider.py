"""
claude_provider.py — Claude Code persistent subprocess chat provider.

Extracted from server.py Phase 10a.
"""

import asyncio
import logging
import time

from auth import load_history, save_history
from claude_bridge import get_claude_pool
from routes.delegation import _check_delegation_completion
from routes.session_events import _emit_event, _db_mark_run_done
from server_helpers import (
    format_attachments_as_text,
    profile_limits,
    worker_identity_for_session,
    claude_model_for_session,
)
import notification_store

logger = logging.getLogger("kukuibot.chat_providers.claude")


async def process_chat_claude(
    queue,
    session_id: str,
    user_message: str,
    run_id: str,
    *,
    attachments: list[dict] | None = None,
    is_internal: bool = False,
    active_tasks: dict,
    runtime_config: dict,
    app_state=None,
):
    """Claude Code persistent subprocess provider.

    Uses a long-lived `claude --print` subprocess with stream-json I/O.
    Claude CLI handles its own tools natively (Bash, Read, Write, Edit, etc.).
    """
    t0 = time.time()
    full_text = ""
    got_result = False
    _stream_exception = None
    proc = None

    # Prepend attachment content as text (Claude CLI is text-only)
    if attachments:
        att_text = format_attachments_as_text(attachments)
        if att_text:
            user_message = f"{att_text}\n\n{user_message}"

    try:
        pool = get_claude_pool()
        if not pool:
            await _emit_event(session_id, queue, {"type": "error", "message": "Claude process pool not initialized."}, run_id=run_id)
            got_result = True
            return
        try:
            _wi = worker_identity_for_session(session_id)
            _cm = claude_model_for_session(session_id)
            proc = pool.get_or_create(session_id, worker_identity=_wi, model=_cm)
        except RuntimeError as e:
            error_msg = str(e) or type(e).__name__
            await _emit_event(session_id, queue, {"type": "error", "message": error_msg}, run_id=run_id)
            got_result = True
            # If this is a delegation session, mark the task as dispatch_failed
            if session_id.startswith("deleg-"):
                try:
                    from delegation import _deleg_db_connection
                    with _deleg_db_connection() as _pdb:
                        _pdb.execute(
                            "UPDATE delegated_tasks SET status='dispatch_failed', result_summary=?, updated_at=? "
                            "WHERE target_session_id=? AND status IN ('dispatched', 'running')",
                            (f"Pool full: {error_msg}", int(time.time()), session_id),
                        )
                        _pdb.commit()
                    logger.warning(f"Delegation dispatch_failed for {session_id}: {error_msg}")
                except Exception as deleg_err:
                    logger.warning(f"Failed to update delegation status for {session_id}: {deleg_err}")
            return

        thinking_started = False
        # State for incremental tool input accumulation (detail extraction from input_json_delta)
        _cur_tool_name = None
        _cur_tool_input_json = ""
        _cur_tool_detail_sent = False

        async for event in proc.send_message(user_message, inject_context=True, suppress_user_broadcast=is_internal):
            event_type = event.get("type", "")

            # Handle stream_event wrapper (streaming deltas from CLI)
            if event_type == "stream_event":
                inner = event.get("event", {})
                inner_type = inner.get("type", "")

                if inner_type == "content_block_start":
                    cb = inner.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        _cur_tool_name = cb.get("name", "tool")
                        _cur_tool_input_json = ""
                        _cur_tool_detail_sent = False
                        tool_input = cb.get("input", {})
                        detail = ""
                        if _cur_tool_name == "Bash" and tool_input.get("command"):
                            detail = tool_input["command"][:200]
                        elif _cur_tool_name in ("Read", "Write", "Edit") and tool_input.get("file_path"):
                            detail = tool_input["file_path"]
                        elif _cur_tool_name in ("Grep", "Glob") and tool_input.get("pattern"):
                            detail = tool_input["pattern"][:200]
                        elif _cur_tool_name in ("WebSearch", "WebFetch"):
                            detail = (tool_input.get("query") or tool_input.get("url") or "")[:200]
                        if detail:
                            _cur_tool_detail_sent = True
                        await _emit_event(session_id, queue, {"type": "tool_use", "name": _cur_tool_name, "input": detail}, run_id=run_id)
                    elif cb.get("type") == "thinking":
                        if not thinking_started:
                            await _emit_event(session_id, queue, {"type": "thinking_start"}, run_id=run_id)
                            thinking_started = True

                elif inner_type == "content_block_delta":
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_chunk = delta.get("text", "")
                        full_text += text_chunk
                        if not thinking_started:
                            await _emit_event(session_id, queue, {"type": "thinking_start"}, run_id=run_id)
                            thinking_started = True
                        await _emit_event(session_id, queue, {"type": "text", "text": text_chunk}, run_id=run_id)
                    elif delta.get("type") == "thinking_delta":
                        thinking_text = delta.get("thinking", "")
                        if thinking_text:
                            await _emit_event(session_id, queue, {"type": "thinking", "text": thinking_text}, run_id=run_id)
                    elif delta.get("type") == "input_json_delta" and _cur_tool_name and not _cur_tool_detail_sent:
                        _cur_tool_input_json += delta.get("partial_json", "")
                        # Try to extract detail from accumulated JSON fragments
                        detail = ""
                        raw = _cur_tool_input_json
                        if _cur_tool_name == "Bash":
                            idx = raw.find('"command"')
                            if idx >= 0:
                                val_start = raw.find('"', idx + 9 + 1)
                                if val_start >= 0:
                                    val_end = raw.find('"', val_start + 1)
                                    if val_end >= 0:
                                        detail = raw[val_start+1:val_end][:200]
                                    elif len(raw) - val_start > 10:
                                        detail = raw[val_start+1:][:200]
                        elif _cur_tool_name in ("Read", "Write", "Edit"):
                            idx = raw.find('"file_path"')
                            if idx >= 0:
                                val_start = raw.find('"', idx + 11 + 1)
                                if val_start >= 0:
                                    val_end = raw.find('"', val_start + 1)
                                    if val_end >= 0:
                                        detail = raw[val_start+1:val_end]
                        elif _cur_tool_name in ("Grep", "Glob"):
                            idx = raw.find('"pattern"')
                            if idx >= 0:
                                val_start = raw.find('"', idx + 9 + 1)
                                if val_start >= 0:
                                    val_end = raw.find('"', val_start + 1)
                                    if val_end >= 0:
                                        detail = raw[val_start+1:val_end][:200]
                        elif _cur_tool_name in ("WebSearch", "WebFetch"):
                            for key in ('"query"', '"url"', '"prompt"'):
                                idx = raw.find(key)
                                if idx >= 0:
                                    val_start = raw.find('"', idx + len(key) + 1)
                                    if val_start >= 0:
                                        val_end = raw.find('"', val_start + 1)
                                        if val_end >= 0:
                                            detail = raw[val_start+1:val_end][:200]
                                            break
                        if detail:
                            _cur_tool_detail_sent = True
                            await _emit_event(session_id, queue, {"type": "tool_use", "name": _cur_tool_name, "input": detail}, run_id=run_id)

                elif inner_type == "content_block_stop":
                    _cur_tool_name = None
                    _cur_tool_input_json = ""
                    _cur_tool_detail_sent = False

            elif event_type == "assistant":
                # Full assistant message (tool calls, text blocks)
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "tool")
                        tool_input = block.get("input", {})
                        detail = ""
                        if tool_name == "Bash" and tool_input.get("command"):
                            detail = tool_input["command"][:200]
                        elif tool_name in ("Read", "Write", "Edit") and tool_input.get("file_path"):
                            detail = tool_input["file_path"]
                        elif tool_name in ("Grep", "Glob") and tool_input.get("pattern"):
                            detail = tool_input["pattern"][:200]
                        elif tool_name in ("WebSearch", "WebFetch"):
                            detail = (tool_input.get("query") or tool_input.get("url") or "")[:200]
                        await _emit_event(session_id, queue, {"type": "tool_use", "name": tool_name, "input": detail}, run_id=run_id)

            elif event_type == "ping":
                await _emit_event(session_id, queue, {"type": "ping", "elapsed": event.get("elapsed", 0), "tool": event.get("tool", "working"), "detail": event.get("detail", "")}, run_id=run_id)

            elif event_type == "error":
                await _emit_event(session_id, queue, {"type": "error", "message": event.get("error", "Unknown error")}, run_id=run_id)

            elif event_type == "result":
                got_result = True
                result_text = event.get("result", full_text)
                result_subtype = event.get("subtype", "")
                duration_ms = int((time.time() - t0) * 1000)
                logger.info(f"Claude result event: session={session_id}, result_len={len(result_text)}, subtype={result_subtype}, full_text_len={len(full_text)}, duration={duration_ms}ms")

                # Surface auth errors as a clear user-facing error message
                if result_subtype == "auth_error":
                    await _emit_event(session_id, queue, {"type": "error", "message": result_text or "Claude authentication failed. OAuth token may be expired — check Settings > Auth."}, run_id=run_id)
                elif result_subtype == "process_died" and not result_text:
                    result_text = "⚠️ Claude process exited unexpectedly. Check server logs for details."

                # Save to history table so auto-name and other features can read Claude sessions
                try:
                    existing_items, _, _ = load_history(session_id)
                    existing_items.append({"role": "user", "content": user_message})
                    # Save full accumulated text for delegation result capture.
                    # result_text is only the final text block; full_text has everything.
                    history_text = full_text if full_text and len(full_text) > len(result_text or "") else result_text
                    existing_items.append({"role": "assistant", "content": history_text})
                    if len(existing_items) > 50:
                        existing_items = existing_items[-50:]
                    save_history(session_id, existing_items, last_api_usage={"provider": "claude-code", "model": "opus"})
                    logger.info(f"Claude history saved: {session_id} → {len(existing_items)} items")
                except Exception as he:
                    logger.warning(f"Failed to save Claude history for auto-name: {he}", exc_info=True)

                # Token count from persistent process
                profile, context_window, compaction_threshold = profile_limits(session_id)
                token_count = proc.last_input_tokens if proc.last_input_tokens > 0 else 0
                if token_count > 0:
                    pct = round(token_count / context_window, 4)
                    await _emit_event(session_id, queue, {"type": "context", "tokens": token_count, "max": context_window, "pct": pct, "source": "api"}, run_id=run_id)

                _model_label = f"claude-code ({proc.model})"
                # Mark task done BEFORE emitting the done event so the client's
                # drained queue message doesn't hit a 409 race window.
                _pre_task = active_tasks.get(session_id, {})
                _pre_task["status"] = "done"
                active_tasks[session_id] = _pre_task
                await _emit_event(session_id, queue, {"type": "done", "text": result_text, "model": _model_label, "duration_ms": duration_ms, "tokens": proc.last_input_tokens}, run_id=run_id)

                # Auto smart-compact for Claude when tokens exceed configured threshold
                auto_pct = runtime_config.get("claude_auto_compact_pct", "none")
                auto_threshold = int(context_window * int(auto_pct) / 100) if auto_pct != "none" else 0
                if auto_threshold > 0 and token_count > auto_threshold and not proc._compacting:
                    logger.info(f"Claude auto smart-compact triggered: {token_count:,} > {auto_threshold:,} ({auto_pct}%)")
                    await _emit_event(session_id, queue, {"type": "info", "message": f"Auto smart-compact triggered at {token_count:,} tokens ({auto_pct}%)..."}, run_id=run_id)
                    try:
                        result = await proc.smart_compact()
                        if result.get("status") == "ok":
                            await _emit_event(session_id, queue, {"type": "info", "message": f"Smart compact complete — context={result.get('summary_length', 0)} chars"}, run_id=run_id)
                            new_tokens = proc.last_input_tokens
                            if new_tokens > 0:
                                new_pct = round(new_tokens / context_window, 4)
                                await _emit_event(session_id, queue, {"type": "context", "tokens": new_tokens, "max": context_window, "pct": new_pct, "source": "api"}, run_id=run_id)
                        else:
                            logger.warning(f"Claude auto smart-compact failed: {result.get('error', 'unknown')}")
                    except Exception as ce:
                        logger.error(f"Claude auto smart-compact error: {ce}")

            elif event_type == "user_message":
                continue
            elif event_type == "user":
                continue  # CLI echo of injected/tool-result user message — already handled
            elif event_type == "context_loaded":
                await _emit_event(session_id, queue, {"type": "context_loaded", "loaded_files": event.get("loaded_files", [])}, run_id=run_id)
            elif event_type == "compaction":
                await _emit_event(session_id, queue, {
                    "type": "compaction",
                    "tokens": event.get("tokens", 0),
                    "active_docs": event.get("active_docs", []),
                    "loaded_files": event.get("loaded_files", []),
                }, run_id=run_id)
            elif event_type == "compaction_done":
                await _emit_event(session_id, queue, {
                    "type": "compaction_done",
                    "summary_length": event.get("summary_length"),
                    "compaction_count": event.get("compaction_count"),
                    "loaded_files": event.get("loaded_files", []),
                }, run_id=run_id)

    except asyncio.CancelledError:
        logger.info(f"Chat cancelled for {session_id}")
        return
    except Exception as e:
        _stream_exception = e
        logger.error(f"Claude persistent stream error: {e}", exc_info=True)
        await _emit_event(session_id, queue, {"type": "error", "message": str(e) or type(e).__name__}, run_id=run_id)
    finally:
        # Mark task done early so the client's drained queue doesn't hit a 409
        task = active_tasks.get(session_id, {})
        task["status"] = "done"
        active_tasks[session_id] = task

        # Always emit a done event so the frontend exits loading state
        if not got_result:
            duration_ms = int((time.time() - t0) * 1000)
            _proc_auth_err = getattr(proc, '_last_auth_error', None) if proc else None
            if _proc_auth_err:
                error_text = f"⚠️ Authentication failed: {_proc_auth_err}"
            elif _stream_exception:
                error_text = f"⚠️ {_stream_exception}"
            else:
                error_text = full_text or "⚠️ No response from Claude process (timed out or process died)."
            logger.warning(f"Claude stream ended without result: session={session_id}, full_text_len={len(full_text)}, duration={duration_ms}ms")
            _fallback_model = f"claude-code ({claude_model_for_session(session_id)})"
            await _emit_event(session_id, queue, {"type": "done", "text": error_text, "model": _fallback_model, "duration_ms": duration_ms, "tokens": 0}, run_id=run_id)

            try:
                existing_items, _, _ = load_history(session_id)
                existing_items.append({"role": "user", "content": user_message})
                existing_items.append({"role": "assistant", "content": error_text})
                if len(existing_items) > 50:
                    existing_items = existing_items[-50:]
                save_history(session_id, existing_items, last_api_usage={"provider": "claude-code", "model": "opus", "error": "timeout"})
                logger.info(f"Claude timeout history saved: {session_id} → {len(existing_items)} items")
            except Exception as he:
                logger.warning(f"Failed to save Claude timeout history: {he}")
        _db_mark_run_done(run_id, "done")
        await queue.put(None)

        # Mark injected notifications as consumed now that the model has processed them
        _consumed_ids = task.get("_notif_ids", [])
        if _consumed_ids:
            notification_store.mark_consumed(_consumed_ids)

        # Trigger event-driven dispatcher — subprocess is now idle
        if app_state and app_state.notification_dispatcher:
            app_state.notification_dispatcher.trigger_subprocess_idle(session_id)

        # Direct completion hook — detect TASK_DONE immediately for deleg-* sessions
        await _check_delegation_completion(session_id)
