"""
security.py — Elevation system, path guards, egress checks.
"""

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from config import SECURITY_POLICY_FILE, WORKSPACE

# --- Default Security Config ---
_DEFAULT_ELEVATE_BASH_PATTERNS = [
    "sudo ",
    "launchctl ",
    "rm -rf",
    " wget ",
    " scp ",
    " sftp ",
    " rsync ",
    " nc ",
    " ncat ",
    " netcat ",
    " ftp ",
    " python -c",
    " python3 -c",
]

_DEFAULT_EGRESS_REGEX = r"(^|[;&|()\\s])(wget|scp|sftp|rsync|nc|ncat|netcat|ftp)([\\s;&|()]|$)"

WORKSPACE_REAL = os.path.realpath(str(WORKSPACE))
_SECURITY_POLICY_BACKUP_FILE = SECURITY_POLICY_FILE.with_name('security-policy.backup.json')
_BUILTIN_ADMIN_BYPASS_SESSIONS: set[str] = set()


def _read_policy_file(path: Path) -> dict | None:
    try:
        if path.exists():
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                return raw
    except Exception:
        return None
    return None


def _load_security_policy() -> dict:
    policy = {
        "workspace_root": WORKSPACE_REAL,
        "allow_read_paths": [],
        "allow_write_paths": [],
        "elevate_write_paths": [],
        "elevate_self_modify": [],
        "admin_bypass_sessions": [],
        "elevate_bash_patterns": list(_DEFAULT_ELEVATE_BASH_PATTERNS),
        "egress_command_regex": _DEFAULT_EGRESS_REGEX,
        "blocked_write_files": [],
    }
    raw = _read_policy_file(SECURITY_POLICY_FILE)
    if raw is None:
        raw = _read_policy_file(_SECURITY_POLICY_BACKUP_FILE)
    if isinstance(raw, dict):
        policy.update({k: v for k, v in raw.items() if k in policy})
        # Keep a last-known-good backup to survive accidental corruption.
        try:
            _SECURITY_POLICY_BACKUP_FILE.write_text(json.dumps(raw, indent=2) + '\n')
        except Exception:
            pass

    ws_root = os.path.realpath(os.path.expanduser(str(policy["workspace_root"])))
    policy["workspace_root"] = ws_root

    def _norm(items):
        out = []
        for p in (items or []):
            if not p:
                continue
            p = str(p)
            expanded = os.path.expanduser(p)
            # Resolve relative paths against workspace root
            if not os.path.isabs(expanded):
                expanded = os.path.join(ws_root, expanded)
            out.append(os.path.realpath(expanded))
        return out

    policy["allow_read_paths"] = _norm(policy.get("allow_read_paths"))
    policy["allow_write_paths"] = _norm(policy.get("allow_write_paths"))
    policy["elevate_write_paths"] = _norm(policy.get("elevate_write_paths"))
    policy["elevate_self_modify"] = [str(x) for x in (policy.get("elevate_self_modify") or [])]
    policy["admin_bypass_sessions"] = [str(x) for x in (policy.get("admin_bypass_sessions") or [])]
    policy["blocked_write_files"] = [str(x) for x in (policy.get("blocked_write_files") or [])]
    return policy


_POLICY = _load_security_policy()
_ALLOW_READ = _POLICY["allow_read_paths"]
_ALLOW_WRITE = _POLICY["allow_write_paths"]
_ELEVATE_WRITE = _POLICY["elevate_write_paths"]
_ELEVATE_SELF_MODIFY = [str(x) for x in (_POLICY.get("elevate_self_modify") or [])]
_ADMIN_BYPASS_SESSIONS = set(str(x) for x in (_POLICY.get("admin_bypass_sessions") or [])) | set(_BUILTIN_ADMIN_BYPASS_SESSIONS)
_ELEVATE_BASH = _POLICY["elevate_bash_patterns"]
_EGRESS_REGEX = _POLICY["egress_command_regex"]


# --- Elevation State ---
_lock = threading.Lock()
_requests: dict[str, dict] = {}
_approved: dict[str, bool] = {}
_approve_all_sessions: set[str] = set()
_elevated_until: dict[str, float] = {}


def request_elevation(tool_name: str, input_data: dict, reason: str, session_id: str = "default") -> str:
    rid = uuid.uuid4().hex[:8]
    with _lock:
        _requests[rid] = {
            "tool_name": tool_name,
            "input_data": input_data,
            "reason": reason,
            "created": time.time(),
            "session_id": session_id,
        }
    return rid


def approve_elevation(request_id: str) -> bool:
    with _lock:
        if request_id in _requests:
            _approved[request_id] = True
            return True
    return False


def deny_elevation(request_id: str) -> bool:
    with _lock:
        _requests.pop(request_id, None)
        _approved.pop(request_id, None)
    return True


def consume_elevation(request_id: str) -> dict | None:
    with _lock:
        if _approved.pop(request_id, False):
            return _requests.pop(request_id, None)
    return None


