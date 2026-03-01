"""claude_bridge.py — Multi-process Claude Code CLI bridge for KukuiBot.

Manages a pool of up to MAX_CLAUDE_PROCESSES `claude --print` subprocesses,
one per Claude tab. Each tab gets its own isolated conversation context.

Architecture (per-process, with dynamic pool):
  - One claude process per Claude tab (max 3 simultaneous)
  - Resume-first: attempts --resume with 30s timeout, falls back to fresh start
  - Context injection on fresh start: SOUL.md, USER.md, TOOLS.md, ROADMAP.md, chat log
  - Smart compaction: non-destructive inject as user message, drain ack silently
  - CLI auto-compaction at ~90%: let it happen naturally, no recovery injection
  - Token tracking from API result events
  - Multi-browser broadcast via subscriber queues (per-process)
  - Idle processes reaped after IDLE_TIMEOUT_S seconds

Compaction strategy:
  - CLI auto-compacts at ~90% — we don't interfere or inject recovery context.
  - Manual smart-compact available via /api/claude/smart-compact endpoint.
  - Smart compact injects verbatim transcript + ROADMAP + active docs as a
    user message (non-destructive, no process restart).

Security posture:
  - No shell=True
  - Messages passed via stdin JSON (not interpolated into argv)
  - ANTHROPIC_API_KEY injected into subprocess env from DB
  - Permission mode: bypassPermissions (trusted local agent)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

from config import (
    COMPACTION_LOG_FILE,
    COMPACTION_LOG_MAX_LINES,
    MAX_CLAUDE_PROCESSES,
    SKILLS_DIR,
    WORKSPACE,
)
from auth import db_connection
from log_store import log_query, log_write
from skill_loader import load_skills_for_worker

logger = logging.getLogger("kukuibot.claude_bridge")


# ---------------------------------------------------------------------------
#  OAuth Token Refresh Helper
# ---------------------------------------------------------------------------

_CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"


def _read_creds() -> tuple[str, str, int]:
    """Read (accessToken, refreshToken, expiresAt_ms) from CLI credentials."""
    try:
        if not _CREDS_PATH.is_file():
            return "", "", 0
        data = json.loads(_CREDS_PATH.read_text())
        oauth = data.get("claudeAiOauth", {})
        tok = (oauth.get("accessToken") or "").strip()
        refresh = (oauth.get("refreshToken") or "").strip()
        exp = int(oauth.get("expiresAt", 0))
        return tok, refresh, exp
    except Exception:
        return "", "", 0


def _write_creds(access_token: str, refresh_token: str, expires_at_ms: int) -> bool:
    """Update the CLI credentials file with new tokens."""
    try:
        data = {}
        if _CREDS_PATH.is_file():
            data = json.loads(_CREDS_PATH.read_text())
        oauth = data.get("claudeAiOauth", {})
        oauth["accessToken"] = access_token
        oauth["refreshToken"] = refresh_token
        oauth["expiresAt"] = expires_at_ms
        data["claudeAiOauth"] = oauth
        _CREDS_PATH.write_text(json.dumps(data, indent=2))
        return True
    except Exception as e:
        logger.warning(f"Failed to write credentials: {e}")
        return False


def _direct_refresh_token(refresh_tok: str) -> dict:
    """Call the Claude OAuth token endpoint directly with a refresh token.

    Returns {"access_token": str, "refresh_token": str, "expires_in": int}
    or raises on failure.
    """
    import urllib.request
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": _CLAUDE_OAUTH_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        _CLAUDE_OAUTH_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


async def refresh_cli_oauth_token() -> tuple[bool, str]:
    """Refresh the Claude CLI OAuth token.

    Strategy:
      1. Try direct refresh via the OAuth token endpoint (works even if token
         is already expired, as long as refresh_token is valid)
      2. Fall back to spawning a throwaway CLI process (older method)

    Returns (success, new_token).
    """
    old_tok, refresh_tok, old_exp = _read_creds()

    # Strategy 1: Direct refresh using the refresh_token
    if refresh_tok:
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _direct_refresh_token, refresh_tok
            )
            new_access = result.get("access_token", "")
            new_refresh = result.get("refresh_token", refresh_tok)
            expires_in = result.get("expires_in", 0)
            if new_access:
                expires_at_ms = int((time.time() + expires_in) * 1000) if expires_in else 0
                _write_creds(new_access, new_refresh, expires_at_ms)
                logger.info(f"refresh_cli_oauth_token: direct refresh succeeded "
                            f"(new expiry in {expires_in / 60:.0f}m)")
                return True, new_access
        except Exception as e:
            logger.warning(f"refresh_cli_oauth_token: direct refresh failed: {e}")

    # Strategy 2: Spawn throwaway CLI process (may work if token is close to
    # expiry but not yet fully expired)
    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, "--print", "-p", "ping",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=45)
    except asyncio.TimeoutError:
        logger.warning("refresh_cli_oauth_token: throwaway process timed out (45s)")
        try:
            proc.kill()
        except Exception:
            pass
        return False, ""
    except Exception as e:
        logger.warning(f"refresh_cli_oauth_token: spawn failed: {e}")
        return False, ""

    new_tok, _, new_exp = _read_creds()
    if new_exp > old_exp and new_tok:
        logger.info(f"refresh_cli_oauth_token: CLI refresh succeeded "
                    f"(new expiry in {(new_exp / 1000 - time.time()) / 60:.0f}m)")
        return True, new_tok
    return False, ""


def _find_claude_binary() -> str:
    """Find the claude binary, searching common install locations.

    Priority:
      1. CLAUDE_BIN env var (explicit override)
      2. 'claude' on current PATH (subprocess exec will find it)
      3. Common install locations: ~/.local/bin, npm global prefix, homebrew, /usr/local/bin
      4. Broad search via 'find' in home directory bin paths
    """
    import shutil
    import subprocess as _sp

    # 1. Explicit env var
    env_bin = os.environ.get("CLAUDE_BIN", "").strip()
    if env_bin:
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            logger.info(f"Claude binary from CLAUDE_BIN env: {env_bin}")
            return env_bin
        # If env var is set but file doesn't exist, log warning and continue searching
        logger.warning(f"CLAUDE_BIN={env_bin} not found or not executable, searching...")

    # 2. Already on PATH
    found = shutil.which("claude")
    if found:
        logger.info(f"Claude binary on PATH: {found}")
        return found

    # 3. Check common locations
    home = Path.home()
    common_paths = [
        home / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        home / ".npm-global" / "bin" / "claude",
        home / ".nvm" / "current" / "bin" / "claude",
    ]

    # Also check npm global prefix if npm is available
    try:
        result = _sp.run(["npm", "prefix", "-g"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            npm_prefix = result.stdout.strip()
            if npm_prefix:
                common_paths.insert(0, Path(npm_prefix) / "bin" / "claude")
    except Exception:
        pass

    # Also check any nvm-managed node versions
    nvm_dir = home / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        try:
            for node_ver in sorted(nvm_dir.iterdir(), reverse=True):
                candidate = node_ver / "bin" / "claude"
                if candidate.is_file():
                    common_paths.insert(0, candidate)
                    break
        except Exception:
            pass

    for p in common_paths:
        if p.is_file() and os.access(p, os.X_OK):
            logger.info(f"Claude binary found at: {p}")
            return str(p)

    # 4. Last resort: search home directory bin-like paths
    search_dirs = [
        str(home / ".local"),
        str(home / ".npm-global"),
        str(home / ".nvm"),
        "/opt/homebrew",
        "/usr/local",
    ]
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        try:
            for root, dirs, files in os.walk(search_dir):
                if "claude" in files:
                    candidate = os.path.join(root, "claude")
                    if os.access(candidate, os.X_OK):
                        logger.info(f"Claude binary discovered via walk: {candidate}")
                        return candidate
                # Don't descend into node_modules or deep trees
                if root.count(os.sep) - search_dir.count(os.sep) > 5:
                    dirs.clear()
                # Skip node_modules
                if "node_modules" in dirs:
                    dirs.remove("node_modules")
        except Exception:
            pass

    # Not found anywhere — return "claude" and let it fail with a clear error at spawn time
    logger.warning("Claude binary not found in any searched location — will fail at spawn time")
    return "claude"


CLAUDE_BIN = _find_claude_binary()

# Context window: Claude Code CLI uses 1M context
CONTEXT_WINDOW = 1_000_000

# Display threshold (CLI auto-compacts at ~90%)
COMPACTION_THRESHOLD = 750_000

# Pool limits (MAX_CLAUDE_PROCESSES imported from config.py)
IDLE_TIMEOUT_S = 2 * 60 * 60  # Kill idle processes after 2 hours
DELEG_IDLE_TIMEOUT_S = 15 * 60  # Kill idle delegation processes after 15 minutes

# Max silence before _receive_response gives up (seconds).
# During tool-heavy turns, the CLI may emit no *broadcast* events while tools
# execute (only non-broadcast assistant events).  We track stdout activity
# separately; this timeout is only checked when _both_ the subscriber queue
# and the raw stdout are silent.  Default 300s (5 min); override via env var.
RESPONSE_SILENCE_TIMEOUT_S = int(os.environ.get("CLAUDE_RESPONSE_TIMEOUT", "300"))

# Resume timeout: skip --resume if last activity was more than this many seconds ago.
# Claude CLI sessions expire after some time; attempting to resume a stale session
# wastes time on a guaranteed failure.
RESUME_TIMEOUT_SECS = 3600  # 1 hour

# Max seconds to wait for a resumed process to produce its first stdout event.
# If the CLI doesn't emit anything within this window, we kill it and start fresh.
RESUME_INIT_TIMEOUT_S = 30

# Session and compaction state files (under ~/.kukuibot/)
# For pool mode, per-slot files use: .claude_session_{slot_id}.json
_SESSION_FILE = WORKSPACE / ".claude_session.json"
_COMPACTION_FILE = WORKSPACE / ".claude_compaction.json"

# Only reabsorb documentation files on compaction — never source code.
_DOC_EXTENSIONS = {".md", ".txt", ".log", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".env"}


# --- Health Check ---

@dataclass
class ClaudeHealth:
    installed: bool
    path: str
    version: str
    error: str


async def claude_health() -> ClaudeHealth:
    try:
        path = CLAUDE_BIN
        if not os.path.isabs(path):
            import shutil
            found = shutil.which(path)
            path = found or path

        if not os.path.isfile(path):
            return ClaudeHealth(False, path, "", f"claude not found at {path}")

        vproc = await asyncio.create_subprocess_exec(
            path, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        vout, verr = await vproc.communicate()
        version = (vout.decode(errors="ignore").strip() if vout else "")
        if vproc.returncode != 0:
            return ClaudeHealth(True, path, "", (verr.decode(errors="ignore") if verr else "version failed"))

        return ClaudeHealth(True, path, version, "")
    except Exception as e:
        return ClaudeHealth(False, "", "", f"{type(e).__name__}: {e}")


# --- Session & Compaction State (per-slot) ---

def _slot_session_file(slot_id: str = "") -> Path:
    """Return the session state file path for a slot."""
    if slot_id:
        return WORKSPACE / f".claude_session_{slot_id}.json"
    return _SESSION_FILE


def _slot_compaction_file(slot_id: str = "") -> Path:
    """Return the compaction state file path for a slot."""
    if slot_id:
        return WORKSPACE / f".claude_compaction_{slot_id}.json"
    return _COMPACTION_FILE


def _load_session_state(slot_id: str = "") -> dict:
    f = _slot_session_file(slot_id)
    try:
        if f.exists():
            return json.loads(f.read_text())
    except Exception as e:
        logger.warning(f"Failed to load session state ({slot_id}): {e}")
    return {}


def _save_session_state(state: dict, slot_id: str = ""):
    f = _slot_session_file(slot_id)
    try:
        f.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save session state ({slot_id}): {e}")


def _load_compaction_state(slot_id: str = "") -> dict:
    f = _slot_compaction_file(slot_id)
    try:
        if f.exists():
            return json.loads(f.read_text())
    except Exception as e:
        logger.warning(f"Failed to load compaction state ({slot_id}): {e}")
    return {"history": [], "last_summary": None, "compaction_count": 0}


def _save_compaction_state(state: dict, slot_id: str = ""):
    f = _slot_compaction_file(slot_id)
    try:
        f.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save compaction state ({slot_id}): {e}")


# Delimiter used when prepending queued delegation notifications before a user
# message. Canonical definition: notification_store.DELEGATION_PREPEND_BOUNDARY.
# Duplicated here to avoid import dependency from subprocess bridge.
_DELEGATION_PREPEND_BOUNDARY = "[[KUKUIBOT_DELEGATION_BOUNDARY_V1]]"


# --- Chat Log Helpers ---

def _append_to_bridge_chat_log(role: str, content: str, kukuibot_session_id: str = "", worker_identity: str = "", model_key: str = ""):
    """Append a message to the persistent SQLite log (never cleared on compact)."""
    try:
        log_write(
            "chat",
            content,
            role=role.lower(),
            session_id=kukuibot_session_id,
            worker=worker_identity,
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


def _load_chat_log_tail(max_chars: int = 20_000, max_line_chars: int = 10_000, kukuibot_session_id: str = "", worker_identity: str = "") -> Optional[str]:
    """Read recent chat log content from SQLite.

    Lines longer than max_line_chars are truncated.
    Total output is capped at max_chars.

    If kukuibot_session_id is provided, only lines belonging to that session are included.
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

        # Calculate character counts and slice the newest rows that fit
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

        # Reverse the final result so it reads chronologically (oldest to newest)
        result.reverse()

        return "\n".join(result) if result else None
    except Exception as e:
        logger.warning(f"Failed to load chat log tail: {e}")
        return None


