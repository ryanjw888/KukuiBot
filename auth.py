"""
auth.py — KukuiBot authentication & onboarding.

Three-layer auth:
  1. Setup (onboarding) — first-run admin account creation + AI provider config
  2. Login — salted SHA-256 password, session cookies
  3. Localhost trust — 127.0.0.1 / ::1 auto-admin, no login needed

Supports two AI provider modes:
  A. OpenAI API Key (official, platform.openai.com)
  B. ChatGPT Session Token (advanced, unofficial)
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from contextlib import contextmanager

from config import DB_PATH, KUKUIBOT_HOME, DB_BACKUP_DIR, DB_CORRUPT_DIR

logger = logging.getLogger("kukuibot.auth")

# --- Constants ---
SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days

# Error strings that indicate DB corruption requiring recovery (all lowercase for matching)
_CORRUPTION_ERRORS = (
    "file is not a database",
    "database disk image is malformed",
    "disk i/o error",
    "database is locked",
)

# Global lock to prevent concurrent recovery attempts
_recovery_lock = threading.Lock()
SESSION_COOKIE = "kukuibot_session"
FORCE_LOGIN_COOKIE = "kukuibot_force_login"

# In-memory session store
_sessions: dict[str, dict] = {}  # token → {"user": str, "role": str, "expires": float}


# --- Database ---

_schema_initialized = False

# Global DB health flag — checked by auth middleware for graceful degradation.
_db_healthy = True
_db_recovery_in_progress = False
_db_degraded = False  # True when DB was recreated fresh (data loss) or recovered from backup


def _open_db() -> sqlite3.Connection:
    """Open a connection with WAL mode and safe pragmas."""
    db = sqlite3.connect(str(DB_PATH), timeout=5.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA wal_autocheckpoint=500")
    return db


def _ensure_schema(db: sqlite3.Connection):
    """Create core tables if they don't exist. Idempotent."""
    global _schema_initialized
    if _schema_initialized:
        return
    try:
        db.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
    except sqlite3.DatabaseError:
        raise  # let caller handle corruption
    db.execute("""CREATE TABLE IF NOT EXISTS auth (
        provider TEXT PRIMARY KEY, access_token TEXT, refresh_token TEXT,
        account_id TEXT, expires_at INTEGER, email TEXT, updated_at INTEGER,
        provider_type TEXT DEFAULT 'session_token'
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS history (
        session_id TEXT PRIMARY KEY, items TEXT, last_response_id TEXT,
        last_api_usage TEXT, updated_at INTEGER
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY, password_hash TEXT, salt TEXT,
        role TEXT, display_name TEXT, email TEXT, created_at INTEGER
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, username TEXT, role TEXT,
        expires_at INTEGER, created_at INTEGER
    )""")
    _schema_initialized = True
    try:
        os.chmod(str(DB_PATH), 0o600)
    except Exception:
        pass


@contextmanager
def db_connection():
    """Context manager that opens a DB connection with schema init and guarantees close.

    Catches corruption errors on open and triggers recovery automatically.

    Usage:
        with db_connection() as db:
            db.execute(...)
            db.commit()
    """
    global _db_healthy
    try:
        db = _open_db()
        # Runtime corruption check — not gated by _schema_initialized
        db.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
    except sqlite3.DatabaseError as e:
        if _is_corruption_error(e):
            logger.error(f"DB corruption detected in db_connection(): {e} — attempting recovery")
            _db_healthy = False  # immediately flag unhealthy
            db = _recover_db()
            _db_healthy = True   # recovery succeeded
        else:
            raise
    try:
        _ensure_schema(db)
        yield db
    finally:
        db.close()


def _is_corruption_error(e: Exception) -> bool:
    """Check if an exception indicates DB corruption that warrants recovery."""
    msg = str(e).lower()
    return any(pattern in msg for pattern in _CORRUPTION_ERRORS)


_last_recovery_method = None  # Set by _recover_db(): 'wal_checkpoint', 'backup_restore', or 'fresh_db'