def get_pending_elevations() -> list[dict]:
    with _lock:
        now = time.time()
        expired = [r for r, req in _requests.items() if now - req["created"] > 300 and r not in _approved]
        for r in expired:
            del _requests[r]
        return [
            {"request_id": r, **{k: v for k, v in req.items() if k != "created"}, "age_seconds": int(now - req["created"])}
            for r, req in _requests.items() if r not in _approved
        ]


def set_approve_all(session_id: str, enabled: bool):
    with _lock:
        if enabled:
            _approve_all_sessions.add(session_id)
        else:
            _approve_all_sessions.discard(session_id)


def is_approve_all(session_id: str) -> bool:
    with _lock:
        return session_id in _approve_all_sessions

def is_admin_bypass_session(session_id: str) -> bool:
    sid = str(session_id or "").strip()
    return bool(sid) and sid in _ADMIN_BYPASS_SESSIONS


def set_elevated_session(session_id: str, enabled: bool, ttl: int = 600) -> dict:
    ttl = max(60, min(ttl, 3600))
    now = time.time()
    with _lock:
        if enabled:
            until = now + ttl
            _elevated_until[session_id] = until
        else:
            _elevated_until.pop(session_id, None)
            until = 0

    remaining = max(0, int(until - now)) if enabled else 0
    return {"ok": True, "session_id": session_id, "enabled": enabled, "ttl_seconds": ttl if enabled else 0, "remaining_seconds": remaining}


def get_elevated_status(session_id: str) -> dict:
    now = time.time()
    with _lock:
        until = _elevated_until.get(session_id, 0)
        if until and until <= now:
            _elevated_until.pop(session_id, None)
            until = 0
    remaining = max(0, int(until - now)) if until else 0
    return {"ok": True, "session_id": session_id, "enabled": remaining > 0, "remaining_seconds": remaining}


def is_session_elevated(session_id: str) -> bool:
    now = time.time()
    with _lock:
        until = _elevated_until.get(session_id, 0)
        if until and until > now:
            return True
        if until and until <= now:
            _elevated_until.pop(session_id, None)
    return False


def clear_session_security(session_id: str):
    """Remove any per-session approval/elevation state and pending requests."""
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _lock:
        _approve_all_sessions.discard(sid)
        _elevated_until.pop(sid, None)

        # Drop pending/approved elevation requests bound to this session.
        to_remove = [rid for rid, req in _requests.items() if str(req.get("session_id") or "") == sid]
        for rid in to_remove:
            _requests.pop(rid, None)
            _approved.pop(rid, None)


# --- Path & Command Checks ---

def _is_within_workspace(path: str) -> bool:
    try:
        resolved = os.path.realpath(os.path.expanduser(path))
        return resolved == WORKSPACE_REAL or resolved.startswith(WORKSPACE_REAL + os.sep)
    except Exception:
        return False


def _in_allowlist(path: str, allow: list[str]) -> bool:
    for root in allow:
        if path == root or path.startswith(root + os.sep):
            return True
    return False


def check_path_access(path: str, *, for_write: bool, elevated: bool, session_id: str = "default") -> str | None:
    """Return reason string if blocked, None if OK."""
    resolved = os.path.realpath(os.path.expanduser(path))

    # Hard-block specific files from tool writes
    if for_write:
        for blocked in _POLICY.get("blocked_write_files", []):
            if resolved.endswith(blocked):
                return f"BLOCKED: {blocked} is protected and cannot be modified by tools."
    if not _is_within_workspace(resolved):
        allow = _ALLOW_WRITE if for_write else _ALLOW_READ
        if not _in_allowlist(resolved, allow):
            if elevated:
                return None
            mode = "write" if for_write else "read"
            return f"Path outside workspace requires approval ({mode}): {resolved}"

    if for_write and not elevated:
        if is_admin_bypass_session(session_id):
            return None
        for selfmod in _ELEVATE_SELF_MODIFY:
            if resolved.endswith(selfmod):
                return f"Modifying {selfmod} requires approval"
        for protected in _ELEVATE_WRITE:
            pr = os.path.realpath(os.path.expanduser(protected))
            if resolved == pr or resolved.startswith(pr + "/"):
                return f"Writing to {protected} requires approval"
    return None


def check_bash_command(command: str, session_id: str = "default") -> str | None:
    """Return reason string if command requires elevation, None if safe."""
    if is_admin_bypass_session(session_id):
        return None
    cmd = f" {command.strip()} "
    for pattern in _ELEVATE_BASH:
        if pattern in cmd:
            return f"Command contains '{pattern.strip()}' — requires approval"
    if _EGRESS_REGEX and re.search(_EGRESS_REGEX, command):
        return "Command invokes network transfer tool — requires approval"
    return None


def get_security_policy() -> dict:
    return {
        "workspace_root": WORKSPACE_REAL,
        "allow_read_paths": list(_ALLOW_READ),
        "allow_write_paths": list(_ALLOW_WRITE),
        "elevate_write_paths": list(_ELEVATE_WRITE),
        "elevate_self_modify": list(_ELEVATE_SELF_MODIFY),
        "elevate_bash_patterns": list(_ELEVATE_BASH),
        "egress_command_regex": _EGRESS_REGEX,
    }
