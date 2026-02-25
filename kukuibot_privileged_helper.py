#!/usr/bin/env python3
"""KukuiBot privileged helper daemon.

Run as root (launchd). Exposes a tiny allowlisted RPC API over a Unix socket.
It can elevate a short-lived per-session capability by prompting for admin auth via
AppleScript `display dialog` with hidden answer. Password is never sent to model/chat.
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
import time
from pathlib import Path

SOCKET_PATH = os.environ.get("KUKUIBOT_PRIV_SOCKET", "/tmp/kukuibot-priv.sock")
LOG_PATH = os.environ.get("KUKUIBOT_PRIV_LOG", "/tmp/kukuibot-privileged.log")
DEFAULT_TTL = int(os.environ.get("KUKUIBOT_PRIV_DEFAULT_TTL", "600"))
MAX_TTL = int(os.environ.get("KUKUIBOT_PRIV_MAX_TTL", "1800"))

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

    def _is_elevated(self, session_id: str) -> int:
        now = time.time()
        until = self.elevated_until.get(session_id, 0)
        if until <= now:
            if session_id in self.elevated_until:
                self.elevated_until.pop(session_id, None)
            return 0
        return int(until - now)

    def _status(self, session_id: str) -> dict:
        rem = self._is_elevated(session_id)
        return {"ok": True, "elevated": rem > 0, "remaining_seconds": rem}

    def _elevate_with_prompt(self, session_id: str, ttl_seconds: int) -> dict:
        # Prompt locally (GUI session) for password. Validate with sudo -S -k -v.
        ttl = max(60, min(int(ttl_seconds or DEFAULT_TTL), MAX_TTL))

        try:
            uid = os.stat('/dev/console').st_uid
            user = pwd.getpwuid(uid).pw_name
        except Exception:
            uid = int(os.environ.get("SUDO_UID") or "501")
            user = pwd.getpwuid(uid).pw_name if str(uid).isdigit() else os.getlogin()

        osa_script = f'''
set promptText to "KukuiBot needs admin approval for privileged actions.\\nPassword is validated locally and never sent to chat/model."
set d to display dialog promptText default answer "" with hidden answer buttons {{"Cancel", "Approve"}} default button "Approve" with icon caution
set pw to text returned of d
if pw is "" then error number -128
return pw
'''.strip()

        # Run AppleScript directly in loginwindow user context.
        rc, out, err = _run([
            "/bin/launchctl", "asuser", str(uid),
            "/usr/bin/osascript", "-e", osa_script,
        ], timeout=120)
        if rc != 0:
            _append_audit({"event": "elevate_denied", "session_id": session_id, "detail": err or out or "cancelled", "uid": uid, "user": user})
            return {"ok": False, "error": "Authentication cancelled or unavailable"}

        pw = out
        # Validate password using sudo for target user (same GUI user context).
        cmd = f"printf %s {shlex.quote(pw)} | /usr/bin/sudo -S -k -p '' -v"
        rc2, out2, err2 = _run([
            "/bin/launchctl", "asuser", str(uid),
            "/usr/bin/sudo", "-u", user, "bash", "-lc", cmd
        ], timeout=30)
        pw = ""  # best effort clear
        if rc2 != 0:
            _append_audit({"event": "elevate_failed", "session_id": session_id, "detail": (err2 or out2 or "bad password")[:200], "uid": uid, "user": user})
            return {"ok": False, "error": "Authentication failed"}

        self.elevated_until[session_id] = time.time() + ttl
        _append_audit({"event": "elevate_ok", "session_id": session_id, "ttl": ttl, "uid": uid, "user": user})
        return {"ok": True, "elevated": True, "remaining_seconds": ttl}

    def _revoke(self, session_id: str) -> dict:
        self.elevated_until.pop(session_id, None)
        _append_audit({"event": "revoke", "session_id": session_id})
        return {"ok": True, "elevated": False, "remaining_seconds": 0}

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
            return self._elevate_with_prompt(sid, int(req.get("ttl_seconds") or DEFAULT_TTL))
        if op == "revoke":
            return self._revoke(sid)
        if op == "run":
            return self._run_action(sid, str(req.get("action") or ""), req.get("args") or {})
        return {"ok": False, "error": "Unknown op"}


def serve_forever():
    helper = Helper()

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
