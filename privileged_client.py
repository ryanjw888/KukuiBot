"""Client for KukuiBot privileged helper (Unix socket JSON protocol)."""

from __future__ import annotations

import json
import socket
from typing import Any


class PrivilegedHelperError(Exception):
    pass


class PrivilegedHelperClient:
    def __init__(self, socket_path: str, timeout: float = 20.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def _request(self, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        import platform
        if platform.system() == "Windows":
            raise OSError("Privileged helper not available on Windows")
        t = float(timeout if timeout is not None else self.timeout)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(t)
                s.connect(self.socket_path)
                data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                s.sendall(data)

                chunks: list[bytes] = []
                while True:
                    b = s.recv(4096)
                    if not b:
                        break
                    chunks.append(b)
                    if b"\n" in b:
                        break
        except FileNotFoundError as e:
            raise PrivilegedHelperError(f"helper socket not found: {self.socket_path}") from e
        except ConnectionRefusedError as e:
            raise PrivilegedHelperError("helper refused connection") from e
        except socket.timeout as e:
            raise PrivilegedHelperError("helper request timed out") from e
        except Exception as e:
            raise PrivilegedHelperError(str(e)) from e

        raw = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        if not raw:
            raise PrivilegedHelperError("empty helper response")
        try:
            obj = json.loads(raw)
        except Exception as e:
            raise PrivilegedHelperError(f"invalid helper response: {raw[:200]}") from e
        if not isinstance(obj, dict):
            raise PrivilegedHelperError("unexpected helper response shape")
        return obj

    def status(self, session_id: str = "default") -> dict[str, Any]:
        return self._request({"op": "status", "session_id": session_id}, timeout=4)

    def elevate(self, session_id: str = "default", ttl_seconds: int = 600) -> dict[str, Any]:
        return self._request({"op": "elevate", "session_id": session_id, "ttl_seconds": int(ttl_seconds)}, timeout=60)

    def revoke(self, session_id: str = "default") -> dict[str, Any]:
        return self._request({"op": "revoke", "session_id": session_id}, timeout=8)

    def run(self, session_id: str, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request({"op": "run", "session_id": session_id, "action": action, "args": args or {}}, timeout=45)
