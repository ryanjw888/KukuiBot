"""
oauth.py — OpenAI ChatGPT PKCE OAuth flow for KukuiBot.

Uses the same public client_id as OpenAI's official Codex CLI
(https://github.com/openai/codex — Apache 2.0).

Flow:
  1. Generate PKCE code_verifier + code_challenge
  2. Build authorize URL -> open in browser
  3. Spin up localhost:1455 callback server
  4. User logs into OpenAI in browser
  5. Callback receives auth code
  6. Exchange code for access_token + refresh_token
  7. Store tokens in DB
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Event
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen

logger = logging.getLogger("kukuibot.oauth")

# --- Constants (from OpenAI's official Codex CLI, Apache 2.0) ---
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CALLBACK_PORT = 1455
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/auth/callback"
SCOPE = "openid profile email offline_access"

SUCCESS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>KukuiBot</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #e5e5e5;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { text-align: center; padding: 40px; border-radius: 16px; background: #1a1a1a; }
  h1 { color: #10b981; margin: 0 0 12px; }
  p { color: #a3a3a3; margin: 0; }
  a { color: #10b981; }
</style>
<script>
  // Redirect back to the admin page on whatever host the user is using
  setTimeout(function() { window.close(); }, 2000);
</script>
</head>
<body><div class="card"><h1>✓ Connected</h1><p>You can close this tab.</p></div></body>
</html>"""