def _recover_db() -> sqlite3.Connection:
    """Attempt to recover a corrupt database. Thread-safe via _recovery_lock.

    Strategy:
      1. Checkpoint the WAL and verify integrity.
      2. If that fails, back up the corrupt file.
      3. Restore from the most recent kukuibot.db.backup-* file.
      4. Fall back to creating a fresh DB only if no backup exists.

    Sets _last_recovery_method to indicate what happened.
    """
    global _last_recovery_method
    from datetime import datetime

    if not _recovery_lock.acquire(timeout=30):
        logger.warning("DB recovery: another recovery in progress, waiting timed out")
        raise sqlite3.OperationalError("DB recovery already in progress")

    try:
        db, method = _recover_db_locked()
        _last_recovery_method = method
        return db
    finally:
        _recovery_lock.release()


def _recover_db_locked() -> tuple:
    """Internal recovery logic — must be called while holding _recovery_lock.

    Returns (db_connection, recovery_method) where recovery_method is one of:
      'wal_checkpoint' — WAL checkpoint + integrity check passed (no data loss)
      'backup_restore' — restored from a backup file
      'fresh_db' — no usable backup, created empty database (data loss)
    """
    from datetime import datetime
    backup_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    corrupt_path = DB_CORRUPT_DIR / f"{DB_PATH.name}.corrupt.{backup_ts}"

    # Step 1: Try WAL checkpoint + integrity validation
    try:
        db = sqlite3.connect(str(DB_PATH), timeout=5.0)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result = db.execute("PRAGMA integrity_check").fetchone()
        if result and result[0].lower() == "ok":
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA wal_autocheckpoint=500")
            logger.info("DB recovery: WAL checkpoint + integrity check passed")
            return db, "wal_checkpoint"
        else:
            detail = result[0] if result else "no result"
            logger.warning(f"DB recovery: integrity_check failed: {detail}")
            db.close()
    except Exception as e:
        logger.warning(f"DB recovery: checkpoint attempt failed: {e}")

    # Step 2: Back up the corrupt file
    logger.warning(f"DB recovery: backing up corrupt DB to {corrupt_path}")
    try:
        if DB_PATH.exists():
            shutil.copy2(str(DB_PATH), str(corrupt_path))
        for suffix in ("-wal", "-shm"):
            src = Path(f"{DB_PATH}{suffix}")
            if src.exists():
                shutil.copy2(str(src), str(corrupt_path) + suffix)
        # Remove corrupt files
        DB_PATH.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{DB_PATH}{suffix}").unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"DB recovery: failed to back up corrupt file: {e}")

    # Step 3: Find most recent backup and restore
    backup_dir = DB_BACKUP_DIR
    backups = sorted(
        [p for p in backup_dir.glob("kukuibot.db.backup-*") if not str(p).endswith(("-wal", "-shm"))],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    # Legacy fallback: older backups may still be in KUKUIBOT_HOME root
    legacy_backup_dir = DB_PATH.parent
    if legacy_backup_dir != backup_dir:
        legacy_backups = sorted(
            [p for p in legacy_backup_dir.glob("kukuibot.db.backup-*") if not str(p).endswith(("-wal", "-shm"))],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        seen = {b.name for b in backups}
        for lb in legacy_backups:
            if lb.name not in seen:
                backups.append(lb)


    for backup_file in backups:
        logger.info(f"DB recovery: attempting restore from {backup_file.name}")
        try:
            shutil.copy2(str(backup_file), str(DB_PATH))
            db = sqlite3.connect(str(DB_PATH), timeout=5.0)
            result = db.execute("PRAGMA integrity_check").fetchone()
            if result and result[0].lower() == "ok":
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("PRAGMA synchronous=NORMAL")
                db.execute("PRAGMA wal_autocheckpoint=500")
                logger.info(f"DB recovery: restored from {backup_file.name} — integrity OK")
                return db, "backup_restore"
            else:
                detail = result[0] if result else "no result"
                logger.warning(f"DB recovery: backup {backup_file.name} failed integrity: {detail}")
                db.close()
                DB_PATH.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"DB recovery: backup {backup_file.name} unusable: {e}")
            DB_PATH.unlink(missing_ok=True)

    # Step 4: No usable backup — create fresh DB
    db = _open_db()
    logger.warning("DB recovery: no usable backup found — created fresh database")
    return db, "fresh_db"


def _get_db() -> sqlite3.Connection:
    """Open a DB connection with schema init. Caller MUST close via try/finally or db_connection().

    Deprecated: prefer ``with db_connection() as db:`` for automatic close.
    """
    global _schema_initialized
    try:
        db = _open_db()
        # Sanity check — runs on every call (not gated by _schema_initialized)
        # to catch corruption that develops at runtime
        db.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
    except sqlite3.DatabaseError as e:
        if _is_corruption_error(e):
            logger.error(f"DB corruption detected: {e} — attempting recovery")
            db = _recover_db()
        else:
            raise
    if not _schema_initialized:
        _ensure_schema(db)
    return db


def init_db():
    """Ensure tables exist and load persisted sessions."""
    with db_connection() as db:
        now = time.time()
        for row in db.execute("SELECT token, username, role, expires_at FROM sessions WHERE expires_at > ?", (int(now),)).fetchall():
            _sessions[row[0]] = {"user": row[1], "role": row[2], "expires": row[3]}
        expired = db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(now),)).rowcount
        if expired:
            db.commit()
            logger.info(f"Cleaned {expired} expired sessions")


