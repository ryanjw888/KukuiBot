"""
anthropic_provider.py — Anthropic Messages API direct chat provider.

Extracted from server.py Phase 10a.
"""

import asyncio
import json
import logging
import os
import time

from auth import get_config, load_history, save_history
from chat_providers import run_with_keepalive
from config import MAX_TOOL_ROUNDS
from anthropic_bridge import (
    anthropic_stream,
    convert_tools_to_anthropic,
    convert_history_to_anthropic,
    ANTHROPIC_MODELS,
    thinking_params,
)
from routes.delegation import _check_delegation_completion
from routes.session_events import _emit_event, _db_mark_run_done
from server_helpers import (
    attachment_summary,
    build_anthropic_attachment_blocks,
    model_key_from_session,
    profile_limits,
    resolve_profile,
    worker_identity_for_session,
)
from tools import TOOL_DEFINITIONS, execute_tool
import notification_store

logger = logging.getLogger("kukuibot.chat_providers.anthropic")

TOOL_ROUND_LIMIT = int(os.environ.get("KUKUIBOT_MAX_TOOL_ROUNDS", str(MAX_TOOL_ROUNDS)))


def _anthropic_api_key() -> str:
    """Get Anthropic API key — check dedicated key first, then fall back to Claude Code key."""
    dedicated = (get_config("anthropic.api_key", "") or "").strip()
    if dedicated:
        return dedicated
    key = (get_config("claude_code.api_key", "") or "").strip()
    if key:
        return key
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _anthropic_model(session_id: str) -> str:
    """Resolve Anthropic model for a session."""
    model = (get_config(f"anthropic.session_model.{session_id}", "") or "").strip()
    if model:
        return model
    profile = resolve_profile(session_id)
    from server_helpers import MODEL_PROFILES
    cfg = MODEL_PROFILES.get(profile, MODEL_PROFILES.get("anthropic", {}))
    return cfg.get("api_model", "claude-sonnet-4-5-20250929")


