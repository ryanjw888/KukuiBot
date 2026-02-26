"""
server.py — KukuiBot unified server.
Single FastAPI process — serves API, auth, settings, and frontend.
"""

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from config import (
    APP_NAME,
    KUKUIBOT_API_URL,
    KUKUIBOT_USER_AGENT,
    CODEX_COMPACTION_THRESHOLD,
    CODEX_CONTEXT_WINDOW,
    DEFAULT_NUDGE_ENABLED,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_SELF_COMPACT,
    HOST,
    SSL_CERT,
    SSL_KEY,
    MAX_TOOL_ROUNDS,
    MODEL,
    SPARK_COMPACTION_THRESHOLD,
    SPARK_CONTEXT_WINDOW,
    SPARK_MODEL,
    PORT,
    WORKER_PORT,
    WORKSPACE,
    KUKUIBOT_HOME,
    SESSION_EVENT_RING_MAX_EVENTS,
    SESSION_EVENT_RING_MAX_BYTES,
    SESSION_EVENT_DB_MAX_EVENTS_PER_SESSION,
    SESSION_EVENT_TTL_SECONDS,
    SSE_KEEPALIVE_SECONDS,
)
from tools import TOOL_DEFINITIONS, execute_tool
from subagent import run_subagent
from security import (
    approve_elevation,
    clear_session_security,
    deny_elevation,
    get_elevated_status,
    get_pending_elevations,
    get_security_policy,
    is_approve_all,
    is_session_elevated,
    set_approve_all,
    set_elevated_session,
)
from auth import (
    _get_db,
    db_connection,
    _sessions,
    clear_history,
    get_auth_status,
    get_provider_type,
    get_request_user,
    get_token,
    get_config,
    set_config,
    extract_account_id,
    import_from_legacy,
    init_db,
    is_localhost,
    is_setup_complete,
    load_history,
    periodic_health_check,
    save_history,
    SESSION_COOKIE,
    startup_db_health_gate,
)
from privileged_client import PrivilegedHelperClient, PrivilegedHelperError

from log_store import init_log_db, log_write, log_query, log_count, log_stats, log_purge, log_db_size, LOG_DB_PATH
from app_state import AppState, get_app_state
import notification_store
import notification_dispatcher
from claude_bridge import claude_health, init_claude_pool, get_claude_pool, CONTEXT_WINDOW as CLAUDE_CONTEXT_WINDOW, COMPACTION_THRESHOLD as CLAUDE_COMPACTION_THRESHOLD
from server_helpers import (
    human_bytes as _human_bytes,
    clamp_int as _clamp_int,
    MODEL_PROFILES as _MODEL_PROFILES,
    resolve_profile as _resolve_profile,
    profile_limits as _profile_limits,
    model_key_from_session as _model_key_from_session,
    resolve_model_file as _resolve_model_file,
    response_has_links as _response_has_links,
    extract_web_links_from_tool_output as _extract_web_links_from_tool_output,
    sanitize_bearer_token as _sanitize_bearer_token,
    repair_tool_items as _repair_tool_items,
    ATTACHMENT_TMP_DIR as _ATTACHMENT_TMP_DIR,
    validate_attachments as _validate_attachments,
    format_attachments_as_text as _format_attachments_as_text,
    save_attachment_image as _save_attachment_image,
    cleanup_old_attachments as _cleanup_old_attachments,
    build_anthropic_attachment_blocks as _build_anthropic_attachment_blocks,
    build_openai_attachment_parts as _build_openai_attachment_parts,
    build_codex_attachment_items as _build_codex_attachment_items,
    attachment_summary as _attachment_summary,
    is_claude_session as _is_claude_session,
    claude_model_for_session as _claude_model_for_session,
    is_openrouter_session as _is_openrouter_session,
    is_anthropic_session as _is_anthropic_session,
    worker_identity_for_session as _worker_identity_for_session,
)
from openrouter_bridge import openrouter_stream, openrouter_health, openrouter_chat
from routes.session_events import (
    SessionEventStore,
    _ensure_chat_event_schema,
    _db_start_run,
    _db_mark_run_done,
    _db_latest_run,
    _db_load_run_events,
    _emit_event,
    init_event_system,
)
from anthropic_bridge import (
    anthropic_stream, anthropic_health, anthropic_chat,
    convert_tools_to_anthropic, convert_history_to_anthropic,
    ANTHROPIC_MODELS, DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL,
    get_persistent_client, close_persistent_client,
    thinking_params,
)
from routes.chat import router as chat_router
from routes.files import router as files_router
from routes.integrations import router as integrations_router
from routes.tabs import router as tabs_router, _cleanup_tab_session, _resolve_owner_username, _ensure_tab_meta_schema
from routes.bridge import (
    router as bridge_router,
    _init_worker_ports,
    _discover_workers,
    _active_bridges,
    _ensure_bridge,
    _get_bridge_client,
    _spawn_bridge,
    _stop_bridge,
    shutdown_bridges,
)
from routes.delegation import (
    router as delegation_router,
    init_delegation_routes,
    _check_delegation_completion,
    _format_delegation_status_notification,
    _deliver_or_queue_parent_notification,
    _do_delegation_cleanup,
    _try_proactive_wake,
    _ensure_claude_subprocess,
    _delegation_monitor,
    _system_wake,
    _get_wake_lock,
)

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# Use an explicit parent logger for all kukuibot.* children. We force INFO here
# because basicConfig() is a no-op if another component (e.g., uvicorn) already
# configured root logging, which can otherwise leave effective level at WARNING.
_kukuibot_logger = logging.getLogger("kukuibot")
_kukuibot_logger.setLevel(logging.INFO)
_kukuibot_logger.propagate = True

logger = logging.getLogger("kukuibot.server")


class _SQLiteLogHandler(logging.Handler):
    """Routes WARNING+ log records from all kukuibot.* loggers to SQLite."""

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            log_write(
                "system",
                self.format(record),
                level=record.levelname,
                source=record.name,
            )
        except Exception:
            pass  # Never let logging errors crash the app


# Attach to root kukuibot logger so all children (kukuibot.server, kukuibot.tools, etc.) are captured
_sqlite_system_handler = _SQLiteLogHandler()
_sqlite_system_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger("kukuibot").addHandler(_sqlite_system_handler)

# --- App ---
app = FastAPI(title=APP_NAME, version="0.1.0")

# --- Centralized Application State ---
# All mutable runtime state lives here. Route modules access it via get_app_state(request).
_app_state = AppState()
app.state.app_state = _app_state

app.include_router(chat_router)
app.include_router(files_router)
app.include_router(integrations_router)
app.include_router(tabs_router)
app.include_router(bridge_router)
app.include_router(delegation_router)

# --- Logging ---

_LOG_DIR = KUKUIBOT_HOME / "logs"
_log_rate_limits = _app_state.log_rate_limits


# _human_bytes, _clamp_int → server_helpers.human_bytes, server_helpers.clamp_int


