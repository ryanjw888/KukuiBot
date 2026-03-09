#!/usr/bin/env python3
"""KukuiBot privileged helper daemon.

Run as root (launchd). Exposes a tiny allowlisted RPC API over a Unix socket.
Elevation writes a temporary sudoers drop-in file granting NOPASSWD to the
requesting user for the TTL duration. A background reaper thread auto-removes
expired sudoers files as a safety net.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import shlex
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

SOCKET_PATH = os.environ.get("KUKUIBOT_PRIV_SOCKET", "/tmp/kukuibot-priv.sock")
LOG_PATH = os.environ.get("KUKUIBOT_PRIV_LOG", "/tmp/kukuibot-privileged.log")
DEFAULT_TTL = int(os.environ.get("KUKUIBOT_PRIV_DEFAULT_TTL", "1800"))
MAX_TTL = int(os.environ.get("KUKUIBOT_PRIV_MAX_TTL", "3600"))
SUDOERS_DIR = Path("/etc/sudoers.d")
SUDOERS_PREFIX = "kukuibot-root-"

logger = logging.getLogger("kukuibot.privhelper")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


def _append_audit(entry: dict):
    try:
        line = json.dumps({**entry, "ts": int(time.time())}, ensure_ascii=False)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _quote(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"')


class Helper:
    def __init__(self):
        self.elevated_until: dict[str, float] = {}
        self.elevated_users: dict[str, str] = {}

    def _is_elevated(self, session_id: str) -> int:
        now = time.time()
        until = self.elevated_until.get(session_id, 0)
        if until <= now:
            if session_id in self.elevated_until:
                self.elevated_until.pop(session_id, None)
                self.elevated_users.pop(session_id, None)
                self._remove_sudoers(session_id)
            return 0
        return int(until - now)

    def _status(self, session_id: str) -> dict:
        rem = self._is_elevated(session_id)
        return {"ok": True, "elevated": rem > 0, "remaining_seconds": rem}

    def _get_console_user(self) -> tuple[int, str]:
        """Detect the currently logged-in console user (works regardless of who launched the helper)."""
        try:
            uid = os.stat('/dev/console').st_uid
            return uid, pwd.getpwuid(uid).pw_name
        except Exception:
            uid = int(os.environ.get("SUDO_UID") or "501")
            return uid, pwd.getpwuid(uid).pw_name

    def _sudoers_path(self, session_id: str) -> Path:
        # Sanitize session_id for filesystem use
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        return SUDOERS_DIR / f"{SUDOERS_PREFIX}{safe}"

    def _write_sudoers(self, user: str, session_id: str) -> None:
        """Write a temporary NOPASSWD sudoers rule for the given user."""
        path = self._sudoers_path(session_id)
        content = f"# KukuiBot temporary root elevation — auto-expires\n{user} ALL=(ALL) NOPASSWD: ALL\n"
        path.write_text(content)
        os.chmod(str(path), 0o440)
        # Validate with visudo — if invalid, remove immediately
        rc, _, err = _run(["/usr/sbin/visudo", "-cf", str(path)], timeout=5)
        if rc != 0:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"sudoers validation failed: {err}")

    def _remove_sudoers(self, session_id: str) -> None:
        """Remove the temporary sudoers file for a session."""
        path = self._sudoers_path(session_id)
        path.unlink(missing_ok=True)

    def _elevate(self, session_id: str, ttl_seconds: int) -> dict:
        ttl = max(60, min(int(ttl_seconds or DEFAULT_TTL), MAX_TTL))
        uid, user = self._get_console_user()

        try:
            self._write_sudoers(user, session_id)
        except Exception as e:
            _append_audit({"event": "elevate_failed", "session_id": session_id, "detail": str(e), "uid": uid, "user": user})
            return {"ok": False, "error": f"Failed to write sudoers rule: {e}"}

        self.elevated_until[session_id] = time.time() + ttl
        self.elevated_users[session_id] = user
        _append_audit({"event": "elevate_ok", "session_id": session_id, "ttl": ttl, "uid": uid, "user": user})
        return {"ok": True, "elevated": True, "remaining_seconds": ttl}

    def _revoke(self, session_id: str) -> dict:
        self.elevated_until.pop(session_id, None)
        self.elevated_users.pop(session_id, None)
        self._remove_sudoers(session_id)
        _append_audit({"event": "revoke", "session_id": session_id})
        return {"ok": True, "elevated": False, "remaining_seconds": 0}

    def _reap_expired(self) -> None:
        """Remove sudoers files for expired sessions. Called periodically by the reaper thread."""
        now = time.time()
        expired = [sid for sid, until in list(self.elevated_until.items()) if until <= now]
        for sid in expired:
            self.elevated_until.pop(sid, None)
            self.elevated_users.pop(sid, None)
            self._remove_sudoers(sid)
            _append_audit({"event": "expired", "session_id": sid})

    def _run_action(self, session_id: str, action: str, args: dict) -> dict:
        rem = self._is_elevated(session_id)
        if rem <= 0:
            return {"ok": False, "error": "Not elevated", "needs_auth": True}

        if action == "spotlight.disable":
            path = str(args.get("path") or "").strip()
            if not path.startswith("/Volumes/"):
                return {"ok": False, "error": "Invalid volume path"}
            rc, out, err = _run(["/usr/bin/mdutil", "-i", "off", path], timeout=25)
        elif action == "spotlight.erase":
            path = str(args.get("path") or "").strip()
            if not path.startswith("/Volumes/"):
                return {"ok": False, "error": "Invalid volume path"}
            rc, out, err = _run(["/usr/bin/mdutil", "-E", path], timeout=45)
        elif action == "spotlight.status":
            path = str(args.get("path") or "").strip()
            if not path.startswith("/Volumes/"):
                return {"ok": False, "error": "Invalid volume path"}
            rc, out, err = _run(["/usr/bin/mdutil", "-s", path], timeout=10)
        else:
            return {"ok": False, "error": "Unknown action"}

        ok = (rc == 0)
        _append_audit({"event": "run", "session_id": session_id, "action": action, "ok": ok, "rc": rc})
        return {
            "ok": ok,
            "action": action,
            "exit_code": rc,
            "stdout": out,
            "stderr": err,
            "remaining_seconds": self._is_elevated(session_id),
        }

    def handle(self, req: dict) -> dict:
        op = (req.get("op") or "").strip().lower()
        sid = str(req.get("session_id") or "default").strip() or "default"
        if op == "status":
            return self._status(sid)
        if op == "elevate":
            return self._elevate(sid, int(req.get("ttl_seconds") or DEFAULT_TTL))
        if op == "revoke":
            return self._revoke(sid)
        if op == "run":
            return self._run_action(sid, str(req.get("action") or ""), req.get("args") or {})
        return {"ok": False, "error": "Unknown op"}


def _reaper_loop(helper: Helper):
    """Background thread that removes expired sudoers files every 10 seconds."""
    while True:
        try:
            helper._reap_expired()
        except Exception as e:
            logger.warning(f"reaper error: {e}")
        time.sleep(10)


def _cleanup_stale_sudoers():
    """Remove any leftover kukuibot sudoers files from a previous run/crash."""
    try:
        for f in SUDOERS_DIR.glob(f"{SUDOERS_PREFIX}*"):
            f.unlink(missing_ok=True)
            logger.info(f"cleaned up stale sudoers file: {f}")
    except Exception as e:
        logger.warning(f"stale sudoers cleanup error: {e}")


def serve_forever():
    import platform as _platform
    if _platform.system() == "Windows":
        print("Privileged helper not supported on Windows")
        import sys
        sys.exit(1)
    _cleanup_stale_sudoers()
    helper = Helper()

    # Start background reaper thread for expired sudoers files
    reaper = threading.Thread(target=_reaper_loop, args=(helper,), daemon=True)
    reaper.start()

    p = Path(SOCKET_PATH)
    try:
        if p.exists() or p.is_socket():
            p.unlink()
    except Exception:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)
    srv.listen(16)
    logger.info(f"privileged helper listening on {SOCKET_PATH}")

    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                data = b""
                while b"\n" not in data and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                line = data.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
                req = json.loads(line) if line else {}
                if not isinstance(req, dict):
                    raise ValueError("bad request")
                resp = helper.handle(req)
            except Exception as e:
                resp = {"ok": False, "error": f"helper error: {e}"}
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    serve_forever()