def _flush_summary_to_memory(summary: str):
    """Append compaction summary to the rolling compaction log."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n\n## Compaction ({timestamp})\n{summary}\n"
        with open(COMPACTION_LOG_FILE, "a") as f:
            f.write(entry)
        with open(COMPACTION_LOG_FILE, "r") as f:
            lines = f.readlines()
        if len(lines) > COMPACTION_LOG_MAX_LINES:
            with open(COMPACTION_LOG_FILE, "w") as f:
                f.writelines(lines[-COMPACTION_LOG_MAX_LINES:])
        logger.info(f"Flushed compaction summary to {COMPACTION_LOG_FILE}")
    except Exception as e:
        logger.warning(f"Failed to flush compaction log: {e}")


# --- Context Injection ---

def _load_context_file(path: Path) -> Optional[str]:
    try:
        if path.exists():
            text = path.read_text().strip()
            return text if text else None
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
    return None


def _load_project_report(path: Path, *, max_chars: int = 6000) -> Optional[str]:
    """Load PROJECT-REPORT.md with optional staleness warning.

    Warning is injected when report mtime is older than 48 hours.
    """
    content = _load_context_file(path)
    if not content:
        return None

    if len(content) > max_chars:
        content = content[:max_chars] + "\n... (truncated)"

    try:
        age_seconds = max(0.0, time.time() - path.stat().st_mtime)
        if age_seconds > 48 * 3600:
            updated = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            warning = (
                "> ⚠️ Staleness warning: PROJECT-REPORT.md is older than 48 hours "
                f"(last updated {updated} local time).\n\n"
            )
            return warning + content
    except Exception:
        pass

    return content


def _build_available_workers_section(current_session_id: str = "") -> str:
    """Query tab_meta and return a concise markdown table of active worker tabs.

    Excludes tombstoned tabs. Marks the current session with '(you)'.
    Returns empty string on any DB error so context loading is never blocked.
    """
    try:
        with db_connection() as db:
            # Resolve owner from current session, or fall back to first owner found
            owner = None
            if current_session_id:
                row = db.execute(
                    "SELECT owner FROM tab_meta WHERE session_id = ? LIMIT 1",
                    (current_session_id,),
                ).fetchone()
                if row:
                    owner = row[0]
            if not owner:
                row = db.execute("SELECT owner FROM tab_meta LIMIT 1").fetchone()
                owner = row[0] if row else None
            if not owner:
                return ""

            # LEFT JOIN tombstones to exclude deleted tabs.
            # Wrap in try/except in case tab_tombstones doesn't exist yet.
            try:
                rows = db.execute(
                    """
                    SELECT tm.label, tm.model_key, COALESCE(tm.worker_identity, ''),
                           tm.session_id, COALESCE(tm.sort_order, 0)
                    FROM tab_meta tm
                    LEFT JOIN tab_tombstones tt
                      ON tt.owner = tm.owner AND tt.session_id = tm.session_id
                    WHERE tm.owner = ?
                      AND COALESCE(tt.deleted_at, 0) = 0
                    ORDER BY tm.sort_order, tm.label
                    """,
                    (owner,),
                ).fetchall()
            except Exception:
                # tab_tombstones may not exist — fall back to plain query
                rows = db.execute(
                    """
                    SELECT label, model_key, COALESCE(worker_identity, ''),
                           session_id, COALESCE(sort_order, 0)
                    FROM tab_meta
                    WHERE owner = ?
                    ORDER BY sort_order, label
                    """,
                    (owner,),
                ).fetchall()

        if not rows:
            return ""

        lines = ["# Available Workers", "| Label | Model | Worker Role | Session ID |",
                 "|---|---|---|---|"]
        for label, model_key, worker, session_id, _sort in rows:
            note = " (you)" if session_id == current_session_id else ""
            lines.append(f"| {label}{note} | {model_key or '?'} | {worker or '-'} | {session_id} |")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Failed to build available workers section: {e}")
        return ""


def build_system_prompt(worker_identity: str = "", include_chat_log: bool = True, kukuibot_session_id: str = "", model: str = "") -> tuple[str, list[str]]:
    """Build the full system prompt for fresh start context injection.

    Loads: SOUL, USER, TOOLS, Model Identity, Worker Identity, chat log tail (20KB).

    Args:
        worker_identity: Worker role key (e.g. "developer", "it-admin")
        include_chat_log: Whether to include recent chat history
        kukuibot_session_id: KukuiBot tab session_id — filters chat log to only this session's messages

    Returns:
        (prompt_text, loaded_files) — the assembled prompt and list of files that were loaded.
    """
    sections = []
    loaded_files = []

    soul = _load_context_file(WORKSPACE / "SOUL.md")
    if soul:
        sections.append(f"# Identity\n{soul}")
        loaded_files.append(str(WORKSPACE / "SOUL.md"))

    user_md = _load_context_file(WORKSPACE / "USER.md")
    if user_md:
        sections.append(f"# About the User\n{user_md}")
        loaded_files.append(str(WORKSPACE / "USER.md"))

    tools_md = _load_context_file(WORKSPACE / "TOOLS.md")
    if tools_md:
        sections.append(f"# Tools & Infrastructure Reference\n{tools_md}")
        loaded_files.append(str(WORKSPACE / "TOOLS.md"))

    # Per-model identity file — resolve dynamically based on model
    model_file = None
    models_dir = WORKSPACE / "models"
    if model and models_dir.is_dir():
        # Try exact match first: claude_sonnet.md, claude_opus.md, then generic claude.md
        for candidate_name in [f"claude_{model}.md", "claude.md"]:
            candidate = models_dir / candidate_name
            if candidate.is_file():
                model_file = candidate
                break
    else:
        # Fallback to the generic claude.md
        fallback = WORKSPACE / "models" / "claude.md"
        if fallback.is_file():
            model_file = fallback

    model_identity = _load_context_file(model_file) if model_file else ""
    if model_identity:
        sections.append(f"# Model Profile\n{model_identity}")
        loaded_files.append(str(model_file))

    # Worker identity (e.g. "developer", "it-admin", "seo-assistant")
    if worker_identity:
        worker_file = WORKSPACE / "workers" / f"{worker_identity}.md"
        worker_content = _load_context_file(worker_file)
        if worker_content:
            sections.append(f"# Worker Role\n{worker_content}")
            loaded_files.append(str(worker_file))

    # Load skills for this worker
    if worker_identity:
        skill_sections = load_skills_for_worker(worker_identity, SKILLS_DIR)
        if skill_sections:
            skills_header = (
                "# Skills (Mandatory Operating Rules)\n"
                "The following skills are binding operational constraints. "
                "If a skill applies with >=1% probability, invoke it BEFORE acting. "
                "Rationalization thoughts are compliance triggers, not exemptions."
            )
            sections.append(skills_header + "\n\n" + "\n\n".join(skill_sections))
            loaded_files.append(f"skills ({len(skill_sections)} loaded)")

    # Project report (shared, concise context for all workers)
    project_report_path = WORKSPACE / "PROJECT-REPORT.md"
    project_report = _load_project_report(project_report_path)
    if project_report:
        sections.append(f"# Project Report\n{project_report}")
        loaded_files.append(str(project_report_path))

    # Available workers (dynamic from DB — gives coordinators delegation awareness)
    workers_section = _build_available_workers_section(current_session_id=kukuibot_session_id)
    if workers_section:
        sections.append(workers_section)
        loaded_files.append("tab_meta (available workers)")

    # Recent chat history (20KB tail — survives compaction, filtered by session)
    if include_chat_log:
        chat_tail = _load_chat_log_tail(kukuibot_session_id=kukuibot_session_id, worker_identity=worker_identity)
        if chat_tail:
            sections.append(f"# Recent Chat History\n{chat_tail}")
            loaded_files.append("chat_log (20KB tail)")

    return "\n\n---\n\n".join(sections), loaded_files


# --- Persistent Claude Process ---

class PersistentClaudeProcess:
    """Manages a single long-lived claude process with stream-json I/O."""

    def __init__(self, slot_id: str = "", api_key_fn=None, oauth_token_fn=None, worker_identity: str = "", model: str = "opus", kukuibot_session_id: str = ""):
        """
        Args:
            slot_id: Unique identifier for this process slot (used for per-slot state files)
            api_key_fn: Callable returning the ANTHROPIC_API_KEY string (from DB)
            oauth_token_fn: Callable returning the CLAUDE_CODE_OAUTH_TOKEN string (from DB)
            worker_identity: Worker role key (e.g. "developer", "it-admin", "seo-assistant")
            model: Claude model shortname for --model flag (e.g. "opus", "sonnet")
            kukuibot_session_id: The KukuiBot tab session_id (e.g. "tab-claude_opus-xxxx") for chat log isolation
        """
        self.slot_id = slot_id
        self._api_key_fn = api_key_fn
        self._oauth_token_fn = oauth_token_fn
        self.worker_identity = worker_identity
        self.model = model
        self.kukuibot_session_id = kukuibot_session_id

        self.proc: Optional[asyncio.subprocess.Process] = None
        self.session_id: Optional[str] = None
        self.message_count: int = 0
        self.total_input_tokens: int = 0    # Cumulative billing counter (grows forever) -- NOT context size
        self.total_output_tokens: int = 0   # Cumulative billing counter
        self.last_input_tokens: int = 0     # Context size THIS turn (the real number)
        self.peak_input_tokens: int = 0     # Max context size seen this session
        self.started_at: Optional[float] = None
        self.last_activity: float = time.time()  # For idle timeout
        self.lock = asyncio.Lock()
        self.chat_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        # --- Wake / Context Injection State ---
        # _context_injected: Tracks whether identity files (SOUL.md, USER.md, TOOLS.md,
        # worker identity, model profile) have been injected into this subprocess's context.
        # - Set True on first message (after context injection completes) or on session resume
        #   (resumed sessions already have context from their prior conversation).
        # - Set False on fresh subprocess spawn — triggers context injection on next send_message().
        # - Checked in send_message(): if False + inject_context=True, identity files are sent
        #   as a "[System Context]" message before the user's actual message, then the ack
        #   response ("Context loaded.") is drained silently.
        self._context_injected = False
        self._draining_ack = False  # True while consuming context ack — suppresses result broadcast
        self._last_response_text: str = ""
        self._last_response_done: bool = True
        self._active_docs: set = set()
        self._response_events: list = []
        self._subscribers: set = set()
        self._pending_notifications: list[str] = []
        self._current_tool: Optional[str] = None
        self._turn_iterations: int = 0    # Count of assistant events in current turn
        self._turn_user_events: int = 0    # Count of user events in current turn (tool_result round-trips → API calls - 1)
        self._last_stdout_activity: float = time.time()  # Updated on ANY stdout event (including non-broadcast assistant events)
        self.resume_status: str = "unknown"
        # _wake_message: Stores the notification text that triggered a proactive wake.
        # Set by _try_proactive_wake() in server.py when a delegation status change arrives
        # and the model is idle. If the user sends a message that preempts the wake, this
        # text is re-queued as a pending notification so it's not lost.
        self._wake_message: Optional[str] = None
        # Auth strategy for Claude subprocess env:
        # - configured: use KukuiBot-configured ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN if set
        # - local: rely on local Claude CLI auth state (no injected auth env vars)
        self._auth_strategy: str = "configured"
        self._last_auth_error: Optional[str] = None  # Captured from stderr/stdout when CLI reports auth failure
        self._auth_recovery_attempts: int = 0
        self._auth_recovery_window_start: float = 0.0
        self._auth_error_recovering: bool = False

        self._compaction_state = _load_compaction_state(slot_id)
        self._compacting = False
        self._session_loaded = False

    # --- Subscriber-based multi-browser broadcast ---

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        logger.info(f"Subscriber added (total: {len(self._subscribers)})")
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)
        logger.info(f"Subscriber removed (total: {len(self._subscribers)})")

    def queue_notification(self, message: str):
        """Queue a notification for delivery on next send_message() call.

        Called by _deliver_or_queue_parent_notification() in server.py (Step 2
        of the 4-step delivery pipeline). Notifications accumulate here while
        the model is busy. On the next send_message() call, drain_notifications()
        pops them all and prepends them to the user message text so the model
        sees them in-context.

        Also called by proactive wake preemption (api_chat user-message-preempts-
        internal-wake path) to re-queue the drained wake text when a user message
        cancels an in-progress internal wake.
        """
        self._pending_notifications.append(message)
        # Cap at 50 to prevent unbounded growth
        if len(self._pending_notifications) > 50:
            dropped = len(self._pending_notifications) - 50
            logger.warning(
                f"Notification queue overflow (slot={self.slot_id}): "
                f"dropping {dropped} oldest notification(s)"
            )
            self._pending_notifications = self._pending_notifications[-50:]
        logger.info(f"Notification queued (slot={self.slot_id}, pending={len(self._pending_notifications)})")

    def drain_notifications(self) -> list[str]:
        """Drain all pending notifications. Returns list of messages.

        Called in two places:
          1. send_message() — drains and prepends to the user's message text.
          2. _try_proactive_wake() in server.py — drains before firing the internal
             run so the notifications become the message itself (avoids double-prepend
             since send_message() would otherwise drain them again).
        """
        msgs = list(self._pending_notifications)
        self._pending_notifications.clear()
        return msgs

    @property
    def is_busy(self) -> bool:
        """True if a message is currently being processed."""
        return self.chat_lock.locked()

    def _broadcast(self, event: dict):
        """Broadcast an event to all subscribers."""
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # --- Tool doc tracking ---

    def _track_tool_docs(self, event: dict):
        """Track doc files touched by tool calls for compaction reabsorption."""
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
            if path.startswith("/Users/") and "/tmp/" not in path:
                _append_to_file_log(name, path)
                ext = Path(path).suffix.lower()
                if ext in _DOC_EXTENSIONS:
                    self._active_docs.add(path)

    # --- Compaction ---

    def _record_exchange(self, user_text: str, assistant_text: str, is_internal: bool = False):
        """Record a user/assistant exchange for compaction history and persistent chat log.

        When is_internal=True (proactive wake, system notifications), the input
        message is logged with role="system" so it renders as a system card in
        the UI instead of a user bubble.
        """
        input_role = "system" if is_internal else "user"
        history = self._compaction_state.get("history", [])
        history.append({"role": input_role, "content": user_text, "timestamp": time.time()})
        history.append({"role": "assistant", "content": assistant_text, "timestamp": time.time()})
        if len(history) > 200:
            history = history[-200:]
        self._compaction_state["history"] = history
        if len(history) % 10 == 0:
            _save_compaction_state(self._compaction_state, self.slot_id)
        _append_to_bridge_chat_log(
            input_role,
            user_text,
            kukuibot_session_id=self.kukuibot_session_id,
            worker_identity=self.worker_identity,
            model_key=f"claude_{self.model}",
        )
        _append_to_bridge_chat_log(
            "assistant",
            assistant_text,
            kukuibot_session_id=self.kukuibot_session_id,
            worker_identity=self.worker_identity,
            model_key=f"claude_{self.model}",
        )

    async def smart_compact(self) -> dict:
        """Smart compact: kill subprocess and spawn fresh with clean context.

        Kills the current Claude CLI process and starts a new one. The new
        process has an empty context window — identity files (SOUL, USER, TOOLS,
        model, worker, chat log) are injected automatically on the first
        send_message() call via the standard context injection path.
        """
        if self._compacting:
            return {"status": "error", "error": "Compaction already in progress"}

        self._compacting = True
        try:
            # Snapshot pre-compact state for SSE broadcast
            pre_compact_tokens = self.last_input_tokens
            pre_compact_docs = sorted(self._active_docs)

            # Build context summary for the memory log flush (not injected into process)
            context, loaded_files = build_system_prompt(worker_identity=self.worker_identity, kukuibot_session_id=self.kukuibot_session_id, model=self.model)

            # Flush to memory log
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _flush_summary_to_memory, context)

            # Update compaction state
            count = self._compaction_state.get("compaction_count", 0) + 1
            self._compaction_state = {
                "history": [],
                "last_summary": context,
                "compaction_count": count,
                "last_compaction_at": time.time(),
            }
            _save_compaction_state(self._compaction_state, self.slot_id)

            # Clear active docs (fresh process has no doc tracking)
            self._active_docs.clear()

            # Broadcast compaction start to SSE subscribers (multi-browser sync)
            self._broadcast({"type": "compaction", "tokens": pre_compact_tokens, "active_docs": pre_compact_docs, "loaded_files": loaded_files})

            # Kill existing subprocess and cancel background tasks (under lock)
            async with self.lock:
                if self.proc:
                    try:
                        self.proc.terminate()
                        try:
                            await asyncio.wait_for(self.proc.wait(), timeout=5)
                        except asyncio.TimeoutError:
                            self.proc.kill()
                            await self.proc.wait()
                    except Exception:
                        pass
                    self.proc = None

                for t in [self._reader_task, self._stderr_task]:
                    if t:
                        t.cancel()
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass
                self._reader_task = None
                self._stderr_task = None

                # Clear session_id so _spawn starts fresh (no --resume)
                self.session_id = None

            # Spawn fresh subprocess (outside lock — _spawn does not acquire self.lock)
            await self._spawn(force_fresh=True)

            # Broadcast compaction done to SSE subscribers (multi-browser sync)
            self._broadcast({
                "type": "compaction_done",
                "summary_length": len(context),
                "compaction_count": count,
                "loaded_files": loaded_files,
            })

            logger.info(f"Smart compact complete (fresh process). loaded_on_next_msg={loaded_files}, total compactions={count}")
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

    # --- Process Lifecycle ---

    async def ensure_running(self):
        """Start or restart the claude process if needed."""
        async with self.lock:
            if self.proc and self.proc.returncode is None:
                return

            if not self._session_loaded:
                state = _load_session_state(self.slot_id)
                self.session_id = state.get("session_id")
                self.last_input_tokens = state.get("last_input_tokens", 0)
                self.peak_input_tokens = state.get("peak_input_tokens", 0)
                self.total_input_tokens = state.get("total_input_tokens", 0)
                self.total_output_tokens = state.get("total_output_tokens", 0)
                self.message_count = state.get("message_count", 0)
                self._session_loaded = True

            logger.info(f"Starting persistent Claude process (slot={self.slot_id}, session_id={self.session_id or 'none'})...")
            await self._spawn()

    async def _spawn(self, force_fresh: bool = False):
        """Spawn the claude process with stream-json I/O.

        If a session_id exists, force_fresh is False, and the session is younger
        than RESUME_TIMEOUT_SECS, attempts --resume to restore the full
        conversation. Falls back to fresh start (with context re-injection) if
        resume fails, times out, or session is stale.
        """
        # Build env with explicit auth strategy handling.
        env = {**os.environ, "NO_COLOR": "1"}
        use_configured_auth = (self._auth_strategy or "configured") != "local"
        if use_configured_auth:
            if self._api_key_fn:
                key = self._api_key_fn()
                if key:
                    env["ANTHROPIC_API_KEY"] = key
            if self._oauth_token_fn:
                token = self._oauth_token_fn()
                if token:
                    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        else:
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

        base_cmd = [
            CLAUDE_BIN, "--print",
            "--model", self.model,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--permission-mode", "bypassPermissions",
            "--tools", "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch",
            "--add-dir", str(WORKSPACE),
            "--add-dir", str(Path.home()),
            "--verbose",
        ]

        # Decide whether to resume or start fresh
        resume_session = None
        if not force_fresh and self.session_id:
            state = _load_session_state(self.slot_id)
            last_active = state.get("last_active_at", 0)
            age_secs = time.time() - last_active if last_active else float("inf")
            if age_secs > RESUME_TIMEOUT_SECS:
                age_str = f"{age_secs / 3600:.1f}h" if age_secs < float("inf") else "unknown"
                logger.info(f"Session too old ({age_str} > {RESUME_TIMEOUT_SECS/3600:.0f}h) — starting fresh (slot={self.slot_id})")
            else:
                resume_session = self.session_id
                logger.info(f"Attempting resume: session={resume_session}, age={age_secs:.0f}s (slot={self.slot_id})")
        else:
            reason = "force_fresh" if force_fresh else "no session_id"
            logger.info(f"Starting fresh session ({reason}, slot={self.slot_id})")

        cmd = list(base_cmd)
        if resume_session:
            cmd.extend(["--resume", resume_session])

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKSPACE),
                env=env,
                limit=10 * 1024 * 1024,  # 10MB line buffer
            )

            self.started_at = time.time()
            # Clear stale state from previous process
            self._wake_message = None
            self._last_auth_error = None
            self._recent_stderr = []  # Ring buffer of last stderr lines

            if resume_session:
                # --- Session Resume Path ---
                # When resuming an existing Claude CLI session (--resume flag), the subprocess
                # picks up its prior conversation history. Context (identity files) was already
                # injected in the original session, so we skip re-injection.
                self.resume_status = "resuming"
                # Keep existing counters — we're continuing the session
                self._context_injected = True  # Resumed session already has context
            else:
                # --- Fresh Start Path ---
                # New subprocess with no prior conversation. _context_injected=False means
                # the first call to send_message() will inject identity files (SOUL.md, etc.)
                # before sending the user's actual message.
                self.resume_status = "fresh"
                # Reset counters for fresh start
                self.message_count = 0
                self.last_input_tokens = 0
                self.peak_input_tokens = 0
                self.total_input_tokens = 0
                self.total_output_tokens = 0
                self._context_injected = False

            # Start background reader + stderr tasks
            self._reader_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._stderr_reader())

            # For resumed sessions, verify the process is stable within RESUME_INIT_TIMEOUT_S
            if resume_session:
                init_ok = await self._wait_for_resume_init()
                if not init_ok:
                    logger.warning(f"Resume failed for session {resume_session} (slot={self.slot_id}), falling back to fresh start")
                    # Kill the failed process and its tasks
                    await self._cleanup_failed_spawn()
                    return await self._spawn(force_fresh=True)
                else:
                    self.resume_status = "resumed"
                    logger.info(f"Session resumed successfully: {resume_session} (slot={self.slot_id})")

            logger.info(f"Claude process started (PID={self.proc.pid}, resume_status={self.resume_status}, slot={self.slot_id})")

        except FileNotFoundError:
            logger.error(f"Claude Code CLI not found at '{CLAUDE_BIN}'. Install with: npm install -g @anthropic-ai/claude-code")
            raise RuntimeError(
                f"Claude Code CLI not found ('{CLAUDE_BIN}'). "
                f"Install it with: npm install -g @anthropic-ai/claude-code"
            )
        except Exception as e:
            logger.error(f"Failed to spawn claude process: {e}")
            if resume_session:
                logger.info(f"Retrying with fresh start after resume exception (slot={self.slot_id})")
                return await self._spawn(force_fresh=True)
            raise

    async def _wait_for_resume_init(self) -> bool:
        """Wait up to RESUME_INIT_TIMEOUT_S for the resumed process to show signs of life.

        Returns True if the process is alive and producing stdout, False if it
        died or timed out without any output.
        """
        deadline = time.time() + RESUME_INIT_TIMEOUT_S
        check_interval = 0.5  # Check every 500ms
        while time.time() < deadline:
            # Process exited → resume failed
            if self.proc is None or self.proc.returncode is not None:
                rc = self.proc.returncode if self.proc else "None"
                logger.warning(f"Resume process exited during init (rc={rc})")
                return False
            # stdout activity means the CLI accepted the --resume and is running
            if self._last_stdout_activity > self.started_at:
                return True
            await asyncio.sleep(check_interval)
        # Timeout — no stdout activity within the window
        logger.warning(f"Resume init timed out after {RESUME_INIT_TIMEOUT_S}s — no stdout activity")
        return False

    async def _cleanup_failed_spawn(self):
        """Kill a failed process and cancel its background tasks."""
        if self.proc:
            try:
                self.proc.kill()
                await self.proc.wait()
            except Exception:
                pass
            self.proc = None
        for t in [self._reader_task, self._stderr_task]:
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._reader_task = None
        self._stderr_task = None

    async def _read_loop(self):
        """Background task to read from stdout and route events to subscribers."""
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

                    # Skip system init messages
                    if event_type == "system":
                        continue

                    # Mark stdout as active for ANY parsed event (including
                    # non-broadcast assistant events).  This lets _receive_response
                    # distinguish "CLI is actively working (tools running)" from
                    # "CLI is truly dead/hung".
                    self._last_stdout_activity = time.time()

                    # Track doc files from tool calls + count iterations
                    if event_type == "assistant":
                        self._turn_iterations += 1
                        self._track_tool_docs(event)
                    elif event_type == "user":
                        self._turn_user_events += 1  # Each user event = tool_result → new API call

                    # Log significant events at INFO, deltas at DEBUG
                    if event_type in ("result", "assistant", "user"):
                        extra = ""
                        if event_type == "assistant":
                            blocks = [b.get("type") for b in event.get("message", {}).get("content", [])]
                            extra = f" blocks={blocks}"
                        logger.info(f"Event: {event_type}{extra}")
                    elif logger.isEnabledFor(logging.DEBUG):
                        extra = ""
                        if event_type == "stream_event":
                            extra = f" inner={event.get('event', {}).get('type', '?')}"
                        logger.debug(f"Event: {event_type}{extra}")

                    # Update session ID and token usage from result events
                    if event_type == "result":
                        new_session = event.get("session_id")
                        if new_session and new_session != self.session_id:
                            self.session_id = new_session
                            logger.info(f"Session updated: {self.session_id}")
                        # Track token usage
                        usage = event.get("usage", {})
                        iters = max(self._turn_iterations, 1)
                        # API calls = user events (tool_result round-trips) + 1 (initial message)
                        # The result event usage is CUMULATIVE across all API calls in the turn,
                        # so we divide by API call count to get per-call context size.
                        api_calls = self._turn_user_events + 1
                        logger.info(f"Result event usage (iters={iters}, api_calls={api_calls}): {usage}")
                        # Extract token usage fields from the result event.
                        uncached = usage.get("input_tokens", 0)
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)
                        total_input = uncached + cache_read + cache_create
                        # Divide cumulative total by API call count to estimate current context.
                        ctx_input = total_input // api_calls
                        billed_input = total_input
                        self.total_input_tokens += billed_input
                        self.total_output_tokens += usage.get("output_tokens", 0)
                        if ctx_input > 0:
                            self.last_input_tokens = ctx_input
                            if ctx_input > self.peak_input_tokens:
                                self.peak_input_tokens = ctx_input
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
                        }, self.slot_id)

                    # Broadcast event to all subscribers (multi-browser support).
                    # With --include-partial-messages, the CLI emits partial
                    # `assistant` snapshots on every delta — these are redundant
                    # with the `stream_event` deltas and very heavy (full message
                    # body each time).  Skip broadcasting them to keep SSE lean;
                    # stream_event deltas provide real-time text to the frontend.
                    # Only the *final* assistant event (after all streaming) is
                    # useful, but we detect tool_use from stream_event too, so
                    # we can safely skip all assistant broadcasts.
                    #
                    # During context ack drain, still broadcast result events
                    # so the ack drain subscriber queue receives them.  The
                    # external SSE consumer (_receive_response) hasn't subscribed
                    # yet at this point, so no spurious events leak out.
                    # Only suppress stream_event deltas during ack drain to
                    # avoid polluting any passive listeners with ack text.
                    if self._draining_ack and event_type == "stream_event":
                        pass  # Suppress ack text deltas
                    elif event_type != "assistant":
                        self._broadcast(event)

                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON: {line[:200]} ({e})")
                    # Detect auth errors in non-JSON stdout (CLI sometimes outputs
                    # plaintext error messages before exiting)
                    low = line.lower()
                    if any(kw in low for kw in ("authentication_error", "oauth token", "token has expired", "401", "failed to authenticate")):
                        logger.error(f"[stdout:auth] {line[:500]}")
                        self._last_auth_error = line[:500]
                        self._broadcast({"type": "error", "error": line[:500]})

        except Exception as e:
            logger.error(f"Read loop error: {e}")
        finally:
            logger.warning("Read loop terminated -- process will auto-restart on next message")

            # Give stderr reader a moment to finish capturing output
            await asyncio.sleep(0.5)
            if self.proc and self.proc.returncode is None:
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

            # Include auth error in the terminal event so the user sees a clear message
            auth_err = self._last_auth_error or self._auth_error_recovering
            self._auth_error_recovering = False
            if auth_err:
                # Attempt auto-recovery before surfacing the error
                now = time.time()
                if now - self._auth_recovery_window_start > 600:
                    self._auth_recovery_attempts = 0
                    self._auth_recovery_window_start = now
                recovered = False
                if self._auth_recovery_attempts < 2:
                    self._auth_recovery_attempts += 1
                    logger.info(f"Auth error detected — attempting token refresh (attempt {self._auth_recovery_attempts}/2, slot={self.slot_id})")
                    try:
                        ok, new_tok = await refresh_cli_oauth_token()
                        if ok:
                            logger.info(f"Auth recovery succeeded — process will restart with fresh token on next message (slot={self.slot_id})")
                            self._context_injected = False
                            self._last_auth_error = None
                            recovered = True
                    except Exception as e:
                        logger.warning(f"Auth recovery failed: {e}")
                if not recovered:
                    auth_msg = self._last_auth_error or "Authentication error (token expired)"
                    self._broadcast({"type": "result", "result": f"⚠️ Authentication failed: {auth_msg}", "subtype": "auth_error"})
                self._last_auth_error = None
            else:
                rc = self.proc.returncode if self.proc else None
                stderr_tail = "\n".join(self._recent_stderr[-5:]) if self._recent_stderr else ""
                if stderr_tail:
                    die_msg = f"⚠️ Claude process exited (rc={rc}):\n{stderr_tail}"
                elif rc is None:
                    die_msg = "⚠️ Claude process exited unexpectedly. Check that Claude CLI is authenticated — run `claude` in terminal to log in, or add an API key in Settings."
                else:
                    die_msg = f"⚠️ Claude process exited (rc={rc}). Check server logs for details."
                logger.error(f"Claude process died: rc={rc}, stderr={stderr_tail!r}")
                self._broadcast({"type": "result", "result": die_msg, "subtype": "process_died"})
            if self.proc:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                self.proc = None

    async def _stderr_reader(self):
        """Background task: read stderr from the claude process."""
        _AUTH_ERROR_KEYWORDS = ("authentication_error", "oauth token", "token has expired", "401", "failed to authenticate")
        try:
            while self.proc and self.proc.returncode is None:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode().strip()
                if not text:
                    continue
                # Keep last 10 stderr lines for diagnostics on crash
                self._recent_stderr.append(text)
                if len(self._recent_stderr) > 10:
                    self._recent_stderr.pop(0)
                low = text.lower()
                # Detect auth failures — flag for recovery in _read_loop finally
                if any(kw in low for kw in _AUTH_ERROR_KEYWORDS):
                    logger.error(f"[stderr:auth] {text}")
                    self._last_auth_error = text
                    self._auth_error_recovering = True
                    self._broadcast({"type": "error", "error": text})
                elif "error" in low or "failed" in low:
                    logger.warning(f"[stderr] {text}")
                else:
                    logger.debug(f"[stderr] {text}")
        except Exception as e:
            logger.debug(f"Stderr reader ended: {e}")

    # --- Message Send/Receive ---

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
            logger.info(f"Message sent (count={self.message_count})")

    async def _receive_response(self) -> AsyncIterator[dict]:
        """Receive all events for the current response via subscriber queue.

        The silence watchdog checks TWO signals before giving up:
        1. No events in the subscriber queue (broadcast events)
        2. No activity on stdout at all (including non-broadcast assistant events
           that arrive during tool execution in multi-tool turns)

        This prevents false timeouts when the CLI is actively executing tools
        (Bash, Read, Grep, etc.) which produce assistant events on stdout but
        those events are not broadcast to subscribers.
        """
        response_queue = self.subscribe()
        t0 = time.time()
        last_event_time = time.time()

        try:
            while True:
                try:
                    event = await asyncio.wait_for(response_queue.get(), timeout=15)
                    last_event_time = time.time()
                except asyncio.TimeoutError:
                    queue_silence = int(time.time() - last_event_time)
                    stdout_silence = int(time.time() - self._last_stdout_activity)

                    # Only give up if BOTH the subscriber queue AND raw stdout
                    # have been silent for the timeout period.  If stdout is
                    # still active (tool execution producing assistant events),
                    # keep waiting — the model is working, just not streaming text.
                    if queue_silence > RESPONSE_SILENCE_TIMEOUT_S and stdout_silence > RESPONSE_SILENCE_TIMEOUT_S:
                        logger.error(
                            f"No events for {queue_silence}s (stdout silent {stdout_silence}s) -- giving up"
                        )
                        yield {"type": "error", "error": f"No response for {queue_silence}s"}
                        break

                    elapsed = int(time.time() - t0)
                    if stdout_silence < queue_silence:
                        # stdout is active but queue is dry → tools are running
                        yield {"type": "ping", "elapsed": elapsed, "tool": "working (tools active)"}
                    else:
                        yield {"type": "ping", "elapsed": elapsed, "tool": "working"}
                    continue

                yield event

                if event.get("type") == "result":
                    break
        finally:
            self.unsubscribe(response_queue)

    async def send_message(self, text: str, inject_context: bool = True, suppress_user_broadcast: bool = False) -> AsyncIterator[dict]:
        """Send a message and yield streaming response events.

        Serialized via chat_lock -- only one message can be in-flight at a time.
        Context is injected on first message if session was not resumed.
        The ack response is drained silently before sending the user's actual message.
        """
        await self.chat_lock.acquire()
        try:
            self.last_activity = time.time()
            await self.ensure_running()

            # --- Context Injection on First Message (Wake/Fresh Start) ---
            # When a subprocess is freshly spawned (not resumed), _context_injected is False.
            # Before sending the user's actual message, we inject the full identity context:
            #   SOUL.md, USER.md, TOOLS.md, model profile, worker identity, recent chat history.
            # This is what makes the model "wake up" with knowledge of who it is and what it does.
            #
            # Flow:
            #   1. Build system prompt from identity files
            #   2. Send it as the first message to the Claude subprocess
            #   3. Mark _context_injected = True (prevents re-injection on subsequent messages)
            #   4. Broadcast "context_loaded" SSE event to browser (shows system card in UI)
            #   5. Drain the model's ack response ("Context loaded.") silently
            #   6. Then proceed to send the user's actual message
            #
            # This also applies to proactive wakes: when _try_proactive_wake() fires
            # _process_chat_claude() as an internal run, inject_context=True ensures the
            # model gets its identity before processing the delegation notification.
            if inject_context and not self._context_injected:
                # Send ping so client knows we're working
                yield {"type": "ping", "elapsed": 0, "tool": "loading context"}

                # FRESH START: inject SOUL, USER, TOOLS, Model Identity, Worker Identity
                context, loaded_files = build_system_prompt(worker_identity=self.worker_identity, kukuibot_session_id=self.kukuibot_session_id, model=self.model)
                await self._send_raw_message(
                    f"[System Context - read carefully but do not repeat]\n{context}\n[End System Context]\n\n"
                    f"Acknowledge with 'Context loaded.' and wait for the first real message.")
                self._context_injected = True
                logger.info(f"Fresh context injected (worker={self.worker_identity or 'none'})")

                # Broadcast context_loaded event so frontend shows system card
                self._broadcast({"type": "context_loaded", "loaded_files": loaded_files})
                yield {"type": "ping", "elapsed": 0, "tool": "context loaded"}

                # Drain the context ack response before sending user message.
                # The CLI responds to the context injection first (e.g. "Context loaded.")
                # and emits a result event. We must consume that before listening for
                # the real user response, otherwise _receive_response() stops at the
                # ack's result event and returns the wrong text.
                # Set _draining_ack to suppress broadcasting these events to SSE.
                self._draining_ack = True
                ack_queue = self.subscribe()
                try:
                    while True:
                        try:
                            ack_evt = await asyncio.wait_for(ack_queue.get(), timeout=60)
                            if ack_evt.get("type") == "result":
                                ack_subtype = ack_evt.get("subtype", "")
                                if ack_subtype in ("process_died", "auth_error"):
                                    err_msg = ack_evt.get("result", "") or f"Claude process failed during startup ({ack_subtype})"
                                    logger.error(f"Process died during context injection: {err_msg}")
                                    raise RuntimeError(err_msg)
                                logger.info("Context ack result consumed (not forwarded to client)")
                                break
                        except asyncio.TimeoutError:
                            logger.warning("Timeout waiting for context ack -- proceeding anyway")
                            break
                finally:
                    self.unsubscribe(ack_queue)
                    self._draining_ack = False

            # --- Notification Drain & Prepend ---
            # Delegation notifications (task completed, running, failed, etc.) accumulate
            # in _pending_notifications while the model is busy or between user messages.
            # Here we drain them all and prepend to the user's message text so the model
            # sees them before the user's actual content.
            #
            # This is the primary delivery mechanism for queued notifications. The proactive
            # wake system (server.py) drains notifications itself before firing an internal
            # run, so this path handles the non-wake case (user sends a message while
            # notifications are queued).
            #
            # After prepending, we broadcast a "notification_delivered" event so the frontend
            # can display a toast confirming delivery.
            notifications = self.drain_notifications()
            if notifications:
                notification_block = "\n\n".join(notifications)
                text = f"{notification_block}\n\n{_DELEGATION_PREPEND_BOUNDARY}\n\n{text}"
                logger.info(f"Prepended {len(notifications)} notification(s) to user message (slot={self.slot_id})")
                # Broadcast notification_delivered event so frontend can show toast
                self._broadcast({"type": "notification_delivered", "count": len(notifications),
                                 "previews": [n[:100] for n in notifications]})

            # Broadcast user message to passive browsers before sending.
            # Suppressed for internal messages (e.g. delegation notifications injected
            # via proactive delivery) — the delegation_notification SSE event already
            # rendered the system card in the UI.
            if not suppress_user_broadcast:
                user_evt = {"type": "user_message", "text": text, "ts": int(time.time() * 1000)}
                self._broadcast(user_evt)

            # Send the actual user message
            await self._send_raw_message(text)

            # Reset response buffer and iteration counter
            self._last_response_text = ""
            self._last_response_done = False
            self._turn_iterations = 0
            self._turn_user_events = 0
            self._response_events = []
            self._current_tool = None

            # Yield all response events and buffer them
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

                yield event

            self._last_response_done = True
            self._current_tool = None

            # If no result event was received (process hung/timed out),
            # broadcast a synthetic result so EventSource subscribers unblock
            has_result = any(e.get("type") == "result" for e in self._response_events)
            if not has_result:
                logger.warning(f"send_message ended without result event — broadcasting synthetic result (slot={self.slot_id})")
                self._broadcast({"type": "result", "result": self._last_response_text, "subtype": "timeout"})

            # Record exchange for compaction history and persistent chat log.
            # Internal messages (proactive wake, system notifications) are logged
            # with role="system" so they render as system cards, not user bubbles.
            if self._last_response_text:
                self._record_exchange(text, self._last_response_text, is_internal=suppress_user_broadcast)
        finally:
            self.chat_lock.release()

    # --- Management ---

    async def restart(self):
        """Restart the claude process."""
        async with self.lock:
            if self.proc:
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except Exception:
                    pass
                self.proc = None

            for t in [self._reader_task, self._stderr_task]:
                if t:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            self._reader_task = None
            self._stderr_task = None

        await self._spawn(force_fresh=True)

    def set_auth_strategy(self, strategy: str):
        strategy = (strategy or "configured").strip().lower()
        if strategy not in {"configured", "local"}:
            strategy = "configured"
        self._auth_strategy = strategy

    def get_auth_strategy(self) -> str:
        return self._auth_strategy or "configured"

    def get_status(self) -> dict:
        """Get current process status."""
        # Eagerly load session state if not yet loaded (for accurate status before first message)
        if not self._session_loaded:
            state = _load_session_state(self.slot_id)
            self.session_id = state.get("session_id")
            self.last_input_tokens = state.get("last_input_tokens", 0)
            self.peak_input_tokens = state.get("peak_input_tokens", 0)
            self.total_input_tokens = state.get("total_input_tokens", 0)
            self.total_output_tokens = state.get("total_output_tokens", 0)
            self.message_count = state.get("message_count", 0)
            self._session_loaded = True

        compaction_info = {
            "compaction_threshold": COMPACTION_THRESHOLD,
            "context_window": CONTEXT_WINDOW,
            "compaction_count": self._compaction_state.get("compaction_count", 0),
            "last_compaction_at": self._compaction_state.get("last_compaction_at"),
            "has_summary": self._compaction_state.get("last_summary") is not None,
            "history_length": len(self._compaction_state.get("history", [])),
            "compacting": self._compacting,
        }

        # Resume eligibility for status display
        state = _load_session_state(self.slot_id)
        last_active = state.get("last_active_at", 0)
        session_age = (time.time() - last_active) if last_active else None

        base = {
            "slot_id": self.slot_id,
            "message_count": self.message_count,
            "session_id": self.session_id,
            "resume_status": self.resume_status,
            "resume_timeout_secs": RESUME_TIMEOUT_SECS,
            "resume_eligible": session_age is not None and session_age < RESUME_TIMEOUT_SECS,
            "auth_strategy": self.get_auth_strategy(),
            "last_input_tokens": self.last_input_tokens,
            "peak_input_tokens": self.peak_input_tokens,
            "cumulative_input_tokens": self.total_input_tokens,
            "cumulative_output_tokens": self.total_output_tokens,
            "compaction": compaction_info,
            "active_docs": sorted(self._active_docs),
            "last_activity": self.last_activity,
        }

        if not self.proc or self.proc.returncode is not None:
            return {**base, "running": False, "busy": False, "uptime_seconds": 0}

        uptime = time.time() - self.started_at if self.started_at else 0
        return {
            **base,
            "running": True,
            "busy": self.chat_lock.locked(),
            "pid": self.proc.pid,
            "uptime_seconds": int(uptime),
            "context_injected": self._context_injected,
        }

    async def kill(self):
        """Kill this process and clean up."""
        async with self.lock:
            if self.proc:
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except Exception:
                    pass
                self.proc = None
            for t in [self._reader_task, self._stderr_task]:
                if t:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            self._reader_task = None
            self._stderr_task = None
            # Notify subscribers that this process is gone
            self._broadcast({"type": "error", "error": "Process killed"})
            logger.info(f"Process killed (slot={self.slot_id})")


# --- Claude Process Pool ---

class ClaudeProcessPool:
    """Manages a pool of up to MAX_CLAUDE_PROCESSES PersistentClaudeProcess instances.

    Each Claude tab gets its own isolated process, keyed by the tab's session_id.
    Idle processes are reaped after IDLE_TIMEOUT_S seconds.
    """

    def __init__(self, api_key_fn=None, oauth_token_fn=None, auth_strategy: str = "configured"):
        self._api_key_fn = api_key_fn
        self._oauth_token_fn = oauth_token_fn
        self._auth_strategy = auth_strategy
        self._processes: dict[str, PersistentClaudeProcess] = {}
        self._reaper_task: Optional[asyncio.Task] = None

    def _make_slot_id(self, session_id: str) -> str:
        """Derive a filesystem-safe slot ID from a session ID."""
        # session_id is like "tab-claude_opus-xxxx" — use as-is but sanitize
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        return safe[:80]  # cap length for sanity

    def get(self, session_id: str) -> Optional[PersistentClaudeProcess]:
        """Get existing process for a session, or None."""
        return self._processes.get(session_id)

    def get_or_create(self, session_id: str, worker_identity: str = "", model: str = "opus") -> PersistentClaudeProcess:
        """Get or create a process for a Claude tab session.

        Raises RuntimeError if pool is full (MAX_CLAUDE_PROCESSES reached).
        """
        proc = self._processes.get(session_id)
        if proc:
            # Update worker identity if changed
            if worker_identity and proc.worker_identity != worker_identity:
                proc.worker_identity = worker_identity
            return proc

        if len(self._processes) >= MAX_CLAUDE_PROCESSES:
            # Try to evict the oldest idle process
            evicted = self._evict_oldest_idle()
            if not evicted:
                raise RuntimeError(
                    f"Claude process pool is full ({MAX_CLAUDE_PROCESSES} max). "
                    f"Close a Claude tab to free a slot."
                )

        slot_id = self._make_slot_id(session_id)
        proc = PersistentClaudeProcess(
            slot_id=slot_id,
            api_key_fn=self._api_key_fn,
            oauth_token_fn=self._oauth_token_fn,
            worker_identity=worker_identity,
            model=model,
            kukuibot_session_id=session_id,
        )
        proc.set_auth_strategy(self._auth_strategy)
        self._processes[session_id] = proc
        logger.info(f"Pool: created process for {session_id} (slot={slot_id}, worker={worker_identity}, pool_size={len(self._processes)})")
        return proc

    def _evict_oldest_idle(self) -> bool:
        """Evict the oldest idle (not loading) process. Returns True if evicted."""
        candidates = []
        for sid, proc in self._processes.items():
            # Don't evict processes that are actively loading
            if proc.chat_lock.locked():
                continue
            candidates.append((sid, proc.last_activity))

        if not candidates:
            return False

        # Sort by last_activity ascending (oldest first)
        candidates.sort(key=lambda x: x[1])
        oldest_sid = candidates[0][0]
        proc = self._processes.pop(oldest_sid)
        asyncio.create_task(proc.kill())
        logger.info(f"Pool: evicted idle process {oldest_sid} to make room")
        return True

    async def kill_session(self, session_id: str):
        """Kill a specific session's process."""
        proc = self._processes.pop(session_id, None)
        if proc:
            await proc.kill()
            logger.info(f"Pool: killed session {session_id} (pool_size={len(self._processes)})")

    async def kill_all(self):
        """Kill all processes in the pool."""
        for sid in list(self._processes.keys()):
            await self.kill_session(sid)

    def get_all_status(self) -> dict:
        """Get status of all processes in the pool."""
        return {
            "pool_size": len(self._processes),
            "max_size": MAX_CLAUDE_PROCESSES,
            "processes": {sid: proc.get_status() for sid, proc in self._processes.items()},
        }

    def queue_notification(self, session_id: str, message: str) -> bool:
        """Queue a notification for a Claude session. Returns True if queued."""
        proc = self._processes.get(session_id)
        if not proc:
            return False
        proc.queue_notification(message)
        return True

    def is_session_busy(self, session_id: str) -> bool:
        """Check if a session process has chat_lock held."""
        proc = self._processes.get(session_id)
        if not proc:
            return False
        return proc.is_busy

    def start_reaper(self):
        """Start the background idle reaper task."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._idle_reaper())

    async def _idle_reaper(self):
        """Background task that kills processes idle for > IDLE_TIMEOUT_S."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                now = time.time()
                to_kill = []
                for sid, proc in list(self._processes.items()):
                    # Never reap coordinator sessions (dev-manager) — they must
                    # stay alive to receive delegation completion notifications.
                    if getattr(proc, 'worker_identity', None) == 'dev-manager':
                        continue
                    idle_s = now - proc.last_activity
                    timeout = DELEG_IDLE_TIMEOUT_S if sid.startswith("deleg-") else IDLE_TIMEOUT_S
                    if idle_s > timeout and not proc.chat_lock.locked():
                        to_kill.append(sid)

                for sid in to_kill:
                    proc = self._processes.get(sid)
                    if proc and proc._pending_notifications:
                        logger.warning(
                            f"Pool reaper: killing idle process {sid} with "
                            f"{len(proc._pending_notifications)} in-memory notification(s) — "
                            f"DB inbox (notification_store) is authoritative; in-memory loss is acceptable"
                        )
                    logger.info(f"Pool reaper: killing idle process {sid} "
                                f"(idle {int(time.time() - self._processes[sid].last_activity)}s)")
                    await self.kill_session(sid)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Pool reaper error: {e}")


