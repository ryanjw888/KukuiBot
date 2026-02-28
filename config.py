"""
config.py — App configuration, paths, env resolution.
All paths are configurable via env vars with sane defaults.
"""

import os
import platform
import shutil
from pathlib import Path

# --- Directories ---
KUKUIBOT_HOME = Path(os.environ.get("KUKUIBOT_HOME", os.path.expanduser("~/.kukuibot")))
WORKSPACE = Path(os.environ.get("KUKUIBOT_WORKSPACE", str(KUKUIBOT_HOME)))
APP_ROOT = Path(__file__).parent  # Where the app source lives


def _load_dotenv_files() -> None:
    """Best-effort .env loader (no external dependency).

    Loads simple KEY=VALUE lines from common project locations into os.environ,
    without overriding variables already set by the process manager.
    """
    candidates = [
        WORKSPACE / ".env",        # ~/.kukuibot/.env (common in this repo)
        APP_ROOT / ".env",         # src/.env
        APP_ROOT.parent / ".env",  # repo/.env
    ]

    seen: set[str] = set()
    for p in candidates:
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        if not p.exists() or not p.is_file():
            continue
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if not k or k in os.environ:
                    continue
                v = v.strip()
                if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
                    v = v[1:-1]
                os.environ[k] = v
        except Exception:
            # Silent best-effort: env loading should never crash startup.
            pass


_load_dotenv_files()

# Ensure dirs exist
KUKUIBOT_HOME.mkdir(parents=True, exist_ok=True)
(KUKUIBOT_HOME / "memory").mkdir(exist_ok=True)
(KUKUIBOT_HOME / "config").mkdir(exist_ok=True)
(KUKUIBOT_HOME / "logs").mkdir(exist_ok=True)
(KUKUIBOT_HOME / "skills").mkdir(exist_ok=True)

# Skills directory path
SKILLS_DIR = KUKUIBOT_HOME / "skills"

# --- Seed skills from bundled repo on startup (add new, don't overwrite existing) ---
_BUNDLED_SKILLS = APP_ROOT / "skills"
if _BUNDLED_SKILLS.is_dir():
    for src in _BUNDLED_SKILLS.rglob("*"):
        if src.is_file():
            rel = src.relative_to(_BUNDLED_SKILLS)
            dest = SKILLS_DIR / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

# --- Seed agent files from bundled templates on first run ---
_AGENT_TEMPLATES = APP_ROOT / "agent"
if _AGENT_TEMPLATES.is_dir():
    for template in _AGENT_TEMPLATES.glob("*.md"):
        dest = KUKUIBOT_HOME / template.name
        if not dest.exists():
            shutil.copy2(template, dest)
    # Also seed security policy
    bundled_policy = APP_ROOT / "config" / "security-policy.json"
    dest_policy = KUKUIBOT_HOME / "config" / "security-policy.json"
    if bundled_policy.exists() and not dest_policy.exists():
        shutil.copy2(bundled_policy, dest_policy)

# --- Seed docs from bundled repo on startup (add new, don't overwrite existing) ---
_BUNDLED_DOCS = APP_ROOT / "docs"
if _BUNDLED_DOCS.is_dir():
    _docs_dest = KUKUIBOT_HOME / "docs"
    _docs_dest.mkdir(exist_ok=True)
    for src in _BUNDLED_DOCS.rglob("*"):
        if src.is_file():
            dest = _docs_dest / src.relative_to(_BUNDLED_DOCS)
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

# --- Files ---
DB_PATH = KUKUIBOT_HOME / "kukuibot.db"

# --- DB Backup ---
DB_BACKUP_DIR = KUKUIBOT_HOME / "backups" / "db"
DB_CORRUPT_DIR = KUKUIBOT_HOME / "backups" / "corrupt"
DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
DB_CORRUPT_DIR.mkdir(parents=True, exist_ok=True)
DB_BACKUP_PREFIX = "kukuibot.db.backup-"
DB_BACKUP_HOURLY_KEEP = 24    # Keep 24 hourly backups
DB_BACKUP_DAILY_KEEP = 7      # Keep 7 daily backups (oldest hourly per day)
DB_BACKUP_INTERVAL = 3600     # Seconds between automated backups (1 hour)

SECURITY_POLICY_FILE = KUKUIBOT_HOME / "config" / "security-policy.json"
TOOL_LOG_PATH = KUKUIBOT_HOME / "logs" / "tool-calls.log"
SERVER_LOG_PATH = KUKUIBOT_HOME / "logs" / "server.log"
TOKEN_ACCURACY_LOG_PATH = KUKUIBOT_HOME / "logs" / "token-accuracy.log"

# Memory files
MEMORY_FILE = KUKUIBOT_HOME / "MEMORY.md"
SOUL_FILE = KUKUIBOT_HOME / "SOUL.md"
USER_FILE = KUKUIBOT_HOME / "USER.md"
TOOLS_FILE = KUKUIBOT_HOME / "TOOLS.md"
AGENTS_FILE = KUKUIBOT_HOME / "AGENTS.md"
IDENTITY_FILE = KUKUIBOT_HOME / "IDENTITY.md"
MEMORY_DIR = KUKUIBOT_HOME / "memory"

