"""
Auth routes — login, logout, setup, OAuth, password management.
Extracted from server.py Phase 6.
"""

import hashlib
import logging
import os
import secrets
import subprocess
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from auth import (
    _sessions,
    complete_setup,
    create_user,
    db_connection,
    get_auth_status,
    get_request_user,
    is_localhost,
    login as auth_login,
    logout as auth_logout,
    save_api_key,
    save_oauth_result,
    save_token,
    extract_account_id,
    is_setup_complete,
    verify_password,
    SESSION_COOKIE,
    FORCE_LOGIN_COOKIE,
    SESSION_MAX_AGE,
    get_config,
    set_config,
)
from oauth import (
    start_oauth_flow,
    get_oauth_status,
    complete_oauth_flow,
    cancel_oauth_flow,
    exchange_pasted_code,
)

import asyncio

logger = logging.getLogger("kukuibot.auth_routes")

router = APIRouter()

# --- Auth exempt sets (imported by server.py for middleware) ---

AUTH_EXEMPT = {
    "/health",
    "/api/db/health",
    "/api/db/recover",
    "/api/db/backup",
    "/auth/status",
    "/auth/setup",
    "/auth/login",
    "/auth/logout",
    "/auth/token",
    "/auth/oauth/start",
    "/auth/oauth/status",
    "/auth/oauth/complete",
    "/auth/oauth/cancel",
    "/auth/oauth/exchange",
    "/api/cert",
    "/auth/callback",
    "/auth/system-login",
    "/auth/reset-password-terminal",
    "/api/claude/oauth/callback",
    "/api/claude/oauth/start",
    "/api/listener/wake",
}
AUTH_EXEMPT_PREFIXES = ("/static/", "/setup", "/login")


def _has_force_login_cookie(request: Request) -> bool:
    v = (request.cookies.get(FORCE_LOGIN_COOKIE) or "").strip().lower()
    return v in ("1", "true", "yes")


# --- Brute-force rate limiter for login endpoints ---
_login_attempts: dict[str, list[float]] = {}  # ip → [timestamps of failed attempts]
_login_lockouts: dict[str, float] = {}  # ip → lockout expiry timestamp

_LOGIN_RATE_LOGGER = logging.getLogger("kukuibot.auth.ratelimit")


def _check_login_rate_limit(request: Request) -> dict | None:
    """Check if IP is rate-limited for login. Returns error dict or None if allowed."""
    client_ip = (request.client.host if request.client else "") or "unknown"
    now = time.time()

    # Check active lockout
    lockout_until = _login_lockouts.get(client_ip, 0)
    if now < lockout_until:
        retry_after = int(lockout_until - now) + 1
        return {"error": "Too many failed login attempts. Try again later.", "retry_after": retry_after}

    # Clean up expired lockout
    _login_lockouts.pop(client_ip, None)
    return None


def _record_failed_login(request: Request):
    """Record a failed login attempt and apply lockout if thresholds exceeded."""
    client_ip = (request.client.host if request.client else "") or "unknown"
    now = time.time()

    attempts = _login_attempts.setdefault(client_ip, [])
    attempts.append(now)

    # Prune attempts older than 15 minutes
    cutoff = now - 900
    _login_attempts[client_ip] = [t for t in attempts if t > cutoff]
    attempts = _login_attempts[client_ip]

    recent_5min = sum(1 for t in attempts if t > now - 300)
    recent_15min = len(attempts)

    if recent_15min >= 10:
        _login_lockouts[client_ip] = now + 600  # 10-minute lockout
        _LOGIN_RATE_LOGGER.warning(f"Login lockout (10min): {client_ip} — {recent_15min} failed attempts in 15min")
    elif recent_5min >= 5:
        _login_lockouts[client_ip] = now + 60  # 60-second lockout
        _LOGIN_RATE_LOGGER.warning(f"Login lockout (60s): {client_ip} — {recent_5min} failed attempts in 5min")
    else:
        _LOGIN_RATE_LOGGER.info(f"Failed login attempt from {client_ip} ({recent_5min} in 5min, {recent_15min} in 15min)")


