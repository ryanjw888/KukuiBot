"""
app_state.py — Centralized application state for dependency injection.

Replaces module-level globals in server.py. All mutable runtime state lives here,
accessed via `get_app_state(request)` in route handlers instead of `from server import _global`.

Multi-instance aware: all paths come from config.py (which reads KUKUIBOT_HOME env var).
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request


@dataclass
class AppState:
    """All mutable runtime state for the KukuiBot server."""

    # --- Runtime Config ---
    # Settings that can change at runtime (reasoning effort, nudge, compact, etc.)
    runtime_config: dict = field(default_factory=dict)

    # --- Per-Session State ---
    # Token usage tracking per session: session_id -> {est_input_tokens, est_output_tokens, ...}
    last_api_usage: dict[str, dict] = field(default_factory=dict)

    # In-flight chat tasks: session_id -> {task, model, started, ...}
    active_tasks: dict[str, dict] = field(default_factory=dict)

    # Files touched via tool calls per session (for smart compact reabsorption)
    active_docs: dict[str, set] = field(default_factory=dict)

    # Anthropic container reuse: session_id -> container_id
    anthropic_containers: dict[str, str] = field(default_factory=dict)

    # --- SSE / Event Subscribers ---
    # Resume event subscribers: session_id -> list[asyncio.Queue]
    resume_subscribers: dict[str, list] = field(default_factory=dict)

    # Anthropic persistent EventSource subscribers: session_id -> list[asyncio.Queue]
    anthropic_event_subscribers: dict[str, list] = field(default_factory=dict)

    # Global broadcast subscribers: all connected browsers (cross-device sync)
    global_broadcast_subscribers: list = field(default_factory=list)

    # Per-session proactive wake locks (delegation system)
    proactive_wake_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    # --- OpenRouter State ---
    # Model -> unix timestamp: tools are considered unsupported until this time
    openrouter_tools_unsupported_until: dict[str, float] = field(default_factory=dict)

    # --- System Prompt Cache ---
    system_prompt_tokens: int = 0
    system_prompt_sig: tuple = ()

    # --- Services (set during startup) ---
    # These are initialized in server.py startup() and attached here.
    # Using Any to avoid circular imports with claude_bridge, notification_dispatcher, etc.
    session_event_store: Any = None
    notification_dispatcher: Any = None

    # --- Background Tasks (set during startup) ---
    delegation_monitor_task: Any = None
    memory_reaper_task: Any = None
    db_health_monitor_task: Any = None
    db_backup_loop_task: Any = None
    system_wake_task: Any = None
    email_sync_task: Any = None

    # --- Usage Cache ---
    usage_cache: dict = field(default_factory=dict)

    # --- Log Rate Limits ---
    log_rate_limits: dict[tuple, float] = field(default_factory=dict)


def get_app_state(request: Request) -> AppState:
    """Retrieve the AppState from the FastAPI app instance.

    Usage in route handlers:
        state = get_app_state(request)
        state.active_tasks[session_id] = ...
    """
    return request.app.state.app_state