# --- App Identity ---
APP_NAME = os.environ.get("KUKUIBOT_APP_NAME", "KukuiBot")

# --- Server ---
# KUKUIBOT_PORT = the external-facing port (what users type in their browser)
# KUKUIBOT_BIND_PORT = the actual port the server binds to (may differ when
#   pfctl forwards a privileged port like 443 to a high port like 8443)
PORT = int(os.environ.get("KUKUIBOT_PORT", "7000"))
BIND_PORT = int(os.environ.get("KUKUIBOT_BIND_PORT", "") or PORT)
HOST = os.environ.get("KUKUIBOT_HOST", "127.0.0.1")
SSL_CERT = Path(__file__).parent / "certs" / "kukuibot.pem"
SSL_KEY = Path(__file__).parent / "certs" / "kukuibot-key.pem"

# --- Model ---
MODEL = "gpt-5.3-codex"
SPARK_MODEL = "spark"
KUKUIBOT_API_URL = "https://chatgpt.com/backend-api/codex/responses"
KUKUIBOT_USER_AGENT = f"kukuibot ({platform.system()} {platform.release()}; {platform.machine()})"

CODEX_CONTEXT_WINDOW = 400_000
CODEX_COMPACTION_THRESHOLD = 300_000
SPARK_CONTEXT_WINDOW = 175_000
SPARK_COMPACTION_THRESHOLD = 150_000

# Backwards-compatible defaults (Codex profile)
CONTEXT_WINDOW = CODEX_CONTEXT_WINDOW
COMPACTION_THRESHOLD = CODEX_COMPACTION_THRESHOLD

# --- Chat Log & File Activity Log ---
CHAT_LOG_FILE = KUKUIBOT_HOME / "logs" / "chat.log"          # Shared log — all messages written here (universal index)
CHAT_LOG_DIR = KUKUIBOT_HOME / "logs"
CHAT_LOG_DIR.mkdir(exist_ok=True)


def chat_log_for_worker(worker: str, model_key: str = "") -> Path:
    """Return the per-worker chat log path.

    If model_key is provided, produces chat-{model}-{worker}.log to keep
    separate logs for e.g. Claude Developer vs Codex Developer.
    Falls back to chat-{worker}.log if no model_key, or shared chat.log if no worker.
    """
    w = (worker or "").strip().lower()
    m = (model_key or "").strip().lower().replace("_", "-")
    if not w:
        return CHAT_LOG_FILE
    if m:
        return CHAT_LOG_DIR / f"chat-{m}-{w}.log"
    return CHAT_LOG_DIR / f"chat-{w}.log"

FILE_ACTIVITY_LOG = KUKUIBOT_HOME / "logs" / "file-activity.log"  # Rolling file ops log (Read/Write/Edit)
FILE_ACTIVITY_MAX_LINES = 1000
COMPACTION_LOG_FILE = KUKUIBOT_HOME / "memory" / "compaction_log.md"
COMPACTION_LOG_MAX_LINES = 1000

# --- Smart Compaction ---
RECENT_MESSAGES_TO_KEEP = 100  # Verbatim messages kept on smart compact (was 40 with LLM summarization)
MAX_DOC_REABSORB_CHARS = 60_000  # Budget for reabsorbing active docs on compact
MAX_TOOL_ROUNDS = 100
BASH_TIMEOUT = 1800  # 30 minutes
MAX_OUTPUT_CHARS = 50000

# --- Session Event Journal ---
SESSION_EVENT_RING_MAX_EVENTS = int(os.environ.get("KUKUIBOT_SESSION_EVENT_RING_MAX_EVENTS", "500"))
SESSION_EVENT_RING_MAX_BYTES = int(os.environ.get("KUKUIBOT_SESSION_EVENT_RING_MAX_BYTES", str(2 * 1024 * 1024)))
SESSION_EVENT_DB_MAX_EVENTS_PER_SESSION = int(os.environ.get("KUKUIBOT_SESSION_EVENT_DB_MAX_EVENTS_PER_SESSION", "5000"))
SESSION_EVENT_TTL_SECONDS = int(os.environ.get("KUKUIBOT_SESSION_EVENT_TTL_SECONDS", "86400"))
SSE_KEEPALIVE_SECONDS = int(os.environ.get("KUKUIBOT_SSE_KEEPALIVE_SECONDS", "15"))

# --- Delegation ---
DELEGATION_MAX_SLOTS = int(os.environ.get("KUKUIBOT_DELEGATION_MAX_SLOTS", "4"))

# --- Claude Process Pool ---
MAX_CLAUDE_PROCESSES = int(os.environ.get("KUKUIBOT_MAX_CLAUDE_PROCESSES", "8"))

# --- Runtime defaults ---
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_NUDGE_ENABLED = True
DEFAULT_SELF_COMPACT = True

# --- Paths for tool execution ---
TOOL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TOOL_ENV = {**os.environ, "PATH": TOOL_PATH}

# --- Server Port ---
# WORKER_PORT kept as alias for code that still references it (delegation, claude_bridge, etc.).
WORKER_PORT = PORT
