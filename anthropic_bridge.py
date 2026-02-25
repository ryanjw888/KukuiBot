"""anthropic_bridge.py — Direct Anthropic Messages API bridge for KukuiBot.

Streaming and non-streaming chat completions via the Anthropic Messages API.
Supports tool-calling, extended thinking, and prompt caching.

Uses raw httpx (not the anthropic SDK) for full control over SSE parsing
and to avoid SDK version coupling.

Security:
- API key passed per-request, never stored in module state
- httpx async client with TLS verification
- No shell=True, no interpolation
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger("kukuibot.anthropic")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

# Models available via direct API
ANTHROPIC_MODELS = {
    "claude-sonnet-4-6": {
        "label": "Claude Sonnet 4.6",
        "context_window": 1_000_000,
        "max_output_tokens": 16384,
        "cost_input_per_mtok": 3.0,
        "cost_output_per_mtok": 15.0,
    },
    "claude-sonnet-4-5-20250929": {
        "label": "Claude Sonnet 4.5",
        "context_window": 200_000,
        "max_output_tokens": 8192,
        "cost_input_per_mtok": 3.0,
        "cost_output_per_mtok": 15.0,
    },
    "claude-haiku-4-5-20251001": {
        "label": "Claude Haiku 4.5",
        "context_window": 200_000,
        "max_output_tokens": 8192,
        "cost_input_per_mtok": 0.80,
        "cost_output_per_mtok": 4.0,
    },
    "claude-opus-4-6": {
        "label": "Claude Opus 4.6",
        "context_window": 200_000,
        "max_output_tokens": 16384,
        "cost_input_per_mtok": 15.0,
        "cost_output_per_mtok": 75.0,
    },
}

DEFAULT_MODEL = "claude-sonnet-4-6"

# Thinking budget mapping: reasoning_effort -> budget_tokens
THINKING_BUDGETS = {
    "none": 0,
    "low": 2048,
    "medium": 8192,
    "high": 32768,
}

# ---------------------------------------------------------------------------
# Persistent Client (connection pooling)
# ---------------------------------------------------------------------------

_persistent_client: httpx.AsyncClient | None = None


def get_persistent_client(timeout_s: int = 300) -> httpx.AsyncClient:
    """Get or create a persistent httpx client for connection reuse."""
    global _persistent_client
    if _persistent_client is None or _persistent_client.is_closed:
        _persistent_client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=15))
    return _persistent_client


async def close_persistent_client():
    """Close the persistent client (for shutdown cleanup)."""
    global _persistent_client
    if _persistent_client and not _persistent_client.is_closed:
        await _persistent_client.aclose()
    _persistent_client = None


# ---------------------------------------------------------------------------
# Prompt Caching Helpers
# ---------------------------------------------------------------------------

def apply_cache_control(system_blocks: list[dict] | None) -> list[dict] | None:
    """Add cache_control to system prompt blocks for prompt caching.

    Marks the last system block with ephemeral cache_control so the entire
    system prompt prefix is cached across requests in the same session.
    """
    if not system_blocks:
        return system_blocks
    # Clone to avoid mutating the original
    blocks = [dict(b) for b in system_blocks]
    # Mark the last block as cacheable
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def thinking_params(reasoning_effort: str | None) -> dict | None:
    """Convert reasoning effort level to Anthropic thinking parameter.

    Returns dict for the 'thinking' request parameter, or None to disable.
    """
    if not reasoning_effort or reasoning_effort == "none":
        return None
    budget = THINKING_BUDGETS.get(reasoning_effort, 0)
    if budget <= 0:
        return None
    return {"type": "enabled", "budget_tokens": budget}


# ---------------------------------------------------------------------------
# Format Converters
# ---------------------------------------------------------------------------

def convert_tools_to_anthropic(
    tool_defs: list[dict] | None,
    advanced_tools: bool = False,
) -> list[dict]:
    """Convert KukuiBot TOOL_DEFINITIONS (OpenAI shape) to Anthropic tool format.

    KukuiBot shape:
        {"type":"function","name":"bash","description":"...","parameters":{...}}
    Anthropic shape:
        {"name":"bash","description":"...","input_schema":{...}}

    When advanced_tools=True:
    - Passes through allowed_callers on tools that have it
    - Removes strict (incompatible with PTC allowed_callers)
    - Appends the code_execution tool entry
    """
    out: list[dict] = []
    for t in tool_defs or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        schema = t.get("parameters") or {"type": "object", "properties": {}}
        # Remove "strict" key — Anthropic doesn't support it
        schema = {k: v for k, v in schema.items() if k != "strict"}
        entry: dict = {
            "name": name,
            "description": str(t.get("description") or ""),
            "input_schema": schema,
        }
        # Pass through allowed_callers when advanced tool use is enabled
        if advanced_tools and t.get("allowed_callers"):
            entry["allowed_callers"] = t["allowed_callers"]
        out.append(entry)

    # Inject code_execution server tool when advanced tool use is enabled
    if advanced_tools:
        out.append({
            "type": "code_execution_20260120",
            "name": "code_execution",
        })

    return out


def convert_history_to_anthropic(
    items: list[dict],
    system_prompt: str = "",
) -> tuple[list[dict], list[dict]]:
    """Convert KukuiBot history to Anthropic Messages format.

    Returns (system_blocks, messages).

    Key differences from OpenAI format:
    - System prompt goes in a separate `system` parameter, not in messages
    - Tool results are role="user" with type="tool_result" content blocks
    - Content blocks are arrays, not plain strings
    - Strict user/assistant alternation required
    - Adjacent same-role messages must be merged
    """
    # System blocks
    system_blocks = []
    if system_prompt and system_prompt.strip():
        system_blocks.append({"type": "text", "text": system_prompt.strip()})

    # Build raw message list
    raw_messages: list[dict] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "")

        # Skip system messages (handled above)
        if role == "system":
            continue

        if role == "user":
            content = item.get("content", "")
            if isinstance(content, str) and content.strip():
                raw_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": content.strip()}],
                })

        elif role == "assistant":
            blocks: list[dict] = []
            text = item.get("content")
            tool_calls = item.get("tool_calls")

            # If content is already a list of Anthropic-format blocks (raw replay),
            # pass through directly — handles server_tool_use, caller fields, etc.
            if isinstance(text, list):
                for block in text:
                    if isinstance(block, dict):
                        blocks.append(block)
            elif isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text.strip()})

            # Convert OpenAI tool_calls to Anthropic tool_use blocks
            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function", {}) or {}
                    tc_id = str(tc.get("id") or tc.get("call_id") or "")
                    name = str(fn.get("name") or tc.get("name") or "")
                    args_str = fn.get("arguments") or tc.get("arguments") or "{}"
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                    if name:
                        tc_block: dict = {
                            "type": "tool_use",
                            "id": tc_id,
                            "name": name,
                            "input": args,
                        }
                        # Preserve caller field for programmatic tool calls
                        if tc.get("caller"):
                            tc_block["caller"] = tc["caller"]
                        blocks.append(tc_block)

            if blocks:
                raw_messages.append({"role": "assistant", "content": blocks})

        elif role == "tool":
            # Convert to Anthropic tool_result (role=user)
            tool_call_id = str(item.get("tool_call_id") or "")
            result_content = item.get("content")
            # If content is already a list of Anthropic-format blocks, pass through
            if isinstance(result_content, list):
                raw_messages.append({
                    "role": "user",
                    "content": result_content,
                })
            else:
                raw_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": str(result_content or "")[:30000],
                    }],
                })

        # Handle Codex-style function_call items
        elif item.get("type") == "function_call":
            call_id = str(item.get("call_id") or "")
            name = str(item.get("name") or "")
            args_str = item.get("arguments") or "{}"
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, ValueError):
                args = {}
            if name:
                raw_messages.append({
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": call_id, "name": name, "input": args}],
                })

        elif item.get("type") == "function_call_output":
            call_id = str(item.get("call_id") or "")
            output = str(item.get("output") or "")
            raw_messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call_id, "content": output[:30000]}],
            })

    # --- Prune orphaned tool_results ---
    # Anthropic requires every tool_result to reference a tool_use in the
    # preceding assistant message.  Orphaned results (e.g. after smart-compact
    # drops the assistant message but keeps the tool result) cause 400 errors.
    known_tool_use_ids: set[str] = set()
    for msg in raw_messages:
        if msg["role"] == "assistant":
            for block in msg.get("content", []):
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype in ("tool_use", "server_tool_use"):
                        tid = block.get("id", "")
                        if tid:
                            known_tool_use_ids.add(tid)

    # Code execution result types that reference server_tool_use IDs
    _CODE_EXEC_RESULT_TYPES = {
        "bash_code_execution_tool_result",
        "text_editor_code_execution_tool_result",
        "code_execution_tool_result",
    }

    pruned: list[dict] = []
    for msg in raw_messages:
        if msg["role"] == "user":
            # Filter out tool_result / code_exec_result blocks with no matching tool_use
            filtered_content = []
            for block in msg.get("content", []):
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "tool_result":
                        if block.get("tool_use_id", "") not in known_tool_use_ids:
                            continue  # drop orphaned tool_result
                    elif btype in _CODE_EXEC_RESULT_TYPES:
                        if block.get("tool_use_id", "") not in known_tool_use_ids:
                            continue  # drop orphaned code exec result
                filtered_content.append(block)
            if filtered_content:
                pruned.append({"role": "user", "content": filtered_content})
            # else: drop empty user message entirely
        else:
            pruned.append(msg)

    # Merge adjacent same-role messages (Anthropic requires strict alternation)
    merged: list[dict] = []
    for msg in pruned:
        if merged and merged[-1]["role"] == msg["role"]:
            # Merge content blocks
            merged[-1]["content"].extend(msg["content"])
        else:
            merged.append(msg)

    # Ensure first message is from user (Anthropic requirement)
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continuing conversation)"}]})

    # Ensure alternation — if we have two same-role in a row after merge (shouldn't happen), fix it
    fixed: list[dict] = []
    for msg in merged:
        if fixed and fixed[-1]["role"] == msg["role"]:
            if msg["role"] == "user":
                fixed.append({"role": "assistant", "content": [{"type": "text", "text": "(acknowledged)"}]})
            else:
                fixed.append({"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
        fixed.append(msg)

    return system_blocks, fixed


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

async def anthropic_health(api_key: str) -> dict:
    """Check Anthropic API key validity by making a minimal request."""
    if not api_key:
        return {"ok": False, "error": "No API key configured"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Minimal valid request — send a tiny message, max_tokens=1
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": ANTHROPIC_API_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": DEFAULT_MODEL,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "ok": True,
                    "model": data.get("model", ""),
                    "usage": data.get("usage", {}),
                }
            elif resp.status_code == 401:
                return {"ok": False, "error": "Invalid API key"}
            else:
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Streaming Chat
# ---------------------------------------------------------------------------

async def anthropic_stream(
    messages: list[dict],
    system: list[dict] | None = None,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    max_tokens: int = 8192,
    tools: list[dict] | None = None,
    temperature: float = 0.7,
    timeout_s: int = 300,
    thinking: dict | None = None,
    use_prompt_caching: bool = True,
    container: str | None = None,
) -> AsyncIterator[dict]:
    """Stream chat completion from Anthropic Messages API.

    Yields structured events:
    - {"type": "text", "text": "chunk"}
    - {"type": "thinking_start"}
    - {"type": "thinking", "text": "chunk"}
    - {"type": "tool_use_start", "id": "...", "name": "..."}
    - {"type": "tool_use_delta", "id": "...", "json_delta": "..."}
    - {"type": "tool_use_done", "id": "...", "name": "...", "input": {...}, "caller": {...} | None}
    - {"type": "server_tool_use_start", "id": "...", "name": "..."}
    - {"type": "code_exec_result", "tool_use_id": "...", "result_type": "...", "stdout": "...", "stderr": "...", "return_code": int, "content": [...]}
    - {"type": "container_info", "id": "...", "expires_at": "..."}
    - {"type": "message_start", "model": "...", "usage": {...}}
    - {"type": "message_delta", "stop_reason": "...", "usage": {...}}
    - {"type": "done"}
    - {"type": "error", "message": "..."}
    """
    if not api_key:
        yield {"type": "error", "message": "No Anthropic API key configured"}
        return

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }

    # Apply prompt caching to system blocks
    cached_system = apply_cache_control(system) if use_prompt_caching else system

    payload: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": True,
    }
    if cached_system:
        payload["system"] = cached_system
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = {"type": "auto"}
    if thinking:
        payload["thinking"] = thinking
        # Extended thinking requires temperature=1 per Anthropic docs
        payload["temperature"] = 1
    elif temperature is not None:
        payload["temperature"] = temperature
    if container:
        payload["container"] = {"id": container}

    # Track current content block for accumulation
    current_block_type = None
    current_tool_id = ""
    current_tool_name = ""
    current_tool_json = ""
    current_tool_caller: dict | None = None
    # Track server_tool_use block for code exec result accumulation
    current_server_tool_id = ""

    client = get_persistent_client(timeout_s)
    async with client.stream("POST", ANTHROPIC_API_URL, headers=headers, json=payload) as resp:
        if resp.status_code != 200:
            body = await resp.aread()
            try:
                err_data = json.loads(body)
                err_msg = err_data.get("error", {}).get("message", body.decode(errors="ignore")[:300])
            except Exception:
                err_msg = body.decode(errors="ignore")[:300]
            yield {"type": "error", "message": f"Anthropic error {resp.status_code}: {err_msg}"}
            return

        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if not data_str:
                continue

            try:
                event = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue

            evt_type = event.get("type", "")

            if evt_type == "message_start":
                msg = event.get("message", {})
                yield {
                    "type": "message_start",
                    "model": msg.get("model", ""),
                    "usage": msg.get("usage", {}),
                }
                # Extract container info from message-level data
                container_info = msg.get("container")
                if container_info and isinstance(container_info, dict):
                    yield {
                        "type": "container_info",
                        "id": container_info.get("id", ""),
                        "expires_at": container_info.get("expires_at", ""),
                    }

            elif evt_type == "content_block_start":
                block = event.get("content_block", {})
                block_type = block.get("type", "")
                current_block_type = block_type

                if block_type == "thinking":
                    yield {"type": "thinking_start"}
                elif block_type == "server_tool_use":
                    # Anthropic-hosted tool invocation (code_execution)
                    current_server_tool_id = block.get("id", "")
                    yield {
                        "type": "server_tool_use_start",
                        "id": current_server_tool_id,
                        "name": block.get("name", "code_execution"),
                    }
                elif block_type == "tool_use":
                    current_tool_id = block.get("id", "")
                    current_tool_name = block.get("name", "")
                    current_tool_json = ""
                    current_tool_caller = block.get("caller")
                    evt_out: dict = {
                        "type": "tool_use_start",
                        "id": current_tool_id,
                        "name": current_tool_name,
                    }
                    if current_tool_caller:
                        evt_out["caller"] = current_tool_caller
                    yield evt_out
                elif block_type in (
                    "bash_code_execution_tool_result",
                    "text_editor_code_execution_tool_result",
                    "code_execution_tool_result",
                ):
                    # Server-side execution result block
                    content = block.get("content", {})
                    if isinstance(content, dict):
                        yield {
                            "type": "code_exec_result",
                            "result_type": block_type,
                            "tool_use_id": block.get("tool_use_id", ""),
                            "stdout": content.get("stdout", ""),
                            "stderr": content.get("stderr", ""),
                            "return_code": content.get("return_code", -1),
                            "content": content.get("content", []),
                        }
                    else:
                        yield {
                            "type": "code_exec_result",
                            "result_type": block_type,
                            "tool_use_id": block.get("tool_use_id", ""),
                            "stdout": "",
                            "stderr": "",
                            "return_code": -1,
                            "content": [],
                        }

            elif evt_type == "content_block_delta":
                delta = event.get("delta", {})
                delta_type = delta.get("type", "")

                if delta_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield {"type": "text", "text": text}

                elif delta_type == "thinking_delta":
                    text = delta.get("thinking", "")
                    if text:
                        yield {"type": "thinking", "text": text}

                elif delta_type == "input_json_delta":
                    partial = delta.get("partial_json", "")
                    if partial:
                        current_tool_json += partial

            elif evt_type == "content_block_stop":
                if current_block_type == "tool_use":
                    # Parse accumulated JSON
                    try:
                        tool_input = json.loads(current_tool_json) if current_tool_json else {}
                    except (json.JSONDecodeError, ValueError):
                        tool_input = {}
                    done_evt: dict = {
                        "type": "tool_use_done",
                        "id": current_tool_id,
                        "name": current_tool_name,
                        "input": tool_input,
                    }
                    if current_tool_caller:
                        done_evt["caller"] = current_tool_caller
                    yield done_evt
                    current_tool_caller = None
                current_block_type = None

            elif evt_type == "message_delta":
                delta = event.get("delta", {})
                usage = event.get("usage", {})
                yield {
                    "type": "message_delta",
                    "stop_reason": delta.get("stop_reason", ""),
                    "usage": usage,
                }

            elif evt_type == "message_stop":
                yield {"type": "done"}

            elif evt_type == "error":
                err = event.get("error", {})
                yield {"type": "error", "message": err.get("message", str(err))}


# ---------------------------------------------------------------------------
# Non-streaming Chat (for tool loop rounds where streaming is wasteful)
# ---------------------------------------------------------------------------

async def anthropic_chat(
    messages: list[dict],
    system: list[dict] | None = None,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    max_tokens: int = 8192,
    tools: list[dict] | None = None,
    temperature: float = 0.7,
    timeout_s: int = 300,
    client: httpx.AsyncClient | None = None,
    thinking: dict | None = None,
    use_prompt_caching: bool = True,
) -> dict:
    """Non-streaming chat completion from Anthropic Messages API.

    Returns:
        {
            "ok": bool,
            "text": str,
            "tool_calls": [{"id": "...", "name": "...", "input": {...}}, ...],
            "stop_reason": str,
            "usage": dict,
            "error": str (if !ok),
        }
    """
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key configured"}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }

    # Apply prompt caching to system blocks
    cached_system = apply_cache_control(system) if use_prompt_caching else system

    payload: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if cached_system:
        payload["system"] = cached_system
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = {"type": "auto"}
    if thinking:
        payload["thinking"] = thinking
        payload["temperature"] = 1
    elif temperature is not None:
        payload["temperature"] = temperature

    try:
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=15))
        try:
            resp = await client.post(ANTHROPIC_API_URL, headers=headers, json=payload)
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code != 200:
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            return {
                "ok": False,
                "error": f"Anthropic error {resp.status_code}: {err_msg}",
                "status_code": resp.status_code,
            }

        data = resp.json()
        content_blocks = data.get("content", [])

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        server_tool_uses: list[dict] = []
        code_exec_results: list[dict] = []

        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tc: dict = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                }
                if block.get("caller"):
                    tc["caller"] = block["caller"]
                tool_calls.append(tc)
            elif btype == "server_tool_use":
                server_tool_uses.append({
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                })
            elif btype in (
                "bash_code_execution_tool_result",
                "text_editor_code_execution_tool_result",
                "code_execution_tool_result",
            ):
                content = block.get("content", {})
                code_exec_results.append({
                    "result_type": btype,
                    "tool_use_id": block.get("tool_use_id", ""),
                    "stdout": content.get("stdout", "") if isinstance(content, dict) else "",
                    "stderr": content.get("stderr", "") if isinstance(content, dict) else "",
                    "return_code": content.get("return_code", -1) if isinstance(content, dict) else -1,
                    "content": content.get("content", []) if isinstance(content, dict) else [],
                })

        result: dict = {
            "ok": True,
            "text": "".join(text_parts),
            "tool_calls": tool_calls,
            "stop_reason": data.get("stop_reason", ""),
            "usage": data.get("usage", {}),
            "content_blocks": content_blocks,
        }
        # Include container info if present
        container_info = data.get("container")
        if container_info:
            result["container"] = container_info
        if server_tool_uses:
            result["server_tool_uses"] = server_tool_uses
        if code_exec_results:
            result["code_exec_results"] = code_exec_results
        return result
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