# Global process pool -- created in server.py startup
_claude_pool: Optional[ClaudeProcessPool] = None


def get_claude_pool() -> Optional[ClaudeProcessPool]:
    """Get the global Claude process pool."""
    return _claude_pool


def init_claude_pool(api_key_fn=None, oauth_token_fn=None, auth_strategy: str = "configured") -> ClaudeProcessPool:
    """Create and return the global process pool."""
    global _claude_pool
    _claude_pool = ClaudeProcessPool(
        api_key_fn=api_key_fn,
        oauth_token_fn=oauth_token_fn,
        auth_strategy=auth_strategy,
    )
    return _claude_pool


# Backwards-compatible aliases for any code that still calls the old API
def get_persistent_claude() -> Optional[PersistentClaudeProcess]:
    """DEPRECATED: Use get_claude_pool() instead."""
    pool = get_claude_pool()
    if not pool:
        return None
    # Return the first process in the pool, or None
    procs = list(pool._processes.values())
    return procs[0] if procs else None


def init_persistent_claude(api_key_fn=None, oauth_token_fn=None) -> ClaudeProcessPool:
    """DEPRECATED: Use init_claude_pool() instead. Returns the pool."""
    return init_claude_pool(api_key_fn=api_key_fn, oauth_token_fn=oauth_token_fn)