ERROR_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>KukuiBot</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #e5e5e5;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { text-align: center; padding: 40px; border-radius: 16px; background: #1a1a1a; }
  h1 { color: #ef4444; margin: 0 0 12px; }
  p { color: #a3a3a3; margin: 0; }
</style></head>
<body><div class="card"><h1>Error</h1><p>{error}</p></div></body>
</html>"""


# --- PKCE ---

def _generate_pkce():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    challenge_hash = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(challenge_hash).rstrip(b"=").decode("ascii")
    return verifier, challenge


# --- Authorization URL ---

def build_authorize_url():
    """Build the OpenAI authorize URL. Returns: (url, verifier, state)"""
    verifier, challenge = _generate_pkce()
    state = secrets.token_hex(16)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "pi",
    }
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    return url, verifier, state


# --- Token Exchange ---

def exchange_code(code, verifier):
    """Exchange authorization code for tokens."""
    body = urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = urlopen(req, timeout=30)
    data = json.loads(resp.read())
    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    expires_in = data.get("expires_in", 0)
    if not access_token or not refresh_token:
        raise ValueError("Token exchange failed: missing fields")
    account_id = _extract_account_id(access_token)
    if not account_id:
        raise ValueError("Failed to extract account ID from token")
    return {
        "access": access_token,
        "refresh": refresh_token,
        "expires": int(time.time() + expires_in) if expires_in else 0,
        "account_id": account_id,
    }


def refresh_token(refresh):
    """Refresh an access token."""
    body = urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLIENT_ID,
    }).encode()
    req = Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = urlopen(req, timeout=30)
    data = json.loads(resp.read())
    access_token = data.get("access_token", "")
    new_refresh = data.get("refresh_token", "")
    expires_in = data.get("expires_in", 0)
    if not access_token or not new_refresh:
        raise ValueError("Token refresh failed")
    account_id = _extract_account_id(access_token)
    return {
        "access": access_token,
        "refresh": new_refresh,
        "expires": int(time.time() + expires_in) if expires_in else 0,
        "account_id": account_id or "",
    }


# --- JWT Helper ---

def _extract_account_id(token):
    """Extract chatgpt_account_id from JWT access token."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        return payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
    except Exception:
        return ""


# --- Local Callback Server ---

class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        params = parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        error = (params.get("error") or [None])[0]
        if error:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(ERROR_HTML.format(error=error).encode())
            return
        if not code:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(ERROR_HTML.format(error="Missing authorization code").encode())
            return
        self.server.oauth_code = code
        self.server.oauth_state = state
        self.server.oauth_received.set()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(SUCCESS_HTML.encode())

    def log_message(self, format, *args):
        pass


class OAuthCallbackServer:
    def __init__(self):
        self.server = None
        self.thread = None
        self._stopped = False

    def start(self):
        try:
            self._stopped = False
            self.server = HTTPServer(("0.0.0.0", CALLBACK_PORT), _OAuthCallbackHandler)
            self.server.oauth_code = None
            self.server.oauth_state = None
            self.server.oauth_received = Event()
            self.server.timeout = 1
            self.thread = Thread(target=self._serve, daemon=True)
            self.thread.start()
            logger.info(f"OAuth callback server started on 0.0.0.0:{CALLBACK_PORT}")
            return True
        except OSError as e:
            logger.error(f"Failed to bind 0.0.0.0:{CALLBACK_PORT}: {e}")
            return False

    def _serve(self):
        while not self._stopped:
            try:
                self.server.handle_request()
            except Exception:
                break

    def wait_for_code(self, timeout=120.0):
        if not self.server:
            return None, None
        if self.server.oauth_received.wait(timeout=timeout):
            return self.server.oauth_code, self.server.oauth_state
        return None, None

    def stop(self):
        self._stopped = True
        if self.server:
            try:
                self.server.server_close()
            except Exception:
                pass
            self.server = None
        logger.info("OAuth callback server stopped")


# --- High-Level Flow (used by server.py) ---

_active_flow = None


def start_oauth_flow():
    """Start an OAuth flow. Returns {"url": str, "flow_id": str}."""
    global _active_flow
    if _active_flow and _active_flow.get("server"):
        _active_flow["server"].stop()
    url, verifier, state = build_authorize_url()
    server = OAuthCallbackServer()
    if not server.start():
        return {"error": f"Port {CALLBACK_PORT} is in use. Close any other OAuth flows and try again."}
    flow_id = secrets.token_hex(8)
    _active_flow = {
        "flow_id": flow_id,
        "url": url,
        "verifier": verifier,
        "state": state,
        "server": server,
        "started": time.time(),
        "status": "waiting",
        "result": None,
    }
    Thread(target=_wait_for_callback, daemon=True).start()
    return {"url": url, "flow_id": flow_id}


def _wait_for_callback():
    global _active_flow
    flow = _active_flow
    if not flow:
        return
    try:
        code, cb_state = flow["server"].wait_for_code(timeout=120)
        if not code:
            flow["status"] = "timeout"
            flow["result"] = {"error": "OAuth timed out"}
            return
        if cb_state and cb_state != flow["state"]:
            flow["status"] = "error"
            flow["result"] = {"error": "OAuth state mismatch"}
            return
        tokens = exchange_code(code, flow["verifier"])
        flow["status"] = "success"
        flow["result"] = tokens
        logger.info(f"OAuth flow complete - account: {tokens['account_id'][:8]}...")
    except Exception as e:
        flow["status"] = "error"
        flow["result"] = {"error": str(e)}
        logger.error(f"OAuth flow failed: {e}")
    finally:
        flow["server"].stop()


def get_oauth_status():
    """Check the status of the active OAuth flow."""
    if not _active_flow:
        return {"status": "none"}
    result = {
        "status": _active_flow["status"],
        "flow_id": _active_flow["flow_id"],
        "elapsed": round(time.time() - _active_flow["started"], 1),
    }
    if _active_flow["status"] == "success":
        result["account_id"] = _active_flow["result"]["account_id"][:8] + "..."
    elif _active_flow["status"] in ("error", "timeout"):
        result["error"] = _active_flow["result"].get("error", "Unknown error")
    return result


def complete_oauth_flow():
    """Get tokens from completed flow and clean up."""
    global _active_flow
    if not _active_flow or _active_flow["status"] != "success":
        return None
    result = _active_flow["result"]
    _active_flow = None
    return result


def exchange_pasted_code(code):
    """Exchange a manually pasted authorization code using the active flow's verifier.
    
    Used when the user is on LAN and the localhost:1455 callback can't reach their browser.
    They paste the failed redirect URL, we extract the code, and exchange it here.
    
    Returns: {"access": str, "refresh": str, "expires": int, "account_id": str}
    Raises on failure.
    """
    if not _active_flow:
        raise ValueError("No active OAuth flow — click 'Sign in with OpenAI' first")
    
    verifier = _active_flow["verifier"]
    tokens = exchange_code(code, verifier)
    
    # Mark flow as success
    _active_flow["status"] = "success"
    _active_flow["result"] = tokens
    
    logger.info(f"OAuth exchange via pasted code — account: {tokens['account_id'][:8]}...")
    return tokens


def cancel_oauth_flow():
    """Cancel any active OAuth flow."""
    global _active_flow
    if _active_flow:
        if _active_flow.get("server"):
            _active_flow["server"].stop()
        _active_flow = None
