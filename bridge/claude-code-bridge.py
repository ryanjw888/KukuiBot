#!/usr/bin/env python3
"""
KukuiBot Claude Code Bridge — Standalone single-process bridge for Claude Code CLI.

Standalone single-process bridge for Claude Code CLI.
Adapted for KukuiBot's context files, worker identity, and paths.

Features:
  - Persistent process: ONE claude process stays alive across all messages
  - Context injection: SOUL.md, USER.md, TOOLS.md, Worker Identity, chat log
  - Streaming SSE + non-streaming JSON responses
  - Auto-restart on crash (always fresh start with full context reload)
  - Health monitoring: uptime, message count, process state
  - Manual smart compaction via /api/smart-compact (no auto-trigger)
  - CLI auto-compaction at 90% (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE in ~/.claude/settings.json)
  - Dynamic worker identity from ~/.kukuibot/workers/*.md
  - Worker list endpoint for UI integration

Usage:
  python3 claude-code-bridge.py --port 9085                            # Default (developer)
  python3 claude-code-bridge.py --port 9085 --worker it-admin          # IT Admin worker
  python3 claude-code-bridge.py --port 9085 --model claude-sonnet-4-6  # Sonnet model
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, AsyncIterator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bridge] %(message)s")
logger = logging.getLogger("claude-bridge")

# Load .env from KukuiBot workspace
WORKSPACE = Path(os.environ.get("KUKUIBOT_HOME", os.path.expanduser("~/.kukuibot")))
for _env_candidate in [WORKSPACE / ".env", WORKSPACE / "config" / ".env"]:
    if _env_candidate.exists():
        with open(_env_candidate) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    _k = _k.strip()
                    _v = _v.strip().strip("'\"")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
        break

# Add src/ to path so we can import log_store (which imports config)
_src_dir = str(WORKSPACE / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from log_store import log_query

# No OpenRouter fallback — KukuiBot handles that in server.py

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
DEFAULT_MODEL = "opus"  # Claude Code alias — maps to latest Opus
DEFAULT_PORT = 9085
_model = DEFAULT_MODEL  # Set from --model arg at startup
_worker_identity = "developer"  # Set from --worker arg at startup
_kukuibot_session_id = ""  # KukuiBot tab session ID (set from --session arg if provided)

# Session tracking — file paths are port-specific (set in __main__)
_SESSIONS_FILE = WORKSPACE / ".bridge_sessions.json"

# --- Compaction Configuration ---
CONTEXT_WINDOW = 1_000_000
COMPACTION_HISTORY_FILE = WORKSPACE / ".bridge_compaction.json"
CHAT_LOG_FILE = WORKSPACE / ".bridge_chat.log"          # Per-worker bridge log
SHARED_CHAT_LOG = WORKSPACE / "logs" / "chat.log"       # Shared log for nightly reports
FILE_LOG_FILE = WORKSPACE / ".bridge_files.log"
FILE_LOG_MAX_LINES = 1000
CHAT_LOG_MAX_CHARS = 5_000
CHAT_LOG_MAX_LINE = 10_000

# Session resume timeout — if the last activity was longer than this ago,
# skip --resume and start fresh with full context re-injection.
# Claude CLI sessions are stored on disk (~/.claude/projects/) so they don't
# expire server-side, but stale sessions may have outdated context.  1 hour
# mirrors what the user requested and keeps context fresh.
RESUME_TIMEOUT_SECS = 3600  # 1 hour

# Per-instance app directory — defaults to workspace root for KukuiBot.
_app_dir = WORKSPACE


def _load_session_state() -> dict:
    """Load session state from disk."""
    try:
        if _SESSIONS_FILE.exists():
            state = json.loads(_SESSIONS_FILE.read_text())
            logger.info(f"Loaded session state: {state.get('session_id', 'none')}")
            return state
    except Exception as e:
        logger.warning(f"Failed to load session state: {e}")
    return {}


def _save_session_state(state: dict):
    """Persist session state to disk."""
    try:
        _SESSIONS_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save session state: {e}")


# --- Compaction History ---

def _load_compaction_state() -> dict:
    """Load compaction state (conversation history + last summary)."""
    try:
        if COMPACTION_HISTORY_FILE.exists():
            return json.loads(COMPACTION_HISTORY_FILE.read_text())
    except Exception as e:
        logger.warning(f"Failed to load compaction state: {e}")
    return {"history": [], "last_summary": None, "compaction_count": 0}


def _save_compaction_state(state: dict):
    """Save compaction state to disk."""
    try:
        COMPACTION_HISTORY_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save compaction state: {e}")


def _append_to_chat_log(role: str, content: str):
    """Append a message to per-worker log AND shared log (for nightly reports)."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_content = content.replace("\n", "\\n")
        # Per-worker log (used for context injection / chatlog endpoint)
        line = f"[{role.upper()} {timestamp}]: {safe_content}\n"
        with open(CHAT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
        # Shared log — includes worker tag for nightly report aggregation
        shared_line = f"[{role.upper()} {timestamp} claude-{_worker_identity}]: {safe_content}\n"
        try:
            SHARED_CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(SHARED_CHAT_LOG, "a", encoding="utf-8") as f:
                f.write(shared_line)
        except Exception:
            pass  # Non-fatal — per-worker log is the primary
    except Exception as e:
        logger.warning(f"Failed to append to chat log: {e}")


def _append_to_file_log(tool: str, path: str):
    """Append a file activity entry to the rolling file log (last 1000 entries)."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{tool.upper()} {timestamp}]: {path}\n"
        # Append first
        with open(FILE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
        # Trim to last FILE_LOG_MAX_LINES if file has grown too large
        # Only trim every ~100 writes to avoid stat overhead on every call
        try:
            size = FILE_LOG_FILE.stat().st_size
            if size > FILE_LOG_MAX_LINES * 200:  # ~200 bytes/line estimate
                with open(FILE_LOG_FILE, "rb") as f:
                    f.seek(0, 2)
                    pos = f.tell()
                    buf = bytearray()
                    lines_found = 0
                    chunk = 8192
                    while pos > 0 and lines_found <= FILE_LOG_MAX_LINES:
                        read_size = min(chunk, pos)
                        pos -= read_size
                        f.seek(pos)
                        data = f.read(read_size)
                        buf = bytearray(data) + buf
                        lines_found = buf.count(b"\n")
                text = buf.decode("utf-8", errors="replace")
                all_lines = text.splitlines(keepends=True)
                trimmed = all_lines[-FILE_LOG_MAX_LINES:]
                FILE_LOG_FILE.write_text("".join(trimmed), encoding="utf-8")
        except Exception:
            pass  # Trim failure is non-fatal
    except Exception as e:
        logger.warning(f"Failed to append to file log: {e}")


def _load_chat_log_tail(max_chars: int = CHAT_LOG_MAX_CHARS, max_line_chars: int = CHAT_LOG_MAX_LINE,
                         kukuibot_session_id: str = "", worker_identity: str = "") -> Optional[str]:
    """Load recent chat from SQLite log store (log_query).

    Falls back to querying without worker filter if no rows found.
    Lines longer than max_line_chars are truncated. Total capped at max_chars.
    """
    try:
        row_estimate = max(50, (max_chars // 150) * (5 if kukuibot_session_id else 1))
        rows = log_query(
            category="chat",
            session_id=kukuibot_session_id or None,
            worker=worker_identity or None,
            limit=row_estimate,
            order="DESC",
        )
        if not rows and worker_identity:
            rows = log_query(
                category="chat",
                session_id=kukuibot_session_id or None,
                limit=row_estimate,
                order="DESC",
            )
        if not rows:
            return None
        result = []
        total = 0
        for r in rows:
            role = (r["role"] or "system").upper()
            ts = r["ts"][:19].replace("T", " ") if r["ts"] else ""
            sid = r["session_id"] or ""
            msg = r["message"]
            line = f"[{role} {ts} {sid}]: {msg}"
            if len(line) > max_line_chars:
                line = line[:max_line_chars] + "... (truncated)"
            if total + len(line) + 1 > max_chars:
                break
            result.append(line)
            total += len(line) + 1
        # Reverse so output reads chronologically (oldest to newest)
        result.reverse()
        return "\n".join(result) if result else None
    except Exception as e:
        logger.warning(f"Failed to load chat log tail: {e}")
        return None


def _get_anthropic_key() -> str:
    """Load Anthropic API key from env var."""
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key.startswith("sk-ant"):
        return env_key
    raise RuntimeError("No Anthropic API key found in ANTHROPIC_API_KEY env var")



COMPACTION_LOG_FILE = WORKSPACE / "memory" / "compaction_log.md"
COMPACTION_LOG_MAX_LINES = 1000

def _flush_summary_to_memory(summary: str):
    """Append compaction summary to the rolling compaction log (last 1000 lines)."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n\n## Compaction ({timestamp})\n{summary}\n"
        with open(COMPACTION_LOG_FILE, "a") as f:
            f.write(entry)
        # Trim to last 1000 lines
        with open(COMPACTION_LOG_FILE, "r") as f:
            lines = f.readlines()
        if len(lines) > COMPACTION_LOG_MAX_LINES:
            with open(COMPACTION_LOG_FILE, "w") as f:
                f.writelines(lines[-COMPACTION_LOG_MAX_LINES:])
        logger.info(f"Flushed compaction summary to {COMPACTION_LOG_FILE}")
    except Exception as e:
        logger.warning(f"Failed to flush compaction log: {e}")


# --- Context Injection ---

def load_context_file(path: Path) -> Optional[str]:
    """Load a context file, return None if missing."""
    try:
        if path.exists():
            text = path.read_text().strip()
            return text if text else None
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
    return None


def build_system_prompt(user_system: Optional[str] = None) -> tuple[str, list[str]]:
    """
    Build context payload — SOUL + USER + TOOLS + Model Identity + Worker Identity + 5KB chat log.
    Used for ALL context injection: fresh start, smart compact, etc.

    KukuiBot context files live at ~/.kukuibot/ (root level):
      SOUL.md, USER.md, TOOLS.md, models/claude_{model}.md, workers/{worker}.md

    Returns:
        (prompt_text, loaded_files) — the assembled prompt and list of files that were loaded.
    """
    sections = []
    loaded_files = []

    # Core identity
    soul = load_context_file(WORKSPACE / "SOUL.md")
    if soul:
        sections.append(f"# Identity\n{soul}")
        loaded_files.append("SOUL.md")

    # User context
    user_md = load_context_file(WORKSPACE / "USER.md")
    if user_md:
        sections.append(f"# About the User\n{user_md}")
        loaded_files.append("USER.md")

    # Tools reference
    tools_md = load_context_file(WORKSPACE / "TOOLS.md")
    if tools_md:
        sections.append(f"# Tools & Infrastructure Reference\n{tools_md}")
        loaded_files.append("TOOLS.md")

    # Per-model identity file — resolve dynamically based on _model global
    model_file = None
    models_dir = WORKSPACE / "models"
    if _model and models_dir.is_dir():
        # Try exact match first: claude_sonnet.md, claude_opus.md, then generic claude.md
        for candidate_name in [f"claude_{_model}.md", "claude.md"]:
            candidate = models_dir / candidate_name
            if candidate.is_file():
                model_file = candidate
                break
    else:
        fallback = models_dir / "claude.md"
        if fallback.is_file():
            model_file = fallback

    model_identity = load_context_file(model_file) if model_file else None
    if model_identity:
        sections.append(f"# Model Profile\n{model_identity}")
        loaded_files.append(f"models/{model_file.name}")

    # Worker identity — loaded from workers/{_worker_identity}.md
    worker_file = WORKSPACE / "workers" / f"{_worker_identity}.md"
    worker = load_context_file(worker_file)
    if worker:
        sections.append(f"# Worker Role\n{worker}")
        loaded_files.append(f"workers/{_worker_identity}.md")

    # Recent chat history (from persistent log — survives compaction)
    chat_tail = _load_chat_log_tail(kukuibot_session_id=_kukuibot_session_id, worker_identity=_worker_identity)
    if chat_tail:
        sections.append(f"# Recent Chat History\n{chat_tail}")
        loaded_files.append("chat_log (20KB tail)")

    base = "\n\n---\n\n".join(sections)

    if user_system:
        return f"{base}\n\n---\n\n# Additional Instructions\n{user_system}", loaded_files

    return base, loaded_files


# --- Persistent Claude Process ---

class PersistentClaudeProcess:
    """Manages a single long-lived claude process with stream-json I/O."""

    def __init__(self):
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.session_id: Optional[str] = None
        self.message_count: int = 0
        self.total_input_tokens: int = 0    # Cumulative billing counter (grows forever) — NOT context size
        self.total_output_tokens: int = 0   # Cumulative billing counter
        self.last_input_tokens: int = 0     # Context size THIS turn (the real number)
        self.peak_input_tokens: int = 0     # Max context size seen this session
        self.context_window: int = CONTEXT_WINDOW  # Updated from CLI's modelUsage if available
        self._turn_iterations: int = 0  # Count of assistant events in current turn
        self._turn_user_events: int = 0  # Count of user events (tool_result round-trips → API calls - 1)
        self.started_at: Optional[float] = None
        self.lock = asyncio.Lock()  # Serialize access to the process
        self.chat_lock = asyncio.Lock()  # Serialize entire send+receive cycles (prevents concurrent message interleaving)
        self._reader_task: Optional[asyncio.Task] = None
        # _current_response removed — replaced by _subscribers set for multi-browser broadcast
        self._context_injected = False
        self._last_response_text: str = ""  # Buffer last response for reconnect
        self._last_response_done: bool = True  # Whether last response is complete
        self._active_docs: set = set()  # Files read/edited this session (for smart compact reabsorption)
        self._response_events: list = []  # Buffer all events for replay
        self._subscribers: set = set()  # All subscribed asyncio.Queues (multi-browser broadcast)
        self._response_file = Path("/tmp/max-response.json")  # Live response file
        self._flush_response_file()
        self.resume_status: str = "unknown"  # "resumed", "fresh", "failed", "unknown"
        self._stderr_task: Optional[asyncio.Task] = None
        self._wake_message: Optional[str] = None  # Set after resume; polled by /status

        # Compaction state
        self._compaction_state = _load_compaction_state()
        self._compacting = False  # True while compaction is in progress

        # Session ID loaded lazily in ensure_running() so port-specific file paths
        # (set in __main__) are resolved before the first load attempt.
        self.session_id = None
        self._session_loaded = False
    
    def _flush_response_file(self):
        """Write current response state to disk."""
        try:
            self._response_file.write_text(json.dumps({
                "text": self._last_response_text,
                "done": self._last_response_done,
                "tool": getattr(self, '_current_tool', None),
                "elapsed": int(time.time() - self.started_at) if self.started_at else 0,
                "ts": time.time(),
            }))
        except Exception:
            pass

    def _record_exchange(self, user_text: str, assistant_text: str):
        """Record a user/assistant exchange for compaction history and persistent chat log."""
        history = self._compaction_state.get("history", [])
        history.append({"role": "user", "content": user_text, "timestamp": time.time()})
        history.append({"role": "assistant", "content": assistant_text, "timestamp": time.time()})
        # Keep last 200 messages max (compaction will trim before this)
        if len(history) > 200:
            history = history[-200:]
        self._compaction_state["history"] = history
        # Save compaction state every 5 messages to reduce disk I/O
        # (always saved on compaction itself — see smart_compact())
        if len(history) % 10 == 0:  # 10 entries = 5 exchanges (user + assistant)
            _save_compaction_state(self._compaction_state)
        # Chat log is append-only (cheap I/O) — always write immediately
        _append_to_chat_log("user", user_text)
        _append_to_chat_log("assistant", assistant_text)

    # Only reabsorb documentation files on compaction — never source code.
    _DOC_EXTENSIONS = {".md", ".txt", ".log", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".env"}

    def _track_tool_docs(self, event: dict):
        """Parse assistant tool_use blocks and record doc file paths touched this session.

        Only tracks documentation/config files (*.md, *.txt, *.log, etc.).
        Source code (*.py, *.js, *.html, *.css, etc.) is never reabsorbed into
        compaction summaries — only .md docs and similar lightweight files.
        """
        FILE_TOOLS = {"Read", "Write", "Edit"}
        msg = event.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            if name not in FILE_TOOLS:
                continue
            inp = block.get("input", {})
            path = inp.get("file_path") or inp.get("path")
            if not path or not isinstance(path, str):
                continue
            # Only track user-space doc files; skip code, /tmp, and system paths
            if path.startswith(str(Path.home())) and "/tmp/" not in path:
                _append_to_file_log(name, path)  # always log all file ops
                ext = Path(path).suffix.lower()
                if ext in self._DOC_EXTENSIONS:
                    self._active_docs.add(path)

    async def smart_compact(self, n_messages: int = 100) -> dict:
        """Smart compact: inject SOUL + USER + TOOLS + 20KB chat log.

        Same payload as fresh start. No LLM summarization, no doc reabsorption.
        The model is prompted to pick up where it left off.
        """
        if self._compacting:
            return {"status": "error", "error": "Compaction already in progress"}

        self._compacting = True
        try:
            count = self._compaction_state.get("compaction_count", 0) + 1

            # Build the same context as fresh start
            context, loaded_files = build_system_prompt()

            # Update compaction state
            self._compaction_state = {
                "history": [],
                "compaction_count": count,
                "last_compaction_at": time.time(),
            }
            _save_compaction_state(self._compaction_state)

            pre_compact_docs = sorted(self._active_docs)
            self._active_docs.clear()

            # Broadcast compaction start to SSE subscribers
            for q in list(self._subscribers):
                try:
                    q.put_nowait({"type": "compaction", "tokens": self.last_input_tokens, "active_docs": pre_compact_docs, "loaded_files": loaded_files})
                except asyncio.QueueFull:
                    pass

            # Inject as a user message (non-destructive — no process restart)
            logger.info(f"Smart compact: injecting {len(loaded_files)} context files...")
            inject_msg = (
                f"[Smart Compact #{count}] Context at {self.last_input_tokens:,} / {CONTEXT_WINDOW:,} tokens. "
                f"Injecting continuity summary. Read this carefully and continue from where we left off. "
                f"Do not re-introduce yourself.\n\n{context}"
            )
            await self._send_raw_message(inject_msg)
            # Drain the ack response — timeout after 60s
            try:
                async with asyncio.timeout(60):
                    async for event in self._receive_response():
                        pass
            except asyncio.TimeoutError:
                logger.warning("Smart compact: drain timed out after 60s — continuing anyway")

            # Broadcast compaction done
            for q in list(self._subscribers):
                try:
                    q.put_nowait({"type": "compaction_done", "summary_length": len(context), "compaction_count": count, "loaded_files": loaded_files})
                except asyncio.QueueFull:
                    pass

            logger.info(f"Smart compact #{count} complete. Context={len(context)} chars, loaded={loaded_files}")
            return {
                "status": "ok",
                "summary_length": len(context),
                "compaction_count": count,
                "loaded_files": loaded_files,
            }

        except Exception as e:
            logger.error(f"Smart compact error: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}
        finally:
            self._compacting = False

    async def ensure_running(self):
        """Start or restart the claude process if needed.

        On first start or crash recovery, attempts --resume if a session_id
        exists on disk and last activity was within RESUME_TIMEOUT_SECS.
        Falls back to fresh start (with context reload) if resume fails or
        session is too old.
        """
        async with self.lock:
            if self.proc and self.proc.returncode is None:
                return  # Already running

            # Lazy session load — deferred so __main__ can set port-specific file paths first
            if not self._session_loaded:
                state = _load_session_state()
                self.session_id = state.get("session_id")
                # Restore token counts so the UI shows correct values after service restart
                self.last_input_tokens = state.get("last_input_tokens", 0)
                self.peak_input_tokens = state.get("peak_input_tokens", 0)
                self.total_input_tokens = state.get("total_input_tokens", 0)
                self.total_output_tokens = state.get("total_output_tokens", 0)
                self.message_count = state.get("message_count", 0)
                self.context_window = CONTEXT_WINDOW  # Always use current constant; CLI modelUsage overrides dynamically
                self._session_loaded = True

            # Try resume if we have a session_id (crash recovery / service restart)
            logger.info(f"Starting persistent Claude process (session_id={self.session_id or 'none'})...")
            await self._spawn(force_fresh=False)
    
    async def _spawn(self, force_fresh: bool = False):
        """Spawn the claude process with stream-json I/O.

        If a session_id exists, force_fresh is False, and the session is younger
        than RESUME_TIMEOUT_SECS, attempts --resume to restore the full
        conversation. Falls back to fresh start (with context re-injection) if
        resume fails or session is stale.
        """
        base_cmd = [
            CLAUDE_BIN, "--print",
            "--model", _model,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--replay-user-messages",
            "--permission-mode", "bypassPermissions",
            "--tools", "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch",
            "--add-dir", str(WORKSPACE),
            "--add-dir", str(Path.home()),
            "--verbose",
        ]

        # Decide whether to resume or start fresh
        resume_session = None
        if not force_fresh and self.session_id:
            # Check session age — skip resume if last activity was too long ago
            state = _load_session_state()
            last_active = state.get("last_active_at", 0)
            age_secs = time.time() - last_active if last_active else float("inf")
            if age_secs > RESUME_TIMEOUT_SECS:
                age_str = f"{age_secs / 3600:.1f}h" if age_secs < float("inf") else "unknown"
                logger.info(f"Session too old ({age_str} > {RESUME_TIMEOUT_SECS/3600:.0f}h) — starting fresh with context reload")
                force_fresh = True
            else:
                resume_session = self.session_id
                logger.info(f"Attempting to resume session: {resume_session} (age: {age_secs:.0f}s)")

        cmd = list(base_cmd)
        if resume_session:
            cmd.extend(["--resume", resume_session])

        try:
            # Build subprocess env: use local CLI auth (Max subscription).
            # Remove ANTHROPIC_API_KEY so the CLI doesn't try to use an
            # API key instead of the local login session. The .env loader
            # may have injected an expired/wrong API key into os.environ.
            _subprocess_env = {**os.environ, "NO_COLOR": "1"}
            # Explicit output token limits — prevent thinking from consuming
            # the entire output budget on fresh installs.
            _subprocess_env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "128000")
            _subprocess_env.setdefault("MAX_THINKING_TOKENS", "0")
            _subprocess_env.pop("ANTHROPIC_API_KEY", None)
            # Only pass explicit OAuth token if set and non-empty.
            if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
                _subprocess_env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]

            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKSPACE),
                env=_subprocess_env,
                limit=10 * 1024 * 1024,  # 10MB line buffer (Claude tool results can be huge)
            )

            self.started_at = time.time()
            self._wake_message = None

            if resume_session:
                self.resume_status = "resuming"
                # Keep existing counters — we're continuing the session
                self._context_injected = True  # Resumed session already has context
            else:
                self.resume_status = "fresh"
                # Reset counters for fresh start
                self.message_count = 0
                self.last_input_tokens = 0
                self.peak_input_tokens = 0
                self.total_input_tokens = 0
                self.total_output_tokens = 0
                self._turn_iterations = 0
                self._turn_user_events = 0
                self._context_injected = False

            # Start background reader + stderr tasks
            self._reader_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._stderr_reader())

            # For resumed sessions, wait briefly to confirm the process is stable
            if resume_session:
                await asyncio.sleep(1.0)
                if self.proc.returncode is not None:
                    logger.warning(f"Resume failed (process exited with rc={self.proc.returncode}), falling back to fresh start")
                    self.proc = None
                    if self._reader_task:
                        self._reader_task.cancel()
                        try:
                            await self._reader_task
                        except asyncio.CancelledError:
                            pass
                        self._reader_task = None
                    if self._stderr_task:
                        self._stderr_task.cancel()
                        try:
                            await self._stderr_task
                        except asyncio.CancelledError:
                            pass
                        self._stderr_task = None
                    # Retry as fresh
                    return await self._spawn(force_fresh=True)
                else:
                    self.resume_status = "resumed"
                    logger.info(f"Session resumed successfully: {resume_session}")

            logger.info(f"Claude process started (PID={self.proc.pid}, resume={self.resume_status})")

        except Exception as e:
            logger.error(f"Failed to spawn claude process: {e}")
            if resume_session:
                logger.info("Retrying with fresh start after resume exception")
                return await self._spawn(force_fresh=True)
            raise
    
    async def _read_loop(self):
        """Background task to read from stdout and route events to current response queue."""
        try:
            while self.proc and self.proc.returncode is None:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                
                line = line.decode().strip()
                if not line:
                    continue
                
                try:
                    event = json.loads(line)
                    event_type = event.get("type", "")
                    
                    # Debug: log all event types
                    if event_type != "system":
                        extra = ""
                        if event_type == "assistant":
                            self._turn_iterations += 1
                            blocks = [b.get("type") for b in event.get("message", {}).get("content", [])]
                            extra = f" blocks={blocks}"
                            # Track any files touched by tool calls this session
                            self._track_tool_docs(event)
                        elif event_type == "user":
                            self._turn_user_events += 1  # Each user event = tool_result → new API call
                        elif event_type == "stream_event":
                            extra = f" inner={event.get('event', {}).get('type', '?')}"
                        logger.info(f"Event: {event_type}{extra}")
                    
                    # Skip system init messages
                    if event_type == "system":
                        continue
                    
                    # Update session ID and token usage from result events
                    if event_type == "result":
                        new_session = event.get("session_id")
                        if new_session and new_session != self.session_id:
                            self.session_id = new_session
                            logger.info(f"Session updated: {self.session_id}")
                        # Log result diagnostics (is_error, num_turns help diagnose early stops)
                        is_error = event.get("is_error", False)
                        num_turns = event.get("num_turns")
                        subtype = event.get("subtype", "")
                        logger.info(f"Result event: is_error={is_error}, num_turns={num_turns}, subtype={subtype}")
                        # Track token usage
                        usage = event.get("usage", {})
                        iters = max(self._turn_iterations, 1)
                        # API calls = user events (tool_result round-trips) + 1 (initial message)
                        # The result event usage is CUMULATIVE across all API calls in the turn,
                        # so we divide by API call count to get per-call context size.
                        api_calls = self._turn_user_events + 1
                        logger.info(f"Result event usage (iters={iters}, api_calls={api_calls}): {usage}")
                        uncached = usage.get("input_tokens", 0)
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)
                        total_input = uncached + cache_create + cache_read
                        # Divide cumulative total by API call count to estimate current context.
                        ctx_input = total_input // api_calls
                        billed_input = total_input
                        self.total_input_tokens += billed_input
                        self.total_output_tokens += usage.get("output_tokens", 0)
                        if ctx_input > 0:
                            self.last_input_tokens = ctx_input  # Current context size (this turn only)
                            if ctx_input > self.peak_input_tokens:
                                self.peak_input_tokens = ctx_input
                        # NOTE: CLI modelUsage reports contextWindow=200000 but actual
                        # limit with prompt caching is 1M. Always use CONTEXT_WINDOW constant.
                        # Log CLI-reported value for diagnostics only.
                        model_usage = event.get("modelUsage", {})
                        for _mu_model, _mu_info in model_usage.items():
                            cw = _mu_info.get("contextWindow", 0)
                            if cw > 0:
                                logger.info(f"CLI reports contextWindow={cw:,} (ignored, using constant {CONTEXT_WINDOW:,})")
                        logger.info(f"Context: {ctx_input:,} / {self.context_window:,} tokens (iters={iters}, api_calls={api_calls}, raw={total_input:,}, uncached={uncached}, cache_create={cache_create}, cache_read={cache_read})")
                        self._turn_iterations = 0  # Reset for next turn
                        self._turn_user_events = 0
                        # Persist session state with token counts (survives service restart)
                        _save_session_state({
                            "session_id": self.session_id,
                            "last_active_at": time.time(),
                            "last_input_tokens": self.last_input_tokens,
                            "peak_input_tokens": self.peak_input_tokens,
                            "total_input_tokens": self.total_input_tokens,
                            "total_output_tokens": self.total_output_tokens,
                            "message_count": self.message_count,
                            "context_window": self.context_window,
                        })
                    
                    # Broadcast event to all subscribers (multi-browser support)
                    for q in list(self._subscribers):
                        try:
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            pass  # slow consumer — skip event
                        
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON: {line[:100]} ({e})")
                    
        except Exception as e:
            logger.error(f"Read loop error: {e}")
            # Signal all subscribers that we crashed
            error_event = {"type": "error", "error": f"Read loop crashed: {e}"}
            for q in list(self._subscribers):
                try:
                    q.put_nowait(error_event)
                except asyncio.QueueFull:
                    pass
        finally:
            logger.warning("Read loop terminated — process will auto-restart on next message")
            # Mark process as dead so ensure_running respawns
            if self.proc:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                self.proc = None
    
    async def _stderr_reader(self):
        """Background task: read stderr from the claude process.

        Parses key lines to confirm resume success/failure, logs everything else.
        Sets self.resume_status based on what we see.
        """
        try:
            while self.proc and self.proc.returncode is None:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode().strip()
                if not text:
                    continue
                low = text.lower()
                if "error" in low or "failed" in low:
                    logger.warning(f"[stderr] {text}")
                else:
                    logger.debug(f"[stderr] {text}")
        except Exception as e:
            logger.debug(f"Stderr reader ended: {e}")

    async def send_message(self, text: str, inject_context: bool = True) -> AsyncIterator[dict]:
        """
        Send a message and yield streaming response events.

        Serialized via chat_lock — only one message can be in-flight at a time.
        Concurrent callers will queue up and be processed sequentially.

        Args:
            text: User message
            inject_context: Whether to inject system context on first message

        Yields:
            Stream-json events from Claude
        """
        # Serialize the entire send+receive cycle to prevent concurrent
        # messages from interleaving (e.g. deep dive parallel groups)
        await self.chat_lock.acquire()
        try:
            await self.ensure_running()

            # Inject context as first message if needed
            if inject_context and not self._context_injected:
                # Send ping so client knows we're working
                yield {"type": "ping", "elapsed": 0, "tool": "loading context"}

                # FRESH START: SOUL + USER + TOOLS + 20KB chat log
                context, _loaded = build_system_prompt()
                await self._send_raw_message(
                    f"[System Context - read carefully but do not repeat]\n{context}\n[End System Context]\n\n"
                    f"Acknowledge with 'Context loaded.' and wait for the first real message.")
                self._context_injected = True
                logger.info("Fresh context injected (SOUL + USER + TOOLS + chat log 20KB)")

                yield {"type": "ping", "elapsed": 0, "tool": "context loaded"}

                # Drain the context ack response before sending user message.
                # The CLI responds to the context injection first (e.g. "Context refreshed.")
                # and emits a result event. We must consume that before listening for
                # the real user response, otherwise _receive_response() stops at the
                # ack's result event and logs the wrong text.
                ack_queue = self.subscribe()
                try:
                    while True:
                        try:
                            ack_evt = await asyncio.wait_for(ack_queue.get(), timeout=60)
                            if ack_evt.get("type") == "result":
                                logger.info("Context ack result consumed (not forwarded to client)")
                                break
                        except asyncio.TimeoutError:
                            logger.warning("Timeout waiting for context ack — proceeding anyway")
                            break
                finally:
                    self.unsubscribe(ack_queue)

            # Broadcast user message to passive browsers before sending
            user_evt = {"type": "user_message", "text": text, "ts": int(time.time() * 1000)}
            for q in list(self._subscribers):
                try:
                    q.put_nowait(user_evt)
                except asyncio.QueueFull:
                    pass

            # Send the actual user message
            await self._send_raw_message(text)

            # Reset response buffer
            self._last_response_text = ""
            self._last_response_done = False
            self._response_events = []
            self._current_tool = None
            self._flush_response_file()

            # Yield all response events and buffer them
            last_flush_time = time.time()
            async for event in self._receive_response():
                self._response_events.append(event)
                # Accumulate text and track tools
                etype = event.get("type", "")
                if etype == "stream_event":
                    inner = event.get("event", {})
                    inner_type = inner.get("type", "")
                    if inner_type == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            self._last_response_text += delta.get("text", "")
                            self._current_tool = None
                    elif inner_type == "content_block_start":
                        cb = inner.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            self._current_tool = cb.get("name")
                elif etype == "result":
                    self._last_response_text = event.get("result", self._last_response_text)
                    self._last_response_done = True

                # Flush to disk at most once per second, or on result
                now = time.time()
                if etype == "result" or (now - last_flush_time) >= 1.0:
                    self._flush_response_file()
                    last_flush_time = now

                yield event
            
            self._last_response_done = True
            self._current_tool = None
            self._flush_response_file()

            # Record exchange for compaction history and persistent chat log
            if self._last_response_text:
                self._record_exchange(text, self._last_response_text)
        finally:
            self.chat_lock.release()
    
    async def _send_raw_message(self, text: str):
        """Send a raw message to claude stdin."""
        async with self.lock:
            if not self.proc or self.proc.returncode is not None:
                raise RuntimeError("Claude process is not running")
            
            msg = json.dumps({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": text
                }
            }) + "\n"
            self.proc.stdin.write(msg.encode())
            await self.proc.stdin.drain()
            self.message_count += 1
            self._turn_iterations = 0  # Reset iteration counter for new turn
            self._turn_user_events = 0
            logger.info(f"Message sent (count={self.message_count})")
    
    def subscribe(self) -> asyncio.Queue:
        """Subscribe to events (multi-browser support). Returns a queue."""
        q = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        logger.info(f"Subscriber added (total: {len(self._subscribers)})")
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """Unsubscribe from events."""
        self._subscribers.discard(q)
        logger.info(f"Subscriber removed (total: {len(self._subscribers)})")

    async def _receive_response(self) -> AsyncIterator[dict]:
        """Receive all events for the current response."""
        response_queue = self.subscribe()
        t0 = time.time()

        last_event_time = time.time()
        try:
            while True:
                # Wait for next event with short timeout for keepalive
                try:
                    event = await asyncio.wait_for(response_queue.get(), timeout=15)
                    last_event_time = time.time()
                except asyncio.TimeoutError:
                    silence = int(time.time() - last_event_time)
                    if silence > 900:
                        logger.error(f"No events for {silence}s — giving up")
                        yield {"type": "error", "error": f"No response for {silence}s"}
                        break
                    elapsed = int(time.time() - t0)
                    yield {"type": "ping", "elapsed": elapsed, "tool": "working"}
                    continue

                yield event

                # Stop after result event
                if event.get("type") == "result":
                    break

        finally:
            self.unsubscribe(response_queue)
    
    async def restart(self, force_fresh: bool = True):
        """Restart the claude process.

        Args:
            force_fresh: If True (default for explicit /restart), starts a new session.
                         If False (crash recovery), attempts --resume first.
        """
        async with self.lock:
            if self.proc:
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except:
                    pass
                self.proc = None

            if self._reader_task:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except asyncio.CancelledError:
                    pass
                self._reader_task = None

            if self._stderr_task:
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except asyncio.CancelledError:
                    pass
                self._stderr_task = None

        await self._spawn(force_fresh=force_fresh)
    
    def get_status(self) -> dict:
        """Get current process status."""
        compaction_info = {
            "context_window": self.context_window,
            "compaction_count": self._compaction_state.get("compaction_count", 0),
            "last_compaction_at": self._compaction_state.get("last_compaction_at"),
            "has_summary": self._compaction_state.get("last_summary") is not None,
            "history_length": len(self._compaction_state.get("history", [])),
            "compacting": self._compacting,
        }

        # Session age for resume eligibility
        state = _load_session_state()
        last_active = state.get("last_active_at", 0)
        session_age = int(time.time() - last_active) if last_active else None

        if not self.proc or self.proc.returncode is not None:
            return {
                "running": False,
                "uptime_seconds": 0,
                "message_count": self.message_count,
                "session_id": self.session_id,
                "resume_status": self.resume_status,
                "session_age_secs": session_age,
                "resume_timeout_secs": RESUME_TIMEOUT_SECS,
                "resume_eligible": session_age is not None and session_age < RESUME_TIMEOUT_SECS,
                "wake_message": self._wake_message,
                "context_window": self.context_window,
                "last_input_tokens": self.last_input_tokens,
                "peak_input_tokens": self.peak_input_tokens,
                "cumulative_input_tokens": self.total_input_tokens,  # Billing counter — NOT context size
                "cumulative_output_tokens": self.total_output_tokens,
                "compaction": compaction_info,
                "active_docs": sorted(self._active_docs),
                "root_active": _root_active(),
                "root_expires_in": _root_expires_in(),
            }

        uptime = time.time() - self.started_at if self.started_at else 0
        return {
            "running": True,
            "pid": self.proc.pid,
            "uptime_seconds": int(uptime),
            "message_count": self.message_count,
            "session_id": self.session_id,
            "context_injected": self._context_injected,
            "resume_status": self.resume_status,
            "session_age_secs": session_age,
            "resume_timeout_secs": RESUME_TIMEOUT_SECS,
            "resume_eligible": session_age is not None and session_age < RESUME_TIMEOUT_SECS,
            "wake_message": self._wake_message,
            "context_window": self.context_window,
            "last_input_tokens": self.last_input_tokens,
            "peak_input_tokens": self.peak_input_tokens,
            "cumulative_input_tokens": self.total_input_tokens,  # Billing counter — NOT context size
            "cumulative_output_tokens": self.total_output_tokens,
            "compaction": compaction_info,
            "active_docs": sorted(self._active_docs),
            "root_active": _root_active(),
            "root_expires_in": _root_expires_in(),
        }


