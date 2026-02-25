"""
subagent.py — Isolated sub-agent runner with fresh context window.
"""

import json
import logging
import platform
import time
import uuid
from datetime import datetime

import requests as http_requests

from config import KUKUIBOT_API_URL, KUKUIBOT_USER_AGENT, MODEL, WORKSPACE
from tools import SUB_AGENT_TOOLS, execute_tool

logger = logging.getLogger("kukuibot.subagent")

_active: dict[str, dict] = {}


def run_subagent(task: str, max_turns: int = 25, parent_session_id: str = "") -> str:
    """Run an isolated sub-agent. Blocks until complete. Returns final text."""
    from auth import get_token, extract_account_id

    subagent_id = f"sub-{uuid.uuid4().hex[:8]}"
    _active[subagent_id] = {"status": "running", "result": "", "turns": 0}

    token = get_token()
    if not token:
        return "ERROR: No OpenAI OAuth token available."

    account_id = extract_account_id(token)
    if not account_id:
        return "ERROR: Failed to extract account ID."

    headers = {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "pi",
        "User-Agent": KUKUIBOT_USER_AGENT,
        "accept": "text/event-stream",
        "content-type": "application/json",
    }

    instructions = (
        "You are a KukuiBot sub-agent — an isolated worker spawned to complete a specific task.\n"
        "You have tools: bash, read_file, write_file, edit_file.\n"
        "Complete the task thoroughly, then provide a clear summary of what you did.\n"
        f"\nWorkspace: {WORKSPACE}\n"
        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )

    items = [{"role": "user", "content": task}]
    final_text = ""

    try:
        for turn in range(max_turns):
            _active[subagent_id]["turns"] = turn + 1

            body = {
                "model": MODEL,
                "store": False,
                "stream": True,
                "instructions": instructions,
                "input": items,
                "tools": SUB_AGENT_TOOLS,
                "tool_choice": "auto",
                "parallel_tool_calls": True,
                "text": {"verbosity": "high"},
            }

            resp = http_requests.post(KUKUIBOT_API_URL, headers=headers, json=body, timeout=1800, stream=True)
            if not resp.ok:
                return f"ERROR: Sub-agent API error {resp.status_code}: {resp.text[:200]}"

            text_parts = []
            tool_calls = []
            current_call_id = None
            current_call_name = None
            current_call_args = ""

            for evt in _parse_sse(resp):
                evt_type = evt.get("type", "")
                if evt_type == "response.output_text.delta":
                    delta = evt.get("delta", "")
                    if delta:
                        text_parts.append(delta)
                elif evt_type == "response.output_item.added":
                    item = evt.get("item", {})
                    if item.get("type") == "function_call":
                        current_call_id = item.get("call_id", str(uuid.uuid4()))
                        current_call_name = item.get("name", "unknown")
                        current_call_args = ""
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

            turn_text = "".join(text_parts)
            if turn_text:
                final_text = turn_text

            if not tool_calls:
                break

            for tc in tool_calls:
                try:
                    parsed = json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else tc["arguments"]
                except (json.JSONDecodeError, TypeError):
                    parsed = {}
                result = execute_tool(tc["name"], parsed)
                items.append({"type": "function_call", "call_id": tc["call_id"], "name": tc["name"], "arguments": tc["arguments"] if isinstance(tc["arguments"], str) else json.dumps(tc["arguments"])})
                items.append({"type": "function_call_output", "call_id": tc["call_id"], "output": result})

            if turn_text:
                items.append({"role": "assistant", "content": turn_text})

    except Exception as e:
        logger.error(f"Sub-agent {subagent_id} error: {e}", exc_info=True)
        return f"ERROR: Sub-agent failed: {e}"
    finally:
        _active[subagent_id] = {"status": "done", "result": final_text[:500], "turns": _active.get(subagent_id, {}).get("turns", 0)}

    return final_text or "Sub-agent completed but produced no text output."


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