# =====================
# ONBOARDING / SETUP
# =====================

def is_setup_complete() -> bool:
    """Setup is complete if we have a valid token (OAuth or API key)."""
    with db_connection() as db:
        row = db.execute("SELECT access_token, provider_type FROM auth WHERE provider = 'openai-kukuibot'").fetchone()
        if not row or not row[0]:
            return False
        return True


def create_user(username: str, password: str, role: str = "admin", display_name: str = "", email: str = "") -> bool:
    """Create a new user with salted SHA-256 password."""
    username = username.strip().lower()
    if not username or not password:
        return False
    salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    with db_connection() as db:
        try:
            db.execute(
                "INSERT INTO users (username, password_hash, salt, role, display_name, email, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (username, pw_hash, salt, role, display_name or username, email, int(time.time())),
            )
            db.commit()
            logger.info(f"User created: {username} (role: {role})")
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"User already exists: {username}")
            return False


def complete_setup(username: str, password: str, display_name: str = "", email: str = "",
                   provider_type: str = "", api_key: str = "", session_token: str = "") -> dict:
    """Complete first-run setup.

    Step 1 data: username, password, display_name, email
    Step 2 data: provider_type ('openai_api_key' | 'session_token' | 'skip'), api_key or session_token

    Returns {"ok": True, "session_token": "..."} or {"error": "..."}.
    """
    if is_setup_complete():
        return {"error": "Setup already complete"}

    if not username or not password:
        return {"error": "Username and password are required"}

    if len(password) < 6:
        return {"error": "Password must be at least 6 characters"}

    if not create_user(username, password, role="admin", display_name=display_name, email=email):
        return {"error": "Failed to create user"}

    # Write USER.md with actual user info
    user_md = KUKUIBOT_HOME / "USER.md"
    try:
        from datetime import datetime
        tz_name = time.tzname[0] if time.tzname else "UTC"
        user_md.write_text(
            f"# USER.md - About Your Human\n\n"
            f"- **Name:** {display_name or username}\n"
            f"- **What to call them:** {display_name or username}\n"
            f"- **Email:** {email or '(not provided)'}\n"
            f"- **Timezone:** {tz_name}\n\n"
            f"## Context\n\n"
            f"_Add anything that helps the agent understand your preferences, work style, or environment._\n"
        )
    except Exception as e:
        logger.warning(f"Failed to write USER.md: {e}")

    # Update MEMORY.md first run date
    mem_md = KUKUIBOT_HOME / "MEMORY.md"
    if mem_md.exists():
        try:
            from datetime import datetime
            content = mem_md.read_text()
            content = content.replace("(auto-populated)", datetime.now().strftime("%Y-%m-%d"))
            mem_md.write_text(content)
        except Exception:
            pass

    # Save AI provider credentials
    if provider_type == "openai_api_key" and api_key:
        save_api_key(api_key)
    elif provider_type == "session_token" and session_token:
        account_id = extract_account_id(session_token)
        if account_id:
            save_token(session_token, expires_in=14 * 86400, provider_type="session_token")
        else:
            logger.warning("Session token provided but couldn't extract account ID")

    # Create a login session for the new admin
    session = login(username, password)

    logger.info(f"Setup complete — admin user: {username}")
    return {"ok": True, "session_token": session.get("token", ""), "role": "admin", "name": display_name or username}