# Global persistent process instance — created in run_server() after __main__
# sets port-specific file paths. Must not construct at import time.
_persistent_claude = None

# Root elevation state (module-level, not persisted — expires on restart)
_root_expires: float = 0.0  # epoch timestamp when root creds expire (0 = not active)


def _root_active() -> bool:
    return time.time() < _root_expires


def _root_expires_in() -> int:
    """Seconds remaining on root elevation, 0 if not active."""
    remaining = int(_root_expires - time.time())
    return max(0, remaining)


# --- HTTP Server ---

async def run_server(port: int = DEFAULT_PORT):
    """Run the bridge as an HTTP server."""
    from aiohttp import web
    
    async def on_startup(app):
        # Create and start the persistent process
        global _persistent_claude
        if _persistent_claude is None:
            _persistent_claude = PersistentClaudeProcess()
        await _persistent_claude.ensure_running()
    
    async def on_cleanup(app):
        # Cleanup
        if _persistent_claude.proc:
            _persistent_claude.proc.kill()
    
    async def health(request):
        try:
            proc = await asyncio.create_subprocess_exec(
                CLAUDE_BIN, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            version = stdout.decode().strip()
            
            status = _persistent_claude.get_status()
            
            return web.json_response({
                "status": "ok",
                "version": version,
                "persistent_process": status,
                "context_injection": True,
                "worker_identity": _worker_identity,
            })
        except Exception as e:
            return web.json_response({"status": "error", "error": str(e)}, status=500)
    
    async def status_endpoint(request):
        """GET /status — Detailed process status."""
        status = _persistent_claude.get_status()
        return web.json_response(status)
    
    async def chat(request):
        """
        POST /chat
        Body: {
            "message": "user prompt",
            "model": "opus",             # optional (ignored for now, always uses persistent process)
            "system": "system prompt",   # optional (ignored, context injected on first message)
            "session_id": "abc123",      # optional (ignored, using global persistent session)
            "stream": true,              # optional, default true
            "max_turns": 10,              # optional (ignored, persistent session has no turn limit)
            "inject_context": true       # optional, default true
        }
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        
        message = body.get("message", "").strip()
        if not message:
            return web.json_response({"error": "Missing 'message'"}, status=400)
        
        do_stream = body.get("stream", True)
        inject_context = body.get("inject_context", True)

        t0 = time.time()

        try:
            if do_stream:
                # Streaming SSE response
                response = web.StreamResponse(
                    status=200,
                    reason="OK",
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "Access-Control-Allow-Origin": "*",
                    },
                )
                await response.prepare(request)
                
                full_text = ""
                result_event = None
                
                client_connected = True
                async for event in _persistent_claude.send_message(message, inject_context):
                    event_type = event.get("type", "")
                    chunk = None
                    
                    # Handle stream_event wrapper
                    if event_type == "stream_event":
                        inner_event = event.get("event", {})
                        inner_type = inner_event.get("type", "")
                        
                        if inner_type == "content_block_start":
                            cb = inner_event.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                elapsed = int(time.time() - t0)
                                tool_name = cb.get("name", "tool")
                                tool_input = cb.get("input", {})
                                # Extract meaningful detail for the work log
                                detail = ""
                                if tool_name == "Bash" and tool_input.get("command"):
                                    detail = tool_input["command"][:200]
                                elif tool_name in ("Read", "Write") and tool_input.get("file_path"):
                                    detail = tool_input["file_path"]
                                elif tool_name == "Edit" and tool_input.get("file_path"):
                                    detail = tool_input["file_path"]
                                elif tool_name in ("Grep", "Glob") and tool_input.get("pattern"):
                                    detail = tool_input["pattern"]
                                chunk = f"data: {json.dumps({'type': 'ping', 'elapsed': elapsed, 'tool': tool_name, 'detail': detail})}\n\n"
                            elif cb.get("type") == "thinking":
                                chunk = f"data: {json.dumps({'type': 'thinking_start'})}\n\n"

                        elif inner_type == "content_block_delta":
                            delta_obj = inner_event.get("delta", {})
                            if delta_obj.get("type") == "text_delta":
                                delta_text = delta_obj.get("text", "")
                                full_text += delta_text
                                chunk = f"data: {json.dumps({'type': 'chunk', 'text': delta_text})}\n\n"
                            elif delta_obj.get("type") == "thinking_delta":
                                thinking_text = delta_obj.get("thinking", "")
                                if thinking_text:
                                    chunk = f"data: {json.dumps({'type': 'thinking', 'text': thinking_text})}\n\n"
                            elif delta_obj.get("type") == "summary_delta":
                                summary_text = delta_obj.get("summary", "")
                                if summary_text:
                                    chunk = f"data: {json.dumps({'type': 'thinking_summary', 'text': summary_text})}\n\n"
                    
                    elif event_type == "assistant":
                        msg = event.get("message", {})
                        content = msg.get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                full_text = block.get("text", "")
                            elif block.get("type") == "tool_use":
                                elapsed = int(time.time() - t0)
                                tool_name = block.get("name", "tool")
                                tool_input = block.get("input", {})
                                detail = ""
                                if tool_name == "Bash" and tool_input.get("command"):
                                    detail = tool_input["command"][:200]
                                elif tool_name in ("Read", "Write") and tool_input.get("file_path"):
                                    detail = tool_input["file_path"]
                                elif tool_name == "Edit" and tool_input.get("file_path"):
                                    detail = tool_input["file_path"]
                                elif tool_name in ("Grep", "Glob") and tool_input.get("pattern"):
                                    detail = tool_input["pattern"]
                                chunk = f"data: {json.dumps({'type': 'ping', 'elapsed': elapsed, 'tool': tool_name, 'detail': detail})}\n\n"
                    
                    elif event_type == "ping":
                        chunk = f"data: {json.dumps({'type': 'ping', 'elapsed': event.get('elapsed', 0), 'tool': event.get('tool', 'working')})}\n\n"
                    
                    elif event_type == "error":
                        chunk = f"data: {json.dumps({'type': 'error', 'error': event.get('error', 'Unknown error')})}\n\n"
                    
                    elif event_type == "result":
                        result_event = event
                        result_text = event.get("result", full_text)
                        duration_ms = int((time.time() - t0) * 1000)
                        session_id = event.get("session_id")
                        chunk = f"data: {json.dumps({'type': 'done', 'text': result_text, 'duration_ms': duration_ms, 'session_id': session_id, 'tokens': _persistent_claude.last_input_tokens, 'context_window': _persistent_claude.context_window})}\n\n"
                    
                    # Write to client (skip if disconnected)
                    if chunk and client_connected:
                        try:
                            await response.write(chunk.encode())
                        except (ConnectionResetError, ConnectionError, Exception) as write_err:
                            if "closing transport" in str(write_err).lower() or "connection" in str(write_err).lower():
                                logger.warning(f"Client disconnected, continuing to drain response")
                                client_connected = False
                            else:
                                raise
                    
                    elif event_type == "error":
                        chunk = f"data: {json.dumps(event)}\n\n"
                        await response.write(chunk.encode())
                
                await response.write_eof()
                return response
                
            else:
                # Non-streaming JSON response
                full_text = ""
                result_event = None
                error = None
                
                async for event in _persistent_claude.send_message(message, inject_context):
                    event_type = event.get("type", "")
                    
                    # Handle stream_event wrapper
                    if event_type == "stream_event":
                        inner_event = event.get("event", {})
                        inner_type = inner_event.get("type", "")
                        
                        if inner_type == "content_block_delta":
                            delta_obj = inner_event.get("delta", {})
                            if delta_obj.get("type") == "text_delta":
                                full_text += delta_obj.get("text", "")
                    
                    elif event_type == "assistant":
                        msg = event.get("message", {})
                        content = msg.get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                full_text = block.get("text", "")
                    
                    elif event_type == "result":
                        result_event = event
                    
                    elif event_type == "error":
                        error = event.get("error", "Unknown error")
                
                duration_ms = int((time.time() - t0) * 1000)
                
                if error:
                    return web.json_response({
                        "text": "",
                        "error": error,
                        "duration_ms": duration_ms,
                    }, headers={"Access-Control-Allow-Origin": "*"})
                
                result_text = result_event.get("result", full_text) if result_event else full_text
                session_id = result_event.get("session_id") if result_event else None
                
                return web.json_response({
                    "text": result_text or "(no response)",
                    "session_id": session_id,
                    "duration_ms": duration_ms,
                    "error": None,
                    "persistent": True,
                }, headers={"Access-Control-Allow-Origin": "*"})
                
        except Exception as e:
            logger.error(f"Chat error: {e}", exc_info=True)
            duration_ms = int((time.time() - t0) * 1000)
            if do_stream:
                try:
                    response = web.StreamResponse(
                        status=200,
                        headers={"Content-Type": "text/event-stream", "Access-Control-Allow-Origin": "*"}
                    )
                    await response.prepare(request)
                    chunk = f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
                    await response.write(chunk.encode())
                    await response.write_eof()
                    return response
                except:
                    pass

            return web.json_response({
                "text": "",
                "error": str(e),
                "duration_ms": duration_ms,
            }, status=500, headers={"Access-Control-Allow-Origin": "*"})
    
    async def events_stream(request):
        """GET /events — SSE stream of all bridge events (multi-browser support).

        Any browser can connect to this endpoint to passively observe streaming
        responses, tool calls, compaction events, etc. in real-time.
        """
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )
        await response.prepare(request)

        q = _persistent_claude.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection timeout
                    try:
                        await response.write(b": keepalive\n\n")
                    except (ConnectionResetError, ConnectionError):
                        break
                    continue

                event_type = event.get("type", "")
                sse_event = None

                # Map internal events to SSE format (same as /chat handler)
                if event_type == "stream_event":
                    inner = event.get("event", {})
                    inner_type = inner.get("type", "")
                    if inner_type == "content_block_start":
                        cb = inner.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            tool_name = cb.get("name", "tool")
                            tool_input = cb.get("input", {})
                            detail = ""
                            if tool_name == "Bash" and tool_input.get("command"):
                                detail = tool_input["command"][:200]
                            elif tool_name in ("Read", "Write", "Edit") and tool_input.get("file_path"):
                                detail = tool_input["file_path"]
                            elif tool_name in ("Grep", "Glob") and tool_input.get("pattern"):
                                detail = tool_input["pattern"]
                            sse_event = {"type": "ping", "tool": tool_name, "detail": detail}
                        elif cb.get("type") == "thinking":
                            sse_event = {"type": "thinking_start"}
                    elif inner_type == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            sse_event = {"type": "chunk", "text": delta.get("text", "")}
                        elif delta.get("type") == "thinking_delta":
                            text = delta.get("thinking", "")
                            if text:
                                sse_event = {"type": "thinking", "text": text}
                        elif delta.get("type") == "summary_delta":
                            text = delta.get("summary", "")
                            if text:
                                sse_event = {"type": "thinking_summary", "text": text}
                elif event_type == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "tool")
                            sse_event = {"type": "ping", "tool": tool_name}
                elif event_type == "ping":
                    sse_event = {"type": "ping", "elapsed": event.get("elapsed", 0), "tool": event.get("tool", "working")}
                elif event_type == "error":
                    sse_event = {"type": "error", "error": event.get("error", "Unknown error")}
                elif event_type == "result":
                    result_text = event.get("result", "")
                    session_id = event.get("session_id")
                    sse_event = {"type": "done", "text": result_text, "session_id": session_id, "tokens": _persistent_claude.last_input_tokens, "context_window": _persistent_claude.context_window}
                elif event_type == "user_message":
                    sse_event = {"type": "user_message", "text": event.get("text", ""), "ts": event.get("ts", 0)}
                elif event_type == "compaction":
                    sse_event = {"type": "compaction", "tokens": event.get("tokens", 0), "active_docs": event.get("active_docs", []), "loaded_files": event.get("loaded_files", [])}
                elif event_type == "compaction_done":
                    sse_event = {"type": "compaction_done", "summary_length": event.get("summary_length"), "compaction_count": event.get("compaction_count"), "loaded_files": event.get("loaded_files", [])}

                if sse_event:
                    try:
                        await response.write(f"data: {json.dumps(sse_event)}\n\n".encode())
                    except (ConnectionResetError, ConnectionError):
                        break
        finally:
            _persistent_claude.unsubscribe(q)

        return response

    async def context_preview(request):
        """GET /context — Preview the injected system prompt (for debugging)."""
        ctx, loaded_files = build_system_prompt()
        return web.json_response({
            "system_prompt_length": len(ctx),
            "system_prompt_preview": ctx[:2000] + ("..." if len(ctx) > 2000 else ""),
            "loaded_files": loaded_files,
        })
    
    async def chatlog_endpoint(request):
        """GET /chatlog?n=100&offset=0 — Return N chat messages with pagination.

        Each entry: {role: "user"|"assistant", text: "...", ts: <unix ms>}
        Used by the frontend to hydrate from server on new devices.
        Only user+assistant pairs are returned — tool calls are in .bridge_files.log.

        Params:
          n      — number of messages to return (default 100, max 2000)
          offset — skip this many messages from the end (default 0 = most recent)
        Returns:
          {messages: [...], count: N, total: T, has_more: bool}
        """
        try:
            n = int(request.rel_url.query.get("n", 100))
            n = min(max(n, 1), 2000)
        except ValueError:
            n = 100
        try:
            offset = int(request.rel_url.query.get("offset", 0))
            offset = max(offset, 0)
        except ValueError:
            offset = 0

        all_messages = []
        try:
            if CHAT_LOG_FILE.exists():
                with open(CHAT_LOG_FILE, "rb") as f:
                    # Read entire file for accurate total count + pagination
                    raw = f.read().decode("utf-8", errors="replace")
                raw_lines = raw.splitlines()

                for line in raw_lines:
                    if not line.strip():
                        continue
                    try:
                        bracket_end = line.index("]:")
                        header = line[1:bracket_end]
                        content = line[bracket_end + 2:].lstrip(" ")
                        parts = header.split(" ", 1)
                        role = parts[0].lower()
                        ts_str = parts[1] if len(parts) > 1 else ""
                        try:
                            import calendar
                            from datetime import datetime as _dt
                            ts_ms = int(calendar.timegm(_dt.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timetuple()) * 1000)
                        except Exception:
                            ts_ms = int(time.time() * 1000)
                        text = content.replace("\\n", "\n")
                        if role in ("user", "assistant"):
                            all_messages.append({"role": role, "text": text, "ts": ts_ms})
                    except (ValueError, IndexError):
                        continue
        except Exception as e:
            logger.warning(f"Failed to read chat log for /chatlog: {e}")

        total = len(all_messages)
        # Slice from the end: offset=0 means last N, offset=100 means 100 before that
        if offset >= total:
            page = []
        else:
            end_idx = total - offset
            start_idx = max(end_idx - n, 0)
            page = all_messages[start_idx:end_idx]

        has_more = (offset + len(page)) < total

        return web.json_response({
            "messages": page, "count": len(page),
            "total": total, "has_more": has_more
        }, headers={"Cache-Control": "no-store"})

    async def last_response_endpoint(request):
        """Return the last response text (for reconnect after disconnect)."""
        if _persistent_claude:
            return web.json_response({
                "text": _persistent_claude._last_response_text,
                "done": _persistent_claude._last_response_done,
                "events": len(_persistent_claude._response_events),
            }, headers={"Access-Control-Allow-Origin": "*"})
        return web.json_response({"text": "", "done": True, "events": 0})

    async def restart_endpoint(request):
        """POST /restart — Restart the persistent Claude process (fresh start)."""
        try:
            body = {}
            try:
                body = await request.json() if request.content_length else {}
            except Exception:
                pass
            # Default: fresh start. Pass {"resume": true} to attempt session resume.
            force_fresh = not body.get("resume", False)
            await _persistent_claude.restart(force_fresh=force_fresh)
            return web.json_response({
                "status": "ok",
                "message": f"Process restarted ({'fresh' if force_fresh else 'resume attempted'})",
                "resume_status": _persistent_claude.resume_status,
                "session_id": _persistent_claude.session_id,
            })
        except Exception as e:
            return web.json_response({"status": "error", "error": str(e)}, status=500)

    async def smart_compact_endpoint(request):
        """POST /smart-compact — Fast verbatim compact: keeps last N messages + ROADMAP, no LLM."""
        try:
            body = await request.json() if request.content_length else {}
            n = int(body.get("n_messages", 100))
            result = await _persistent_claude.smart_compact(n_messages=n)
            status = 200 if result.get("status") == "ok" else 500
            return web.json_response(result, status=status)
        except Exception as e:
            return web.json_response({"status": "error", "error": str(e)}, status=500)
    
    # Sudoers file written on root enable, removed on revoke/expiry
    SUDOERS_FILE = "/etc/sudoers.d/kukuibot-root-30m"
    SUDOERS_USER = os.environ.get("USER", os.getlogin())
    SUDOERS_RULES = (
        f"{SUDOERS_USER} ALL=(ALL) NOPASSWD: "
        "/bin/launchctl, /usr/bin/launchctl, "
        "/bin/chmod, /usr/sbin/chown, "
        "/usr/bin/install, /bin/mkdir, "
        "/usr/sbin/visudo, /bin/cat"
    )

    async def _write_sudoers(password: str) -> bool:
        """Write the NOPASSWD sudoers file using the validated password.
        Uses sudo -n (no password needed — already cached by the -v call above).
        Writes via a shell heredoc to avoid stdin conflicts.
        """
        import tempfile, stat
        content = f"# KukuiBot Root 30M — auto-removed on expiry\n{SUDOERS_RULES}\n"
        # Write content to a temp file, then sudo cp it into place
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sudoers', delete=False) as tf:
            tf.write(content)
            tmp_path = tf.name
        try:
            # Copy to sudoers.d with correct ownership and permissions via sudo
            cp_proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", "cp", tmp_path, SUDOERS_FILE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(cp_proc.communicate(), timeout=10.0)
            if cp_proc.returncode != 0:
                logger.warning(f"Failed to write sudoers: {stderr.decode().strip()}")
                return False
            # Fix permissions: sudoers.d files must be 0440, owned by root
            await (await asyncio.create_subprocess_exec(
                "sudo", "-n", "chmod", "0440", SUDOERS_FILE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )).wait()
            await (await asyncio.create_subprocess_exec(
                "sudo", "-n", "chown", "root:wheel", SUDOERS_FILE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )).wait()
            return True
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def _remove_sudoers():
        """Remove the NOPASSWD sudoers file."""
        try:
            if not Path(SUDOERS_FILE).exists():
                return
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", "rm", "-f", SUDOERS_FILE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            logger.info("Sudoers file removed")
        except Exception as e:
            logger.warning(f"Failed to remove sudoers file: {e}")

    async def root_enable_endpoint(request):
        """POST /root-enable — Validate sudo password, write NOPASSWD sudoers file for 30 min.
        Body: {"password": "..."}
        Password is passed to sudo stdin and immediately discarded — never stored.
        On expiry or revoke, the sudoers file is automatically deleted.
        """
        global _root_expires
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        password = body.get("password", "")
        if not password:
            return web.json_response({"error": "Missing password"}, status=400)

        try:
            # Step 1: validate password with sudo -S -v
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-S", "-v",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=(password + "\n").encode()),
                timeout=10.0,
            )
            if proc.returncode != 0:
                err = stderr.decode().strip()
                logger.warning(f"Root enable failed: {err}")
                return web.json_response({"error": "Authentication failed"}, status=403)

            # Step 2: write NOPASSWD sudoers file
            ok = await _write_sudoers(password)
            if not ok:
                return web.json_response({"error": "Failed to write sudoers file"}, status=500)

            _root_expires = time.time() + 1800  # 30 minutes
            logger.info("Root elevation active — sudoers file written, expires in 30 min")

            # Step 3: schedule auto-removal after 30 min
            async def _auto_revoke():
                await asyncio.sleep(1800)
                if _root_active():
                    return  # was manually extended — skip
                await _remove_sudoers()
                logger.info("Root elevation expired — sudoers file removed")
            asyncio.create_task(_auto_revoke())

            return web.json_response({"status": "ok", "expires_in": 1800})
        except asyncio.TimeoutError:
            return web.json_response({"error": "sudo timed out"}, status=500)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def root_revoke_endpoint(request):
        """POST /root-revoke — Remove NOPASSWD sudoers file and clear sudo cache."""
        global _root_expires
        _root_expires = 0.0
        await _remove_sudoers()
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-k",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
        except Exception:
            pass
        logger.info("Root elevation revoked — sudoers file removed")
        return web.json_response({"status": "ok"})

    async def options_handler(request):
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    # --- Worker list endpoint ---
    async def workers_endpoint(request):
        """GET /workers — List available worker identity files."""
        workers_dir = WORKSPACE / "workers"
        workers = []
        if workers_dir.is_dir():
            for f in sorted(workers_dir.glob("*.md")):
                key = f.stem  # e.g. "developer", "it-admin"
                content = f.read_text(encoding="utf-8", errors="replace")
                # Extract first heading as display name, strip "Worker Identity — " prefix
                name = key.replace("-", " ").title()
                for line in content.splitlines():
                    if line.startswith("# "):
                        raw = line.lstrip("# ").strip()
                        # Strip common prefix patterns
                        for prefix in ("Worker Identity — ", "Worker Identity - "):
                            if raw.startswith(prefix):
                                raw = raw[len(prefix):]
                                break
                        name = raw
                        break
                workers.append({"key": key, "name": name, "active": key == _worker_identity})
        return web.json_response({"workers": workers, "active": _worker_identity})

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Route definitions
    app.router.add_get("/health", health)
    app.router.add_get("/status", status_endpoint)
    app.router.add_get("/context", context_preview)
    app.router.add_post("/chat", chat)
    app.router.add_get("/events", events_stream)
    app.router.add_get("/chatlog", chatlog_endpoint)
    app.router.add_get("/last-response", last_response_endpoint)
    app.router.add_post("/restart", restart_endpoint)
    app.router.add_post("/smart-compact", smart_compact_endpoint)
    app.router.add_post("/root-enable", root_enable_endpoint)
    app.router.add_post("/root-revoke", root_revoke_endpoint)
    app.router.add_get("/workers", workers_endpoint)
    app.router.add_options("/chat", options_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    # TLS support — try wildcard cert first, then mkcert local cert
    ssl_ctx = None
    cert_candidates = [
        (Path.home() / "letsencrypt" / "config" / "live" / "example.com" / "fullchain.pem",
         Path.home() / "letsencrypt" / "config" / "live" / "example.com" / "privkey.pem"),
        (WORKSPACE / "src" / "certs" / "kukuibot.pem",
         WORKSPACE / "src" / "certs" / "kukuibot-key.pem"),
    ]
    for cert_file, key_file in cert_candidates:
        if cert_file.exists() and key_file.exists():
            import ssl
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(str(cert_file), str(key_file))
            logger.info(f"TLS enabled with cert: {cert_file}")
            break

    site = web.TCPSite(runner, "0.0.0.0", port, ssl_context=ssl_ctx)
    await site.start()
    proto = "https" if ssl_ctx else "http"
    logger.info(f"KukuiBot Claude Bridge running on {proto}://127.0.0.1:{port}")
    logger.info(f"  Worker: {_worker_identity}")
    logger.info(f"  Model: {_model}")
    logger.info(f"  POST /chat     — Send messages (streaming SSE or JSON)")
    logger.info(f"  GET  /health   — Check status + process info")
    logger.info(f"  GET  /status   — Detailed process status (uptime, message count)")
    logger.info(f"  GET  /workers  — List available worker identities")
    logger.info(f"  POST /restart  — Restart the persistent process")
    logger.info(f"  POST /smart-compact — Smart compact (verbatim transcript, no LLM)")
    logger.info(f"  GET  /context  — Preview injected system prompt")
    logger.info(f"  Context: SOUL + USER + TOOLS + Model + Worker ({_worker_identity})")
    logger.info(f"  Compaction: CLI auto-compact at 90% / manual via /smart-compact / window={CONTEXT_WINDOW}")
    
    await asyncio.Event().wait()


async def test_query(prompt: str):
    """Quick test without server."""
    print(f"Testing: {prompt}")
    print("---")
    
    await _persistent_claude.ensure_running()
    
    t0 = time.time()
    full_text = ""
    
    async for event in _persistent_claude.send_message(prompt, inject_context=True):
        event_type = event.get("type", "")
        
        # Handle stream_event wrapper
        if event_type == "stream_event":
            inner_event = event.get("event", {})
            inner_type = inner_event.get("type", "")
            
            if inner_type == "content_block_delta":
                delta_obj = inner_event.get("delta", {})
                if delta_obj.get("type") == "text_delta":
                    full_text += delta_obj.get("text", "")
        
        elif event_type == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", [])
            for block in content:
                if block.get("type") == "text":
                    full_text = block.get("text", "")
        
        elif event_type == "result":
            result_text = event.get("result", full_text)
            duration_ms = int((time.time() - t0) * 1000)
            print(result_text)
            print(f"--- ({duration_ms}ms, session={event.get('session_id', 'none')[:8]})")
            break
        
        elif event_type == "error":
            print(f"ERROR: {event.get('error')}")
            break


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KukuiBot Claude Code Bridge")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help="Claude model to use (e.g. opus, claude-sonnet-4-6)")
    parser.add_argument("--worker", type=str, default="developer", help="Worker identity (e.g. developer, it-admin, seo)")
    parser.add_argument("--app-dir", type=str, default=None, help="Per-instance app directory (contains ROADMAP.md, etc.)")
    parser.add_argument("--session", type=str, default="", help="KukuiBot tab session ID for chat log filtering")
    parser.add_argument("--test", type=str, help="Quick test query")
    args = parser.parse_args()

    # Set model, worker, and session globals
    globals()['_model'] = args.model
    globals()['_worker_identity'] = args.worker
    globals()['_kukuibot_session_id'] = args.session

    # Set per-instance app directory (for ROADMAP.md etc.)
    globals()['_app_dir'] = Path(args.app_dir) if args.app_dir else WORKSPACE

    # Make session/compaction/chat-log files worker-specific so multiple bridges don't collide
    worker = args.worker or "developer"
    worker_suffix = f".{worker}" if worker != "developer" else ""
    globals()['_SESSIONS_FILE'] = WORKSPACE / f".bridge_sessions{worker_suffix}.json"
    globals()['COMPACTION_HISTORY_FILE'] = WORKSPACE / f".bridge_compaction{worker_suffix}.json"
    globals()['CHAT_LOG_FILE'] = WORKSPACE / f".bridge_chat{worker_suffix}.log"
    globals()['FILE_LOG_FILE'] = WORKSPACE / f".bridge_files{worker_suffix}.log"

    if args.test:
        asyncio.run(test_query(args.test))
    else:
        asyncio.run(run_server(args.port))
