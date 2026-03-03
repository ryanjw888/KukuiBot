"""openrouter_bridge.py — OpenRouter API bridge for KukuiBot.

Streaming chat completions via OpenRouter's OpenAI-compatible API.
Supports any model available on OpenRouter (Gemini, Grok, Llama, etc.).

Security:
- API key passed per-request, never stored in module state
- httpx async client with TLS verification
- No shell=True, no interpolation
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx


def _normalize_tools_for_chat_completions(tools: list[dict] | None) -> list[dict]:
    """Convert Responses-style tool defs to ChatCompletions-style defs.

    KukuiBot TOOL_DEFINITIONS use shape:
      {"type":"function","name":...,"description":...,"parameters":...,"strict":...}

    OpenRouter chat/completions expects:
      {"type":"function","function":{"name":...,"description":...,"parameters":...}}
    """
    out: list[dict] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") != "function":
            continue
        fn_name = str(t.get("name") or "").strip()
        if not fn_name:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": fn_name,
                "description": str(t.get("description") or ""),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return out

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Default models available in the UI
DEFAULT_MODELS = {
    "google/gemini-2.5-flash": "Gemini 2.5 Flash",
    "google/gemini-2.5-pro": "Gemini 2.5 Pro",
    "google/gemini-3.1-pro-preview": "Gemini 3.1 Pro Preview",
    "moonshotai/kimi-k2.5": "Kimi K2.5 (Moonshot)",
    "x-ai/grok-3": "Grok 3",
    "x-ai/grok-3-mini": "Grok 3 Mini",
    "meta-llama/llama-4-maverick": "Llama 4 Maverick",
}


@dataclass
class OpenRouterUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    cost: float = 0.0


async def openrouter_health(api_key: str) -> dict:
    """Check OpenRouter API connectivity and key validity."""
    if not api_key:
        return {"ok": False, "error": "No API key configured"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return {"ok": True, "label": data.get("label", ""), "limit": data.get("limit"), "usage": data.get("usage")}
            return {"ok": False, "error": f"HTTP {resp.status_code}", "body": resp.text[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def openrouter_chat(
    messages: list[dict],
    *,
    model: str = "google/gemini-2.5-flash",
    api_key: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.7,
    timeout_s: int = 300,
    tools: list[dict] | None = None,
    tool_choice: str | dict = "auto",
    reasoning_effort: str = "medium",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Non-streaming chat completion from OpenRouter.

    Returns:
      {
        "ok": bool,
        "text": str,
        "tool_calls": list,
        "raw": dict,
        "error": str (if !ok),
        "status_code": int (if !ok)
      }
    """
    if not api_key:
        return {"ok": False, "error": "No OpenRouter API key configured"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://kukuibot.local",
        "X-Title": "KukuiBot",
        "X-OpenRouter-Privacy": "enabled",  # Enable Zero Data Retention (ZDR)
    }

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    # Best-effort reasoning hint (provider/model dependent).
    if reasoning_effort and reasoning_effort != "none":
        payload["reasoning"] = {"effort": reasoning_effort}
    if tools:
        norm_tools = _normalize_tools_for_chat_completions(tools)
        if norm_tools:
            payload["tools"] = norm_tools
            payload["tool_choice"] = tool_choice

    try:
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=15))
        try:
            resp = await client.post(OPENROUTER_API_URL, headers=headers, json=payload)
        finally:
            if owns_client:
                await client.aclose()
        if resp.status_code != 200:
            return {
                "ok": False,
                "error": f"OpenRouter error {resp.status_code}: {resp.text[:300]}",
                "status_code": int(resp.status_code),
            }
        data = resp.json()
        choices = data.get("choices") or []
        msg = (choices[0] or {}).get("message", {}) if choices else {}

        # OpenRouter model routes may return content as either:
        # - string
        # - array of typed blocks, e.g. [{"type":"text","text":"..."}]
        raw_content = msg.get("content")
        text = ""
        if isinstance(raw_content, str):
            text = raw_content
        elif isinstance(raw_content, list):
            parts: list[str] = []
            for block in raw_content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str):
                    parts.append(block.get("text") or "")
                elif isinstance(block.get("content"), str):
                    parts.append(block.get("content") or "")
            text = "".join(parts)

        reasoning_details = msg.get("reasoning_details") or msg.get("reasoning") or None

        return {
            "ok": True,
            "text": text,
            "tool_calls": msg.get("tool_calls") or [],
            "reasoning_details": reasoning_details,
            "raw": data,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def openrouter_stream(
    messages: list[dict],
    *,
    model: str = "google/gemini-2.5-flash",
    api_key: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.7,
    timeout_s: int = 300,
    reasoning_effort: str = "medium",
) -> AsyncIterator[str]:
    """Stream chat completion from OpenRouter, yielding text chunks.

    Args:
        messages: OpenAI-format messages [{"role": "user", "content": "..."}]
        model: OpenRouter model ID (e.g. "google/gemini-2.5-flash")
        api_key: OpenRouter API key
        max_tokens: max output tokens
        temperature: sampling temperature
        timeout_s: request timeout
        reasoning_effort: reasoning effort hint (none/low/medium/high)
    """
    if not api_key:
        yield "[Error: No OpenRouter API key configured. Add it in Settings.]\n"
        return

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://kukuibot.local",
        "X-Title": "KukuiBot",
        "X-OpenRouter-Privacy": "enabled",  # Enable Zero Data Retention (ZDR)
    }

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if reasoning_effort and reasoning_effort != "none":
        payload["reasoning"] = {"effort": reasoning_effort}

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=15)) as client:
        async with client.stream(
            "POST",
            OPENROUTER_API_URL,
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield f"[OpenRouter error {resp.status_code}: {body.decode(errors='ignore')[:300]}]\n"
                return

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield content
                # Fallback: some models put text in reasoning blocks when content is empty
                elif not content:
                    reasoning = delta.get("reasoning")
                    if isinstance(reasoning, str) and reasoning:
                        yield reasoning