def _clear_login_attempts(request: Request):
    """Clear failed attempts for an IP after successful login."""
    client_ip = (request.client.host if request.client else "") or "unknown"
    _login_attempts.pop(client_ip, None)
    _login_lockouts.pop(client_ip, None)


# --- Auth Middleware ---

class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce authentication on all routes except exempt ones."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Static files, auth endpoints, health checks — always pass through
        if path in AUTH_EXEMPT or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
            request.state.user = get_request_user(request) or {}
            return await call_next(request)

        # Favicon and manifest — always public
        if path.startswith("/favicon") or path in ("/manifest.json", "/robots.txt"):
            request.state.user = {}
            return await call_next(request)

        # Check authentication
        user = get_request_user(request)
        if user:
            request.state.user = user
            return await call_next(request)

        # Not authenticated — decide response format
        # API/JSON requests get 401 JSON
        accept = request.headers.get("accept", "")
        if (path.startswith("/api/") or path.startswith("/auth/")
                or "application/json" in accept
                or request.method != "GET"):
            return JSONResponse({"error": "Not authenticated"}, status_code=401)

        # Browser navigation — redirect to login
        return RedirectResponse("/login.html", status_code=302)


# --- Auth Routes ---

@router.get("/auth/status")
async def auth_status_endpoint(request: Request):
    status = get_auth_status()
    is_local = is_localhost(request)
    force_login = _has_force_login_cookie(request)

    # Determine real login state
    user = get_request_user(request)
    if user and not (is_local and force_login):
        status["logged_in"] = True
        status["user"] = user.get("user", "")
        status["role"] = user.get("role", "")
        # User is logged in (or localhost auto-admin) — they're authenticated
        # to use the app regardless of whether an AI provider is connected.
        # Provider connection status is in provider_type / openai_connected.
        status["openai_connected"] = status.get("authenticated", False)
        status["authenticated"] = True
    else:
        status["logged_in"] = False
        status["user"] = ""
        status["role"] = ""
        status["openai_connected"] = False

    status["is_localhost"] = is_local
    status["force_login_required"] = is_local and force_login
    return status


@router.post("/auth/setup")
async def setup_endpoint(req: Request):
    """First-run setup — create admin + connect AI provider."""
    body = await req.json()
    skip_account = body.get("skip_account", False)

    if skip_account:
        # Localhost-only mode — skip account creation entirely
        if not is_localhost(req):
            return JSONResponse({"error": "Skip is only available from localhost"}, status_code=403)
        result = complete_setup(skip_account=True)
        if result.get("error"):
            return JSONResponse(result, status_code=400)
        return JSONResponse({"ok": True, "name": "localhost", "localhost_only": True})

    username = body.get("username", "").strip()
    password = body.get("password", "")
    display_name = body.get("display_name", "").strip()
    email = body.get("email", "").strip()

    result = complete_setup(
        username, password,
        display_name=display_name or username,
        email=email,
    )
    if result.get("error"):
        return JSONResponse(result, status_code=400)

    # Auto-login after setup
    response = JSONResponse({"ok": True, "name": result.get("name", username)})
    if result.get("session_token"):
        response.set_cookie(
            key=SESSION_COOKIE, value=result["session_token"],
            max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=True,
        )
    response.delete_cookie(FORCE_LOGIN_COOKIE, path="/")
    return response


@router.post("/auth/login")
async def login_endpoint(req: Request):
    # Rate limit check
    rate_err = _check_login_rate_limit(req)
    if rate_err:
        resp = JSONResponse({"error": rate_err["error"]}, status_code=429)
        resp.headers["Retry-After"] = str(rate_err["retry_after"])
        return resp

    body = await req.json()
    result = auth_login(body.get("username", "").strip(), body.get("password", ""))
    if result.get("error"):
        _record_failed_login(req)
        return JSONResponse(result, status_code=401)

    _clear_login_attempts(req)
    response = JSONResponse({"ok": True, "role": result["role"], "name": result["name"]})
    response.set_cookie(
        key=SESSION_COOKIE, value=result["token"],
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=True,
    )
    response.delete_cookie(FORCE_LOGIN_COOKIE, path="/")
    return response