# =====================
# PASSWORD AUTH
# =====================

def verify_password(username: str, password: str) -> dict | None:
    """Verify username/password. Returns user dict or None."""
    username = username.strip().lower()
    with db_connection() as db:
        row = db.execute("SELECT password_hash, salt, role, display_name FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    stored_hash, salt, role, display_name = row
    attempt_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    if not secrets.compare_digest(attempt_hash, stored_hash):
        return None
    return {"username": username, "role": role, "display_name": display_name}


def login(username: str, password: str) -> dict:
    """Authenticate and create session."""
    user = verify_password(username, password)
    if not user:
        return {"error": "Invalid username or password"}

    token = secrets.token_hex(32)
    expires = time.time() + SESSION_MAX_AGE

    _sessions[token] = {"user": user["username"], "role": user["role"], "expires": expires}

    with db_connection() as db:
        db.execute(
            "INSERT INTO sessions (token, username, role, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (token, user["username"], user["role"], int(expires), int(time.time())),
        )
        db.commit()

    logger.info(f"Login: {user['username']} (role: {user['role']})")
    return {"ok": True, "token": token, "role": user["role"], "name": user["display_name"]}


def logout(session_token: str):
    """Destroy a session."""
    _sessions.pop(session_token, None)
    with db_connection() as db:
        db.execute("DELETE FROM sessions WHERE token = ?", (session_token,))
        db.commit()


# =====================
# SESSION VERIFICATION
# =====================

def verify_session(session_token: str) -> dict | None:
    """Check if session token is valid. Returns {"user": ..., "role": ...} or None."""
    if not session_token:
        return None
    session = _sessions.get(session_token)
    if not session:
        return None
    if time.time() > session["expires"]:
        _sessions.pop(session_token, None)
        return None
    return session


def is_localhost(request) -> bool:
    """Check if request comes from localhost."""
    client_ip = (request.client.host if request.client else "") or ""
    return client_ip in ("127.0.0.1", "::1", "localhost")


def get_request_user(request) -> dict | None:
    """Get the authenticated user from a request.

    Priority: valid session cookie → localhost auto-admin fallback → None
    """
    token = request.cookies.get(SESSION_COOKIE)
    session_user = verify_session(token)
    if session_user:
        return session_user
    if is_localhost(request):
        return {"user": "localhost", "role": "admin", "display_name": "Admin (local)"}
    return None


# =====================
# AI PROVIDER — TOKEN / API KEY
# =====================

def save_api_key(api_key: str):
    """Save an OpenAI API key (sk-...)."""
    with db_connection() as db:
        db.execute(
            "INSERT OR REPLACE INTO auth (provider, access_token, refresh_token, account_id, expires_at, email, updated_at, provider_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("openai-kukuibot", api_key, "", "", 0, "", int(time.time()), "api_key"),
        )
        db.commit()
    logger.info("OpenAI API key saved")


def save_oauth_result(result: dict):
    """Save tokens from a completed OAuth flow.
    
    Args:
        result: {"access": str, "refresh": str, "expires": int, "account_id": str}
    """
    expires_in = max(0, result["expires"] - int(time.time())) if result.get("expires") else 14 * 86400
    save_token(
        access_token=result["access"],
        refresh_token=result.get("refresh", ""),
        expires_in=expires_in,
        provider_type="oauth",
    )


def save_token(access_token: str, refresh_token: str = "", email: str = "", expires_in: int = 0, provider_type: str = "session_token"):
    """Save a ChatGPT session token."""
    account_id = extract_account_id(access_token)
    expires_at = int(time.time() + expires_in) if expires_in else 0
    with db_connection() as db:
        db.execute(
            "INSERT OR REPLACE INTO auth (provider, access_token, refresh_token, account_id, expires_at, email, updated_at, provider_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("openai-kukuibot", access_token, refresh_token, account_id, expires_at, email, int(time.time()), provider_type),
        )
        db.commit()
    logger.info(f"Token saved (type: {provider_type}, account: {account_id[:8] if account_id else 'n/a'}..., expires: {expires_at})")


def get_token() -> str:
    """Get current access token or API key."""
    with db_connection() as db:
        row = db.execute("SELECT access_token, expires_at, provider_type FROM auth WHERE provider = 'openai-kukuibot'").fetchone()
    if not row:
        return ""
    token, expires_at, provider_type = row
    # API keys don't expire
    if provider_type == "api_key":
        return token or ""
    # Session tokens can expire
    if expires_at and time.time() > expires_at:
        logger.warning("Session token expired")
        return ""
    return token or ""


def get_provider_type() -> str:
    """Get the configured provider type ('api_key', 'session_token', or '')."""
    with db_connection() as db:
        row = db.execute("SELECT provider_type FROM auth WHERE provider = 'openai-kukuibot'").fetchone()
    return row[0] if row else ""


def get_account_id() -> str:
    """Get cached account ID (session token mode only)."""
    with db_connection() as db:
        row = db.execute("SELECT account_id FROM auth WHERE provider = 'openai-kukuibot'").fetchone()
    return row[0] if row else ""


def get_auth_status() -> dict:
    """Get full auth status — setup state + token state."""
    setup_done = is_setup_complete()
    with db_connection() as db:
        row = db.execute("SELECT access_token, account_id, expires_at, email, updated_at, provider_type FROM auth WHERE provider = 'openai-kukuibot'").fetchone()
    if not row:
        return {"setup_complete": setup_done, "authenticated": False, "provider_type": ""}
    token, account_id, expires_at, email, updated_at, provider_type = row
    now = time.time()
    if provider_type == "api_key":
        authenticated = bool(token)
        expired = False
    else:
        expired = expires_at > 0 and now > expires_at
        authenticated = bool(token) and not expired
    return {
        "setup_complete": setup_done,
        "authenticated": authenticated,
        "provider_type": provider_type or "",
        "email": email or "",
        "account_id": account_id[:8] + "..." if account_id else "",
        "expires_at": expires_at,
        "expired": expired,
        "remaining_hours": max(0, round((expires_at - now) / 3600, 1)) if expires_at and provider_type != "api_key" else None,
    }


def clear_token():
    """Remove stored token/API key."""
    with db_connection() as db:
        db.execute("DELETE FROM auth WHERE provider = 'openai-kukuibot'")
        db.commit()


def extract_account_id(token: str) -> str:
    """Extract chatgpt_account_id from JWT token. Returns '' for API keys."""
    if token.startswith("sk-"):
        return ""  # API keys don't have account IDs
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        return payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
    except Exception as e:
        logger.warning(f"Failed to extract account ID: {e}")
        return ""


# =====================
# DB HEALTH & RECOVERY (API helpers)
# =====================

def db_health_check() -> dict:
    """Run a quick DB health check. Returns status dict for /api/db/health."""
    result = {"healthy": False, "file_exists": DB_PATH.exists(), "file_size": 0, "tables": 0, "detail": ""}
    if not DB_PATH.exists():
        result["detail"] = "Database file does not exist"
        return result
    result["file_size"] = DB_PATH.stat().st_size
    try:
        db = sqlite3.connect(str(DB_PATH), timeout=5.0)
        try:
            check = db.execute("PRAGMA quick_check").fetchone()
            if check and check[0].lower() == "ok":
                result["healthy"] = True
                result["detail"] = "ok"
            else:
                result["detail"] = check[0] if check else "no result from quick_check"
            tables = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()
            result["tables"] = tables[0] if tables else 0
        finally:
            db.close()
    except Exception as e:
        result["detail"] = str(e)
    return result


def db_manual_recover() -> dict:
    """Trigger manual DB recovery. Returns result dict for /api/db/recover."""
    global _schema_initialized
    try:
        db = _recover_db()
        _schema_initialized = False
        _ensure_schema(db)
        db.close()
        return {"ok": True, "message": "Recovery completed successfully"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def startup_db_health_gate() -> dict:
    """Run at startup before serving traffic. Returns status dict.
    Flow:
      1. Run PRAGMA quick_check
      2. If healthy -> return {"healthy": True}
      3. If corrupt -> attempt _recover_db()
      4. If recovery succeeds -> return {"healthy": True, "recovered": True}
      5. If no backup -> create fresh DB, return {"healthy": True, "degraded": True, "data_loss": True}
    """
    global _db_healthy, _db_recovery_in_progress, _db_degraded

    if not DB_PATH.exists():
        logger.warning("Startup health gate: no DB file — creating fresh database")
        db = _open_db()
        _ensure_schema(db)
        db.close()
        _db_healthy = True
        return {"healthy": True, "fresh": True}

    # Step 1: quick_check
    try:
        db = sqlite3.connect(str(DB_PATH), timeout=5.0)
        check = db.execute("PRAGMA quick_check").fetchone()
        db.close()
        if check and check[0].lower() == "ok":
            _db_healthy = True
            logger.info("Startup health gate: DB integrity OK")
            return {"healthy": True}
        else:
            detail = check[0] if check else "no result"
            logger.error(f"Startup health gate: quick_check FAILED: {detail}")
    except Exception as e:
        logger.error(f"Startup health gate: DB access failed: {e}")

    # Step 2: Attempt recovery
    _db_recovery_in_progress = True
    try:
        db = _recover_db()
        global _schema_initialized
        _schema_initialized = False
        _ensure_schema(db)
        db.close()

        # Step 3: Post-recovery verification — quick_check the recovered DB
        try:
            verify_db = sqlite3.connect(str(DB_PATH), timeout=5.0)
            verify = verify_db.execute("PRAGMA quick_check").fetchone()
            verify_db.close()
            if not (verify and verify[0].lower() == "ok"):
                logger.error("Startup health gate: post-recovery quick_check FAILED")
                _db_healthy = False
                return {"healthy": False, "error": "Post-recovery verification failed"}
        except Exception as ve:
            logger.error(f"Startup health gate: post-recovery verification error: {ve}")
            _db_healthy = False
            return {"healthy": False, "error": f"Post-recovery verification error: {ve}"}

        # Step 4: Check recovery method — fresh DB means data loss
        recovery_method = _last_recovery_method
        if recovery_method == "fresh_db":
            _db_healthy = True
            _db_degraded = True
            logger.warning("Startup health gate: recovery created fresh DB — data loss, degraded mode")
            return {"healthy": True, "recovered": True, "degraded": True, "data_loss": True}

        _db_healthy = True
        logger.info(f"Startup health gate: recovery succeeded (method: {recovery_method})")
        return {"healthy": True, "recovered": True}
    except Exception as e:
        logger.error(f"Startup health gate: recovery failed: {e}")
        _db_healthy = False
        return {"healthy": False, "error": str(e)}
    finally:
        _db_recovery_in_progress = False


def periodic_health_check() -> bool:
    """Lightweight health check for background monitor. Returns True if healthy."""
    global _db_healthy, _db_recovery_in_progress

    if _db_recovery_in_progress:
        return _db_healthy

    try:
        db = sqlite3.connect(str(DB_PATH), timeout=3.0)
        try:
            check = db.execute("PRAGMA quick_check").fetchone()
            if check and check[0].lower() == "ok":
                if not _db_healthy:
                    logger.info("Periodic health check: DB is healthy again")
                    _db_healthy = True
                return True
            else:
                logger.error(f"Periodic health check: corruption detected: {check}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Periodic health check: DB access failed: {e}")

    # Corruption detected — attempt recovery
    _db_recovery_in_progress = True
    try:
        db = _recover_db()
        global _schema_initialized
        _schema_initialized = False
        _ensure_schema(db)
        db.close()
        _db_healthy = True
        logger.info("Periodic health check: recovery succeeded")
        return True
    except Exception as e:
        logger.error(f"Periodic health check: recovery failed: {e}")
        _db_healthy = False
        return False
    finally:
        _db_recovery_in_progress = False


# =====================
# DB BACKUP (sqlite3.backup API)
# =====================

def db_backup(backup_dir: Path | None = None) -> dict:
    """Create a SQLite-aware backup using sqlite3.backup() API.

    Safe for online use — handles WAL mode correctly without stopping writes.
    Writes to a temp file, then renames atomically.

    Returns:
        {"ok": True, "path": str, "name": str, "size": int, "duration_ms": float}
        or {"ok": False, "error": str}
    """
    from datetime import datetime
    import tempfile

    if backup_dir is None:
        backup_dir = DB_BACKUP_DIR

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_name = f"kukuibot.db.backup-{ts}"
    final_path = backup_dir / backup_name

    if not DB_PATH.exists():
        return {"ok": False, "error": "Source database does not exist"}

    start = time.time()
    tmp_fd = None
    tmp_path = None
    try:
        # Write to temp file in same directory (same filesystem = atomic rename)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="kukuibot-backup-", suffix=".tmp", dir=str(backup_dir)
        )
        os.close(tmp_fd)
        tmp_fd = None

        # Open source (read-only) and destination
        src_db = sqlite3.connect(str(DB_PATH), timeout=10.0)
        dst_db = sqlite3.connect(tmp_path)
        try:
            src_db.backup(dst_db)
        finally:
            dst_db.close()
            src_db.close()

        # Atomic rename
        os.rename(tmp_path, str(final_path))
        tmp_path = None  # prevent cleanup

        # Set restrictive permissions
        try:
            os.chmod(str(final_path), 0o600)
        except Exception:
            pass

        duration_ms = (time.time() - start) * 1000
        size = final_path.stat().st_size
        logger.info(f"DB backup created: {backup_name} ({size} bytes, {duration_ms:.1f}ms)")
        return {"ok": True, "path": str(final_path), "name": backup_name, "size": size, "duration_ms": round(duration_ms, 1)}

    except Exception as e:
        logger.error(f"DB backup failed: {e}")
        # Clean up temp file on failure
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        return {"ok": False, "error": str(e)}


def db_backup_rotate(backup_dir: Path | None = None, hourly_keep: int = 24, daily_keep: int = 7) -> dict:
    """Rotate backup files: keep N hourly + M daily, delete the rest.

    Rotation logic:
      1. List all kukuibot.db.backup-YYYYMMDD-HHMMSS files, sorted newest first
      2. Keep the newest `hourly_keep` files unconditionally
      3. From the remaining, keep the oldest backup per calendar day for `daily_keep` days
      4. Delete everything else

    Returns: {"kept": int, "deleted": int, "errors": list[str]}
    """
    import re

    if backup_dir is None:
        backup_dir = DB_BACKUP_DIR

    # Find all backup files (exclude -wal, -shm companions)
    all_backups = sorted(
        [p for p in backup_dir.glob("kukuibot.db.backup-*")
         if not str(p).endswith(("-wal", "-shm"))
         and re.match(r"kukuibot\.db\.backup-\d{8}-\d{6}$", p.name)],
        key=lambda p: p.name,
        reverse=True,  # newest first
    )

    if len(all_backups) <= hourly_keep:
        return {"kept": len(all_backups), "deleted": 0, "errors": []}

    # Step 1: Keep newest hourly_keep unconditionally
    keep_set = set(all_backups[:hourly_keep])
    remaining = all_backups[hourly_keep:]

    # Step 2: From remaining, keep oldest per calendar day for daily_keep days
    daily_buckets: dict[str, Path] = {}
    for backup in reversed(remaining):  # oldest first
        match = re.search(r"backup-(\d{8})-\d{6}$", backup.name)
        if match:
            date_str = match.group(1)
            daily_buckets.setdefault(date_str, backup)

    # Keep up to daily_keep days
    daily_dates = sorted(daily_buckets.keys(), reverse=True)[:daily_keep]
    for date_str in daily_dates:
        keep_set.add(daily_buckets[date_str])

    # Step 3: Delete everything not in keep_set
    deleted = 0
    errors = []
    for backup in all_backups:
        if backup not in keep_set:
            try:
                backup.unlink()
                # Also clean up companion files
                for suffix in ("-wal", "-shm"):
                    companion = Path(str(backup) + suffix)
                    if companion.exists():
                        companion.unlink()
                deleted += 1
            except Exception as e:
                errors.append(f"{backup.name}: {e}")

    logger.info(f"Backup rotation: kept {len(keep_set)}, deleted {deleted}")
    return {"kept": len(keep_set), "deleted": deleted, "errors": errors}


# =====================
# HISTORY (SQLite)
# =====================

def load_history(session_id: str) -> tuple[list, str, dict]:
    with db_connection() as db:
        row = db.execute("SELECT items, last_response_id, last_api_usage FROM history WHERE session_id = ?", (session_id,)).fetchone()
    if not row:
        return [], "", {}
    return json.loads(row[0]) if row[0] else [], row[1] or "", json.loads(row[2]) if row[2] else {}


def save_history(session_id: str, items: list, last_response_id: str = "", last_api_usage: dict = None):
    with db_connection() as db:
        db.execute(
            "INSERT OR REPLACE INTO history (session_id, items, last_response_id, last_api_usage, updated_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, json.dumps(items), last_response_id, json.dumps(last_api_usage or {}), int(time.time())),
        )
        db.commit()


def clear_history(session_id: str):
    with db_connection() as db:
        db.execute("DELETE FROM history WHERE session_id = ?", (session_id,))
        db.commit()


# =====================
# CONFIG (SQLite)
# =====================

def get_config(key: str, default: str = "") -> str:
    with db_connection() as db:
        row = db.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_config(key: str, value: str):
    with db_connection() as db:
        db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        db.commit()


# =====================
# LEGACY IMPORT
# =====================

def import_from_legacy() -> bool:
    """Try to import token from legacy auth sources."""
    env_token = os.environ.get("KUKUIBOT_TOKEN", "")
    if env_token:
        if env_token.startswith("sk-"):
            save_api_key(env_token)
        else:
            save_token(env_token, expires_in=14 * 86400)
        logger.info("Imported token from KUKUIBOT_TOKEN env var")
        return True
    try:
        auth_path = os.path.expanduser("~/.kukuibot/legacy/auth-profiles.json")
        if not os.path.exists(auth_path):
            return False
        with open(auth_path) as f:
            data = json.load(f)
        for provider_key in ["openai-codex:default", "openai-kukuibot:default"]:
            profile = data.get("profiles", {}).get(provider_key, {})
            token = profile.get("access", "")
            if token:
                refresh = profile.get("refresh", "")
                email = profile.get("email", "")
                expires = profile.get("expires", 0)
                expires_in = max(0, int((expires - time.time() * 1000) / 1000)) if expires else 0
                save_token(token, refresh, email, expires_in)
                logger.info(f"Imported token from legacy auth ({provider_key})")
                return True
        return False
    except Exception as e:
        logger.debug(f"Legacy import not available: {e}")
        return False
