"""
openrouter_provider.py — OpenRouter chat provider with tool-calling loop.

Extracted from server.py Phase 10a.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid

import httpx

from auth import get_config, load_history, save_history
from chat_providers import run_with_keepalive
from config import MAX_TOOL_ROUNDS
from log_store import log_write
from openrouter_bridge import openrouter_stream, openrouter_chat
from routes.delegation import _check_delegation_completion
from routes.session_events import _emit_event, _db_mark_run_done
from server_helpers import (
    attachment_summary,
    build_openai_attachment_parts,
    model_key_from_session,
    profile_limits,
    project_id_for_session,
    resolve_profile,
    worker_identity_for_session,
)
from tools import TOOL_DEFINITIONS, execute_tool
import notification_store

logger = logging.getLogger("kukuibot.chat_providers.openrouter")

TOOL_ROUND_LIMIT = int(os.environ.get("KUKUIBOT_MAX_TOOL_ROUNDS", str(MAX_TOOL_ROUNDS)))


def _openrouter_api_key() -> str:
    """Get OpenRouter API key from DB config, fallback to env var."""
    key = (get_config("openrouter.api_key", "") or "").strip()
    if key:
        return key
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip()


def _openrouter_model(session_id: str) -> str:
    """Get model for an OpenRouter session. Stored per-session or fallback to default."""
    model = (get_config(f"openrouter.session_model.{session_id}", "") or "").strip()
    if model:
        return model
    # Reverse-map: extract model_key from session_id and resolve to a model ID
    model_key = model_key_from_session(session_id)
    if model_key.startswith("openrouter_"):
        slug = model_key[len("openrouter_"):]  # e.g. "moonshotai_kimi_k2_5"
        # Normalize: replace / - . with _ for comparison
        def _norm(s: str) -> str:
            return s.replace("/", "_").replace("-", "_").replace(".", "_")
        # Load model registry (builtins + user overrides)
        try:
            import server as _srv
            registry = _srv._or_load_models()
        except (ImportError, AttributeError):
            registry = {}
        # Also merge any user-added models from DB
        if not registry:
            try:
                raw = get_config("openrouter.models", "")
                if raw:
                    registry = json.loads(raw) if raw else {}
            except Exception:
                registry = {}
        # Check registry for a model_id whose normalized form matches the slug
        for model_id in registry:
            if _norm(model_id) == slug:
                return model_id
        # No registry match — try simple slug→model_id conversion (first _ → /)
        if "_" in slug:
            candidate = slug.replace("_", "/", 1)  # moonshotai/kimi_k2_5
            return candidate
    return (get_config("openrouter.default_model", "") or "google/gemini-2.5-flash").strip()


def _or_model_config(model: str) -> dict:
    """Get config for an OpenRouter model — merged built-in + user overrides."""
    try:
        import server as _srv
        return _srv._or_model_config(model)
    except (ImportError, AttributeError):
        return {"max_tokens": 16384, "reasoning": "", "temperature": 0.7}


def _extract_openrouter_pseudo_tool_calls(text: str) -> list[dict]:
    """Parse fallback textual tool-call format emitted by some OpenRouter routes."""
    s = str(text or "")
    out: list[dict] = []
    needle = "call:default_api:"
    idx = 0
    while True:
        start = s.find(needle, idx)
        if start < 0:
            break
        i = start + len(needle)
        j = i
        while j < len(s) and (s[j].isalnum() or s[j] == "_"):
            j += 1
        name = s[i:j].strip()
        k = j
        while k < len(s) and s[k].isspace():
            k += 1
        if not name or k >= len(s) or s[k] != "{":
            idx = j
            continue
        depth = 0
        end = -1
        m = k
        while m < len(s):
            ch = s[m]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = m
                    break
            m += 1
        if end < 0:
            break
        arg_str = s[k:end + 1]
        try:
            obj = json.loads(arg_str)
            if not isinstance(obj, dict):
                obj = {}
            arg_str = json.dumps(obj)
        except Exception:
            idx = end + 1
            continue
        out.append({
            "id": str(uuid.uuid4()),
            "function": {"name": name, "arguments": arg_str},
        })
        idx = end + 1
    return out


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


async def process_chat_openrouter(
    queue,
    session_id: str,
    user_message: str,
    run_id: str,
    *,
    attachments: list[dict] | None = None,
    active_tasks: dict,
    runtime_config: dict,
    last_api_usage: dict,
    openrouter_tools_unsupported_until: dict,
    app_state=None,
):
    """OpenRouter provider — supports tool-calling loop with KukuiBot tools."""
    _or_client: httpx.AsyncClient | None = None
    items = []
    usage_info = {}
    model = ""
    try:
        api_key = _openrouter_api_key()
        if not api_key:
            await _emit_event(session_id, queue, {"type": "error", "message": "No OpenRouter API key configured. Add it in Settings."}, run_id=run_id)
            # Save error to history so delegation monitor can detect the failure
            try:
                items_err, _, _ = load_history(session_id)
                items_err.append({"role": "assistant", "content": "[ERROR] No OpenRouter API key configured. Add it in Settings."})
                save_history(session_id, items_err)
            except Exception:
                pass
            return

        model = _openrouter_model(session_id)

        items, _, _ = load_history(session_id)
        from server_helpers import model_key_from_session as _mk, worker_identity_for_session as _wi, project_id_for_session as _pi
        instructions = _get_system_prompt_via_server(model_key=_mk(session_id), worker_identity=_wi(session_id), project_id=_pi(session_id))

        # Build OpenAI-format messages (system + recent turns)
        messages: list[dict] = []
        if instructions.strip():
            messages.append({"role": "system", "content": instructions.strip()})

        for it in items[-60:]:
            if not isinstance(it, dict):
                continue
            role = it.get("role")
            if role == "tool":
                messages.append({
                    "role": "tool",
                    "tool_call_id": it.get("tool_call_id", ""),
                    "name": it.get("name", "tool"),
                    "content": str(it.get("content", ""))[:8000],
                })
            elif role == "assistant":
                msg: dict = {"role": "assistant"}
                content = it.get("content")
                tool_calls = it.get("tool_calls")
                if tool_calls:
                    msg["content"] = content or ""
                    msg["tool_calls"] = tool_calls
                elif isinstance(content, str) and content.strip():
                    msg["content"] = content.strip()
                else:
                    continue
                messages.append(msg)
            elif role == "user" and isinstance(it.get("content"), str) and it["content"].strip():
                messages.append({"role": role, "content": it["content"].strip()})

        # Build user content — multipart if attachments present
        if attachments:
            att_parts = build_openai_attachment_parts(attachments)
            att_parts.append({"type": "text", "text": user_message.strip()})
            messages.append({"role": "user", "content": att_parts})
        else:
            messages.append({"role": "user", "content": user_message.strip()})

        hist_content = user_message
        if attachments:
            hist_content = f"{attachment_summary(attachments)}\n{user_message}"
        items.append({"role": "user", "content": hist_content})

        await _emit_event(session_id, queue, {"type": "thinking_start"}, run_id=run_id)

        final_text = ""
        max_rounds = TOOL_ROUND_LIMIT
        tool_500_retried = False
        blocked_until = float(openrouter_tools_unsupported_until.get(model, 0) or 0)
        tools_enabled = time.time() >= blocked_until
        if not tools_enabled:
            rem = int(max(1, blocked_until - time.time()))
            await _emit_event(session_id, queue, {
                "type": "info",
                "message": f"OpenRouter tools disabled for this model (cached) — retry in ~{rem}s.",
            }, run_id=run_id)
        usage_info = {}
        effort = str(runtime_config.get("reasoning_effort", "medium") or "medium")
        mcfg = _or_model_config(model)
        model_max_tokens = mcfg["max_tokens"]
        model_temperature = mcfg.get("temperature", 0.7)
        if mcfg["reasoning"]:
            effort = mcfg["reasoning"]
        _or_client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15))

        _OPENROUTER_TOOLS_UNSUPPORTED_TTL_S = 2 * 60

        for round_idx in range(max_rounds):
            if round_idx > 0:
                await _emit_event(session_id, queue, {"type": "ping", "round": round_idx}, run_id=run_id)

            if not tools_enabled:
                stream_text = ""
                async for chunk in openrouter_stream(messages, model=model, api_key=api_key, max_tokens=model_max_tokens, temperature=model_temperature, reasoning_effort=effort):
                    stream_text += chunk
                    await _emit_event(session_id, queue, {"type": "text", "text": chunk}, run_id=run_id)
                if stream_text.strip():
                    final_text = stream_text
                    break

            _TOOL_CALL_TIMEOUT_S = 120  # Hard timeout for tool-calling requests
            try:
                resp = await asyncio.wait_for(
                    run_with_keepalive(
                        openrouter_chat(
                            messages, model=model, api_key=api_key,
                            max_tokens=model_max_tokens, temperature=model_temperature,
                            tools=TOOL_DEFINITIONS if tools_enabled else None,
                            tool_choice="auto", reasoning_effort=effort,
                            client=_or_client,
                        ),
                        session_id, queue, run_id, emit_event=_emit_event,
                    ),
                    timeout=_TOOL_CALL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                if tools_enabled:
                    logger.warning(f"[openrouter] Tool-calling request timed out for {model} after {_TOOL_CALL_TIMEOUT_S}s — retrying without tools")
                    tools_enabled = False
                    openrouter_tools_unsupported_until[model] = time.time() + _OPENROUTER_TOOLS_UNSUPPORTED_TTL_S
                    await _emit_event(session_id, queue, {
                        "type": "info",
                        "message": f"Tool-calling request timed out for {model} — retrying without tools.",
                    }, run_id=run_id)
                    continue
                # Timeout on a no-tools request — save error and bail
                logger.error(f"[openrouter] Non-tool request also timed out for {model} after {_TOOL_CALL_TIMEOUT_S}s")
                items.append({"role": "assistant", "content": f"[ERROR] OpenRouter request timed out after {_TOOL_CALL_TIMEOUT_S}s."})
                save_history(session_id, items, last_api_usage=usage_info or {"provider": "openrouter", "model": model})
                await _emit_event(session_id, queue, {"type": "error", "message": f"OpenRouter request timed out after {_TOOL_CALL_TIMEOUT_S}s."}, run_id=run_id)
                return
            if not resp.get("ok"):
                err = str(resp.get("error", "OpenRouter request failed"))
                status_code = int(resp.get("status_code", 0) or 0)
                if tools_enabled and (status_code == 500 or " 500" in err or "error 500" in err.lower()):
                    if not tool_500_retried:
                        tool_500_retried = True
                        logger.warning(f"[openrouter] 500 on tool call (round {round_idx}) — retrying once with tools")
                        await asyncio.sleep(1)
                        continue
                    tools_enabled = False
                    openrouter_tools_unsupported_until[model] = time.time() + _OPENROUTER_TOOLS_UNSUPPORTED_TTL_S
                    await _emit_event(session_id, queue, {
                        "type": "info",
                        "message": "OpenRouter tool-calling unavailable for this model route; retrying without tools.",
                    }, run_id=run_id)
                    continue
                await _emit_event(session_id, queue, {"type": "error", "message": err}, run_id=run_id)
                return

            text = str(resp.get("text") or "")
            tool_calls = resp.get("tool_calls") or []
            reasoning_details = resp.get("reasoning_details")
            if not tool_calls:
                tool_calls = _extract_openrouter_pseudo_tool_calls(text)

            # Check finish_reason: model wanted tool calls but we couldn't parse them
            finish_reason = str(resp.get("finish_reason") or "")
            if not tool_calls and finish_reason in ("tool_calls", "function_call"):
                logger.warning(
                    f"[openrouter] Model returned finish_reason={finish_reason} but "
                    f"no parseable tool_calls (round {round_idx}) — retrying without tools"
                )
                tools_enabled = False
                openrouter_tools_unsupported_until[model] = time.time() + _OPENROUTER_TOOLS_UNSUPPORTED_TTL_S
                await _emit_event(session_id, queue, {
                    "type": "status",
                    "message": "Model requested tools but format not supported — retrying without tools.",
                }, run_id=run_id)
                continue

            # Capture provider usage
            raw = resp.get("raw") if isinstance(resp.get("raw"), dict) else {}
            u = raw.get("usage") if isinstance(raw, dict) else {}
            if isinstance(u, dict) and u:
                from server_helpers import resolve_profile as _rp
                prompt_toks = int(u.get("prompt_tokens", 0) or 0)
                completion_toks = int(u.get("completion_tokens", 0) or 0)
                total_toks = int(u.get("total_tokens", 0) or 0)
                est_input = _estimate_total_context(items)
                usage_info = {
                    "provider": "openrouter",
                    "model": model,
                    "input_tokens": prompt_toks,
                    "output_tokens": completion_toks,
                    "total_tokens": total_toks,
                    "cached_tokens": int((u.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0) or 0),
                    "reasoning_tokens": int((u.get("completion_tokens_details", {}) or {}).get("reasoning_tokens", 0) or 0),
                    "est_input_tokens": est_input,
                    "captured_at": int(time.time()),
                    "profile": "openrouter",
                }
                last_api_usage[session_id] = usage_info
                est_now = _estimate_total_context(items)
                eff_now, eff_src = _effective_context_tokens(items, usage_info)
                _log_token_drift(session_id, usage_info, est_input, est_now, eff_now, eff_src)

            if not tool_calls:
                final_text = text
                if not final_text.strip():
                    raw = resp.get("raw") if isinstance(resp.get("raw"), dict) else {}
                    try:
                        c0 = ((raw.get("choices") or [])[0] or {})
                        m0 = c0.get("message") or {}
                        alt = m0.get("reasoning") or c0.get("text") or raw.get("output_text") or ""
                        if not alt and isinstance(m0.get("reasoning_details"), list):
                            rd_parts = []
                            for rd in m0["reasoning_details"]:
                                if isinstance(rd, dict):
                                    rd_parts.append(rd.get("text") or rd.get("summary") or rd.get("content") or "")
                            alt = "\n".join(p for p in rd_parts if p)
                        if isinstance(alt, str):
                            final_text = alt
                    except Exception:
                        pass
                if not final_text.strip() and round_idx > 0:
                    messages.append({"role": "user", "content": "Please summarize the results of the tool calls above and provide your answer."})
                    logger.warning(f"[openrouter] Empty response after {round_idx} tool rounds — nudging model for summary")
                    continue
                if final_text.strip():
                    await _emit_event(session_id, queue, {"type": "text", "text": final_text}, run_id=run_id)
                break

            assistant_msg = {"role": "assistant", "content": text or "", "tool_calls": tool_calls}
            if reasoning_details:
                assistant_msg["reasoning_details"] = reasoning_details
            messages.append(assistant_msg)
            items.append(assistant_msg)

            for call in tool_calls:
                fn = (call or {}).get("function", {}) or {}
                tool_name = str(fn.get("name") or "")
                arg_str = str(fn.get("arguments") or "{}")
                try:
                    parsed_args = json.loads(arg_str) if arg_str else {}
                except Exception:
                    parsed_args = {}

                await _emit_event(session_id, queue, {"type": "tool_use", "name": tool_name or "tool", "input": json.dumps(parsed_args)[:200]}, run_id=run_id)
                _track_tool_file(session_id, tool_name, parsed_args)

                result = await run_with_keepalive(
                    asyncio.get_event_loop().run_in_executor(None, execute_tool, tool_name, parsed_args, None, session_id),
                    session_id, queue, run_id, emit_event=_emit_event,
                )

                # Handle elevation-required
                if isinstance(result, str) and result.startswith("ELEVATION_REQUIRED:"):
                    parts = result.split(":", 2)
                    elev_id = parts[1] if len(parts) > 1 else ""
                    reason = parts[2] if len(parts) > 2 else "Restricted action"
                    await _emit_event(session_id, queue, {
                        "type": "elevation_required",
                        "request_id": elev_id,
                        "tool_name": tool_name,
                        "reason": reason,
                        "input": json.dumps(parsed_args)[:300],
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
                            asyncio.get_event_loop().run_in_executor(None, execute_tool, tool_name, parsed_args, elev_id, session_id),
                            session_id, queue, run_id, emit_event=_emit_event,
                        )
                    else:
                        result = "BLOCKED: Elevation denied or timed out."

                tool_call_id = str((call or {}).get("id") or "")
                if not tool_call_id:
                    tool_call_id = str(uuid.uuid4())

                result_str = str(result)
                if len(result_str) > 30000:
                    result_str = result_str[:30000] + f"\n... (truncated from {len(result_str)} chars)"

                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name or "tool",
                    "content": result_str,
                }
                messages.append(tool_result_msg)
                items.append(tool_result_msg)

            save_history(session_id, items, last_api_usage=usage_info or {"provider": "openrouter", "model": model})

        if not final_text.strip():
            # Save error marker so delegation monitor can detect the failure
            items.append({"role": "assistant", "content": "[ERROR] OpenRouter returned empty response."})
            save_history(session_id, items, last_api_usage=usage_info or {"provider": "openrouter", "model": model})
            _or_log_msg = f"{attachment_summary(attachments)}\n{user_message}" if attachments else user_message
            _append_to_chat_log(session_id, "user", _or_log_msg)
            await _emit_event(session_id, queue, {"type": "error", "message": "OpenRouter returned empty response."}, run_id=run_id)
            return

        items.append({"role": "assistant", "content": final_text})
        _or_log_msg = f"{attachment_summary(attachments)}\n{user_message}" if attachments else user_message
        _append_to_chat_log(session_id, "user", _or_log_msg)
        _append_to_chat_log(session_id, "assistant", final_text)
        if usage_info:
            usage_info["est_input_tokens"] = _estimate_total_context(items)
        save_history(session_id, items, last_api_usage=usage_info or {"provider": "openrouter", "model": model})

        profile, context_window, _ = profile_limits(session_id)
        token_count, token_source = _effective_context_tokens(items, usage_info or last_api_usage.get(session_id, {}))
        await _emit_event(session_id, queue, {"type": "context", "tokens": token_count, "max": context_window, "pct": round(token_count / context_window, 4), "source": token_source}, run_id=run_id)

        # Mark task done BEFORE emitting the done event so the client's
        # drained queue message doesn't hit a 409 race window.
        _pre_task = active_tasks.get(session_id, {})
        _pre_task["status"] = "done"
        active_tasks[session_id] = _pre_task
        await _emit_event(session_id, queue, {"type": "done", "text": final_text, "model": f"openrouter ({model})"}, run_id=run_id)

    except asyncio.CancelledError:
        logger.info(f"Chat cancelled for {session_id}")
        return
    except Exception as e:
        logger.error(f"OpenRouter stream error: {e}", exc_info=True)
        try:
            if items:
                save_history(session_id, items, last_api_usage=usage_info or {"provider": "openrouter", "model": model})
        except Exception:
            pass
        err_msg = str(e).strip() or f"{type(e).__name__}: connection to API failed"
        await _emit_event(session_id, queue, {"type": "error", "message": err_msg}, run_id=run_id)
    finally:
        if _or_client is not None:
            try:
                await _or_client.aclose()
            except Exception:
                pass
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


# --- Thin wrappers for server.py functions needed by this module ---
# These call back into server.py to avoid duplicating state-dependent code.

def _get_system_prompt_via_server(model_key: str = "", worker_identity: str = "", project_id: str = "") -> str:
    import server as _srv
    return _srv._get_system_prompt(model_key=model_key, worker_identity=worker_identity, project_id=project_id)

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