@router.post("/auth/logout")
async def logout_endpoint(req: Request):
    token = req.cookies.get(SESSION_COOKIE)
    if token:
        auth_logout(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.set_cookie(
        key=FORCE_LOGIN_COOKIE, value="1",
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=True, path="/",
    )
    return response


@router.post("/auth/token")
async def auth_token_paste(req: Request):
    """Save an API key or session token (settings page, post-setup)."""
    body = await req.json()
    token = body.get("token", "").strip()
    provider_type = body.get("provider_type", "").strip()

    if not token:
        return JSONResponse({"error": "No token provided"}, status_code=400)

    # Lazy import to avoid circular dependency
    from server_helpers import sanitize_bearer_token as _sanitize_bearer_token

    # Claude Code — local CLI auth (set strategy to "local")
    if provider_type == "claude_code_cli":
        set_config("claude_code.auth_strategy", "local")
        return {"ok": True, "provider_type": "claude_code_cli", "auth_strategy": "local"}

    # Claude Code / Anthropic API key
    if provider_type == "claude_code":
        key = _sanitize_bearer_token(token)
        set_config("claude_code.api_key", key)
        set_config("claude_code.auth_strategy", "configured")
        return {"ok": True, "provider_type": "claude_code", "saved": bool(key)}

    # Anthropic Direct API key
    if provider_type == "anthropic":
        set_config("anthropic.api_key", token)
        return {"ok": True, "provider_type": "anthropic", "saved": bool(token)}

    # OpenRouter API key
    if provider_type == "openrouter":
        set_config("openrouter.api_key", token)
        return {"ok": True, "provider_type": "openrouter", "saved": bool(token)}

    # OpenAI API key
    if token.startswith("sk-") or provider_type == "api_key":
        save_api_key(token)
        return {"ok": True, "provider_type": "api_key"}
    else:
        account_id = extract_account_id(token)
        if not account_id:
            return JSONResponse({"error": "Invalid session token — could not extract account ID"}, status_code=400)
        save_token(token, expires_in=14 * 86400, provider_type="session_token")
        return {"ok": True, "provider_type": "session_token", "account_id": account_id[:8] + "..."}



# --- OAuth Flow ---

@router.post("/auth/oauth/start")
async def oauth_start():
    """Start the OpenAI OAuth PKCE flow. Returns URL to open in browser."""
    result = start_oauth_flow()
    if result.get("error"):
        return JSONResponse(result, status_code=500)
    return result


@router.get("/auth/oauth/status")
async def oauth_status():
    """Poll the OAuth flow status."""
    return get_oauth_status()


@router.post("/auth/oauth/complete")
async def oauth_complete():
    """Finalize a successful OAuth flow — save tokens to DB."""
    tokens = complete_oauth_flow()
    if not tokens:
        return JSONResponse({"error": "No completed OAuth flow"}, status_code=400)
    save_oauth_result(tokens)
    return {"ok": True, "account_id": tokens["account_id"][:8] + "..."}


@router.post("/auth/oauth/cancel")
async def oauth_cancel():
    """Cancel an in-progress OAuth flow."""
    cancel_oauth_flow()
    return {"ok": True}


@router.post("/auth/oauth/exchange")
async def oauth_exchange(req: Request):
    """Exchange a manually pasted authorization code for tokens.
    Used when the user is on LAN and localhost:1455 callback can't reach their browser.
    """
    body = await req.json()
    code = body.get("code", "").strip()
    if not code:
        return JSONResponse({"error": "No authorization code provided"}, status_code=400)
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, exchange_pasted_code, code
        )
        save_oauth_result(result)
        cancel_oauth_flow()  # Clean up the callback server
        return {"ok": True, "account_id": result["account_id"][:8] + "..."}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/auth/callback")