def _init_server_log():
    """Set up RotatingFileHandler for server.log (process-level safety net).

    Attached to the 'kukuibot' parent logger so all children (kukuibot.server,
    kukuibot.claude_bridge, kukuibot.delegation, etc.) write to the same file.
    """
    from logging.handlers import RotatingFileHandler
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = _LOG_DIR / "server.log"
    try:
        handler = RotatingFileHandler(
            str(target), maxBytes=10 * 1024 * 1024, backupCount=5,
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logging.getLogger("kukuibot").addHandler(handler)
    except Exception as e:
        logger.warning(f"Failed to configure server.log handler: {e}")


def _rate_limit_key(req: Request, action: str) -> tuple[str, str]:
    session = req.cookies.get(SESSION_COOKIE, "")
    if not session:
        session = req.headers.get("x-session-id", "")
    if not session:
        session = req.client.host if req.client else "anon"
    return (action, session)


def _check_rate_limit(req: Request, action: str, cooldown_seconds: int = 60) -> bool:
    key = _rate_limit_key(req, action)
    now = time.time()
    last = _log_rate_limits.get(key, 0)
    if now - last < cooldown_seconds:
        return False
    _log_rate_limits[key] = now
    return True



# --- DB Recovery Page (served as 503 when DB is unhealthy) ---
_DB_RECOVERY_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>KukuiBot — Service Recovery</title>
<style>
  body { background: #0f1117; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { background: #1a1e2e; border: 1px solid #2d3348; border-radius: 16px; padding: 40px 48px; max-width: 440px; text-align: center; }
  h1 { font-size: 20px; margin: 16px 0 8px; }
  p { font-size: 14px; color: #94a3b8; line-height: 1.6; }
  .icon { font-size: 48px; }
  .status { color: #f59e0b; font-weight: 600; }
  .retry { font-size: 12px; color: #64748b; margin-top: 20px; }
</style>
</head>
<body>
<div class="card">
  <div class="icon">&#128295;</div>
  <h1>KukuiBot is recovering</h1>
  <p class="status">Database maintenance in progress</p>
  <p>The system detected an issue with the database and is performing automatic recovery. This page will refresh automatically.</p>
  <p class="retry">Auto-refresh in 30 seconds &middot; Localhost admin access is unaffected</p>
</div>
</body>
</html>"""

# --- Runtime State ---
RUNTIME_STARTED = time.time()


def _clamp_session_event_config() -> tuple[int, int, int, int, int]:
    ring_events = _clamp_int(
        SESSION_EVENT_RING_MAX_EVENTS,
        default=500,
        min_value=50,
        max_value=20_000,
    )
    ring_bytes = _clamp_int(
        SESSION_EVENT_RING_MAX_BYTES,
        default=2 * 1024 * 1024,
        min_value=128 * 1024,
        max_value=64 * 1024 * 1024,
    )
    db_max_per_session = _clamp_int(
        SESSION_EVENT_DB_MAX_EVENTS_PER_SESSION,
        default=5000,
        min_value=200,
        max_value=200_000,
    )
    ttl_seconds = _clamp_int(
        SESSION_EVENT_TTL_SECONDS,
        default=86400,
        min_value=300,
        max_value=30 * 86400,
    )
    sse_keepalive = _clamp_int(
        SSE_KEEPALIVE_SECONDS,
        default=15,
        min_value=5,
        max_value=120,
    )
    return ring_events, ring_bytes, db_max_per_session, ttl_seconds, sse_keepalive


_EVENT_RING_MAX_EVENTS, _EVENT_RING_MAX_BYTES, _EVENT_DB_MAX_EVENTS_PER_SESSION, _EVENT_TTL_SECONDS, _SSE_KEEPALIVE_SECONDS = _clamp_session_event_config()
_VALID_AUTO_COMPACT_PCTS = {"none", "50", "60", "70", "80"}
# --- Mutable Runtime State (lives in _app_state, aliases for backwards compat) ---
_app_state.runtime_config = {
    "reasoning_effort": DEFAULT_REASONING_EFFORT,
    "nudge_enabled": DEFAULT_NUDGE_ENABLED,
    "self_compact": DEFAULT_SELF_COMPACT,
    "auto_name_enabled": get_config("auto_name_enabled", "0") == "1",
    "claude_auto_compact_pct": get_config("claude_auto_compact_pct", "none"),
}
# Module-level aliases — existing code (and lazy imports from routes) can still use these.
# All point to the same dict/list objects on _app_state, so mutations are shared.
_runtime_config = _app_state.runtime_config
_last_api_usage = _app_state.last_api_usage
_active_tasks = _app_state.active_tasks
_active_docs = _app_state.active_docs
_anthropic_containers = _app_state.anthropic_containers
_resume_subscribers = _app_state.resume_subscribers
_anthropic_event_subscribers = _app_state.anthropic_event_subscribers
# _notification_dispatcher — no module alias; always use _app_state.notification_dispatcher
# (it gets set in startup(), module-level alias would be stale)

# OpenRouter capability cache: model -> unix timestamp until tools are considered unsupported.
_OPENROUTER_TOOLS_UNSUPPORTED_UNTIL = _app_state.openrouter_tools_unsupported_until
_OPENROUTER_TOOLS_UNSUPPORTED_TTL_S = 2 * 60  # 2 minutes (intermittent 500s, not permanent)

# Per-model OpenRouter config: max_tokens + reasoning overrides.
# Stored in DB (openrouter.models JSON), merged with these built-in defaults.
# Gemini reasoning tokens eat into max_tokens — set high enough for both reasoning and response.
# High reasoning eliminates hallucinations (8.4/10 vs 5/10 at low).
_OPENROUTER_BUILTIN_MODELS: dict[str, dict] = {
    "google/gemini-2.5-flash": {"label": "Gemini 2.5 Flash", "max_tokens": 16384, "reasoning": "high", "temperature": 0.4},
    "google/gemini-2.5-pro": {"label": "Gemini 2.5 Pro", "max_tokens": 32768, "reasoning": "high", "temperature": 0.4},
    "google/gemini-3.1-pro-preview": {"label": "Gemini 3.1 Pro", "max_tokens": 32768, "reasoning": "high", "temperature": 0.3},
    "x-ai/grok-3": {"label": "Grok 3", "max_tokens": 16384},
    "x-ai/grok-3-mini": {"label": "Grok 3 Mini", "max_tokens": 16384},
    "meta-llama/llama-4-maverick": {"label": "Llama 4 Maverick", "max_tokens": 16384},
}

def _or_load_models() -> dict[str, dict]:
    """Load merged model registry: DB overrides on top of built-in defaults."""
    merged = {k: dict(v) for k, v in _OPENROUTER_BUILTIN_MODELS.items()}
    try:
        raw = get_config("openrouter.models", "")
        if raw:
            user_models = json.loads(raw)
            if isinstance(user_models, dict):
                for model_id, cfg in user_models.items():
                    if isinstance(cfg, dict) and isinstance(model_id, str) and model_id.strip():
                        merged[model_id.strip()] = {**merged.get(model_id.strip(), {}), **cfg}
    except (json.JSONDecodeError, Exception):
        pass
    return merged

def _or_save_user_models(user_models: dict[str, dict]):
    """Persist user model overrides/additions to DB."""
    set_config("openrouter.models", json.dumps(user_models, indent=2))

def _or_model_config(model: str) -> dict:
    """Return per-model config for an OpenRouter model."""
    registry = _or_load_models()
    cfg = registry.get(model, {})
    return {
        "max_tokens": cfg.get("max_tokens", 16384),
        "reasoning": cfg.get("reasoning"),
    }

# Privileged helper (local Unix socket)
_PRIV_SOCKET = os.environ.get("KUKUIBOT_PRIV_SOCKET", "/tmp/kukuibot-priv.sock")
_priv_client = PrivilegedHelperClient(_PRIV_SOCKET)

# Runtime-overridable tool round safety cap
TOOL_ROUND_LIMIT = int(os.environ.get("KUKUIBOT_MAX_TOOL_ROUNDS", str(MAX_TOOL_ROUNDS)))

# System prompt cache — use _app_state.system_prompt_tokens / _app_state.system_prompt_sig directly

# _active_docs alias already created above from _app_state.active_docs

# One-time schema backfill guard moved to routes.session_events


# --- Append-only Chat Log (SQLite) ---

def _append_to_chat_log(session_id: str, role: str, content: str):
    """Append a message to the persistent SQLite log. NEVER cleared on compact or reset."""
    worker = _worker_identity_for_session(session_id)
    model_key = _model_key_from_session(session_id)
    try:
        log_write(
            "chat",
            content,
            role=role.lower(),
            session_id=session_id,
            worker=worker,
            source=f"chat.{model_key}" if model_key else "chat",
        )
    except Exception as e:
        logger.warning(f"Failed to append to chat log: {e}")


def _append_to_file_log(tool: str, path: str):
    """Append a file activity entry to SQLite."""
    try:
        log_write("file_activity", path, source=tool.upper())
    except Exception as e:
        logger.warning(f"Failed to append to file log: {e}")


# Only reabsorb documentation files on compaction — never source code.
_DOC_EXTENSIONS = {".md", ".txt", ".log", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".env"}


def _track_tool_file(session_id: str, tool_name: str, args: dict):
    """Track file operations for active docs and file activity log.

    Only documentation/config files (*.md, *.txt, *.log, etc.) are added to
    active_docs for compaction reabsorption. Source code (*.py, *.js, *.html,
    etc.) is logged but never reabsorbed — it would bloat the compaction summary.
    """
    file_tools = {"read_file", "write_file", "edit_file"}
    if tool_name not in file_tools:
        return
    path = args.get("file_path") or args.get("path") or args.get("file", "")
    if not path or not isinstance(path, str):
        return
    # Only track user-space files
    if path.startswith("/Users/") and "/tmp/" not in path:
        _append_to_file_log(tool_name, path)  # always log all file ops
        ext = os.path.splitext(path)[1].lower()
        if ext in _DOC_EXTENSIONS:
            docs = _active_docs.setdefault(session_id, set())
            docs.add(path)


# _MODEL_PROFILES → server_helpers.MODEL_PROFILES


# _resolve_profile → server_helpers.resolve_profile


# _profile_limits → server_helpers.profile_limits


def _log_token_drift(session_id: str, usage: dict, est_input: int, est_now: int, effective: int, source: str):
    try:
        api_input = int((usage or {}).get("input_tokens", 0) or 0)
        if api_input <= 0:
            return
        drift = api_input - est_input
        drift_pct = round((drift / api_input) * 100, 2) if api_input else 0.0
        rec = {
            "ts": int(time.time()),
            "session_id": session_id,
            "profile": _resolve_profile(session_id),
            "api_input_tokens": api_input,
            "estimated_input_tokens": int(est_input),
            "estimated_now_tokens": int(est_now),
            "effective_tokens": int(effective),
            "effective_source": source,
            "delta_tokens": int(drift),
            "delta_pct_of_api": drift_pct,
            "cached_tokens": int((usage or {}).get("cached_tokens", 0) or 0),
            "reasoning_tokens": int((usage or {}).get("reasoning_tokens", 0) or 0),
        }
        log_write(
            "token_accuracy",
            f"drift={drift_pct}% api={api_input} est={int(est_input)}",
            source="kukuibot.token-accuracy",
            session_id=session_id,
            metadata=rec,
        )
    except Exception:
        pass


# --- System Prompt ---

# _model_key_from_session → server_helpers.model_key_from_session


# _resolve_model_file → server_helpers.resolve_model_file


# _worker_identity_for_session → server_helpers (imported above)


def _load_project_report_content(max_chars: int = 6000) -> str:
    """Load PROJECT-REPORT.md with optional staleness warning.

    Returns empty string when file is missing/unreadable.
    """
    project_report_path = KUKUIBOT_HOME / "PROJECT-REPORT.md"
    try:
        if not project_report_path.exists():
            return ""

        content = project_report_path.read_text()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... (truncated)"

        try:
            age_seconds = max(0.0, time.time() - project_report_path.stat().st_mtime)
            if age_seconds > 48 * 3600:
                updated = datetime.fromtimestamp(project_report_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                warning = (
                    "> ⚠️ Staleness warning: PROJECT-REPORT.md is older than 48 hours "
                    f"(last updated {updated} local time).\n\n"
                )
                return warning + content
        except Exception:
            pass

        return content
    except Exception:
        return ""


def _get_system_prompt(model_key: str = "", worker_identity: str = "") -> str:
    """Build the system prompt for Codex/Spark/OpenRouter tabs.

    Loads: SOUL, USER, TOOLS, Model Identity, Worker Identity.
    No ROADMAP, no MEMORY.md, no daily memory, no README/DESIGN docs.
    """
    src_path = KUKUIBOT_HOME / "src"
    src_dir = str(src_path.resolve()) if src_path.exists() else "(not linked)"

    parts = [
        f"You are {APP_NAME}, a self-hosted AI agent running on macOS.",
        "You have direct access to the filesystem and can run commands via tools.",
        "Be concise, helpful, and get things done.",
        "On first message in a session, greet the user by their name from USER.md.",
        "",
        "## Self-Knowledge",
        f"Your source code is at: {src_dir}",
        f"Your data directory is: {KUKUIBOT_HOME}",
        "You can read and modify your own source code to add features, fix bugs, or change behavior.",
        "Read docs/DESIGN.md and README.md before making architectural changes.",
        "After modifying source files, the server needs a restart to pick up changes.",
        "",
        "## Tool Use Policy",
        "You are an AGENT, not a chatbot. Use tools to complete tasks.",
        "Call tools IMMEDIATELY — don't say 'I will...' without tool calls.",
        "Don't ask for permission for routine work. Just do it.",
        "Test before declaring done. If you can't test, say so.",
        "",
        "## Long-Running Processes",
        "For commands >30s: use bash_background, then poll with bash_check in a LOOP in the SAME turn.",
        "Keep calling bash_check (with wait_seconds) until status is 'done'.",
        "",
        "## Memory",
        "Before answering about prior work, decisions, preferences: use memory_search first.",
        "Then memory_read for full context.",
        "",
        "## Security",
        "Some operations require elevation (approval). The user will see a prompt.",
        "Workspace-first file access. Paths outside workspace need elevation.",
        "",
        "## Web Research Output Format",
        "When you use web_search_ddg, ALWAYS include related links in your final answer.",
        "Add a 'Sources' section at the end with 3-5 clickable links (use links_markdown if available).",
        "Do not provide a web-research summary without links.",
        "If available, prefer tool field results_html first (no markdown syntax), else results_markdown.",
        "For each highlighted result: show hyperlinked title, URL/domain line, and short snippet text.",
        "",
    ]

    # Load identity files: SOUL, USER, TOOLS
    for fname, path in [
        ("SOUL.md", KUKUIBOT_HOME / "SOUL.md"),
        ("USER.md", KUKUIBOT_HOME / "USER.md"),
        ("TOOLS.md", KUKUIBOT_HOME / "TOOLS.md"),
    ]:
        try:
            content = path.read_text()
            if len(content) > 30000:
                content = content[:30000] + "\n... (truncated)"
            parts.append(f"## {fname}\n{content}\n")
        except Exception:
            pass

    # Load per-model identity file
    model_file = _resolve_model_file(model_key)
    if model_file:
        try:
            content = model_file.read_text()
            if len(content) > 10000:
                content = content[:10000] + "\n... (truncated)"
            parts.append(f"## Model Profile\n{content}\n")
        except Exception:
            pass

    # Load worker identity file
    if worker_identity:
        worker_file = KUKUIBOT_HOME / "workers" / f"{worker_identity}.md"
        try:
            if worker_file.exists():
                content = worker_file.read_text()
                if len(content) > 10000:
                    content = content[:10000] + "\n... (truncated)"
                parts.append(f"## Worker Role\n{content}\n")
        except Exception:
            pass

    # Load project report (shared context for all workers)
    project_report = _load_project_report_content(max_chars=6000)
    if project_report:
        parts.append(f"## Project Report\n{project_report}\n")

    parts.append(f"\nWorkspace: {WORKSPACE}")
    parts.append(f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(parts)


def _safe_file_sig(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return int(st.st_mtime), int(st.st_size)
    except Exception:
        return 0, 0


def _system_prompt_signature() -> tuple:
    sig_parts = [datetime.now().strftime("%Y-%m-%d")]

    tracked = [
        KUKUIBOT_HOME / "SOUL.md",
        KUKUIBOT_HOME / "USER.md",
        KUKUIBOT_HOME / "TOOLS.md",
        KUKUIBOT_HOME / "PROJECT-REPORT.md",
    ]

    for p in tracked:
        sig_parts.append((str(p), *_safe_file_sig(p)))
    sig_parts.append(len(json.dumps(TOOL_DEFINITIONS, sort_keys=True)))
    return tuple(sig_parts)


def _get_system_prompt_tokens() -> int:
    sig = _system_prompt_signature()
    if _app_state.system_prompt_tokens == 0 or sig != _app_state.system_prompt_sig:
        prompt = _get_system_prompt(model_key="codex")
        tools_str = json.dumps(TOOL_DEFINITIONS, sort_keys=True)
        _app_state.system_prompt_tokens = (len(prompt) + len(tools_str)) // 4
        _app_state.system_prompt_sig = sig
    return _app_state.system_prompt_tokens


def _estimate_tokens(items: list) -> int:
    return sum(len(json.dumps(item)) for item in items) // 4


def _estimate_total_context(items: list) -> int:
    return _get_system_prompt_tokens() + _estimate_tokens(items)


def _effective_context_tokens(items: list, usage: dict | None = None) -> tuple[int, str]:
    """Best-effort estimate of *next request* context tokens.

    - If API usage is available, anchor to API input tokens.
    - Add estimated delta for content appended after the anchored request
      (tool outputs + assistant final text, etc).
    - Never return less than the raw estimate.
    """
    est_now = _estimate_total_context(items)
    if not isinstance(usage, dict):
        return est_now, "estimate"

    api_input = int(usage.get("input_tokens", 0) or 0)
    if api_input <= 0:
        return est_now, "estimate"

    est_input = int(usage.get("est_input_tokens", 0) or 0)
    if est_input > 0:
        delta = max(0, est_now - est_input)
        blended = api_input + delta
        return max(est_now, blended), "api+delta"

    return max(est_now, api_input), "api|max_estimate"


# --- Session Event System (extracted to routes/session_events.py) ---

_session_event_store = SessionEventStore(
    ring_max_events=_EVENT_RING_MAX_EVENTS,
    ring_max_bytes=_EVENT_RING_MAX_BYTES,
    db_max_events_per_session=_EVENT_DB_MAX_EVENTS_PER_SESSION,
    ttl_seconds=_EVENT_TTL_SECONDS,
)
_app_state.session_event_store = _session_event_store

# Wire the event system module with server.py runtime globals.
# init_event_system() is called at module load so _emit_event works immediately.
init_event_system(
    store=_session_event_store,
    active_tasks=_active_tasks,
    resume_subscribers=_resume_subscribers,
    anthropic_event_subscribers=_anthropic_event_subscribers,
    ring_max_events=_EVENT_RING_MAX_EVENTS,
)


# --- ChatGPT API Integration → chat_providers/codex_provider.py (Phase 10a) ---
# _build_headers, _do_request, _parse_sse → chat_providers/codex_provider.py


def _claude_api_key() -> str:
    # Prefer DB-configured key (set via Settings) if present; fallback to env var.
    key = (get_config("claude_code.api_key", "") or "").strip()
    if key:
        return key
    # Option B: read from environment
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


# _sanitize_bearer_token → server_helpers.sanitize_bearer_token


def _claude_oauth_token() -> str:
    # Prefer DB-configured token (set via Settings) if present; fallback to env var.
    tok = (get_config("claude_code.oauth_token", "") or "")
    tok = _sanitize_bearer_token(tok)
    if tok:
        return tok
    return _sanitize_bearer_token(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "")


def _claude_auth_strategy() -> str:
    strategy = (get_config("claude_code.auth_strategy", "configured") or "configured").strip().lower()
    return strategy if strategy in {"configured", "local"} else "configured"


def _openrouter_api_key() -> str:
    """Get OpenRouter API key from DB config, fallback to env var."""
    key = (get_config("openrouter.api_key", "") or "").strip()
    if key:
        return key
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip()


def _openrouter_model(session_id: str) -> str:
    """Get model for an OpenRouter session. Stored per-session or fallback to default."""
    # Check if session has a specific model set
    model = (get_config(f"openrouter.session_model.{session_id}", "") or "").strip()
    if model:
        return model
    return (get_config("openrouter.default_model", "") or "google/gemini-2.5-flash").strip()


# _is_claude_auth_failure → chat_providers/openrouter_provider.py, anthropic_provider.py
# _extract_openrouter_pseudo_tool_calls → chat_providers/openrouter_provider.py
# _check_delegation_completion → routes/delegation.py (Phase 9)


# --- Chat Providers extracted to chat_providers/ (Phase 10a) ---
# _process_chat_claude → chat_providers/claude_provider.py
# _process_chat_openrouter → chat_providers/openrouter_provider.py
# _process_chat_anthropic → chat_providers/anthropic_provider.py
# _process_chat (codex) → chat_providers/codex_provider.py
# _run_with_keepalive → chat_providers/__init__.py



async def _process_chat_claude(queue: asyncio.Queue, session_id: str, user_message: str, run_id: str, *, attachments: list[dict] | None = None, is_internal: bool = False):
    """Claude Code persistent subprocess provider — delegated to chat_providers/claude_provider.py."""
    from chat_providers.claude_provider import process_chat_claude
    await process_chat_claude(
        queue, session_id, user_message, run_id,
        attachments=attachments, is_internal=is_internal,
        active_tasks=_active_tasks,
        runtime_config=_runtime_config,
        app_state=_app_state,
    )


async def _run_with_keepalive(coro, session_id: str, queue: asyncio.Queue, run_id: str, interval: float | None = None):
    """Run an awaitable while sending SSE keepalive pings — delegated to chat_providers."""
    from chat_providers import run_with_keepalive
    if interval is None:
        interval = float(_SSE_KEEPALIVE_SECONDS)
    return await run_with_keepalive(coro, session_id, queue, run_id, interval=interval, emit_event=_emit_event)


async def _process_chat_openrouter(queue: asyncio.Queue, session_id: str, user_message: str, run_id: str, *, attachments: list[dict] | None = None):
    """OpenRouter provider — delegated to chat_providers/openrouter_provider.py."""
    from chat_providers.openrouter_provider import process_chat_openrouter
    await process_chat_openrouter(
        queue, session_id, user_message, run_id,
        attachments=attachments,
        active_tasks=_active_tasks,
        runtime_config=_runtime_config,
        last_api_usage=_last_api_usage,
        openrouter_tools_unsupported_until=_OPENROUTER_TOOLS_UNSUPPORTED_UNTIL,
        app_state=_app_state,
    )


async def _process_chat_anthropic(queue: asyncio.Queue, session_id: str, user_message: str, run_id: str, *, attachments: list[dict] | None = None):
    """Anthropic Messages API provider — delegated to chat_providers/anthropic_provider.py."""
    from chat_providers.anthropic_provider import process_chat_anthropic
    await process_chat_anthropic(
        queue, session_id, user_message, run_id,
        attachments=attachments,
        active_tasks=_active_tasks,
        runtime_config=_runtime_config,
        last_api_usage=_last_api_usage,
        anthropic_containers=_anthropic_containers,
        app_state=_app_state,
    )


async def _process_chat(queue: asyncio.Queue, session_id: str, user_message: str, run_id: str, *, attachments: list[dict] | None = None):
    """Codex/OpenAI provider — delegated to chat_providers/codex_provider.py."""
    from chat_providers.codex_provider import process_chat_codex
    await process_chat_codex(
        queue, session_id, user_message, run_id,
        attachments=attachments,
        active_tasks=_active_tasks,
        runtime_config=_runtime_config,
        last_api_usage=_last_api_usage,
        active_docs=_active_docs,
        app_state=_app_state,
    )


# --- Chat routes extracted to routes/chat.py (Phase 10b) ---
# /api/chat → routes/chat.py
# /api/chat/cancel → routes/chat.py


# --- API Routes ---

# _is_claude_session, _claude_model_for_session, _is_openrouter_session,
# _is_anthropic_session → server_helpers (imported above)


def _anthropic_api_key() -> str:
    """Get Anthropic API key — check dedicated key first, then fall back to Claude Code key."""
    dedicated = (get_config("anthropic.api_key", "") or "").strip()
    if dedicated:
        return dedicated
    return _claude_api_key()


def _anthropic_model(session_id: str) -> str:
    """Resolve Anthropic model for a session. Per-session override or profile default."""
    model = (get_config(f"anthropic.session_model.{session_id}", "") or "").strip()
    if model:
        return model
    # Derive from profile
    profile = _resolve_profile(session_id)
    cfg = _MODEL_PROFILES.get(profile, _MODEL_PROFILES["anthropic"])
    return cfg["api_model"]


# @app.post("/api/chat") → routes/chat.py (Phase 10b)
# @app.post("/api/chat/cancel") → routes/chat.py (Phase 10b)


@app.get("/api/tokens")
async def api_tokens(session_id: str = "default"):
    profile, context_window, compaction_threshold = _profile_limits(session_id)
    items, _, db_usage = load_history(session_id)
    live_usage = _last_api_usage.get(session_id, {})
    usage = live_usage if live_usage else (db_usage if isinstance(db_usage, dict) else {})
    tokens, source = _effective_context_tokens(items, usage)
    return {
        "estimated_tokens": tokens,
        "context_window": context_window,
        "compaction_threshold": compaction_threshold,
        "usage_percent": round(tokens / context_window * 100, 1),
        "message_count": len(items),
        "source": source,
        "profile": profile,
        "model": _MODEL_PROFILES[profile]["ui_model"],
        "api_model": _MODEL_PROFILES[profile]["api_model"],
        "api_input_tokens": int((usage or {}).get("input_tokens", 0) or 0),
        "estimated_input_tokens": int((usage or {}).get("est_input_tokens", 0) or 0),
        "cached_tokens": int((usage or {}).get("cached_tokens", 0) or 0),
        "reasoning_tokens": int((usage or {}).get("reasoning_tokens", 0) or 0),
    }


@app.get("/api/token-debug")
async def api_token_debug(session_id: str = "default"):
    profile, context_window, compaction_threshold = _profile_limits(session_id)
    items, _, db_usage = load_history(session_id)
    live_usage = _last_api_usage.get(session_id, {})
    usage = live_usage if live_usage else (db_usage if isinstance(db_usage, dict) else {})

    est_now = _estimate_total_context(items)
    effective, source = _effective_context_tokens(items, usage)
    est_input = int((usage or {}).get("est_input_tokens", 0) or 0)
    api_input = int((usage or {}).get("input_tokens", 0) or 0)
    delta = api_input - est_input if api_input else 0
    delta_pct = round((delta / api_input) * 100, 2) if api_input else 0.0

    return {
        "session_id": session_id,
        "profile": profile,
        "model": _MODEL_PROFILES[profile]["ui_model"],
        "api_model": _MODEL_PROFILES[profile]["api_model"],
        "context_window": context_window,
        "compaction_threshold": compaction_threshold,
        "message_count": len(items),
        "estimate_now_tokens": est_now,
        "effective_tokens": effective,
        "effective_source": source,
        "api_input_tokens": api_input,
        "estimated_input_tokens": est_input,
        "delta_tokens": delta,
        "delta_pct_of_api": delta_pct,
        "cached_tokens": int((usage or {}).get("cached_tokens", 0) or 0),
        "reasoning_tokens": int((usage or {}).get("reasoning_tokens", 0) or 0),
        "usage_captured_at": int((usage or {}).get("captured_at", 0) or 0),
        "log_store": "sqlite",
    }


@app.post("/api/auto-name")
async def api_auto_name(req: Request):
    """Auto-name worker tabs from recent session activity (<=2 words each)."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    tab_specs = body.get("tabs") or []
    if not isinstance(tab_specs, list) or not tab_specs:
        return JSONResponse({"error": "No tabs provided"}, status_code=400)

    # Build per-tab context from SQLite chat logs (last 10 messages per session).
    skip_rx = re.compile(
        r"(?i)^(commit\b|push\b|git\s+status\b|status\b|done\b|check\b|commit\s+check\b|please\s+commit\b)$"
    )
    prepared_tabs: list[dict] = []
    for t in tab_specs[:40]:
        if not isinstance(t, dict):
            continue
        tab_id = str(t.get("id") or "").strip()
        session_id = str(t.get("session_id") or "").strip()
        current_label = str(t.get("label") or "").strip()
        model_key = str(t.get("model_key") or "").strip()
        if not tab_id or not session_id:
            continue

        recent: list[dict] = []
        try:
            rows = log_query(category="chat", session_id=session_id, limit=30, order="DESC")
            rows.reverse()
            for r in rows:
                role = (r["role"] or "").lower()
                if role not in ("user", "assistant"):
                    continue
                text = (r["message"] or "").strip()
                if not text:
                    continue
                compact = re.sub(r"\s+", " ", text)[:120]
                if len(compact) <= 24 and skip_rx.match(compact):
                    continue
                recent.append({"role": role, "content": text[:450]})
            recent = recent[-10:]
        except Exception:
            pass

        prepared_tabs.append({
            "id": tab_id,
            "session_id": session_id,
            "label": current_label,
            "model_key": model_key,
            "recent": recent,
        })

    if not prepared_tabs:
        return JSONResponse({"error": "No valid tabs"}, status_code=400)

    task = (
        "Name each chat tab in exactly 2 words. Return ONLY valid JSON (no markdown).\n"
        "Output schema: {\"names\":[{\"id\":\"...\",\"name\":\"Two Words\"}]}\n"
        "Pick the most specific noun or verb+noun from the conversation. Title case.\n"
        "Examples: 'Codex Rebrand', 'Sidebar Fix', 'Gmail Setup', 'Icon Update', 'Auth Bug', 'Token Audit'.\n"
        "Ignore commit/push/status chatter — focus on the main technical work.\n"
        "NEVER use generic words like General, Help, Request, Chat, Task, Question.\n\n"
        f"Tabs JSON:\n{json.dumps(prepared_tabs, ensure_ascii=False)}"
    )

    # run_subagent is blocking; execute in thread pool.
    result_text = await asyncio.get_event_loop().run_in_executor(None, run_subagent, task, 8, "auto-name")

    names: dict[str, str] = {}
    raw = (result_text or "").strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        parsed = json.loads(m.group(0)) if m else {}

    banned_name_rx = re.compile(r"(?i)^(commit|commit\s+check|check|status|done|push)(\s+\w+)?$")

    bad_names_rx = re.compile(r"(?i)^(general|help|request|chat|new chat|question|task|assist|assistance|update|fix)$")

    for row in (parsed.get("names") or []):
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip()
        nm = str(row.get("name") or "").strip()
        if not rid:
            continue
        nm = re.sub(r"\s+", " ", nm).strip().title()
        words = [w for w in nm.split(" ") if w]
        nm = " ".join(words[:2]) if words else ""
        if not nm or bad_names_rx.match(nm) or banned_name_rx.match(nm):
            nm = "New Chat"
        names[rid] = nm

    renamed = []
    for t in prepared_tabs:
        rid = t["id"]
        if rid in names:
            renamed.append({
                "id": rid,
                "session_id": t.get("session_id", ""),
                "model_key": t.get("model_key", ""),
                "name": names[rid],
            })

    # Persist auto-generated names server-side so labels are consistent across devices
    # even if the client doesn't immediately complete a /api/tabs/sync round-trip.
    owner = _resolve_owner_username(req)
    if owner and renamed:
        try:
            with db_connection() as db:
                _ensure_tab_meta_schema(db)
                now_s = int(time.time())
                now_ms = int(time.time() * 1000)
                for row in renamed:
                    session_id = str(row.get("session_id") or "").strip()
                    if not session_id:
                        continue
                    tab_id = str(row.get("id") or "").strip()
                    model_key = str(row.get("model_key") or "").strip()
                    label = str(row.get("name") or "").strip()
                    db.execute(
                        """
                        INSERT INTO tab_meta (owner, session_id, tab_id, model_key, label, label_updated_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(owner, session_id) DO UPDATE SET
                            tab_id = excluded.tab_id,
                            model_key = excluded.model_key,
                            label = CASE
                                WHEN excluded.label_updated_at >= COALESCE(tab_meta.label_updated_at, 0)
                                    THEN excluded.label
                                ELSE tab_meta.label
                            END,
                            label_updated_at = CASE
                                WHEN excluded.label_updated_at >= COALESCE(tab_meta.label_updated_at, 0)
                                    THEN excluded.label_updated_at
                                ELSE COALESCE(tab_meta.label_updated_at, 0)
                            END,
                            updated_at = excluded.updated_at
                        """,
                        (owner, session_id, tab_id, model_key, label, now_ms, now_s),
                    )
                db.commit()
        except Exception:
            # Keep endpoint resilient: return rename suggestions even if persistence fails.
            pass

    return {"ok": True, "renamed": [{"id": r["id"], "name": r["name"]} for r in renamed], "count": len(renamed)}


@app.post("/api/auto-name-single")
async def api_auto_name_single(req: Request):
    """Lightweight single-tab auto-name from first user message text."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    tab_id = str(body.get("tab_id") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    model_key = str(body.get("model_key") or "").strip()
    user_text = str(body.get("text") or "").strip()[:500]

    if not tab_id or not user_text:
        return JSONResponse({"error": "tab_id and text required"}, status_code=400)

    task = (
        "Name this chat tab in exactly 2 words. Return ONLY {\"name\":\"Two Words\"}.\n"
        "Pick the most specific noun or verb+noun. Title case.\n"
        "Examples: 'Codex Rebrand', 'Sidebar Fix', 'Gmail Setup', 'Icon Update', 'Auth Bug', 'Token Audit'.\n"
        "NEVER use generic words like General, Help, Request, Chat, Task, Question, Update, Fix alone.\n\n"
        f"User message:\n{user_text}"
    )

    result_text = await asyncio.get_event_loop().run_in_executor(None, run_subagent, task, 6, "auto-name-single")

    # Build a fallback name from the user's message (2 most specific words)
    _stop = {"please", "can", "you", "could", "would", "i", "want", "to", "the", "a", "an",
             "my", "me", "for", "this", "that", "it", "is", "are", "be", "do", "hi", "hey",
             "hello", "thanks", "thank", "ok", "okay", "use", "show", "left", "right",
             "bar", "on", "in", "of", "with", "from", "so", "just", "also", "now", "here"}
    _fallback_words = [w for w in re.findall(r"[a-zA-Z]{2,}", user_text) if w.lower() not in _stop]
    fallback_name = " ".join(_fallback_words[:2]).title() if _fallback_words else "New Chat"

    raw = (result_text or "").strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        parsed = json.loads(m.group(0)) if m else {}

    nm = str(parsed.get("name") or "").strip()
    nm = re.sub(r"\s+", " ", nm).strip().title()
    words = [w for w in nm.split(" ") if w]
    nm = " ".join(words[:2]) if words else ""

    # Reject generic/useless names — use fallback instead
    _bad = re.compile(r"(?i)^(general|help|request|chat|new chat|question|task|assist|assistance|update|fix)$")
    _banned = re.compile(r"(?i)^(commit|commit\s+check|check|status|done|push)(\s+\w+)?$")
    if not nm or _bad.match(nm) or _banned.match(nm):
        nm = fallback_name
    name = nm or fallback_name

    # Persist server-side
    owner = _resolve_owner_username(req)
    if owner and session_id:
        try:
            with db_connection() as db:
                _ensure_tab_meta_schema(db)
                now_s = int(time.time())
                now_ms = int(time.time() * 1000)
                db.execute(
                    """
                    INSERT INTO tab_meta (owner, session_id, tab_id, model_key, label, label_updated_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner, session_id) DO UPDATE SET
                        tab_id = excluded.tab_id,
                        model_key = excluded.model_key,
                        label = CASE
                            WHEN excluded.label_updated_at >= COALESCE(tab_meta.label_updated_at, 0)
                                THEN excluded.label
                            ELSE tab_meta.label
                        END,
                        label_updated_at = CASE
                            WHEN excluded.label_updated_at >= COALESCE(tab_meta.label_updated_at, 0)
                                THEN excluded.label_updated_at
                            ELSE COALESCE(tab_meta.label_updated_at, 0)
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (owner, session_id, tab_id, model_key, name, now_ms, now_s),
                )
                db.commit()
        except Exception:
            pass

    return {"ok": True, "name": name}


@app.post("/api/compact")
async def api_compact(req: Request):
    body = await req.json()
    session_id = body.get("session_id", "default")
    items, _, _ = load_history(session_id)
    if len(items) <= 10:
        return {"ok": False, "reason": "Too few messages"}
    before_tokens = _estimate_total_context(items)
    before_items = len(items)
    from compaction import compact_messages

    _mk3 = _model_key_from_session(session_id)
    _wi3 = _worker_identity_for_session(session_id)
    items = await asyncio.get_event_loop().run_in_executor(
        None, lambda: compact_messages(items, session_id=session_id, model_key=_mk3, worker_identity=_wi3),
    )
    _active_docs.pop(session_id, None)
    save_history(session_id, items)
    _last_api_usage.pop(session_id, None)
    after_tokens = _estimate_total_context(items)
    return {
        "ok": True,
        "before": {"tokens": before_tokens, "items": before_items},
        "after": {"tokens": after_tokens, "items": len(items)},
        "docs_reabsorbed": 0,
        "kept_messages": len(items) - 2,  # subtract summary + ack pair
    }


@app.get("/api/chatlog")
async def api_chatlog(session_id: str = "", n: int = 10, offset: int = 0, before_id: int = 0):
    """GET /api/chatlog?session_id=xxx&n=10&offset=0 — Paginated chat messages from SQLite log.

    Each entry: {id, role, text, ts, session_id}
    Used for cross-device UI hydration (server is source of truth).
    Filters by session_id if provided; returns all sessions if empty.
    Orders by row id (deterministic) instead of ts_unix (has duplicates).

    Params:
      n         — number of messages to return (default 10, max 2000)
      offset    — skip this many messages from the end (default 0 = most recent)
      before_id — cursor-based pagination: only return rows with id < before_id
    Returns:
      {messages: [...], count: N, total: T, has_more: bool}
    """
    n = min(max(n, 1), 2000)
    offset = max(offset, 0)

    try:
        total = log_count(category="chat", session_id=session_id or None)

        # Direct SQL query ordered by id (deterministic) instead of ts_unix (has duplicates)
        import sqlite3 as _chatlog_sqlite3
        conn = _chatlog_sqlite3.connect(str(LOG_DB_PATH), timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")

        clauses = ["category = 'chat'"]
        params: list = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if before_id > 0:
            clauses.append("id < ?")
            params.append(before_id)

        where = "WHERE " + " AND ".join(clauses)
        params.extend([n, offset])

        rows = conn.execute(
            f"SELECT id, ts_unix, role, message, session_id FROM logs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()

        # Count matching rows for accurate has_more when using before_id cursor
        if before_id > 0:
            count_row = conn.execute(f"SELECT COUNT(*) FROM logs {where}", params[:-2]).fetchone()
            filtered_total = count_row[0] if count_row else 0
        else:
            filtered_total = total
        conn.close()

        # Reverse to chronological order
        page = []
        for r in reversed(rows):
            role = r[2] or "system"
            if role not in ("user", "assistant", "system"):
                continue
            ts_ms = int(r[1] * 1000) if r[1] else int(time.time() * 1000)
            page.append({
                "id": r[0],
                "role": role,
                "text": r[3],
                "ts": ts_ms,
                "session_id": r[4] or "",
            })

        has_more = (offset + len(rows)) < filtered_total
    except Exception as e:
        logger.warning(f"Failed to read chat log for /api/chatlog: {e}")
        page = []
        total = 0
        has_more = False

    return JSONResponse({"messages": page, "count": len(page), "total": total, "has_more": has_more}, headers={"Cache-Control": "no-store"})


@app.get("/api/logs/status")
async def api_logs_status():
    started = time.perf_counter()
    try:
        stats = log_stats()
        db_size = log_db_size()
        payload = {
            "stats": stats,
            "db_size": db_size,
            "db_size_human": _human_bytes(db_size),
        }
    except Exception as e:
        payload = {"stats": [], "error": str(e)}
    payload["scan_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/logs/cleanup")
async def api_logs_cleanup(req: Request):
    if not _check_rate_limit(req, "logs_cleanup", 60):
        return JSONResponse({"ok": False, "error": "Rate limit: wait 60 seconds"}, status_code=429)

    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    max_age_days = _clamp_int(
        (body or {}).get("max_age_days", 30),
        default=30, min_value=1, max_value=3650,
    )
    try:
        result = log_purge(max_age_days=max_age_days)
        return {"ok": True, "max_age_days": max_age_days, **result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/logs/purge")
async def api_logs_purge(req: Request):
    """Purge SQLite logs older than N days."""
    if not _check_rate_limit(req, "logs_purge", 60):
        return JSONResponse({"ok": False, "error": "Rate limit: wait 60 seconds"}, status_code=429)
    try:
        body = await req.json()
    except Exception:
        body = {}
    max_age_days = _clamp_int(
        (body or {}).get("max_age_days", 30),
        default=30, min_value=1, max_value=3650,
    )
    try:
        result = log_purge(max_age_days=max_age_days)
        return {"ok": True, **result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/logs/query")
async def api_logs_query(
    category: str = "",
    session_id: str = "",
    worker: str = "",
    level: str = "",
    source: str = "",
    search: str = "",
    since: float = 0,
    until: float = 0,
    limit: int = 100,
    offset: int = 0,
):
    """Query SQLite logs with filters. Returns structured log entries."""
    try:
        rows = log_query(
            category=category or None,
            session_id=session_id or None,
            worker=worker or None,
            level=level or None,
            source=source or None,
            search=search or None,
            since_unix=since if since > 0 else None,
            until_unix=until if until > 0 else None,
            limit=min(max(limit, 1), 1000),
            offset=max(offset, 0),
        )
        total = log_count(
            category=category or None,
            session_id=session_id or None,
            since_unix=since if since > 0 else None,
            until_unix=until if until > 0 else None,
            level=level or None,
        )
        return {
            "ok": True,
            "entries": rows,
            "count": len(rows),
            "total": total,
            "has_more": (offset + len(rows)) < total,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)



@app.get("/api/status")
async def api_status(session_id: str = "default"):
    task = _active_tasks.get(session_id)
    latest = _session_event_store.latest_event_id(session_id)
    if not task:
        return {"status": "idle", "runtime_started": RUNTIME_STARTED, "last_seq": int(latest), "app_name": APP_NAME}
    last_seq = max(int(latest), int(task.get("next_seq", 1)) - 1)
    return {
        "status": task["status"],
        "run_id": str(task.get("run_id") or ""),
        "elapsed": round(time.time() - task.get("started", time.time()), 1),
        "runtime_started": RUNTIME_STARTED,
        "last_seq": int(last_seq),
        "last_event_at": float(task.get("last_event_at", task.get("started", time.time()))),
        "app_name": APP_NAME,
    }

@app.get("/api/events")
async def api_events(req: Request):
    session_id = str(req.query_params.get("session_id", "")).strip()
    if not session_id:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)

    raw_last = req.query_params.get("last_event_id")
    if raw_last is None:
        raw_last = req.headers.get("Last-Event-ID", "0")
    try:
        after = int(str(raw_last or "0").strip())
        if after < 0:
            raise ValueError("last_event_id must be >= 0")
    except Exception:
        return JSONResponse({"ok": False, "error": "last_event_id must be an integer >= 0"}, status_code=400)

    def _event_id_from_sse_payload(payload: str) -> int:
        try:
            parts: list[str] = []
            for line in str(payload).splitlines():
                if line.startswith("data: "):
                    parts.append(line[6:])
            if not parts:
                return 0
            evt = json.loads("\n".join(parts))
            return int(evt.get("event_id") or evt.get("seq") or 0)
        except Exception:
            return 0

    async def stream():
        q: asyncio.Queue = asyncio.Queue()
        _resume_subscribers.setdefault(session_id, []).append(q)
        last_sent_id = int(after)
        try:
            replay = _session_event_store.replay(session_id=session_id, after_event_id=after)
            for evt in replay:
                eid = int(evt.get("event_id") or evt.get("seq") or 0)
                if eid <= last_sent_id:
                    continue
                last_sent_id = eid
                yield f"id: {eid}\ndata: {json.dumps(evt)}\n\n"

            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=float(_SSE_KEEPALIVE_SECONDS))
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break

                event_id_val = _event_id_from_sse_payload(event)
                if event_id_val > 0:
                    if event_id_val <= last_sent_id:
                        continue
                    last_sent_id = event_id_val
                    yield f"id: {event_id_val}\n{event}"
                else:
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            subs = _resume_subscribers.get(session_id, [])
            _resume_subscribers[session_id] = [x for x in subs if x is not q]

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Delegation routes extracted to routes/delegation.py (Phase 9) ---
# api_delegated_tasks, api_delegate, api_delegate_check, api_delegate_list,
# api_delegate_activity, api_delegate_dismiss


@app.get("/api/resume")
async def api_resume(session_id: str = "default", after_seq: int = 0):
    async def stream():
        after = int(after_seq or 0)
        replay = _session_event_store.replay_events(session_id, after_event_id=after)
        for evt in replay:
            yield f"data: {json.dumps(evt)}\n\n"

        task = _active_tasks.get(session_id)
        if not task:
            latest = _session_event_store.latest_event_id(session_id)
            reason = "idle" if latest <= after else "replay_complete"
            yield f"data: {json.dumps({'type': 'resume_end', 'reason': reason, 'run_id': ''})}\n\n"
            return
        if task.get("status") != "running":
            yield f"data: {json.dumps({'type': 'resume_end', 'reason': 'done', 'run_id': str(task.get('run_id') or '')})}\n\n"
            return

        q: asyncio.Queue = asyncio.Queue()
        _resume_subscribers.setdefault(session_id, []).append(q)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=float(_SSE_KEEPALIVE_SECONDS))
                except asyncio.TimeoutError:
                    keepalive = {"type": "keepalive", "ts": time.time(), "run_id": str(task.get('run_id') or '')}
                    yield f"data: {json.dumps(keepalive)}\n\n"
                    continue
                if event is None:
                    break
                yield event
                if '"type": "done"' in event:
                    break
        finally:
            subs = _resume_subscribers.get(session_id, [])
            _resume_subscribers[session_id] = [x for x in subs if x is not q]
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


## Old duplicate /api/claude/events removed — the canonical one is below in the
## "Claude Persistent Process Management" section, updated for multi-process pool.


@app.get("/api/runtime")
async def api_runtime():
    return {"runtime_started": RUNTIME_STARTED}


@app.get("/api/system-stats")
async def api_system_stats():
    """Lightweight host stats for /status slash output.

    Best-effort endpoint: returns partial data instead of failing hard.
    """
    errors: list[str] = []

    # CPU
    cores = os.cpu_count() or 1
    load1 = 0.0
    cpu_percent = 0.0
    try:
        load1, _, _ = os.getloadavg()
        cpu_percent = round(max(0.0, min(100.0, (load1 / max(1, cores)) * 100.0)), 1)
    except Exception as e:
        errors.append(f"cpu:{e}")

    # Memory (macOS via vm_stat)
    used_bytes = 0
    free_bytes = 0
    total_bytes = 0
    mem_used_pct = 0.0
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)
        vm_out = vm.stdout or ""
        page_size = 4096
        m = re.search(r"page size of (\d+) bytes", vm_out)
        if m:
            page_size = int(m.group(1))

        def _pages(label: str) -> int:
            mm = re.search(rf"{re.escape(label)}:\s+(\d+)\.", vm_out)
            return int(mm.group(1)) if mm else 0

        free_pages = _pages("Pages free")
        speculative_pages = _pages("Pages speculative")
        active_pages = _pages("Pages active")
        wired_pages = _pages("Pages wired down")
        compressed_pages = _pages("Pages occupied by compressor")

        free_bytes = int((free_pages + speculative_pages) * page_size)
        used_bytes = int((active_pages + wired_pages + compressed_pages) * page_size)
        total_bytes = int(free_bytes + used_bytes)
        mem_used_pct = round((used_bytes / total_bytes) * 100.0, 1) if total_bytes > 0 else 0.0
    except Exception as e:
        errors.append(f"memory:{e}")

    # Disk (filesystem where KUKUIBOT_HOME lives)
    disk_total = 0
    disk_used = 0
    disk_free = 0
    disk_used_pct = 0.0
    try:
        du = shutil.disk_usage(str(KUKUIBOT_HOME))
        disk_total = int(du.total)
        disk_used = int(du.used)
        disk_free = int(du.free)
        disk_used_pct = round((disk_used / disk_total) * 100.0, 1) if disk_total > 0 else 0.0
    except Exception as e:
        errors.append(f"disk:{e}")

    return {
        "ok": True,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "cpu": {
            "cores": cores,
            "load1": round(load1, 2),
            "used_percent": cpu_percent,
        },
        "memory": {
            "used_bytes": int(used_bytes),
            "free_bytes": int(free_bytes),
            "total_bytes": int(total_bytes),
            "used_percent": mem_used_pct,
        },
        "disk": {
            "used_bytes": disk_used,
            "free_bytes": disk_free,
            "total_bytes": disk_total,
            "used_percent": disk_used_pct,
        },
        "errors": errors,
    }


@app.get("/api/security-quick-check")
async def api_security_quick_check():
    """Fast local security posture checks for status card health indicators.

    Non-invasive and quick-to-run only. Returns severity-tagged findings.
    """

    def _run(cmd: list[str], timeout: float = 2.5) -> tuple[int, str, str]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
        except Exception as e:
            return 1, "", str(e)

    findings: list[dict] = []

    # 1) Sudo posture: detect blanket NOPASSWD: ALL for current user.
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    sudo_state = "unknown"
    sudo_detail = "sudo check unavailable"
    rc, out, err = _run(["sudo", "-n", "-l"])
    if rc == 0:
        txt = out.lower()
        has_nopasswd_all = ("nopasswd: all" in txt)
        has_any_nopasswd = ("nopasswd:" in txt)
        if has_nopasswd_all:
            sudo_state = "critical"
            sudo_detail = "passwordless sudo is enabled (NOPASSWD: ALL)"
            findings.append({
                "id": "sudo_nopasswd_all",
                "severity": "critical",
                "title": "Passwordless sudo",
                "status": "critical",
                "detail": sudo_detail,
                "recommendation": "Remove broad NOPASSWD: ALL and use least-privilege sudo rules.",
            })
        elif has_any_nopasswd:
            sudo_state = "warn"
            sudo_detail = "sudo has command-level NOPASSWD entries"
            findings.append({
                "id": "sudo_nopasswd_entries",
                "severity": "warn",
                "title": "Sudo NOPASSWD entries",
                "status": "warn",
                "detail": "Detected command-level NOPASSWD rules.",
                "recommendation": "Keep narrowly scoped; remove if not required.",
            })
        else:
            sudo_state = "ok"
            sudo_detail = "no NOPASSWD sudo entries detected"
    else:
        # Password-required sudo is a safer default than passwordless sudo.
        detail = (err or out or "sudo list denied or requires password")[:240]
        if "password is required" in detail.lower() or "a password is required" in detail.lower():
            sudo_state = "ok"
            sudo_detail = "sudo requires password (non-interactive check blocked)"
        else:
            sudo_state = "warn"
            sudo_detail = detail
            findings.append({
                "id": "sudo_visibility",
                "severity": "warn",
                "title": "Sudo policy visibility",
                "status": "warn",
                "detail": "Could not inspect sudo policy non-interactively.",
                "recommendation": "Run `sudo -l` manually to verify no broad NOPASSWD rules exist.",
            })

    # 2) Firewall status — warn only if disabled (as requested).
    fw_state = "unknown"
    fw_detail = "firewall status unavailable"
    rc, out, err = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    txt = (out or err).lower()
    if rc == 0 and txt:
        if "disabled" in txt or "state = 0" in txt:
            fw_state = "warn"
            fw_detail = "Firewall disabled"
            findings.append({
                "id": "firewall_disabled",
                "severity": "warn",
                "title": "macOS Firewall",
                "status": "warn",
                "detail": "Firewall is disabled.",
                "recommendation": "Enable firewall unless you intentionally rely on unrestricted LAN exposure.",
            })
        elif "enabled" in txt or "state = 1" in txt:
            fw_state = "ok"
            fw_detail = "Firewall enabled"
        else:
            fw_state = "warn"
            fw_detail = out or err or "unknown firewall state"
    else:
        fw_state = "warn"
        fw_detail = (err or out or "firewall check failed")[:200]

    # 3) Remote Login (SSH) status.
    ssh_state = "unknown"
    ssh_detail = "remote login status unavailable"
    rc, out, err = _run(["sudo", "-n", "/usr/sbin/systemsetup", "-getremotelogin"])
    txt = (out or err).lower()
    if rc == 0 and txt:
        if "on" in txt:
            ssh_state = "warn"
            ssh_detail = out or "Remote Login: On"
            findings.append({
                "id": "remote_login_on",
                "severity": "warn",
                "title": "Remote Login (SSH)",
                "status": "warn",
                "detail": "Remote Login is enabled.",
                "recommendation": "Disable if not needed, or restrict via firewall and key-only auth.",
            })
        elif "off" in txt:
            ssh_state = "ok"
            ssh_detail = out or "Remote Login: Off"
        else:
            ssh_state = "warn"
            ssh_detail = out or err
    else:
        detail = (err or out or "remote login check failed")[:240]
        if "password is required" in detail.lower() or "administrator access" in detail.lower():
            ssh_state = "unknown"
            ssh_detail = "check requires sudo password"
        else:
            ssh_state = "warn"
            ssh_detail = detail

    # 4) Sudoers include mode hygiene (best-practice: 0440)
    sudoers_mode_state = "warn"
    sudoers_mode_detail = "sudoers include mode check failed"
    mode_issues: list[str] = []
    rc, out, err = _run(["/bin/sh", "-lc", "ls -l /etc/sudoers.d 2>/dev/null | tail -n +2"])
    if rc == 0:
        for line in (out or "").splitlines():
            parts = line.split()
            if len(parts) < 9:
                continue
            perm = parts[0]
            name = parts[-1]
            # Expected -r--r----- (440) for strict mode.
            if perm != "-r--r-----":
                mode_issues.append(f"{name} ({perm})")
        if mode_issues:
            sudoers_mode_state = "warn"
            sudoers_mode_detail = ", ".join(mode_issues)
            findings.append({
                "id": "sudoers_include_mode",
                "severity": "warn",
                "title": "sudoers.d permissions",
                "status": "warn",
                "detail": "Some sudoers include files are not 0440.",
                "recommendation": "Set mode 0440 on /etc/sudoers.d/* and validate with `visudo -c`.",
            })
        else:
            sudoers_mode_state = "ok"
            sudoers_mode_detail = "all include files are 0440"
    else:
        sudoers_mode_state = "warn"
        sudoers_mode_detail = (err or out or "unable to list /etc/sudoers.d")[:220]

    # 5) FileVault status.
    filevault_state = "warn"
    filevault_detail = "FileVault status unavailable"
    rc, out, err = _run(["fdesetup", "status"])
    txt = (out or err).lower()
    if rc == 0 and txt:
        if "filevault is on" in txt:
            filevault_state = "ok"
            filevault_detail = "FileVault is on"
        elif "filevault is off" in txt:
            filevault_state = "warn"
            filevault_detail = "FileVault is off"
            findings.append({
                "id": "filevault_off",
                "severity": "warn",
                "title": "FileVault",
                "status": "warn",
                "detail": "Full-disk encryption is disabled.",
                "recommendation": "Enable FileVault to protect data at rest.",
            })
        else:
            filevault_detail = (out or err)[:220]

    # 6) SIP status.
    sip_state = "warn"
    sip_detail = "SIP status unavailable"
    rc, out, err = _run(["csrutil", "status"])
    txt = (out or err).lower()
    if txt:
        if "enabled" in txt:
            sip_state = "ok"
            sip_detail = "SIP enabled"
        elif "disabled" in txt:
            sip_state = "warn"
            sip_detail = "SIP disabled"
            findings.append({
                "id": "sip_disabled",
                "severity": "warn",
                "title": "System Integrity Protection",
                "status": "warn",
                "detail": "SIP is disabled.",
                "recommendation": "Re-enable SIP unless you intentionally require it off.",
            })
        else:
            sip_detail = (out or err)[:220]

    # Overall severity roll-up.
    sev_rank = {"ok": 0, "warn": 1, "critical": 2}
    overall = "ok"
    for f in findings:
        if sev_rank.get(f.get("severity", "ok"), 0) > sev_rank[overall]:
            overall = f.get("severity", "ok")

    return {
        "ok": True,
        "checked_user": user,
        "checked_at": int(time.time()),
        "overall": overall,
        "checks": {
            "sudo": {"state": sudo_state, "detail": sudo_detail},
            "firewall": {"state": fw_state, "detail": fw_detail},
            "remote_login": {"state": ssh_state, "detail": ssh_detail},
            "sudoers_include_mode": {"state": sudoers_mode_state, "detail": sudoers_mode_detail},
            "filevault": {"state": filevault_state, "detail": filevault_detail},
            "sip": {"state": sip_state, "detail": sip_detail},
        },
        "findings": findings,
    }


# --- Usage cache (60s TTL) to avoid blocking event loop on every page load ---
_usage_cache = _app_state.usage_cache
_USAGE_CACHE_TTL = 60  # seconds

@app.get("/api/usage")
async def api_usage():
    """Fetch ChatGPT plan usage — returns both main Codex and Spark limits."""
    # Return cached result if fresh
    now = time.time()
    if _usage_cache.get("data") and (now - _usage_cache.get("ts", 0)) < _USAGE_CACHE_TTL:
        return _usage_cache["data"]

    try:
        token = get_token()
        if not token:
            return {"ok": False, "error": "No token"}
        account_id = extract_account_id(token)
        headers = {"Authorization": f"Bearer {token}", "ChatGPT-Account-Id": account_id, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://chatgpt.com/backend-api/wham/usage", headers=headers)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}"}
        data = resp.json()

        # Main Codex usage (5hr window + weekly)
        rate = data.get("rate_limit", {})
        primary = rate.get("primary_window", {})
        secondary = rate.get("secondary_window", {})

        result = {
            "ok": True,
            "plan": data.get("plan_type", "unknown"),
            "codex": {
                "hourlyUsedPct": primary.get("used_percent", 0),
                "hourlyResetMin": round(primary.get("reset_after_seconds", 0) / 60),
                "weeklyUsedPct": secondary.get("used_percent", 0),
                "weeklyLeft": max(0, 100 - secondary.get("used_percent", 0)),
            },
        }

        # Spark usage (from additional_rate_limits — may be null on some plans)
        for extra in (data.get("additional_rate_limits") or []):
            if "spark" in extra.get("limit_name", "").lower() or "spark" in extra.get("metered_feature", "").lower():
                spark_rate = extra.get("rate_limit", {})
                sp = spark_rate.get("primary_window", {})
                ss = spark_rate.get("secondary_window") or {}
                result["spark"] = {
                    "hourlyUsedPct": sp.get("used_percent", 0),
                    "hourlyResetMin": round(sp.get("reset_after_seconds", 0) / 60),
                    "weeklyUsedPct": ss.get("used_percent", 0),
                    "weeklyLeft": max(0, 100 - ss.get("used_percent", 0)),
                }
                break

        # Backward compat — top-level fields use codex values
        result["weeklyUsedPct"] = result["codex"]["weeklyUsedPct"]
        result["weeklyLeft"] = result["codex"]["weeklyLeft"]

        # Cache the result
        _usage_cache["data"] = result
        _usage_cache["ts"] = now

        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- Config ---

@app.get("/api/config")
async def api_config_get():
    return _runtime_config


# --- Claude Code Config ---

@app.post("/api/claude/key")
async def api_claude_key_set(req: Request):
    """Set Anthropic API key (admin only). Never returned by API."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    try:
        body = await req.json()
    except Exception:
        body = {}
    raw = str(body.get("api_key") or "")
    key = _sanitize_bearer_token(raw)
    set_config("claude_code.api_key", key)
    return {"ok": True, "saved": bool(key), "length": len(key)}


@app.post("/api/claude/key/clear")
async def api_claude_key_clear(req: Request):
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    set_config("claude_code.api_key", "")
    return {"ok": True, "cleared": True}


@app.get("/api/claude/config")
async def api_claude_config_get(req: Request):
    """Return Claude Code config (admin only)."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    tok = (get_config("claude_code.oauth_token", "") or "")
    has_key = bool((get_config("claude_code.api_key", "") or "").strip() or (os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    strategy = _claude_auth_strategy()
    # Return raw token so user can verify it saved; UI does not mask it.
    # API key is never returned.
    return {"ok": True, "oauth_token": tok, "has_api_key": has_key, "auth_strategy": strategy}


@app.post("/api/claude/config")
async def api_claude_config_set(req: Request):
    """Set Claude Code OAuth token / auth strategy (admin only)."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    try:
        body = await req.json()
    except Exception:
        body = {}
    raw = str(body.get("oauth_token") or "")
    tok = _sanitize_bearer_token(raw)
    set_config("claude_code.oauth_token", tok)

    strategy_raw = str(body.get("auth_strategy") or "").strip().lower()
    if strategy_raw in {"configured", "local"}:
        set_config("claude_code.auth_strategy", strategy_raw)
        pool = get_claude_pool()
        if pool:
            pool._auth_strategy = strategy_raw
            # Update all existing processes
            for proc in pool._processes.values():
                proc.set_auth_strategy(strategy_raw)

    return {
        "ok": True,
        "saved": bool(tok),
        # Helpful debug signal; does not reveal token content.
        "length": len(tok),
        "sanitized": (tok != raw.strip()),
        "auth_strategy": _claude_auth_strategy(),
        "hint": "If Claude says Invalid bearer token, you may have pasted an API key here. API keys belong in ANTHROPIC_API_KEY, not CLAUDE_CODE_OAUTH_TOKEN.",
    }


# --- Claude Persistent Process Management ---

@app.get("/api/claude/status")
async def api_claude_status(req: Request):
    """Return Claude process status for a session, or pool-wide status."""
    pool = get_claude_pool()
    if not pool:
        return JSONResponse({"ok": False, "error": "Not initialized"})
    session_id = req.query_params.get("session_id", "")
    if session_id:
        proc = pool.get(session_id)
        if not proc:
            return {"ok": True, "running": False, "pool": pool.get_all_status()}
        return {"ok": True, **proc.get_status()}
    return {"ok": True, **pool.get_all_status()}


@app.post("/api/claude/restart")
async def api_claude_restart(req: Request):
    """Restart a specific Claude process (by session_id) or all."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    pool = get_claude_pool()
    if not pool:
        return JSONResponse({"ok": False, "error": "Not initialized"})
    try:
        body = await req.json()
    except Exception:
        body = {}
    session_id = body.get("session_id", "")
    try:
        if session_id:
            proc = pool.get(session_id)
            if not proc:
                return JSONResponse({"ok": False, "error": f"No process for session {session_id}"})
            await proc.restart()
            return {"ok": True, "message": f"Process restarted for {session_id}"}
        else:
            await pool.kill_all()
            return {"ok": True, "message": "All Claude processes killed"}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/claude/smart-compact")
async def api_claude_smart_compact(req: Request):
    """Trigger smart compaction on a specific Claude process."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    pool = get_claude_pool()
    if not pool:
        return JSONResponse({"ok": False, "error": "Not initialized"})
    try:
        body = await req.json()
    except Exception:
        body = {}
    session_id = body.get("session_id", "")
    if not session_id:
        # Fall back: compact first process if only one exists
        procs = list(pool._processes.values())
        if len(procs) == 1:
            proc = procs[0]
        else:
            return JSONResponse({"ok": False, "error": "session_id required (multiple processes active)"}, status_code=400)
    else:
        proc = pool.get(session_id)
    if not proc:
        return JSONResponse({"ok": False, "error": f"No process for session {session_id}"})
    result = await proc.smart_compact()
    status_code = 200 if result.get("status") == "ok" else 500
    return JSONResponse({"ok": result.get("status") == "ok", **result}, status_code=status_code)


@app.get("/api/workers")
async def api_workers(req: Request):
    """List available worker identities."""
    import time as _time_profile
    start_time = _time_profile.time()
    
    workers_dir = KUKUIBOT_HOME / "workers"
    workers = []
    if workers_dir.is_dir():
        for f in sorted(workers_dir.glob("*.md")):
            key = f.stem  # e.g. "developer", "it-admin", "seo-assistant"
            try:
                first_line = f.read_text().split("\n", 1)[0].strip()
                # Extract name from "# Worker Identity — Developer" format
                name = first_line.replace("# Worker Identity —", "").replace("# Worker Identity —", "").strip()
                if not name:
                    name = key.replace("-", " ").title()
            except Exception:
                name = key.replace("-", " ").title()
            workers.append({"key": key, "name": name})
    
    elapsed_ms = (_time_profile.time() - start_time) * 1000
    logger.info(f"[PROFILE] /api/workers took {elapsed_ms:.1f}ms (found {len(workers)} workers)")
    
    return {"workers": workers, "workers_dir": str(workers_dir)}


@app.get("/api/workers/{key}")
async def api_worker_get(key: str):
    """Read a worker identity file."""
    # Sanitise: only allow alphanumeric + hyphens
    import re as _re
    if not _re.fullmatch(r"[a-z0-9-]+", key):
        return JSONResponse({"ok": False, "error": "Invalid worker key"}, status_code=400)
    worker_file = KUKUIBOT_HOME / "workers" / f"{key}.md"
    if not worker_file.is_file():
        return JSONResponse({"ok": False, "error": "Worker not found"}, status_code=404)
    content = worker_file.read_text(encoding="utf-8")
    return {"ok": True, "key": key, "content": content}


@app.put("/api/workers/{key}")
async def api_worker_put(key: str, req: Request):
    """Create or update a worker identity file."""
    import re as _re
    if not _re.fullmatch(r"[a-z0-9-]+", key):
        return JSONResponse({"ok": False, "error": "Invalid worker key"}, status_code=400)
    body = await req.json()
    content = str(body.get("content") or "").strip()
    if not content:
        return JSONResponse({"ok": False, "error": "Content is required"}, status_code=400)
    workers_dir = KUKUIBOT_HOME / "workers"
    workers_dir.mkdir(parents=True, exist_ok=True)
    worker_file = workers_dir / f"{key}.md"
    worker_file.write_text(content + "\n", encoding="utf-8")
    return {"ok": True, "key": key}


@app.delete("/api/workers/{key}")
async def api_worker_delete(key: str):
    """Delete a worker identity file."""
    import re as _re
    if not _re.fullmatch(r"[a-z0-9-]+", key):
        return JSONResponse({"ok": False, "error": "Invalid worker key"}, status_code=400)
    worker_file = KUKUIBOT_HOME / "workers" / f"{key}.md"
    if not worker_file.is_file():
        return JSONResponse({"ok": False, "error": "Worker not found"}, status_code=404)
    worker_file.unlink()
    return {"ok": True, "key": key}


@app.post("/api/tab/worker-identity")
async def api_set_worker_identity(req: Request):
    """Set the worker identity for a tab."""
    body = await req.json()
    session_id = str(body.get("session_id") or "").strip()
    worker_identity = str(body.get("worker_identity") or "").strip()
    if not session_id:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    owner = _resolve_owner_username(req)
    if not owner:
        return JSONResponse({"ok": False, "error": "Unable to resolve user"}, status_code=400)
    try:
        with db_connection() as db:
            _ensure_tab_meta_schema(db)
            db.execute(
                "UPDATE tab_meta SET worker_identity = ? WHERE owner = ? AND session_id = ?",
                (worker_identity, owner, session_id),
            )
            db.commit()
            return {"ok": True, "session_id": session_id, "worker_identity": worker_identity}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/claude/events")
async def api_claude_events(req: Request):
    """SSE stream of Claude process events for a specific session.

    Each Claude tab connects with its own session_id to receive events only
    from its own process. Supports multi-browser sync within the same tab session.
    """
    from starlette.responses import StreamingResponse

    pool = get_claude_pool()
    if not pool:
        return JSONResponse({"ok": False, "error": "Not initialized"})

    session_id = req.query_params.get("session_id", "")
    if not session_id:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)

    # Get or create the process for this session so SSE can subscribe
    try:
        _cm = _claude_model_for_session(session_id)
        proc = pool.get_or_create(session_id, model=_cm)
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=429)

    async def stream():
        q = proc.subscribe()
        _cur_tool_name = None
        _cur_tool_input_json = ""
        _cur_tool_detail_sent = False
        _accumulated_text = ""  # Track streamed text for done fallback
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=float(_SSE_KEEPALIVE_SECONDS))
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                event_type = event.get("type", "")
                sse_event = None

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
                                detail = tool_input["pattern"]
                            if detail:
                                _cur_tool_detail_sent = True
                            sse_event = {"type": "ping", "tool": _cur_tool_name, "detail": detail}
                        elif cb.get("type") == "thinking":
                            sse_event = {"type": "thinking_start"}
                    elif inner_type == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            _chunk = delta.get("text", "")
                            _accumulated_text += _chunk
                            sse_event = {"type": "chunk", "text": _chunk}
                        elif delta.get("type") == "thinking_delta":
                            text = delta.get("thinking", "")
                            if text:
                                sse_event = {"type": "thinking", "text": text}
                        elif delta.get("type") == "input_json_delta" and _cur_tool_name and not _cur_tool_detail_sent:
                            _cur_tool_input_json += delta.get("partial_json", "")
                            # Try to extract detail from accumulated JSON fragments
                            detail = ""
                            raw = _cur_tool_input_json
                            if _cur_tool_name == "Bash":
                                # Look for "command":"..." pattern
                                idx = raw.find('"command"')
                                if idx >= 0:
                                    # Find the value after the key
                                    val_start = raw.find('"', idx + 9 + 1)  # skip key + colon area
                                    if val_start >= 0:
                                        val_end = raw.find('"', val_start + 1)
                                        if val_end >= 0:
                                            detail = raw[val_start+1:val_end][:200]
                                        elif len(raw) - val_start > 10:
                                            # Still streaming but we have partial command
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
                                sse_event = {"type": "ping", "tool": _cur_tool_name, "detail": detail}
                    elif inner_type == "content_block_stop":
                        _cur_tool_name = None
                        _cur_tool_input_json = ""
                        _cur_tool_detail_sent = False
                elif event_type == "assistant":
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
                            sse_event = {"type": "ping", "tool": tool_name, "detail": detail}
                elif event_type == "ping":
                    sse_event = {"type": "ping", "elapsed": event.get("elapsed", 0), "tool": event.get("tool", "working")}
                elif event_type == "error":
                    sse_event = {"type": "error", "error": event.get("error", "Unknown error")}
                elif event_type == "result":
                    # Emit context update before done so frontend has fresh token count
                    ctx_tokens = proc.last_input_tokens
                    if ctx_tokens > 0:
                        _, _ctx_win, _ = _profile_limits(session_id)
                        ctx_pct = round(ctx_tokens / _ctx_win, 4)
                        yield f"data: {json.dumps({'type': 'context', 'tokens': ctx_tokens, 'max': _ctx_win, 'pct': ctx_pct, 'source': 'api'})}\n\n"
                    _result_text = event.get("result", "") or _accumulated_text
                    sse_event = {"type": "done", "text": _result_text, "session_id": event.get("session_id"), "tokens": proc.last_input_tokens}
                    _accumulated_text = ""  # Reset for next turn
                elif event_type == "user_message":
                    sse_event = {"type": "user_message", "text": event.get("text", ""), "ts": event.get("ts", 0)}
                elif event_type == "compaction":
                    sse_event = {"type": "compaction", "tokens": event.get("tokens", 0), "active_docs": event.get("active_docs", []), "loaded_files": event.get("loaded_files", [])}
                elif event_type == "compaction_done":
                    sse_event = {"type": "compaction_done", "summary_length": event.get("summary_length"), "compaction_count": event.get("compaction_count"), "loaded_files": event.get("loaded_files", [])}
                elif event_type == "context_loaded":
                    sse_event = {"type": "context_loaded", "loaded_files": event.get("loaded_files", [])}
                elif event_type == "delegation_notification":
                    sse_event = {"type": "delegation_notification", "task_id": event.get("task_id", ""), "status": event.get("status", ""), "message": event.get("message", "")}

                if sse_event:
                    yield f"data: {json.dumps(sse_event)}\n\n"
        finally:
            proc.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- Anthropic Persistent EventSource ---

@app.get("/api/anthropic/events")
async def api_anthropic_events(req: Request):
    """Persistent SSE stream for Anthropic tab events.

    Mirrors the Claude /api/claude/events pattern — browser opens one
    EventSource per Anthropic tab session and receives all events
    (text chunks, tool use, done, errors) through it.
    """
    session_id = req.query_params.get("session_id", "")
    if not session_id or not _is_anthropic_session(session_id):
        return JSONResponse({"ok": False, "error": "Invalid anthropic session_id"}, status_code=400)

    async def stream():
        q: asyncio.Queue = asyncio.Queue()
        _anthropic_event_subscribers.setdefault(session_id, []).append(q)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=float(_SSE_KEEPALIVE_SECONDS))
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            subs = _anthropic_event_subscribers.get(session_id, [])
            _anthropic_event_subscribers[session_id] = [x for x in subs if x is not q]

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- OpenRouter Config ---

@app.get("/api/openrouter/config")
async def api_openrouter_config_get(req: Request):
    """Return OpenRouter config (admin only), including full model registry."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    db_key = (get_config("openrouter.api_key", "") or "").strip()
    env_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    has_key = bool(db_key or env_key)
    key_source = "db" if db_key else ("env" if env_key else "none")
    default_model = (get_config("openrouter.default_model", "") or "google/gemini-2.5-flash").strip()
    registry = _or_load_models()
    models = [
        {"id": mid, "label": cfg.get("label", mid), "max_tokens": cfg.get("max_tokens", 16384),
         "reasoning": cfg.get("reasoning"), "builtin": mid in _OPENROUTER_BUILTIN_MODELS}
        for mid, cfg in registry.items()
    ]
    return {"ok": True, "has_api_key": has_key, "has_dedicated_key": bool(db_key), "key_source": key_source, "default_model": default_model, "models": models}


@app.post("/api/openrouter/key")
async def api_openrouter_key_set(req: Request):
    """Set OpenRouter API key (admin only). Never returned by API."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    try:
        body = await req.json()
    except Exception:
        body = {}
    key = str(body.get("api_key") or "").strip()
    set_config("openrouter.api_key", key)
    return {"ok": True, "saved": bool(key), "length": len(key)}


@app.post("/api/openrouter/key/clear")
async def api_openrouter_key_clear(req: Request):
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    set_config("openrouter.api_key", "")
    return {"ok": True, "cleared": True}


@app.post("/api/openrouter/model")
async def api_openrouter_model_set(req: Request):
    """Set default OpenRouter model (admin only)."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    try:
        body = await req.json()
    except Exception:
        body = {}
    model = str(body.get("model") or "").strip()
    if model:
        set_config("openrouter.default_model", model)
    return {"ok": True, "model": model or "google/gemini-2.5-flash"}


@app.post("/api/openrouter/session-model")
async def api_openrouter_session_model_set(req: Request):
    """Set OpenRouter model for a specific session/tab."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    session_id = str(body.get("session_id") or "").strip()
    model = str(body.get("model") or "").strip()
    if not session_id:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
    if not model:
        return JSONResponse({"ok": False, "error": "model required"}, status_code=400)

    # Keep config key safe + bounded.
    if len(session_id) > 200 or len(model) > 200:
        return JSONResponse({"ok": False, "error": "invalid input"}, status_code=400)

    set_config(f"openrouter.session_model.{session_id}", model)
    return {"ok": True, "session_id": session_id, "model": model}


@app.post("/api/openrouter/models")
async def api_openrouter_model_upsert(req: Request):
    """Add or update an OpenRouter model config."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    try:
        body = await req.json()
    except Exception:
        body = {}
    model_id = str(body.get("id") or "").strip()
    if not model_id or "/" not in model_id or len(model_id) > 200:
        return JSONResponse({"ok": False, "error": "Valid model ID required (e.g. google/gemini-2.5-flash)"}, status_code=400)
    label = str(body.get("label") or model_id.split("/")[-1]).strip()[:80]
    max_tokens = int(body.get("max_tokens", 16384) or 16384)
    if max_tokens < 256:
        max_tokens = 256
    if max_tokens > 65536:
        max_tokens = 65536
    reasoning = body.get("reasoning")
    if reasoning and reasoning not in ("low", "medium", "high"):
        reasoning = None
    # Load existing user overrides from DB
    try:
        raw = get_config("openrouter.models", "")
        user_models = json.loads(raw) if raw else {}
        if not isinstance(user_models, dict):
            user_models = {}
    except (json.JSONDecodeError, Exception):
        user_models = {}
    user_models[model_id] = {"label": label, "max_tokens": max_tokens, "reasoning": reasoning}
    _or_save_user_models(user_models)
    return {"ok": True, "model": {"id": model_id, "label": label, "max_tokens": max_tokens, "reasoning": reasoning}}


@app.delete("/api/openrouter/models/{model_id:path}")
async def api_openrouter_model_delete(model_id: str, req: Request):
    """Remove an OpenRouter model from the registry."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    model_id = model_id.strip()
    if not model_id:
        return JSONResponse({"ok": False, "error": "model_id required"}, status_code=400)
    try:
        raw = get_config("openrouter.models", "")
        user_models = json.loads(raw) if raw else {}
        if not isinstance(user_models, dict):
            user_models = {}
    except (json.JSONDecodeError, Exception):
        user_models = {}
    removed = model_id in user_models
    user_models.pop(model_id, None)
    _or_save_user_models(user_models)
    # If it's a built-in model, it still exists in the registry (just reverts to defaults)
    is_builtin = model_id in _OPENROUTER_BUILTIN_MODELS
    return {"ok": True, "removed": removed, "builtin": is_builtin,
            "note": "Reverted to built-in defaults" if is_builtin and removed else None}


@app.get("/api/openrouter/health")
async def api_openrouter_health_get(req: Request):
    """Check OpenRouter API connectivity."""
    key = _openrouter_api_key()
    result = await openrouter_health(key)
    return result


# --- Anthropic Direct API ---

@app.get("/api/anthropic/health")
async def api_anthropic_health():
    """Check Anthropic API key validity."""
    key = _anthropic_api_key()
    result = await anthropic_health(key)
    return result


@app.get("/api/anthropic/config")
async def api_anthropic_config():
    """Return Anthropic connection config including models."""
    key = _anthropic_api_key()
    key_source = "none"
    if (get_config("anthropic.api_key", "") or "").strip():
        key_source = "db"
    elif (get_config("claude_code.api_key", "") or "").strip():
        key_source = "db (shared with Claude Code)"
    elif (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        key_source = "env"
    default_model = get_config("anthropic.default_model", "") or ANTHROPIC_DEFAULT_MODEL
    models = []
    for model_id, info in ANTHROPIC_MODELS.items():
        models.append({
            "id": model_id,
            "label": info["label"],
            "context_window": info["context_window"],
            "max_output_tokens": info["max_output_tokens"],
        })
    has_dedicated = bool((get_config("anthropic.api_key", "") or "").strip())
    return {
        "ok": True,
        "has_api_key": bool(key),
        "has_dedicated_key": has_dedicated,
        "key_source": key_source,
        "default_model": default_model,
        "models": models,
        "advanced_tools": get_config("anthropic.advanced_tools", "0") == "1",
    }


@app.get("/api/anthropic/models")
async def api_anthropic_models():
    """Return available Anthropic models with metadata."""
    models = []
    for model_id, info in ANTHROPIC_MODELS.items():
        models.append({
            "id": model_id,
            "label": info["label"],
            "context_window": info["context_window"],
            "max_output_tokens": info["max_output_tokens"],
        })
    return {"ok": True, "models": models}


@app.post("/api/anthropic/key")
async def api_anthropic_key_set(req: Request):
    """Set Anthropic API key (admin only)."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    key = str(body.get("api_key") or "").strip()
    set_config("anthropic.api_key", key)
    return {"ok": True, "saved": bool(key), "length": len(key)}


@app.post("/api/anthropic/key/clear")
async def api_anthropic_key_clear(req: Request):
    """Clear Anthropic API key (admin only)."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    set_config("anthropic.api_key", "")
    return {"ok": True, "cleared": True}


@app.post("/api/anthropic/model")
async def api_anthropic_model_set(req: Request):
    """Set default Anthropic model (admin only)."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    model = str(body.get("model") or "").strip()
    if model:
        set_config("anthropic.default_model", model)
    return {"ok": True, "model": model}


@app.post("/api/anthropic/advanced-tools")
async def api_anthropic_advanced_tools(req: Request):
    """Toggle advanced tool use (code execution + PTC) for Anthropic (admin only)."""
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    enabled = bool(body.get("enabled", False))
    set_config("anthropic.advanced_tools", "1" if enabled else "0")
    return {"ok": True, "advanced_tools": enabled}


# --- Max Sessions (policy + status) ---

_MAX_DEFAULTS = {
    "max_total_sessions": 20,
    "max_codex_sessions": 12,
    "max_spark_sessions": 8,
}


def _is_admin(req: Request) -> bool:
    user = get_request_user(req) or {}
    return str(user.get("role") or "") == "admin"


def _load_max_policy() -> dict:
    """Load max-session policy from SQLite config, merged with defaults."""
    raw = get_config("max_sessions.policy_json", "")
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    merged = dict(_MAX_DEFAULTS)
    for k in _MAX_DEFAULTS.keys():
        if k in data:
            try:
                merged[k] = int(data.get(k))
            except Exception:
                pass
    # Sanitize
    for k in list(merged.keys()):
        try:
            merged[k] = int(merged[k])
        except Exception:
            merged[k] = _MAX_DEFAULTS[k]
    return merged


def _validate_max_policy(p: dict) -> tuple[bool, str, dict]:
    """Return (ok, error, cleaned_policy)."""
    if not isinstance(p, dict):
        return False, "Body must be a JSON object", {}

    cleaned = dict(_MAX_DEFAULTS)
    for k in cleaned.keys():
        if k in p:
            try:
                cleaned[k] = int(p.get(k))
            except Exception:
                return False, f"{k} must be an integer", {}

    # Basic range checks (keep sane; allow 0 to disable a category)
    for k, v in cleaned.items():
        if v < 0:
            return False, f"{k} must be >= 0", {}
        if v > 500:
            return False, f"{k} too large (max 500)", {}

    # Consistency: per-model caps cannot exceed total if total > 0
    total = cleaned["max_total_sessions"]
    if total > 0:
        if cleaned["max_codex_sessions"] > total:
            return False, "max_codex_sessions cannot exceed max_total_sessions", {}
        if cleaned["max_spark_sessions"] > total:
            return False, "max_spark_sessions cannot exceed max_total_sessions", {}

    return True, "", cleaned


def _max_counts_for_owner(owner: str) -> dict:
    """Count persisted sessions for owner from tab_meta excluding tombstones."""
    owner = str(owner or "").strip().lower()
    if not owner:
        return {"active_total": 0, "active_codex": 0, "active_spark": 0}

    with db_connection() as db:
        _ensure_tab_meta_schema(db)

        # tab_tombstones table exists in DB from prior tab hygiene work.
        # Exclude tombstoned sessions.
        rows = db.execute(
            """
            SELECT tm.session_id, COALESCE(tm.model_key, '')
            FROM tab_meta tm
            LEFT JOIN tab_tombstones tt
              ON tt.owner = tm.owner AND tt.session_id = tm.session_id
            WHERE tm.owner = ? AND COALESCE(tt.deleted_at, 0) = 0
            """,
            (owner,),
        ).fetchall()

    total = 0
    codex = 0
    spark = 0
    for _sid, mk in rows:
        total += 1
        if mk == "codex":
            codex += 1
        elif mk == "spark":
            spark += 1

    return {"active_total": total, "active_codex": codex, "active_spark": spark}


def _max_at_limit(counts: dict, limits: dict) -> dict:
    """Compute at-limit booleans (total + per-model)."""
    total = int(counts.get("active_total", 0) or 0)
    codex = int(counts.get("active_codex", 0) or 0)
    spark = int(counts.get("active_spark", 0) or 0)

    lt = int(limits.get("max_total_sessions", 0) or 0)
    lc = int(limits.get("max_codex_sessions", 0) or 0)
    ls = int(limits.get("max_spark_sessions", 0) or 0)

    # If a limit is 0, treat it as "no capacity" for that bucket.
    at_total = (lt == 0 and total > 0) or (lt > 0 and total >= lt)
    at_codex = (lc == 0 and codex > 0) or (lc > 0 and codex >= lc)
    at_spark = (ls == 0 and spark > 0) or (ls > 0 and spark >= ls)

    return {
        "at_total_limit": at_total,
        "at_codex_limit": at_codex,
        "at_spark_limit": at_spark,
        "at_any_limit": at_total or at_codex or at_spark,
    }


@app.get("/api/max/config")
async def api_max_config_get(req: Request):
    # Any authenticated user can read.
    owner = _resolve_owner_username(req)
    return {"ok": True, "owner": owner, "limits": _load_max_policy()}


@app.post("/api/max/config")
async def api_max_config_set(req: Request):
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)

    try:
        body = await req.json()
    except Exception:
        body = {}

    ok, err, cleaned = _validate_max_policy(body)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    set_config("max_sessions.policy_json", json.dumps(cleaned))
    return {"ok": True, "limits": cleaned}


@app.get("/api/max/status")
async def api_max_status(req: Request, include_sessions: int = 0):
    owner = _resolve_owner_username(req)
    limits = _load_max_policy()
    counts = _max_counts_for_owner(owner)
    flags = _max_at_limit(counts, limits)

    # Optional: include a session list (persisted tabs) for Max UI
    sessions: list[dict] = []
    if int(include_sessions or 0) == 1:
        try:
            with db_connection() as db:
                _ensure_tab_meta_schema(db)
                rows = db.execute(
                    """
                    SELECT
                      tm.session_id,
                      tm.tab_id,
                      COALESCE(tm.model_key, ''),
                      COALESCE(tm.label, ''),
                      COALESCE(tm.updated_at, 0) AS tab_updated_at,
                      COALESCE(h.updated_at, 0) AS history_updated_at,
                      CASE WHEN h.session_id IS NULL THEN 0 ELSE 1 END AS has_history
                    FROM tab_meta tm
                    LEFT JOIN tab_tombstones tt
                      ON tt.owner = tm.owner AND tt.session_id = tm.session_id
                    LEFT JOIN history h
                      ON h.session_id = tm.session_id
                    WHERE tm.owner = ? AND COALESCE(tt.deleted_at, 0) = 0
                    ORDER BY MAX(COALESCE(tm.updated_at, 0), COALESCE(h.updated_at, 0)) DESC
                    LIMIT 250
                    """,
                    (owner,),
                ).fetchall()

            for r in rows or []:
                sessions.append({
                    "session_id": str(r[0] or ""),
                    "tab_id": str(r[1] or ""),
                    "model_key": str(r[2] or ""),
                    "label": str(r[3] or ""),
                    "tab_updated_at": int(r[4] or 0),
                    "history_updated_at": int(r[5] or 0),
                    "has_history": bool(int(r[6] or 0)),
                })
        except Exception:
            sessions = []

    return {"ok": True, "owner": owner, **counts, "limits": limits, **flags, "sessions": sessions}


@app.post("/api/max/session/terminate")
async def api_max_session_terminate(req: Request):
    """Terminate/delete a persisted session/tab (admin only).

    This reuses the existing background cleanup path (`_cleanup_tab_session`) which:
    - clears history
    - removes runtime state
    - clears per-session security state
    - deletes tab_meta row and writes a tombstone
    """
    if not _is_admin(req):
        return JSONResponse({"ok": False, "error": "Admin only"}, status_code=403)

    try:
        body = await req.json()
    except Exception:
        body = {}

    session_id = str(body.get("session_id") or "").strip()
    tab_id = str(body.get("tab_id") or "").strip()
    if not session_id:
        return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)

    owner = _resolve_owner_username(req)
    # Background cleanup to avoid blocking request.
    asyncio.create_task(_cleanup_tab_session(session_id, owner=owner, tab_id=tab_id))
    return {"ok": True, "queued": True, "session_id": session_id}


@app.post("/api/config")
async def api_config_set(req: Request):
    body = await req.json()
    if "reasoning_effort" in body and body["reasoning_effort"] in ("none", "low", "medium", "high"):
        _runtime_config["reasoning_effort"] = body["reasoning_effort"]
    if "nudge_enabled" in body:
        _runtime_config["nudge_enabled"] = bool(body["nudge_enabled"])
    if "self_compact" in body:
        _runtime_config["self_compact"] = bool(body["self_compact"])
    if "auto_name_enabled" in body:
        _runtime_config["auto_name_enabled"] = bool(body["auto_name_enabled"])
        set_config("auto_name_enabled", "1" if _runtime_config["auto_name_enabled"] else "0")
    if "claude_auto_compact_pct" in body:
        val = str(body["claude_auto_compact_pct"]).strip()
        if val in _VALID_AUTO_COMPACT_PCTS:
            _runtime_config["claude_auto_compact_pct"] = val
            set_config("claude_auto_compact_pct", val)

    # (Claude token now handled by /api/claude/config)

    return {"ok": True, **_runtime_config}


# --- Internal API ---

@app.post("/internal/config")
async def internal_config_sync(req: Request):
    """Receive config updates. Localhost only."""
    if not is_localhost(req):
        return JSONResponse({"error": "Internal only"}, status_code=403)
    body = await req.json()
    for key in ("reasoning_effort", "nudge_enabled", "self_compact", "auto_name_enabled", "claude_auto_compact_pct"):
        if key in body:
            _runtime_config[key] = body[key]
    return {"ok": True}


# --- Scheduler + Reports routes (extracted to routes/) ---
from routes.scheduler import router as scheduler_router
from routes.reports import router as reports_router
from routes.reports import _sync_nightly_report_cron_job, _ensure_launchd_sync, _ensure_report_history_table

app.include_router(scheduler_router)
app.include_router(reports_router)

# --- Auth routes ---
from routes.auth_routes import router as auth_router, AuthMiddleware, AUTH_EXEMPT, AUTH_EXEMPT_PREFIXES
app.include_router(auth_router)
app.add_middleware(AuthMiddleware)


@app.get("/api/security-policy")
async def api_security_policy():
    return get_security_policy()


# --- Content Guard (DeBERTa first pass → Spark second pass) ---

@app.post("/api/scan")
async def api_scan(req: Request):
    """Two-pass content scan: DeBERTa (fast) → Spark sub-agent if flagged (smart).
    Returns verdict with both stage results."""
    import asyncio
    from injection_guard import scan_text
    body = await req.json()
    text = body.get("text", "")
    source = body.get("source", "api")
    skip_spark = body.get("skip_spark", False)

    # Stage 1: DeBERTa
    first_pass = scan_text(text, source=source)
    if first_pass["verdict"] == "LEGIT" or skip_spark:
        return {"verdict": first_pass["verdict"], "first_pass": first_pass, "spark_assessment": None}

    # Stage 2: Spark
    try:
        from spark_guard import assess_inbound
        assessment = await asyncio.get_event_loop().run_in_executor(
            None, assess_inbound, text, first_pass
        )
        final_verdict = "LEGIT" if assessment.get("action") == "ALLOW" else "INJECTION"
        return {"verdict": final_verdict, "first_pass": first_pass, "spark_assessment": assessment}
    except Exception as e:
        logger.warning(f"Spark inbound assessment failed: {e}")
        return {"verdict": first_pass["verdict"], "first_pass": first_pass, "spark_assessment": {"error": str(e)}}


@app.post("/api/scan/stage1")
async def api_scan_stage1(req: Request):
    """Stage 1 only — fast DeBERTa + regex (no Spark second pass)."""
    from injection_guard import scan_text
    body = await req.json()
    text = body.get("text", "")
    source = body.get("source", "api")
    return scan_text(text, source=source)


@app.post("/api/scan/egress")
async def api_scan_egress(req: Request):
    """Scan outbound content for sensitive data. Spark reviews if flagged."""
    import asyncio
    from email_sanitize import scan
    body = await req.json()
    text = body.get("text", "")
    skip_spark = body.get("skip_spark", False)

    findings = scan(text)
    if not findings:
        return {"passed": True, "findings": [], "spark_assessment": None}
    if skip_spark:
        return {"passed": False, "findings": findings, "spark_assessment": None}

    try:
        from spark_guard import assess_outbound
        assessment = await asyncio.get_event_loop().run_in_executor(
            None, assess_outbound, "", text, findings
        )
        return {"passed": assessment.get("action") == "ALLOW", "findings": findings, "spark_assessment": assessment}
    except Exception as e:
        return {"passed": False, "findings": findings, "spark_assessment": {"action": "BLOCK", "reason": str(e)}}


@app.get("/api/content-guard/health")
async def api_content_guard_health():
    """Check content guard subsystem status."""
    from injection_guard import _get_classifier, get_guard_diagnostics
    classifier = _get_classifier()
    diag = get_guard_diagnostics()
    token = get_token()
    return {
        "status": "ok",
        "stage1": {
            "regex": "ok",
            "deberta": "ok" if classifier else "unavailable",
            "model": diag.get("model"),
            "model_dir_exists": bool(diag.get("model_dir_exists")),
            "last_load_error": diag.get("last_load_error") or None,
            "last_load_error_at": diag.get("last_load_error_at") or 0.0,
        },
        "stage2_spark": {
            "available": bool(token),
            "provider": get_provider_type() or "none",
        },
    }


# --- Email Sanitization ---

@app.post("/api/email-preflight")
async def api_email_preflight(req: Request):
    """Two-pass preflight: regex rules (fast) → Spark assessment if flagged (smart)."""
    import asyncio
    from email_sanitize import preflight_email
    body = await req.json()
    subject = body.get("subject", "")
    email_body = body.get("body", "")
    skip_spark = body.get("skip_spark", False)

    passed, findings = preflight_email(subject, email_body)
    if passed:
        return {"passed": True, "findings": [], "spark_assessment": None}
    if skip_spark:
        return {"passed": False, "findings": findings, "spark_assessment": None}

    # Escalate to Spark for intelligent assessment
    try:
        from spark_guard import assess_outbound
        assessment = await asyncio.get_event_loop().run_in_executor(
            None, assess_outbound, subject, email_body, findings
        )
        return {
            "passed": assessment.get("action") == "ALLOW",
            "findings": findings,
            "spark_assessment": assessment,
        }
    except Exception as e:
        logger.warning(f"Spark outbound assessment failed: {e}")
        return {"passed": False, "findings": findings, "spark_assessment": {"action": "BLOCK", "reason": f"Spark unavailable: {e}"}}


# --- Elevation ---

@app.get("/api/elevations")
async def api_elevations():
    return {"requests": get_pending_elevations()}


@app.post("/api/elevate")
async def api_elevate(req: Request):
    body = await req.json()
    rid = body.get("request_id", "")
    action = body.get("action", "")
    if action == "approve":
        return {"ok": approve_elevation(rid), "action": "approved"}
    elif action == "deny":
        return {"ok": deny_elevation(rid), "action": "denied"}
    return {"error": "Invalid action"}


@app.get("/api/approve-all")
async def api_approve_all_get(session_id: str = "default"):
    return {"enabled": is_approve_all(session_id)}


@app.post("/api/approve-all")
async def api_approve_all_set(req: Request):
    body = await req.json()
    session_id = body.get("session_id", "default")
    set_approve_all(session_id, bool(body.get("enabled", False)))
    return {"ok": True, "enabled": is_approve_all(session_id)}


@app.get("/api/elevated-session")
async def api_elevated_get(session_id: str = "default"):
    return get_elevated_status(session_id)


@app.post("/api/elevated-session")
async def api_elevated_set(req: Request):
    body = await req.json()
    session_id = body.get("session_id", "default")
    return set_elevated_session(session_id, bool(body.get("enabled", False)), int(body.get("ttl_seconds", 600)))


@app.get("/api/privileged/status")
async def api_privileged_status(session_id: str = "default"):
    try:
        return _priv_client.status(session_id=session_id)
    except PrivilegedHelperError as e:
        return {"ok": False, "error": str(e), "elevated": False, "remaining_seconds": 0}


@app.post("/api/privileged/elevate")
async def api_privileged_elevate(req: Request):
    body = await req.json()
    session_id = body.get("session_id", "default")
    ttl = int(body.get("ttl_seconds", 600))
    try:
        return _priv_client.elevate(session_id=session_id, ttl_seconds=ttl)
    except PrivilegedHelperError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/api/privileged/revoke")
async def api_privileged_revoke(req: Request):
    body = await req.json()
    session_id = body.get("session_id", "default")
    try:
        return _priv_client.revoke(session_id=session_id)
    except PrivilegedHelperError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/api/privileged/run")
async def api_privileged_run(req: Request):
    body = await req.json()
    session_id = body.get("session_id", "default")
    action = (body.get("action") or "").strip()
    args = body.get("args") or {}

    allowed = {"spotlight.disable", "spotlight.erase", "spotlight.status"}
    if action not in allowed:
        return JSONResponse({"ok": False, "error": "Action not allowed"}, status_code=400)

    try:
        return _priv_client.run(session_id=session_id, action=action, args=args)
    except PrivilegedHelperError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/api/restart")
async def api_restart(req: Request):
    """Restart the server. Launchd KeepAlive will respawn automatically."""
    user = get_request_user(req) or {}
    role = str(user.get("role") or "")
    if role != "admin" and not is_localhost(req):
        return JSONResponse({"error": "Admin access required"}, status_code=403)

    # Block restarts from delegated sessions (they cause restart loops)
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    caller_session = str(body.get("session_id", "") or req.headers.get("x-session-id", ""))
    if caller_session.startswith("deleg-"):
        return JSONResponse(
            {"error": "Server restart is not allowed from delegated sessions. The Dev Manager will coordinate restarts."},
            status_code=403,
        )

    async def _delayed_exit():
        await asyncio.sleep(1.0)
        logger.info("Restart requested — exiting (launchd will respawn)")
        os._exit(0)

    asyncio.create_task(_delayed_exit())
    return {"ok": True, "message": "Restarting server..."}


# --- DB Health & Recovery ---

@app.get("/api/db/health")
async def api_db_health(req: Request):
    """On-demand DB health check: runs PRAGMA quick_check, returns status + file size + table count."""
    user = get_request_user(req) or {}
    role = str(user.get("role") or "")
    if role != "admin" and not is_localhost(req):
        return JSONResponse({"error": "Admin access required"}, status_code=403)
    from auth import db_health_check, _db_healthy, _db_degraded
    result = db_health_check()
    result["global_flag_healthy"] = _db_healthy
    result["degraded"] = _db_degraded
    # Include backup info
    import re as _re_health
    from config import DB_BACKUP_DIR, DB_PATH

    # Primary backup location
    backups = sorted(
        [p for p in DB_BACKUP_DIR.glob("kukuibot.db.backup-*")
         if not str(p).endswith(("-wal", "-shm"))
         and _re_health.match(r"kukuibot\.db\.backup-\d{8}-\d{6}$", p.name)],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    # Legacy fallback: count backups that may still live in KUKUIBOT_HOME root
    legacy_backup_dir = DB_PATH.parent
    if legacy_backup_dir != DB_BACKUP_DIR:
        legacy_backups = [
            p for p in legacy_backup_dir.glob("kukuibot.db.backup-*")
            if not str(p).endswith(("-wal", "-shm"))
            and _re_health.match(r"kukuibot\.db\.backup-\d{8}-\d{6}$", p.name)
            and not (DB_BACKUP_DIR / p.name).exists()
        ]
        backups.extend(legacy_backups)
        backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    result["backup_count"] = len(backups)
    if backups:
        newest = backups[0]
        result["latest_backup"] = newest.name
        result["latest_backup_age_hours"] = round((time.time() - newest.stat().st_mtime) / 3600, 1)
        result["latest_backup_size"] = newest.stat().st_size
    return result


@app.post("/api/db/recover")
async def api_db_recover(req: Request):
    """Manual DB recovery trigger (admin-only). Backs up corrupt file and restores from backup."""
    user = get_request_user(req) or {}
    role = str(user.get("role") or "")
    if role != "admin" and not is_localhost(req):
        return JSONResponse({"error": "Admin access required"}, status_code=403)
    from auth import db_manual_recover
    result = db_manual_recover()
    status = 200 if result.get("ok") else 500
    return JSONResponse(result, status_code=status)


@app.post("/api/db/backup")
async def api_db_backup(req: Request):
    """Manual DB backup trigger (admin-only). Uses sqlite3.backup() API."""
    user = get_request_user(req) or {}
    role = str(user.get("role") or "")
    if role != "admin" and not is_localhost(req):
        return JSONResponse({"error": "Admin access required"}, status_code=403)
    from auth import db_backup
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, db_backup)
    status = 200 if result.get("ok") else 500
    return JSONResponse(result, status_code=status)


@app.post("/api/backup")
async def api_backup(req: Request):
    """Trigger a git backup (commit + push)."""
    import subprocess
    script = os.path.join(os.path.dirname(__file__), "backup.sh")
    repo_dir = os.path.join(os.path.dirname(__file__))
    if not os.path.isfile(script):
        return JSONResponse({"error": "backup.sh not found"}, status_code=404)

    # Preflight: ensure origin exists
    try:
        has_origin = subprocess.run(
            ["git", "-C", repo_dir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        ).returncode == 0
    except Exception:
        has_origin = False

    if not has_origin:
        return JSONResponse(
            {
                "ok": False,
                "needs_setup": True,
                "error": "GitHub backup is not configured (missing origin remote).",
            },
            status_code=400,
        )

    try:
        result = subprocess.run(["bash", script], capture_output=True, text=True, timeout=30)
        # Read last few lines of backup log
        log_path = os.path.join(os.path.expanduser("~/.kukuibot/logs"), "backup.log")
        last_lines = ""
        if os.path.isfile(log_path):
            with open(log_path) as f:
                lines = f.readlines()
                last_lines = "".join(lines[-6:]).strip()

        if result.returncode != 0:
            err = (result.stderr or "").strip() or "Backup failed"
            return JSONResponse({"ok": False, "error": err, "output": last_lines, "returncode": result.returncode}, status_code=500)

        return {"ok": True, "output": last_lines, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Backup timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/backup/status")
async def api_backup_status():
    """Check backup config + last backup log line."""
    import subprocess
    repo_dir = os.path.join(os.path.dirname(__file__))

    origin_url = ""
    branch = get_config("backup_branch", "") or ""
    configured = False

    try:
        p = subprocess.run(["git", "-C", repo_dir, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            origin_url = (p.stdout or "").strip()
            configured = bool(origin_url)
    except Exception:
        configured = False

    if not branch:
        try:
            bp = subprocess.run(["git", "-C", repo_dir, "symbolic-ref", "--quiet", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
            if bp.returncode == 0:
                branch = (bp.stdout or "").strip()
        except Exception:
            branch = ""

    log_path = os.path.join(os.path.expanduser("~/.kukuibot/logs"), "backup.log")
    last_backup = None
    lines_count = 0
    if os.path.isfile(log_path):
        with open(log_path) as f:
            lines = f.readlines()
            lines_count = len(lines)
        if lines:
            last_backup = lines[-1].strip()

    return {
        "configured": configured,
        "repo_url": origin_url,
        "branch": branch or "master",
        "last_backup": last_backup,
        "lines": lines_count,
    }


@app.post("/api/backup/config")
async def api_backup_config(req: Request):
    """Configure GitHub backup remote for the local source repo.

    Validates remote connectivity before saving so first backup works.
    """
    import subprocess

    body = await req.json()
    repo_url = (body.get("repo_url") or "").strip()
    branch = (body.get("branch") or "").strip() or "main"
    prefer_ssh = bool(body.get("prefer_ssh", True))
    repo_dir = os.path.join(os.path.dirname(__file__))

    if not repo_url:
        return JSONResponse({"error": "repo_url is required"}, status_code=400)

    def to_ssh_if_github(url: str) -> str:
        u = url.strip()
        m = re.match(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", u)
        if not m:
            return u
        owner, repo = m.group(1), m.group(2)
        return f"git@github.com:{owner}/{repo}.git"

    candidate_urls = [repo_url]
    if prefer_ssh:
        ssh_url = to_ssh_if_github(repo_url)
        if ssh_url != repo_url:
            candidate_urls = [ssh_url, repo_url]

    try:
        # Ensure git repo exists
        if subprocess.run(["git", "-C", repo_dir, "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, timeout=5).returncode != 0:
            return JSONResponse({"error": f"Not a git repo: {repo_dir}"}, status_code=400)

        selected_url = None
        last_err = ""
        for test_url in candidate_urls:
            test = subprocess.run(["git", "ls-remote", test_url, "HEAD"], capture_output=True, text=True, timeout=12)
            if test.returncode == 0:
                selected_url = test_url
                break
            last_err = (test.stderr or test.stdout or "").strip()

        if not selected_url:
            return JSONResponse({
                "error": "Could not access repository. Check URL and credentials (SSH key or HTTPS token).",
                "detail": last_err[:300],
            }, status_code=400)

        has_origin = subprocess.run(["git", "-C", repo_dir, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5).returncode == 0
        if has_origin:
            subprocess.run(["git", "-C", repo_dir, "remote", "set-url", "origin", selected_url], check=True, timeout=10)
        else:
            subprocess.run(["git", "-C", repo_dir, "remote", "add", "origin", selected_url], check=True, timeout=10)

        set_config("backup_repo_url", selected_url)
        set_config("backup_branch", branch)

        return {"ok": True, "configured": True, "repo_url": selected_url, "branch": branch}
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"Failed to configure git remote: {e}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    from auth import _db_healthy, _db_degraded
    status = "ok"
    if not _db_healthy:
        status = "degraded"
    elif _db_degraded:
        status = "degraded_data_loss"
    return {
        "status": status,
        "db_healthy": _db_healthy,
        "db_degraded": _db_degraded,
        "model": MODEL,
        "port": PORT,
        "uptime": round(time.time() - RUNTIME_STARTED, 1),
        "authenticated": get_auth_status()["authenticated"] if _db_healthy else False,
    }


# =============================================
# UPDATE CHECK / APPLY
# =============================================

@app.post("/api/update/check")
async def update_check(req: Request):
    """Check if there are updates available on the remote."""
    src_dir = str(KUKUIBOT_HOME / "src")
    try:
        result = subprocess.run(
            ["git", "-C", src_dir, "fetch", "origin", "--quiet"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return JSONResponse({"error": f"Fetch failed: {result.stderr.strip()}"}, status_code=500)

        branch = subprocess.run(
            ["git", "-C", src_dir, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "master"

        behind = subprocess.run(
            ["git", "-C", src_dir, "rev-list", "--count", f"HEAD..origin/{branch}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        behind_count = int(behind) if behind.isdigit() else 0

        commit = subprocess.run(
            ["git", "-C", src_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        return {"up_to_date": behind_count == 0, "behind": behind_count, "commit": commit, "branch": branch}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/update/apply")
async def update_apply(req: Request):
    """Pull latest updates and restart."""
    src_dir = str(KUKUIBOT_HOME / "src")
    try:
        subprocess.run(["git", "-C", src_dir, "stash", "--quiet"], capture_output=True, timeout=5)

        branch = subprocess.run(
            ["git", "-C", src_dir, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "master"

        result = subprocess.run(
            ["git", "-C", src_dir, "pull", "origin", branch, "--ff-only"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return JSONResponse({"error": f"Pull failed: {result.stderr.strip()}"}, status_code=500)

        subprocess.run(["git", "-C", src_dir, "stash", "pop", "--quiet"], capture_output=True, timeout=5)

        summary = result.stdout.strip().split("\n")[-1] if result.stdout.strip() else "Updated"

        async def _delayed_restart():
            await asyncio.sleep(1.0)
            os._exit(0)

        asyncio.create_task(_delayed_restart())
        return {"ok": True, "summary": summary}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Static Files ---
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/settings")
async def serve_settings():
    """Settings page."""
    page = os.path.join(STATIC_DIR, "settings.html")
    if os.path.isfile(page):
        return FileResponse(page)
    return HTMLResponse("<h1>Settings</h1><p>Settings page not found.</p>")


@app.get("/")
async def serve_root():
    if not is_setup_complete():
        return RedirectResponse("/setup.html", status_code=302)
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return HTMLResponse(f"<h1>{APP_NAME}</h1><p>Frontend not built yet. Use /health or /api/* endpoints.</p>")


@app.get("/{path:path}")
async def serve_static(path: str):
    # Main app static files
    file_path = os.path.join(STATIC_DIR, path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    # Directory index support
    if os.path.isdir(file_path):
        idx = os.path.join(file_path, "index.html")
        if os.path.isfile(idx):
            return FileResponse(idx)

    # SPA fallback
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return JSONResponse({"error": "Not found"}, status_code=404)


# --- Delegation helpers extracted to routes/delegation.py (Phase 9) ---
# _do_delegation_cleanup, _format_delegation_status_notification,
# _deliver_or_queue_parent_notification, _try_proactive_wake,
# _ensure_claude_subprocess, _get_wake_lock — all imported from routes.delegation



# --- DB Health Monitor ---
# Background task: periodic DB health check via PRAGMA quick_check.
# Runs every 60 seconds. Detects mid-operation corruption and attempts recovery.

async def _db_health_monitor():
    """Background task: periodic DB health check via PRAGMA quick_check.
    Runs every 60 seconds. Detects mid-operation corruption and attempts recovery."""
    await asyncio.sleep(30)  # Initial delay — let startup finish
    while True:
        try:
            await asyncio.sleep(60)
            loop = asyncio.get_running_loop()
            healthy = await loop.run_in_executor(None, periodic_health_check)
            if not healthy:
                logger.warning("DB health monitor: database is unhealthy — recovery attempted")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"DB health monitor error: {e}")


# --- DB Backup Loop ---
# Background task: hourly SQLite backup with rotation.
# Uses sqlite3.backup() API for online, consistent backups.

async def _db_backup_loop():
    """Background task: hourly SQLite backup with rotation.

    Uses sqlite3.backup() API for online, consistent backups.
    Runs rotation after each backup to enforce retention policy.
    """
    from config import DB_BACKUP_INTERVAL, DB_BACKUP_HOURLY_KEEP, DB_BACKUP_DAILY_KEEP
    from auth import db_backup, db_backup_rotate

    # Initial delay: 5 minutes after startup (let everything settle)
    await asyncio.sleep(300)

    while True:
        try:
            loop = asyncio.get_running_loop()

            # Run backup in thread pool (blocking I/O)
            result = await loop.run_in_executor(None, db_backup)
            if result.get("ok"):
                logger.info(f"Hourly DB backup: {result['name']} ({result['size']} bytes, {result['duration_ms']}ms)")
            else:
                logger.error(f"Hourly DB backup failed: {result.get('error')}")

            # Run rotation in thread pool
            rot_result = await loop.run_in_executor(
                None,
                lambda: db_backup_rotate(
                    hourly_keep=DB_BACKUP_HOURLY_KEEP,
                    daily_keep=DB_BACKUP_DAILY_KEEP,
                ),
            )
            if rot_result["deleted"]:
                logger.info(f"Backup rotation: deleted {rot_result['deleted']} old backups, kept {rot_result['kept']}")
            if rot_result["errors"]:
                logger.warning(f"Backup rotation errors: {rot_result['errors']}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"DB backup loop error: {e}")

        await asyncio.sleep(DB_BACKUP_INTERVAL)


# _delegation_monitor → routes/delegation.py (Phase 9)


# --- Startup ---

_SESSION_STALE_AGE_S = 3600  # 1 hour -- reap in-memory state for sessions with no activity


async def _memory_reaper():
    """Background task: periodically clean stale in-memory session state to prevent leaks."""
    while True:
        try:
            await asyncio.sleep(300)  # Run every 5 minutes
            now = time.time()
            reaped = 0

            # 1. SessionEventStore ring buffers (biggest leak)
            reaped += _session_event_store.reap_stale_sessions(max_age_seconds=_SESSION_STALE_AGE_S)

            # 2. _active_tasks — reap entries idle for > stale age
            stale_tasks = []
            for sid, task in _active_tasks.items():
                last_event = task.get("last_event_at", task.get("started", 0))
                if (now - last_event) > _SESSION_STALE_AGE_S:
                    stale_tasks.append(sid)
            for sid in stale_tasks:
                _active_tasks.pop(sid, None)
            reaped += len(stale_tasks)

            # 3. _resume_subscribers — remove empty subscriber lists
            empty_subs = [sid for sid, subs in _resume_subscribers.items() if not subs]
            for sid in empty_subs:
                _resume_subscribers.pop(sid, None)

            # 4. _anthropic_event_subscribers — remove empty subscriber lists
            empty_asubs = [sid for sid, subs in _anthropic_event_subscribers.items() if not subs]
            for sid in empty_asubs:
                _anthropic_event_subscribers.pop(sid, None)

            # 5. _last_api_usage — reap stale (no way to know age, but clean if session not in _active_tasks)
            stale_usage = [sid for sid in _last_api_usage if sid not in _active_tasks]
            for sid in stale_usage:
                _last_api_usage.pop(sid, None)

            # 6. _active_docs — same: clean if session not active
            stale_docs = [sid for sid in _active_docs if sid not in _active_tasks]
            for sid in stale_docs:
                _active_docs.pop(sid, None)

            # 7. _anthropic_containers — clean if session not active and no subscribers
            stale_containers = [
                sid for sid in _anthropic_containers
                if sid not in _active_tasks and sid not in _anthropic_event_subscribers
            ]
            for sid in stale_containers:
                _anthropic_containers.pop(sid, None)

            # 8. _proactive_wake_locks — clean up wake locks for inactive sessions.
            # Wake locks accumulate as delegation notifications trigger proactive wakes
            # (one asyncio.Lock per session_id in _proactive_wake_locks dict).
            # Once a session is no longer active (not in _active_tasks) and its lock
            # is not currently held (no wake in progress), the lock can be safely
            # removed to prevent unbounded dict growth. A new lock is created
            # automatically by _get_wake_lock() if the session needs one again.
            stale_locks = [
                sid for sid, lock in _app_state.proactive_wake_locks.items()
                if sid not in _active_tasks and not lock.locked()
            ]
            for sid in stale_locks:
                _app_state.proactive_wake_locks.pop(sid, None)

            # 9. Notification dispatcher per-session events/tasks
            stale_dispatcher = 0
            if _app_state.notification_dispatcher:
                for sid in stale_tasks:
                    _app_state.notification_dispatcher.cleanup_session(sid)
                    stale_dispatcher += 1

            total = reaped + len(empty_subs) + len(empty_asubs) + len(stale_usage) + len(stale_docs) + len(stale_containers) + len(stale_locks) + stale_dispatcher
            if total > 0:
                logger.info(
                    f"Memory reaper: cleaned {total} stale entries "
                    f"(rings={reaped}, tasks={len(stale_tasks)}, subs={len(empty_subs)}, "
                    f"asubs={len(empty_asubs)}, usage={len(stale_usage)}, "
                    f"docs={len(stale_docs)}, containers={len(stale_containers)}, "
                    f"wake_locks={len(stale_locks)}, dispatcher={stale_dispatcher})"
                )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Memory reaper error: {e}")


@app.on_event("startup")
async def startup():
    _init_server_log()
    init_log_db()
    # --- Startup Integrity Gate (Phase 3) ---
    gate_result = startup_db_health_gate()
    if gate_result.get("healthy"):
        if gate_result.get("data_loss"):
            logger.warning("Startup: DB was corrupt — created fresh DB (DATA LOSS, degraded mode)")
        elif gate_result.get("recovered"):
            logger.warning("Startup: DB was corrupt — recovered from backup")
        elif gate_result.get("fresh"):
            logger.warning("Startup: created fresh database (no existing DB)")
    else:
        logger.error(f"Startup: DB unhealthy and recovery failed — serving in degraded mode: {gate_result}")
    # --- End Integrity Gate ---
    if gate_result.get("healthy"):
        init_db()
        _ensure_report_history_table()
        notification_store.ensure_schema()
        notification_store.recover()
        try:
            with db_connection() as db:
                _ensure_chat_event_schema(db)
                db.commit()
        except Exception as e:
            logger.warning(f"Failed to ensure session event schema on startup: {e}")
        # Ensure nightly report job is synced to launchd on startup
        _sync_nightly_report_cron_job()
        # Sync all DB scheduled jobs to launchd plists
        _ensure_launchd_sync()
        # Try importing token from legacy path on first run
        status = get_auth_status()
        if not status["authenticated"]:
            if import_from_legacy():
                logger.info("Imported auth from legacy source")
    else:
        logger.error("Startup: skipping DB-dependent initialization (degraded mode)")
    # Initialize Claude process pool (processes spawned lazily on first message per tab)
    pool = init_claude_pool(
        api_key_fn=_claude_api_key,
        oauth_token_fn=_claude_oauth_token,
        auth_strategy=_claude_auth_strategy(),
    )
    pool.start_reaper()
    from claude_bridge import MAX_CLAUDE_PROCESSES
    logger.info(f"Claude process pool initialized (max={MAX_CLAUDE_PROCESSES}, auth_strategy={pool._auth_strategy})")
    # Initialize delegation routes module callback (must be before NotificationDispatcher)
    init_delegation_routes(process_chat_claude_fn=_process_chat_claude)
    # Start notification dispatcher (event-driven delivery — Phase 3)
    # Wrap proactive wake to bind _app_state (routes/delegation functions need it)
    async def _try_proactive_wake_bound(session_id, proc, notify_msg, task_id, to_status, label="", _retry_count=0):
        return await _try_proactive_wake(session_id, proc, notify_msg, task_id, to_status, label=label, _retry_count=_retry_count, _app_state=_app_state)
    _app_state.notification_dispatcher = notification_dispatcher.NotificationDispatcher(
        get_claude_pool=get_claude_pool,
        ensure_subprocess=_ensure_claude_subprocess,
        try_proactive_wake=_try_proactive_wake_bound,
        get_active_tasks=lambda: _app_state.active_tasks,
    )
    await _app_state.notification_dispatcher.start()
    # Start delegation monitor (demoted to 45s reconciler — event-driven dispatcher handles delivery)
    _app_state.delegation_monitor_task = asyncio.create_task(_delegation_monitor(poll_interval=120.0, _app_state=_app_state))
    # Start memory reaper (prevents unbounded growth of per-session in-memory state)
    _app_state.memory_reaper_task = asyncio.create_task(_memory_reaper())
    # Start DB health monitor (Phase 3 — periodic PRAGMA quick_check every 60s)
    _app_state.db_health_monitor_task = asyncio.create_task(_db_health_monitor())
    # Start automated DB backup loop (Phase 4 — hourly sqlite3.backup + rotation)
    _app_state.db_backup_loop_task = asyncio.create_task(_db_backup_loop())
    # --- System Wake (Post-Restart Recovery) ---
    # Runs once after startup as a background task. Three-phase process:
    #   Phase 1: Reconcile stale tasks — mark tasks > 2h old as timed_out
    #   Phase 2: Identify coordinator sessions to wake (parent sessions with
    #            active tasks + dev-manager/coordinator worker sessions)
    #   Phase 3: Send [SYSTEM WAKE] notifications via _deliver_or_queue_parent_notification(),
    #            which triggers context injection on freshly-spawned subprocesses
    # Deduplication: minute-granularity dedupe key prevents double notifications.
    # Non-tab parents (claude-code-api) route to dev-manager tabs via fallback;
    # direct wakes for those tabs are skipped to avoid duplicates.
    # See _system_wake() docstring for the full recovery flow.
    _app_state.system_wake_task = asyncio.create_task(_system_wake())
    _init_worker_ports()
    logger.info(f"{APP_NAME} unified server started on {HOST}:{PORT}")


@app.on_event("shutdown")
async def shutdown():
    """Graceful shutdown: cancel background tasks, stop bridges, flush WAL, close connections."""
    import sqlite3 as _shutdown_sqlite3
    from config import DB_PATH as _shutdown_db_path
    from log_store import LOG_DB_PATH as _shutdown_log_db_path

    logger.info("Shutdown: beginning graceful shutdown sequence")

    # Step 0: Stop notification dispatcher
    if _app_state.notification_dispatcher:
        await _app_state.notification_dispatcher.stop()
    logger.info("Shutdown: notification dispatcher stopped")

    # Step 1: Cancel background tasks (3s timeout each)
    _bg_tasks = {
        "delegation_monitor": _app_state.delegation_monitor_task,
        "memory_reaper": _app_state.memory_reaper_task,
        "db_health_monitor": _app_state.db_health_monitor_task,
        "db_backup_loop": _app_state.db_backup_loop_task,
        "system_wake": _app_state.system_wake_task,
    }
    for task_name, task in _bg_tasks.items():
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
    logger.info("Shutdown: background tasks cancelled")

    # Step 2: Stop bridge processes
    await shutdown_bridges()
    logger.info("Shutdown: bridge processes stopped")

    # Step 3: WAL checkpoint on kukuibot.db
    loop = asyncio.get_running_loop()
    try:
        def _flush_main_wal():
            try:
                db = _shutdown_sqlite3.connect(str(_shutdown_db_path), timeout=5.0)
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.close()
                logger.info("Shutdown: WAL checkpoint completed (TRUNCATE)")
            except Exception as e:
                logger.warning(f"Shutdown: WAL checkpoint failed: {e}")

        await asyncio.wait_for(
            loop.run_in_executor(None, _flush_main_wal),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Shutdown: WAL checkpoint timed out (10s)")

    # Step 4: WAL checkpoint on logs DB
    try:
        def _flush_logs_wal():
            try:
                if Path(_shutdown_log_db_path).exists():
                    db = _shutdown_sqlite3.connect(str(_shutdown_log_db_path), timeout=5.0)
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    db.close()
                    logger.info("Shutdown: logs DB WAL checkpoint completed")
            except Exception as e:
                logger.warning(f"Shutdown: logs DB WAL checkpoint failed: {e}")

        await asyncio.wait_for(
            loop.run_in_executor(None, _flush_logs_wal),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Shutdown: logs DB WAL checkpoint timed out (5s)")

    logger.info("Shutdown: graceful shutdown complete")


# _system_wake → routes/delegation.py (Phase 9)


# --- Entry Point ---

if __name__ == "__main__":
    import asyncio
    from hypercorn.asyncio import serve
    from hypercorn.config import Config as HyperConfig

    hc = HyperConfig()
    hc.bind = [f"{HOST}:{PORT}"]
    hc.loglevel = "info"
    if SSL_CERT.exists() and SSL_KEY.exists():
        hc.certfile = str(SSL_CERT)
        hc.keyfile = str(SSL_KEY)

    # Use uvloop for non-blocking TLS — prevents event loop freezes under
    # concurrent SSL connections (Python's default asyncio SSL transport
    # can block the event loop in _ssl__SSLSocket_read → poll()).
    # uvloop moves TLS operations to libuv's C thread pool.
    try:
        import uvloop
        loop_factory = uvloop.new_event_loop
        tls_note = "uvloop"
    except ImportError:
        loop_factory = None
        tls_note = "asyncio"

    if SSL_CERT.exists() and SSL_KEY.exists():
        logger.info(f"{APP_NAME} HTTPS+H2 on {HOST}:{PORT} (Hypercorn, {tls_note})")
    else:
        logger.info(f"{APP_NAME} HTTP on {HOST}:{PORT} (no TLS certs, {tls_note})")

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(serve(app, hc))