def _is_claude_auth_failure(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return False
    markers = [
        "invalid api key", "invalid bearer token", "not logged in",
        "please run /login", "authentication_failed", "unauthorized",
        "forbidden", "token expired", "expired token", "fix external api key",
    ]
    return any(m in s for m in markers)


async def process_chat_anthropic(
    queue,
    session_id: str,
    user_message: str,
    run_id: str,
    *,
    attachments: list[dict] | None = None,
    active_tasks: dict,
    runtime_config: dict,
    last_api_usage: dict,
    anthropic_containers: dict,
    app_state=None,
):
    """Anthropic Messages API provider — direct API with native tool-calling."""
    items = []
    usage_info = {}
    model = ""
    try:
        api_key = _anthropic_api_key()
        if not api_key:
            await _emit_event(session_id, queue, {"type": "error", "message": "No Anthropic API key configured. Set ANTHROPIC_API_KEY or add it in Settings."}, run_id=run_id)
            return

        model = _anthropic_model(session_id)
        items, _, _ = load_history(session_id)
        instructions = _get_system_prompt_via_server(model_key=model_key_from_session(session_id), worker_identity=worker_identity_for_session(session_id))

        system_blocks, messages = convert_history_to_anthropic(items, instructions)

        user_blocks: list[dict] = []
        if attachments:
            user_blocks.extend(build_anthropic_attachment_blocks(attachments))
        user_blocks.append({"type": "text", "text": user_message.strip()})
        messages.append({"role": "user", "content": user_blocks})
        hist_content = user_message
        if attachments:
            hist_content = f"{attachment_summary(attachments)}\n{user_message}"
        items.append({"role": "user", "content": hist_content})
        _append_to_chat_log(session_id, "user", hist_content)

        advanced_tools = get_config("anthropic.advanced_tools", "0") == "1"
        anthropic_tools = convert_tools_to_anthropic(TOOL_DEFINITIONS, advanced_tools=advanced_tools)

        reasoning_effort = (get_config("anthropic.reasoning_effort", "") or "").strip() or None
        thinking = thinking_params(reasoning_effort)

        model_info = ANTHROPIC_MODELS.get(model, {})
        max_tokens = model_info.get("max_output_tokens", 8192)

        container_id = anthropic_containers.get(session_id) if advanced_tools else None

        await _emit_event(session_id, queue, {"type": "thinking_start"}, run_id=run_id)

        final_text = ""
        usage_info = {}

        for round_idx in range(TOOL_ROUND_LIMIT):
            if round_idx > 0:
                await _emit_event(session_id, queue, {"type": "ping", "round": round_idx}, run_id=run_id)

            round_text = ""
            tool_calls: list[dict] = []
            server_tool_uses: list[dict] = []
            code_exec_results: list[dict] = []
            stop_reason = ""

            async for evt in anthropic_stream(
                messages, system_blocks,
                model=model, api_key=api_key, max_tokens=max_tokens,
                tools=anthropic_tools, temperature=0.7,
                thinking=thinking,
                container=container_id,
            ):
                evt_type = evt.get("type", "")

                if evt_type == "text":
                    chunk = evt.get("text", "")
                    if chunk:
                        round_text += chunk
                        await _emit_event(session_id, queue, {"type": "text", "text": chunk}, run_id=run_id)

                elif evt_type == "thinking_start":
                    await _emit_event(session_id, queue, {"type": "thinking_start"}, run_id=run_id)

                elif evt_type == "thinking":
                    await _emit_event(session_id, queue, {"type": "thinking", "text": evt.get("text", "")}, run_id=run_id)

                elif evt_type == "server_tool_use_start":
                    server_tool_uses.append({
                        "id": evt.get("id", ""),
                        "name": evt.get("name", "code_execution"),
                    })
                    await _emit_event(session_id, queue, {
                        "type": "tool_use",
                        "name": "code_execution (server-side)",
                        "input": "(executing on Anthropic servers...)",
                    }, run_id=run_id)

                elif evt_type == "tool_use_start":
                    caller = evt.get("caller")
                    caller_type = (caller or {}).get("type") if isinstance(caller, dict) else ""
                    label = evt.get("name", "tool")
                    if caller_type == "code_execution_20260120":
                        label = f"{label} (programmatic)"
                    await _emit_event(session_id, queue, {"type": "tool_use", "name": label, "input": "(preparing...)"}, run_id=run_id)

                elif evt_type == "tool_use_done":
                    tc_entry: dict = {
                        "id": evt.get("id", ""),
                        "name": evt.get("name", ""),
                        "input": evt.get("input", {}),
                    }
                    if evt.get("caller"):
                        tc_entry["caller"] = evt["caller"]
                    tool_calls.append(tc_entry)

                elif evt_type == "code_exec_result":
                    stdout = evt.get("stdout", "")
                    stderr = evt.get("stderr", "")
                    rc = evt.get("return_code", -1)
                    code_exec_results.append(evt)
                    display = stdout[:500] if stdout else ""
                    if stderr:
                        display += f"\nSTDERR: {stderr[:200]}"
                    if rc != 0:
                        display += f"\n[exit code: {rc}]"
                    if display.strip():
                        await _emit_event(session_id, queue, {"type": "text", "text": f"\n```\n{display.strip()}\n```\n"}, run_id=run_id)

                elif evt_type == "container_info":
                    cid = evt.get("id", "")
                    if cid:
                        anthropic_containers[session_id] = cid
                        container_id = cid

                elif evt_type == "message_start":
                    u = evt.get("usage", {})
                    if u:
                        usage_info = {
                            "provider": "anthropic",
                            "model": model,
                            "input_tokens": u.get("input_tokens", 0),
                            "output_tokens": 0,
                            "total_tokens": u.get("input_tokens", 0),
                            "cached_tokens": u.get("cache_read_input_tokens", 0),
                            "cache_creation_tokens": u.get("cache_creation_input_tokens", 0),
                            "est_input_tokens": _estimate_total_context(items),
                            "captured_at": int(time.time()),
                            "profile": resolve_profile(session_id),
                        }

                elif evt_type == "message_delta":
                    stop_reason = evt.get("stop_reason", "")
                    u = evt.get("usage", {})
                    if u:
                        out_tok = u.get("output_tokens", 0)
                        usage_info["output_tokens"] = out_tok
                        usage_info["total_tokens"] = usage_info.get("input_tokens", 0) + out_tok
                        usage_info["captured_at"] = int(time.time())
                        last_api_usage[session_id] = usage_info

                elif evt_type == "error":
                    err_msg = evt.get("message", "Unknown error")
                    if _is_claude_auth_failure(err_msg):
                        await _emit_event(session_id, queue, {"type": "error", "message": "Anthropic auth failed — check API key."}, run_id=run_id)
                        return
                    await _emit_event(session_id, queue, {"type": "error", "message": f"Anthropic: {err_msg}"}, run_id=run_id)
                    return

            # Handle pause_turn
            if stop_reason == "pause_turn":
                assistant_blocks: list[dict] = []
                if round_text.strip():
                    assistant_blocks.append({"type": "text", "text": round_text})
                for stu in server_tool_uses:
                    assistant_blocks.append({"type": "server_tool_use", "id": stu["id"], "name": stu["name"], "input": {}})
                for tc in tool_calls:
                    tc_block: dict = {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
                    if tc.get("caller"):
                        tc_block["caller"] = tc["caller"]
                    assistant_blocks.append(tc_block)
                if assistant_blocks:
                    messages.append({"role": "assistant", "content": assistant_blocks})
                user_blocks_pt: list[dict] = []
                for cer in code_exec_results:
                    user_blocks_pt.append({
                        "type": cer.get("result_type", "code_execution_tool_result"),
                        "tool_use_id": cer.get("tool_use_id", ""),
                        "content": {
                            "type": cer.get("result_type", "code_execution_result").replace("_tool_result", "_result"),
                            "stdout": cer.get("stdout", ""),
                            "stderr": cer.get("stderr", ""),
                            "return_code": cer.get("return_code", 0),
                        },
                    })
                for tc in tool_calls:
                    tool_name = tc["name"]
                    tool_input = tc["input"]
                    tool_id = tc["id"]
                    caller_type = ((tc.get("caller") or {}).get("type") if isinstance(tc.get("caller"), dict) else "")
                    is_programmatic = caller_type == "code_execution_20260120"
                    await _emit_event(session_id, queue, {"type": "tool_use", "name": tool_name, "input": json.dumps(tool_input)[:200]}, run_id=run_id)
                    _track_tool_file(session_id, tool_name, tool_input)
                    result = await run_with_keepalive(
                        asyncio.get_event_loop().run_in_executor(None, execute_tool, tool_name, tool_input, None, session_id),
                        session_id, queue, run_id, emit_event=_emit_event,
                    )
                    result_str = str(result)
                    if len(result_str) > 30000:
                        result_str = result_str[:30000] + f"\n... (truncated from {len(result_str)} chars)"
                    user_blocks_pt.append({"type": "tool_result", "tool_use_id": tool_id, "content": result_str})
                    if not is_programmatic:
                        items.append({"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": result_str})
                if user_blocks_pt:
                    messages.append({"role": "user", "content": user_blocks_pt})
                logger.info(f"Anthropic pause_turn — continuing round {round_idx + 1} (session={session_id})")
                continue

            # Separate direct vs programmatic tool calls
            direct_calls = [
                tc for tc in tool_calls
                if not (isinstance(tc.get("caller"), dict) and (tc.get("caller") or {}).get("type") == "code_execution_20260120")
            ]
            programmatic_calls = [
                tc for tc in tool_calls
                if isinstance(tc.get("caller"), dict) and (tc.get("caller") or {}).get("type") == "code_execution_20260120"
            ]

            if not direct_calls and not programmatic_calls:
                final_text = round_text
                if not final_text.strip() and round_idx > 0:
                    messages.append({"role": "user", "content": [{"type": "text", "text": "Please summarize the results above."}]})
                    continue
                break

            # Build assistant message
            assistant_blocks = []
            if round_text.strip():
                assistant_blocks.append({"type": "text", "text": round_text})
            for stu in server_tool_uses:
                assistant_blocks.append({"type": "server_tool_use", "id": stu["id"], "name": stu["name"], "input": {}})
            for tc in tool_calls:
                tc_block = {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
                if tc.get("caller"):
                    tc_block["caller"] = tc["caller"]
                assistant_blocks.append(tc_block)
            messages.append({"role": "assistant", "content": assistant_blocks})

            assistant_hist: dict = {"role": "assistant", "content": round_text or ""}
            if direct_calls:
                assistant_hist["tool_calls"] = [
                    {"id": tc["id"], "function": {"name": tc["name"], "arguments": json.dumps(tc["input"])}}
                    for tc in direct_calls
                ]
            items.append(assistant_hist)

            # Execute tool calls
            tool_result_blocks: list[dict] = []

            for cer in code_exec_results:
                tool_result_blocks.append({
                    "type": cer.get("result_type", "code_execution_tool_result"),
                    "tool_use_id": cer.get("tool_use_id", ""),
                    "content": {
                        "type": cer.get("result_type", "code_execution_result").replace("_tool_result", "_result"),
                        "stdout": cer.get("stdout", ""),
                        "stderr": cer.get("stderr", ""),
                        "return_code": cer.get("return_code", 0),
                    },
                })

            for tc in tool_calls:
                tool_name = tc["name"]
                tool_input = tc["input"]
                tool_id = tc["id"]
                caller_type = ((tc.get("caller") or {}).get("type") if isinstance(tc.get("caller"), dict) else "")
                is_programmatic = caller_type == "code_execution_20260120"

                await _emit_event(session_id, queue, {"type": "tool_use", "name": tool_name, "input": json.dumps(tool_input)[:200]}, run_id=run_id)
                _track_tool_file(session_id, tool_name, tool_input)

                result = await run_with_keepalive(
                    asyncio.get_event_loop().run_in_executor(None, execute_tool, tool_name, tool_input, None, session_id),
                    session_id, queue, run_id, emit_event=_emit_event,
                )

                if not is_programmatic and isinstance(result, str) and result.startswith("ELEVATION_REQUIRED:"):
                    parts = result.split(":", 2)
                    elev_id = parts[1] if len(parts) > 1 else ""
                    reason = parts[2] if len(parts) > 2 else "Restricted action"
                    await _emit_event(session_id, queue, {
                        "type": "elevation_required",
                        "request_id": elev_id,
                        "tool_name": tool_name,
                        "reason": reason,
                        "input": json.dumps(tool_input)[:300],
                    }, run_id=run_id)

                    approved = False
                    for _ in range(60):
                        await asyncio.sleep(1)
                        from security import _lock, _approve_all_sessions, _approved, _requests
                        with _lock:
                            if session_id in _approve_all_sessions and elev_id in _requests:
                                _approved[elev_id] = True
                            if elev_id in _approved:
                                approved = True
                                break
                            if elev_id not in _requests:
                                break

                    if approved:
                        await _emit_event(session_id, queue, {"type": "elevation_approved", "request_id": elev_id}, run_id=run_id)
                        result = await run_with_keepalive(
                            asyncio.get_event_loop().run_in_executor(None, execute_tool, tool_name, tool_input, elev_id, session_id),
                            session_id, queue, run_id, emit_event=_emit_event,
                        )
                    else:
                        result = "BLOCKED: Elevation denied or timed out."

                result_str = str(result)
                if len(result_str) > 30000:
                    result_str = result_str[:30000] + f"\n... (truncated from {len(result_str)} chars)"

                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_str,
                })

                if not is_programmatic:
                    items.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "name": tool_name,
                        "content": result_str,
                    })

            messages.append({"role": "user", "content": tool_result_blocks})
            save_history(session_id, items, last_api_usage=usage_info or {"provider": "anthropic", "model": model})

        if not final_text.strip():
            save_history(session_id, items, last_api_usage=usage_info or {"provider": "anthropic", "model": model})
            await _emit_event(session_id, queue, {"type": "error", "message": "Anthropic returned empty response."}, run_id=run_id)
            return

        items.append({"role": "assistant", "content": final_text})
        _append_to_chat_log(session_id, "assistant", final_text)
        if usage_info:
            usage_info["est_input_tokens"] = _estimate_total_context(items)
        save_history(session_id, items, last_api_usage=usage_info or {"provider": "anthropic", "model": model})

        profile, context_window, _ = profile_limits(session_id)
        token_count, token_source = _effective_context_tokens(items, usage_info or last_api_usage.get(session_id, {}))
        await _emit_event(session_id, queue, {"type": "context", "tokens": token_count, "max": context_window, "pct": round(token_count / context_window, 4), "source": token_source}, run_id=run_id)

        # Mark task done BEFORE emitting the done event so the client's
        # drained queue message doesn't hit a 409 race window.
        _pre_task = active_tasks.get(session_id, {})
        _pre_task["status"] = "done"
        active_tasks[session_id] = _pre_task
        await _emit_event(session_id, queue, {"type": "done", "text": final_text, "model": f"anthropic ({model})"}, run_id=run_id)

    except asyncio.CancelledError:
        logger.info(f"Chat cancelled for {session_id}")
        return
    except Exception as e:
        logger.error(f"Anthropic stream error: {e}", exc_info=True)
        try:
            if items:
                save_history(session_id, items, last_api_usage=usage_info or {"provider": "anthropic", "model": model})
        except Exception:
            pass
        err_msg = str(e).strip() or f"{type(e).__name__}: connection to Anthropic API failed"
        await _emit_event(session_id, queue, {"type": "error", "message": err_msg}, run_id=run_id)
    finally:
        task = active_tasks.get(session_id, {})
        task["status"] = "done"
        active_tasks[session_id] = task
        _db_mark_run_done(run_id, "done")
        await queue.put(None)
        _consumed_ids = task.get("_notif_ids", [])
        if _consumed_ids:
            notification_store.mark_consumed(_consumed_ids)
        if app_state and app_state.notification_dispatcher:
            app_state.notification_dispatcher.trigger_subprocess_idle(session_id)
        await _check_delegation_completion(session_id)


# --- Thin wrappers for server.py functions ---

def _get_system_prompt_via_server(model_key: str = "", worker_identity: str = "") -> str:
    import server as _srv
    return _srv._get_system_prompt(model_key=model_key, worker_identity=worker_identity)

def _estimate_total_context(items: list) -> int:
    import server as _srv
    return _srv._estimate_total_context(items)

def _effective_context_tokens(items: list, usage: dict | None = None) -> tuple[int, str]:
    import server as _srv
    return _srv._effective_context_tokens(items, usage)

def _append_to_chat_log(session_id: str, role: str, content: str):
    import server as _srv
    _srv._append_to_chat_log(session_id, role, content)

def _track_tool_file(session_id: str, tool_name: str, args: dict):
    import server as _srv
    _srv._track_tool_file(session_id, tool_name, args)