async def oauth_callback(request: Request):
    """OAuth callback proxy — allows LAN users to use <mac-ip>:7000 instead of localhost:1455."""
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error", "")

    if error:
        return HTMLResponse(f'<html><body style="background:#0a0a0a;color:#e5e5e5;font-family:system-ui;display:flex;align-items:center;justify-content:center;min-height:100vh"><div style="text-align:center"><h1 style="color:#ef4444">Error</h1><p>{error}</p></div></body></html>')
    if not code:
        return HTMLResponse('<html><body style="background:#0a0a0a;color:#e5e5e5;font-family:system-ui;display:flex;align-items:center;justify-content:center;min-height:100vh"><div style="text-align:center"><h1 style="color:#ef4444">Error</h1><p>Missing authorization code</p></div></body></html>')

    # Forward to the internal oauth callback server
    try:
        import urllib.request
        cb_url = f"http://127.0.0.1:1455/auth/callback?code={code}"
        if state:
            cb_url += f"&state={state}"
        urllib.request.urlopen(cb_url, timeout=5)
    except Exception:
        pass  # The callback server may have already processed it

    return HTMLResponse("""<!doctype html>
<html><head><meta charset="utf-8"><title>KukuiBot</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #e5e5e5;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { text-align: center; padding: 40px; border-radius: 16px; background: #1a1a1a; }
  h1 { color: #10b981; margin: 0 0 12px; }
  p { color: #a3a3a3; margin: 0; }
</style>
<script>setTimeout(function(){ window.close(); }, 2000);</script>
</head>
<body><div class="card"><h1>Connected</h1><p>You can close this tab.</p></div></body></html>""")


# --- Health ---

@router.get("/auth/me")
async def auth_me(req: Request):
    """Return current user info."""
    user = get_request_user(req)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    username = user.get("user") or user.get("username", "")
    display_name = user.get("display_name", username)
    role = user.get("role", "")
    # If localhost, resolve the actual admin user
    if username == "localhost":
        with db_connection() as db:
            row = db.execute("SELECT username, display_name, role, email FROM users WHERE role = 'admin' LIMIT 1").fetchone()
            if row:
                username, display_name, role = row[0], row[1] or row[0], row[2]
    return {"username": username, "display_name": display_name, "role": role}


