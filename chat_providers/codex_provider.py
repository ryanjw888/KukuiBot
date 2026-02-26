"""
codex_provider.py — OpenAI Codex/GPT chat provider (Responses API).

Extracted from server.py Phase 10a. This is the default provider for
sessions that aren't Claude, Anthropic, or OpenRouter.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid

from auth import (
    extract_account_id,
    get_config,
    get_token,
    import_from_legacy,
    load_history,
    save_history,
)
from config import KUKUIBOT_API_URL, KUKUIBOT_USER_AGENT, MAX_TOOL_ROUNDS, MODEL
from routes.delegation import _check_delegation_completion
from routes.session_events import _emit_event, _db_mark_run_done
from server_helpers import (
    MODEL_PROFILES,
    attachment_summary,
    build_codex_attachment_items,
    extract_web_links_from_tool_output,
    model_key_from_session,
    profile_limits,
    repair_tool_items,
    response_has_links,
    worker_identity_for_session,
)
from tools import TOOL_DEFINITIONS, execute_tool
import notification_store

logger = logging.getLogger("kukuibot.chat_providers.codex")

TOOL_ROUND_LIMIT = int(os.environ.get("KUKUIBOT_MAX_TOOL_ROUNDS", str(MAX_TOOL_ROUNDS)))


def _build_headers(token: str, account_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "pi",
        "User-Agent": KUKUIBOT_USER_AGENT,
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


def _do_request(token, account_id, headers, instructions, items, tools, tool_choice="auto", session_id=None, model_name=None):
    import requests as http_requests
    runtime_config = _get_runtime_config()
    effort = runtime_config["reasoning_effort"]
    body = {
        "model": MODEL,
        "store": False,
        "stream": True,
        "instructions": instructions,
        "input": items,
        "tool_choice": tool_choice,
        "parallel_tool_calls": True,
        "text": {"verbosity": "high"},
    }
    if effort != "none":
        body["reasoning"] = {"effort": effort, "summary": "detailed"}
        body["include"] = ["reasoning.encrypted_content"]
    if session_id:
        body["prompt_cache_key"] = session_id
    if tools:
        body["tools"] = tools
    # timeout=(connect, read): 30s to connect, 120s between SSE chunks
    return http_requests.post(KUKUIBOT_API_URL, headers=headers, json=body, timeout=(30, 120), stream=True)


def _parse_sse(response):
    for line in response.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except (json.JSONDecodeError, ValueError):
            continue


async def process_chat_codex(
    queue,
    session_id: str,
    user_message: str,
    run_id: str,
    *,
    attachments: list[dict] | None = None,
    active_tasks: dict,
    runtime_config: dict,
    last_api_usage: dict,
    active_docs: dict,
    app_state=None,
):
    """Codex/OpenAI provider (Responses API) — background coroutine, survives disconnect."""
    items = []
    usage_info = {}
    try:
        token = get_token()
        if not token:
            if import_from_legacy():
                token = get_token()
            if not token:
                await _emit_event(session_id, queue, {"type": "error", "message": "No OpenAI API key configured. This tab uses Codex (OpenAI) — add an API key in Settings, or create a Claude tab instead."}, run_id=run_id)
                return

        account_id = extract_account_id(token)
        if not account_id:
            await _emit_event(session_id, queue, {"type": "error", "message": "Failed to extract account ID from token."}, run_id=run_id)
            return

        headers = _build_headers(token, account_id)
        profile, context_window, compaction_threshold = profile_limits(session_id)
        model_name = MODEL_PROFILES[profile]["api_model"]
        items, _, _ = load_history(session_id)
        logger.info(f"[{session_id}] Starting chat: {len(items)} history items, profile={profile}, model={model_name}")

        if attachments:
            att_parts = build_codex_attachment_items(attachments)
            att_parts.append({"type": "input_text", "text": user_message})
            items.append({"role": "user", "content": att_parts})
            hist_content = f"{attachment_summary(attachments)}\n{user_message}"
            _append_to_chat_log(session_id, "user", hist_content)
        else:
            items.append({"role": "user", "content": user_message})
            _append_to_chat_log(session_id, "user", user_message)

        instructions = _get_system_prompt_via_server(model_key=model_key_from_session(session_id), worker_identity=worker_identity_for_session(session_id))

        est, _ = _effective_context_tokens(items, last_api_usage.get(session_id, {}))
        if est > compaction_threshold:
            await _emit_event(session_id, queue, {"type": "text", "text": "⏳ Compacting conversation history..."}, run_id=run_id)
            from compaction import compact_messages

            _mk = model_key_from_session(session_id)
            _wi = worker_identity_for_session(session_id)
            items = await asyncio.get_event_loop().run_in_executor(
                None, lambda: compact_messages(items, session_id=session_id, model_key=_mk, worker_identity=_wi),
            )
            active_docs.pop(session_id, None)
            last_api_usage.pop(session_id, None)
            new_est = _estimate_total_context(items)
            await _emit_event(session_id, queue, {"type": "text", "text": f" Done! (~{est:,} → ~{new_est:,} tokens)\n\n"}, run_id=run_id)

        items = repair_tool_items(items)

        usage_info = {}
        tool_call_count = 0
        _force_tool_next = False
        web_search_used = False
        web_search_links: list[str] = []
        explicit_web_patterns = [
            r"\buse\s+web_search_ddg\b", r"\bweb\s+search\b", r"\bsearch\s+the\s+web\b",
            r"\blook\s+up\s+online\b", r"\bfind\s+sources\b", r"\bcitations?\b",
            r"\bsearch\b", r"\blook\s+up\b", r"\bfind\b",
        ]
        lower_msg = user_message.lower()
        looks_like_question = "?" in user_message or bool(re.match(r"^(what|when|where|who|why|how|is|are|can|could|should|do|does|did)\b", lower_msg.strip()))
        explicit_web = any(re.search(p, lower_msg) for p in explicit_web_patterns)
        web_allowed = True
        should_force_web = looks_like_question or explicit_web
        web_force_used = False

        for round_num in range(TOOL_ROUND_LIMIT):
            try:
                full_text = ""
                tool_calls = []
                current_call_id = current_call_name = None
                current_call_args = ""

                tc = "required" if _force_tool_next else "auto"
                _force_tool_next = False
                if should_force_web and not web_force_used and not web_search_used and round_num == 0:
                    tc = "required"

                if should_force_web and not web_force_used and not web_search_used and round_num == 0:
                    items.append({
                        "role": "user",
                        "content": "Use web_search_ddg now for this request before finalizing. Then answer and include sources links."
                    })

                response = await asyncio.get_event_loop().run_in_executor(
                    None, _do_request, token, account_id, headers, instructions, items, TOOL_DEFINITIONS, tc, session_id, model_name,
                )

                logger.info(f"[{session_id}] API response: status={response.status_code}, round={round_num}")
                if not response.ok:
                    error_text = response.text
                    logger.warning(f"[{session_id}] API error: {response.status_code} — {error_text[:300]}")
                    if response.status_code == 429:
                        await _emit_event(session_id, queue, {"type": "error", "message": "Rate limited — try again shortly."}, run_id=run_id)
                    elif response.status_code in (401, 403):
                        await _emit_event(session_id, queue, {"type": "error", "message": "Auth failed — token may need refresh."}, run_id=run_id)
                    else:
                        await _emit_event(session_id, queue, {"type": "error", "message": f"API error {response.status_code}: {error_text[:200]}"}, run_id=run_id)
                    break

                for evt in _parse_sse(response):
                    evt_type = evt.get("type", "")

                    if evt_type == "response.output_text.delta":
                        delta = evt.get("delta", "")
                        if delta:
                            full_text += delta
                            await _emit_event(session_id, queue, {"type": "text", "text": delta}, run_id=run_id)

                    elif evt_type == "response.reasoning.delta":
                        delta = evt.get("delta", "")
                        if delta:
                            await _emit_event(session_id, queue, {"type": "thinking", "text": delta}, run_id=run_id)

                    elif evt_type in ("response.reasoning_summary.delta", "response.reasoning_summary_text.delta"):
                        delta = evt.get("delta", "") or evt.get("text", "") or evt.get("content", "")
                        if delta:
                            await _emit_event(session_id, queue, {"type": "thinking_summary", "text": delta}, run_id=run_id)

                    elif evt_type == "response.output_item.added":
                        item = evt.get("item", {})
                        if item.get("type") == "function_call":
                            current_call_id = item.get("call_id", str(uuid.uuid4()))
                            current_call_name = item.get("name", "unknown")
                            current_call_args = ""
                            await _emit_event(session_id, queue, {"type": "tool_use", "name": current_call_name, "input": "(preparing...)"}, run_id=run_id)
                        elif item.get("type") == "reasoning":
                            await _emit_event(session_id, queue, {"type": "thinking_start"}, run_id=run_id)

                    elif evt_type == "response.function_call_arguments.delta":
                        current_call_args += evt.get("delta", "")

                    elif evt_type == "response.output_item.done":
                        item = evt.get("item", {})
                        if item.get("type") == "function_call":
                            tool_calls.append({
                                "call_id": item.get("call_id", current_call_id),
                                "name": item.get("name", current_call_name),
                                "arguments": item.get("arguments", current_call_args),
                            })
                            current_call_id = current_call_name = None
                            current_call_args = ""

                    elif evt_type == "response.completed":
                        u = evt.get("response", {}).get("usage", {})
                        est_input = _estimate_total_context(items)
                        usage_info = {
                            "input_tokens": u.get("input_tokens", 0),
                            "output_tokens": u.get("output_tokens", 0),
                            "total_tokens": u.get("total_tokens", 0),
                            "cached_tokens": (u.get("input_tokens_details", {}) or {}).get("cached_tokens", 0),
                            "reasoning_tokens": (u.get("output_tokens_details", {}) or {}).get("reasoning_tokens", 0),
                            "est_input_tokens": est_input,
                            "captured_at": int(time.time()),
                            "profile": profile,
                            "model": model_name,
                        }
                        last_api_usage[session_id] = usage_info
                        est_now = _estimate_total_context(items)
                        eff_now, eff_src = _effective_context_tokens(items, usage_info)
                        _log_token_drift(session_id, usage_info, est_input, est_now, eff_now, eff_src)

                    elif evt_type == "error":
                        msg = evt.get("message", "") or str(evt)
                        await _emit_event(session_id, queue, {"type": "error", "message": f"KukuiBot error: {msg}"}, run_id=run_id)

                    elif evt_type == "response.failed":
                        msg = evt.get("response", {}).get("error", {}).get("message", "Response failed")
                        await _emit_event(session_id, queue, {"type": "error", "message": msg}, run_id=run_id)

                if round_num >= TOOL_ROUND_LIMIT - 5:
                    near_msg = f"Approaching safety cap ({round_num+1}/{TOOL_ROUND_LIMIT} rounds). Finish and summarize."
                    items.append({"role": "user", "content": near_msg})

                if not tool_calls:
                    if full_text:
                        if web_search_used and not response_has_links(full_text):
                            deduped = []
                            seen = set()
                            for u in web_search_links:
                                if u in seen:
                                    continue
                                seen.add(u)
                                deduped.append(u)
                            links = deduped[:5]

                            if links:
                                auto_sources = "\n".join(f"- {u}" for u in links)
                                suffix = "\n\nSources\n" + auto_sources
                                full_text = full_text.rstrip() + suffix
                                await _emit_event(session_id, queue, {"type": "text", "text": suffix}, run_id=run_id)
                            else:
                                reminder = (
                                    "Add a Sources section now using the web_search_ddg links. "
                                    "Keep your prior answer concise and append 3-5 clickable links."
                                )
                                items.append({"role": "assistant", "content": full_text})
                                items.append({"role": "user", "content": reminder})
                                _force_tool_next = False
                                continue

                        items.append({"role": "assistant", "content": full_text})
                    break

                # Execute tools
                for tc_item in tool_calls:
                    tool_call_count += 1
                    try:
                        parsed_args = json.loads(tc_item["arguments"]) if isinstance(tc_item["arguments"], str) else tc_item["arguments"]
                    except (json.JSONDecodeError, TypeError):
                        parsed_args = {}

                    items.append({"type": "function_call", "call_id": tc_item["call_id"], "name": tc_item["name"], "arguments": tc_item["arguments"] if isinstance(tc_item["arguments"], str) else json.dumps(tc_item["arguments"])})
                    await _emit_event(session_id, queue, {"type": "tool_use", "name": tc_item["name"], "input": json.dumps(parsed_args)[:200]}, run_id=run_id)
                    _track_tool_file(session_id, tc_item["name"], parsed_args)

                    if tc_item["name"] == "web_search_ddg" and not web_allowed:
                        result = "ERROR: web_search_ddg not allowed for this request unless user explicitly asks for web search/sources."
                    else:
                        result = await asyncio.get_event_loop().run_in_executor(None, execute_tool, tc_item["name"], parsed_args, None, session_id)

                    if tc_item["name"] == "web_search_ddg" and not str(result).startswith("ERROR:"):
                        web_search_used = True
                        web_force_used = True
                        web_search_links.extend(extract_web_links_from_tool_output(result))

                    # Handle elevation
                    if isinstance(result, str) and result.startswith("ELEVATION_REQUIRED:"):
                        parts = result.split(":", 2)
                        elev_id = parts[1]
                        reason = parts[2] if len(parts) > 2 else "Restricted action"
                        await _emit_event(session_id, queue, {"type": "elevation_required", "request_id": elev_id, "tool_name": tc_item["name"], "reason": reason, "input": json.dumps(parsed_args)[:300]}, run_id=run_id)

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
                            if tc_item["name"] == "web_search_ddg" and not web_allowed:
                                result = "ERROR: web_search_ddg not allowed for this request unless user explicitly asks for web search/sources."
                            else:
                                result = await asyncio.get_event_loop().run_in_executor(None, execute_tool, tc_item["name"], parsed_args, elev_id, session_id)
                            if tc_item["name"] == "web_search_ddg" and not str(result).startswith("ERROR:"):
                                web_search_used = True
                                web_force_used = True
                                web_search_links.extend(extract_web_links_from_tool_output(result))
                        else:
                            await _emit_event(session_id, queue, {"type": "elevation_denied", "request_id": elev_id}, run_id=run_id)
                            result = f"Action denied: {reason}"

                    await _emit_event(session_id, queue, {"type": "tool_result", "name": tc_item["name"], "output": str(result)[:500]}, run_id=run_id)
                    items.append({"type": "function_call_output", "call_id": tc_item["call_id"], "output": str(result)})

                await _emit_event(session_id, queue, {"type": "tool_use", "name": "_thinking", "input": "Processing results..."}, run_id=run_id)

                if full_text:
                    items.append({"role": "assistant", "content": full_text})
                    full_text = ""

                # Mid-loop compaction check
                mid_est, _ = _effective_context_tokens(items, last_api_usage.get(session_id, {}))
                if mid_est > compaction_threshold:
                    await _emit_event(session_id, queue, {"type": "text", "text": "\n\n⏳ Auto-compacting mid-session..."}, run_id=run_id)
                    from compaction import compact_messages

                    _mk2 = model_key_from_session(session_id)
                    _wi2 = worker_identity_for_session(session_id)
                    items = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: compact_messages(items, session_id=session_id, model_key=_mk2, worker_identity=_wi2),
                    )
                    active_docs.pop(session_id, None)
                    last_api_usage.pop(session_id, None)
                    post = _estimate_total_context(items)
                    await _emit_event(session_id, queue, {"type": "text", "text": f" Done! (~{mid_est:,} → ~{post:,} tokens)\n\n"}, run_id=run_id)
                    await _emit_event(session_id, queue, {"type": "context", "tokens": post, "max": context_window, "pct": round(post / context_window, 4), "source": "estimate"}, run_id=run_id)

            except Exception as e:
                import requests as _rq
                if isinstance(e, (_rq.exceptions.ReadTimeout, _rq.exceptions.ConnectionError)):
                    logger.warning(f"[{session_id}] API connection issue (round {round_num}): {type(e).__name__}: {e}")
                    await _emit_event(session_id, queue, {"type": "error", "message": f"Connection to OpenAI lost — try again."}, run_id=run_id)
                else:
                    logger.error(f"Stream error: {e}", exc_info=True)
                    await _emit_event(session_id, queue, {"type": "error", "message": str(e) or type(e).__name__}, run_id=run_id)
                break

        items = repair_tool_items(items)
        save_history(session_id, items, last_api_usage=usage_info)

        final_text = ""
        for item in reversed(items):
            if isinstance(item, dict) and item.get("role") == "assistant":
                final_text = item.get("content", "")
                break
        if final_text:
            _append_to_chat_log(session_id, "assistant", final_text)

        token_count, token_source = _effective_context_tokens(items, usage_info or last_api_usage.get(session_id, {}))
        await _emit_event(session_id, queue, {"type": "context", "tokens": token_count, "max": context_window, "pct": round(token_count / context_window, 4), "source": token_source}, run_id=run_id)

        await _emit_event(session_id, queue, {"type": "done", "text": final_text, "model": f"{MODEL} (KukuiBot)", "usage": usage_info}, run_id=run_id)

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        await _emit_event(session_id, queue, {"type": "error", "message": str(e) or type(e).__name__}, run_id=run_id)
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

def _get_runtime_config() -> dict:
    import server as _srv
    return _srv._runtime_config

def _get_system_prompt_via_server(model_key: str = "", worker_identity: str = "") -> str:
    import server as _srv
    return _srv._get_system_prompt(model_key=model_key, worker_identity=worker_identity)

def _estimate_total_context(items: list) -> int:
    import server as _srv
    return _srv._estimate_total_context(items)

def _effective_context_tokens(items: list, usage: dict | None = None) -> tuple[int, str]:
    import server as _srv
    return _srv._effective_context_tokens(items, usage)

def _log_token_drift(session_id: str, usage: dict, est_input: int, est_now: int, effective: int, source: str):
    import server as _srv
    _srv._log_token_drift(session_id, usage, est_input, est_now, effective, source)

def _append_to_chat_log(session_id: str, role: str, content: str):
    import server as _srv
    _srv._append_to_chat_log(session_id, role, content)

def _track_tool_file(session_id: str, tool_name: str, args: dict):
    import server as _srv
    _srv._track_tool_file(session_id, tool_name, args)