@router.get("/auth/account-status")
async def account_status(req: Request):
    """Check whether an admin user account exists."""
    with db_connection() as db:
        row = db.execute("SELECT username, display_name FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if row:
            return {"exists": True, "username": row[0], "display_name": row[1] or row[0]}
        return {"exists": False}


@router.post("/auth/create-account")
async def create_account(req: Request):
    """Create admin account (localhost only, when no user exists yet)."""
    if not is_localhost(req):
        return JSONResponse({"error": "Account creation only available from localhost"}, status_code=403)
    # Check no admin exists already
    with db_connection() as db:
        row = db.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if row:
            return JSONResponse({"error": "Admin account already exists. Use Change Password instead."}, status_code=400)
    body = await req.json()
    username = body.get("username", "").strip().lower()
    password = body.get("password", "")
    display_name = body.get("display_name", "").strip()
    if not username or not password:
        return JSONResponse({"error": "Username and password are required"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters"}, status_code=400)
    if not create_user(username, password, role="admin", display_name=display_name or username):
        return JSONResponse({"error": "Failed to create user"}, status_code=500)
    logger.info(f"Admin account created from settings: {username}")
    return {"ok": True, "username": username, "display_name": display_name or username}


@router.post("/auth/change-password")
async def change_password(req: Request):
    """Change password for the currently logged-in user."""
    user = get_request_user(req)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    username = user.get("user") or user.get("username", "")
    if username == "localhost":
        # Localhost auto-admin — need to find the actual admin user
        with db_connection() as db:
            row = db.execute("SELECT username FROM users WHERE role = 'admin' LIMIT 1").fetchone()
            if not row:
                return JSONResponse({"error": "No admin account exists yet. Create one first."}, status_code=400)
            username = row[0]
    body = await req.json()
    new_pw = body.get("new_password", "")
    current_pw = body.get("current_password", "")
    if not new_pw:
        return JSONResponse({"error": "New password required"}, status_code=400)
    if len(new_pw) < 6:
        return JSONResponse({"error": "New password must be at least 6 characters"}, status_code=400)
    # Require current password for remote (non-localhost) sessions
    if not is_localhost(req):
        if not current_pw:
            return JSONResponse({"error": "Current password is required"}, status_code=400)
        if not verify_password(username, current_pw):
            return JSONResponse({"error": "Current password is incorrect"}, status_code=403)
    # Update password
    salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((salt + new_pw).encode()).hexdigest()
    with db_connection() as db:
        db.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
                   (pw_hash, salt, username))
        db.commit()
    return {"ok": True, "username": username}


@router.post("/auth/reset-password-terminal")
async def reset_password_terminal(req: Request):
    """Open Terminal with the password reset script. Only works from localhost."""
    client_ip = req.client.host if req.client else ""
    if client_ip not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "Password reset terminal only available from the local machine"}, status_code=403)
    import subprocess as _subprocess
    # This module is in src/routes/ — go up one level to reach src/
    src_dir = os.path.dirname(os.path.dirname(__file__))
    script_path = os.path.join(src_dir, "reset-password.py")
    applescript = f'''
    tell application "Terminal"
        activate
        do script "cd {src_dir} && python3 {script_path}"
    end tell
    '''
    try:
        _subprocess.Popen(["osascript", "-e", applescript])
        return {"ok": True, "message": "Terminal opened with password reset"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/auth/system-login")
async def system_login(req: Request):
    """Sign in using macOS system password. Localhost only.
    Verifies the Mac login password via dscl, then creates a KukuiBot session."""
    client_ip = req.client.host if req.client else ""
    if client_ip not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "System login only available from the local machine"}, status_code=403)

    # Rate limit check
    rate_err = _check_login_rate_limit(req)
    if rate_err:
        resp = JSONResponse({"error": rate_err["error"]}, status_code=429)
        resp.headers["Retry-After"] = str(rate_err["retry_after"])
        return resp

    body = await req.json()
    system_password = body.get("system_password", "")
    if not system_password:
        return JSONResponse({"error": "macOS password is required"}, status_code=400)

    # Verify macOS system password via dscl
    import pwd
    mac_user = pwd.getpwuid(os.getuid()).pw_name
    try:
        result = subprocess.run(
            ["/usr/bin/dscl", ".", "-authonly", mac_user, system_password],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            _record_failed_login(req)
            return JSONResponse({"error": "Incorrect macOS password"}, status_code=401)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "System auth timed out"}, status_code=500)
    except Exception as e:
        logger.error(f"dscl auth check failed: {e}")
        return JSONResponse({"error": "System auth check failed"}, status_code=500)

    # Find the KukuiBot admin user
    with db_connection() as db:
        row = db.execute("SELECT username, display_name FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if not row:
            return JSONResponse({"error": "No admin user found"}, status_code=400)
        username, display_name = row[0], row[1] or row[0]

    # Create a login session
    token = secrets.token_hex(32)
    expires = time.time() + SESSION_MAX_AGE
    _sessions[token] = {"user": username, "role": "admin", "expires": expires}
    with db_connection() as db:
        db.execute(
            "INSERT INTO sessions (token, username, role, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (token, username, "admin", int(expires), int(time.time())),
        )
        db.commit()

    _clear_login_attempts(req)
    logger.info(f"System login (macOS auth) for user: {username}")

    resp = JSONResponse({"ok": True, "username": username, "name": display_name, "token": token})
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE, httponly=True, secure=True, samesite="lax",
    )
    resp.delete_cookie(FORCE_LOGIN_COOKIE, path="/")
    return resp
