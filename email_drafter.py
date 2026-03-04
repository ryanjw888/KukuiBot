"""
email_drafter.py — AI-powered email draft generator.

Checks inbox for new unread emails, generates draft replies using Claude
in the user's writing style, and saves them as Gmail drafts (never sends).

The style profile is built from the user's sent emails and cached locally.
It's rebuilt automatically when stale (> 7 days).

Integration points:
  - gmail_bridge: IMAP/SMTP operations, permissions
  - anthropic_bridge: AI calls via non-streaming anthropic_chat()
  - email_sanitize: Content sanitization on all outbound drafts
  - auth: Config storage in DB
"""

import email
import email.header
import email.utils
import imaplib
import json
import logging
import os
import re
import time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from config import KUKUIBOT_HOME, DB_PATH

logger = logging.getLogger("kukuibot.drafter")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STYLE_PROFILE_PATH = KUKUIBOT_HOME / "email_style_profile.md"
STATE_PATH = KUKUIBOT_HOME / "var" / "email_drafter_state.json"

MAX_EMAILS_PER_RUN = 10
MAX_SENT_FOR_PROFILE = 100
PROFILE_MAX_AGE_DAYS = 7
MAX_BODY_CHARS = 3000
MAX_THREAD_MESSAGES = 8         # max prior messages to include as context
MAX_THREAD_BODY_CHARS = 1500    # truncation limit per older thread message
MAX_PROCESSED_IDS = 1000

# Custom header to identify auto-drafted emails
X_DRAFTER_HEADER = "X-KukuiBot-Draft"
X_DRAFTER_VALUE = "auto"

HST = ZoneInfo("US/Hawaii")

# Patterns that indicate automated/noreply senders
_AUTOMATED_PATTERNS = re.compile(
    r"(noreply|no-reply|no_reply|donotreply|mailer-daemon|notifications?@|alerts?@|"
    r"bounce[sd]?@|support@.*\.zendesk|jira@|github\.com|gitlab\.com|"
    r"notify@|updates?@|info@.*\.automated|postmaster@|daemon@)",
    re.IGNORECASE,
)

# Headers that indicate mailing lists / bulk mail
_BULK_HEADERS = ["List-Unsubscribe", "List-Id", "X-Mailer-Daemon", "X-Auto-Response-Suppress"]

# 2FA / MFA / verification code detection
_2FA_SENDER_DOMAINS = re.compile(
    r"@(?:.*\.)?(accounts\.google\.com|accountprotection\.microsoft\.com|"
    r"id\.apple\.com|icloud\.com|"
    r"verify\.facebook\.com|facebookmail\.com|"
    r"noreply\.github\.com|"
    r"login\.coinbase\.com|"
    r"no-reply\.sns\.amazonaws\.com|"
    r"duosecurity\.com|okta\.com|auth0\.com|twilio\.com|"
    r"steampowered\.com|login\.uber\.com)$",
    re.IGNORECASE,
)

_2FA_SUBJECT_RE = re.compile(
    r"\b(?:verification\s+code|security\s+code|one[\s-]?time\s+(?:pass)?code|"
    r"your\s+otp|sign[\s-]?in\s+code|login\s+code|"
    r"confirm\s+your\s+(?:email|identity|account)|"
    r"two[\s-]?factor|2fa|mfa\s+code|"
    r"password\s+reset|reset\s+your\s+password|"
    r"backup\s+codes?|recovery\s+code|"
    r"authentication\s+code)\b",
    re.IGNORECASE,
)

_2FA_BODY_CODE_RE = re.compile(
    r"(?:(?:verification|security|login|sign[\s-]?in|one[\s-]?time|confirmation|authentication)\s+code"
    r"(?:\s+is)?[\s:]*\d{4,8}\b|"
    r"\byour\s+(?:\w+\s+)?code\s+is[\s:]*\d{4,8}\b|"
    r"\benter\s+(?:this\s+)?code[\s:]*\d{4,8}\b|"
    r"\bcode\s*[:=]\s*\d{4,8}\b|"
    r"\b\d{4,8}\s+is\s+your\s+(?:verification|security|login)\s+code\b)",
    re.IGNORECASE,
)

_2FA_EXPIRY_RE = re.compile(
    r"\b(?:expires?|valid\s+for)\s+(?:in\s+)?\d{1,3}\s*(?:minutes?|mins?|hours?)\b",
    re.IGNORECASE,
)

# Financial / banking content detection
_FINANCIAL_SENDER_DOMAINS = re.compile(
    r"@(?:.*\.)?(chase\.com|jpmorgan\.com|"
    r"bankofamerica\.com|bofa\.com|"
    r"wellsfargo\.com|"
    r"citibank\.com|citi\.com|"
    r"americanexpress\.com|aexp\.com|"
    r"capitalone\.com|discover\.com|"
    r"usbank\.com|pnc\.com|"
    r"td\.com|tdbank\.com|tdameritrade\.com|"
    r"ally\.com|navyfederal\.org|"
    r"fidelity\.com|schwab\.com|vanguard\.com|etrade\.com|"
    r"robinhood\.com|sofi\.com|"
    r"coinbase\.com|binance\.com|kraken\.com|"
    r"paypal\.com|venmo\.com|zellepay\.com|cash\.app|"
    r"wise\.com|revolut\.com|"
    r"hsbc\.com|barclays\.com|santander\.com|"
    r"scotiabank\.com|rbc\.com|bmo\.com|cibc\.com|"
    r"irs\.gov|turbotax\.intuit\.com|hrblock\.com)$",
    re.IGNORECASE,
)

_FINANCIAL_SUBJECT_RE = re.compile(
    r"\b(?:statement\s+(?:is\s+)?ready|account\s+statement|"
    r"transaction\s+alert|fraud\s+alert|"
    r"payment\s+(?:received|sent|due|failed|confirmed)|"
    r"deposit\s+(?:received|posted)|"
    r"balance\s+(?:update|alert|available)|"
    r"account\s+activity|purchase\s+alert|"
    r"wire\s+transfer|ach\s+(?:credit|debit|transfer)|"
    r"bill\s+(?:due|paid)|autopay|"
    r"credit\s+card\s+(?:statement|alert|activity)|"
    r"(?:your\s+)?tax\s+(?:document|form|return)|"
    r"\b1099\b|\bw[\s-]?2\b|"
    r"suspicious\s+(?:activity|transaction)|"
    r"account\s+(?:frozen|locked|suspended)|"
    r"monthly\s+statement)\b",
    re.IGNORECASE,
)

_FINANCIAL_BODY_CC_RE = re.compile(
    r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{1,4}\b"
)

_FINANCIAL_BODY_SSN_RE = re.compile(r"\b\d{3}[\s-]\d{2}[\s-]\d{4}\b")

_FINANCIAL_BODY_ROUTING_RE = re.compile(
    r"\b(?:routing|aba|transit)\s*(?:number|#|no\.?)?\s*[:#-]?\s*\d{9}\b",
    re.IGNORECASE,
)

_FINANCIAL_BODY_ACCOUNT_RE = re.compile(
    r"\b(?:account)\s*(?:number|#|no\.?)?\s*[:#-]?\s*\d{8,17}\b",
    re.IGNORECASE,
)

_FINANCIAL_BODY_LAST4_RE = re.compile(
    r"\b(?:ending\s+in|last\s+4|x{4,})\s*[-:]?\s*\d{4}\b",
    re.IGNORECASE,
)

# Default signature (can be overridden via config)
DEFAULT_SIGNATURE_HTML = ""

# ---------------------------------------------------------------------------
# Default filters — each has an id, name, description, enabled flag, and type.
# "builtin" filters run hardcoded logic. "exclusion" filters use pattern matching.
# ---------------------------------------------------------------------------

DEFAULT_FILTERS = [
    {
        "id": "automated_senders",
        "name": "Skip automated/noreply senders",
        "description": "Filters noreply@, mailer-daemon, GitHub/GitLab notifications, Jira, Zendesk, etc.",
        "enabled": True,
        "type": "builtin",
    },
    {
        "id": "bulk_mail",
        "name": "Skip mailing lists & bulk mail",
        "description": "Filters emails with List-Unsubscribe, List-Id headers, or Precedence: bulk/list/junk.",
        "enabled": True,
        "type": "builtin",
    },
    {
        "id": "empty_body",
        "name": "Skip emails with empty body",
        "description": "Filters emails with no body text or fewer than 10 characters.",
        "enabled": True,
        "type": "builtin",
    },
    {
        "id": "from_self",
        "name": "Skip emails from yourself",
        "description": "Filters emails sent from your own Gmail address.",
        "enabled": True,
        "type": "builtin",
    },
    {
        "id": "cc_only",
        "name": "Skip emails where you're only CC'd",
        "description": "Only draft replies when you're in the To field, not just CC or BCC.",
        "enabled": True,
        "type": "builtin",
    },
    {
        "id": "security_codes",
        "name": "Skip 2FA/MFA verification emails",
        "description": "Blocks one-time passcodes, verification codes, password resets, and backup codes from being auto-drafted.",
        "enabled": True,
        "type": "builtin",
    },
    {
        "id": "financial_content",
        "name": "Skip financial/banking emails",
        "description": "Blocks bank statements, transaction alerts, tax documents, and payment notifications from being auto-drafted.",
        "enabled": True,
        "type": "builtin",
    },
    {
        "id": "spam_detection",
        "name": "AI spam/phishing detection",
        "description": "Uses AI to classify emails as spam or phishing.",
        "enabled": True,
        "type": "builtin",
        "spam_action": "label",        # "label" = prepend SPAM: to subject
                                        # "trash" = move to Trash
                                        # "spam"  = move to [Gmail]/Spam
        "notify": True,                 # show notification in UI when spam detected
        "confidence_threshold": 0.6,    # minimum confidence to act (0.0 - 1.0)
    },
    {
        "id": "exclude_senders",
        "name": "Excluded senders",
        "description": "Skip emails from specific senders. Use * as wildcard (e.g. *@noreply.github.com).",
        "enabled": True,
        "type": "exclusion",
        "patterns": [],
    },
    {
        "id": "exclude_subjects",
        "name": "Excluded subjects",
        "description": "Skip emails matching subject patterns. Use * as wildcard (e.g. *newsletter*).",
        "enabled": True,
        "type": "exclusion",
        "patterns": [],
    },
]


# ---------------------------------------------------------------------------
# Config helpers (reuse existing auth module)
# ---------------------------------------------------------------------------

def _get_config(key: str, default: str = "") -> str:
    from auth import get_config
    return get_config(key, default)


def _set_config(key: str, value: str):
    from auth import set_config
    set_config(key, value)


def _get_filters() -> list[dict]:
    """Load filters from config, merging with defaults for any missing builtin filters."""
    raw = _get_config("drafter.filters", "")
    saved: list[dict] = []
    if raw:
        try:
            saved = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            saved = []

    # Build a lookup of saved filters by id
    saved_by_id = {f["id"]: f for f in saved if isinstance(f, dict) and "id" in f}

    # Merge: use saved state if it exists, otherwise use default
    merged = []
    for default in DEFAULT_FILTERS:
        if default["id"] in saved_by_id:
            # Preserve saved enabled/patterns state but update name/description from defaults
            s = saved_by_id[default["id"]]
            entry = {**default, "enabled": s.get("enabled", default["enabled"])}
            if "patterns" in s:
                entry["patterns"] = s["patterns"]
            merged.append(entry)
        else:
            merged.append({**default})

    # Also include any user-added custom filters not in defaults
    default_ids = {d["id"] for d in DEFAULT_FILTERS}
    for s in saved:
        if isinstance(s, dict) and s.get("id") and s["id"] not in default_ids:
            merged.append(s)

    return merged


def _save_filters(filters: list[dict]):
    """Save filters to config."""
    _set_config("drafter.filters", json.dumps(filters))


def _migrate_exclusions_to_filters():
    """One-time migration: move old exclusions config into the new filter format."""
    raw_excl = _get_config("drafter.exclusions", "")
    raw_filters = _get_config("drafter.filters", "")
    if not raw_excl or raw_filters:
        return  # nothing to migrate, or already migrated
    try:
        excl = json.loads(raw_excl)
    except (json.JSONDecodeError, TypeError):
        return
    filters = _get_filters()
    for f in filters:
        if f["id"] == "exclude_senders" and excl.get("senders"):
            f["patterns"] = excl["senders"]
        if f["id"] == "exclude_subjects" and excl.get("subjects"):
            f["patterns"] = excl["subjects"]
    _save_filters(filters)
    logger.info("Migrated old exclusions to new filter format")


def _get_api_key() -> str:
    """Resolve Anthropic API key using same chain as anthropic_provider."""
    key = (_get_config("anthropic.api_key", "") or "").strip()
    if key:
        return key
    key = (_get_config("claude_code.api_key", "") or "").strip()
    if key:
        return key
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _has_ai_backend() -> bool:
    """Check if any AI backend is available (API key OR Claude Code pool)."""
    if _get_api_key():
        return True
    # Claude Code subprocess pool doesn't need a stored API key
    try:
        from claude_bridge import get_claude_pool
        pool = get_claude_pool()
        if pool is not None:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# DB: drafter_history table
# ---------------------------------------------------------------------------

def _ensure_history_table():
    """Create drafter_history table if it doesn't exist."""
    import sqlite3
    if not DB_PATH.exists():
        return
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS drafter_history (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                from_addr TEXT NOT NULL,
                subject TEXT NOT NULL,
                action TEXT NOT NULL,
                skip_reason TEXT DEFAULT '',
                draft_preview TEXT DEFAULT '',
                draft_uid TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_drafter_history_created
                ON drafter_history(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_drafter_history_action
                ON drafter_history(action);
        """)
        con.commit()
        con.close()
    except Exception as e:
        logger.warning(f"Failed to create drafter_history table: {e}")


def _record_history(message_id: str, from_addr: str, subject: str,
                    action: str, skip_reason: str = "", draft_preview: str = "",
                    draft_uid: str = ""):
    """Insert a row into drafter_history."""
    import sqlite3
    import uuid
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """INSERT INTO drafter_history
               (id, message_id, from_addr, subject, action, skip_reason, draft_preview, draft_uid, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), message_id, from_addr[:200], subject[:200],
             action, skip_reason[:200], draft_preview[:300], draft_uid, int(time.time())),
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.warning(f"Failed to record drafter history: {e}")


def get_history(limit: int = 50, offset: int = 0, action_filter: str = "") -> dict:
    """Return paginated drafter history."""
    import sqlite3
    result = {"items": [], "total": 0}
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        con.row_factory = sqlite3.Row

        where = "WHERE 1=1"
        params: list = []
        if action_filter:
            where += " AND action = ?"
            params.append(action_filter)

        total = con.execute(f"SELECT COUNT(*) FROM drafter_history {where}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT * FROM drafter_history {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        con.close()

        result["total"] = total
        result["items"] = [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"Failed to read drafter history: {e}")
    return result


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to load drafter state: {e}")
    return {"last_run_at": 0, "processed_ids": [], "profile_built_at": 0}


def _save_state(state: dict):
    if len(state.get("processed_ids", [])) > MAX_PROCESSED_IDS:
        state["processed_ids"] = state["processed_ids"][-MAX_PROCESSED_IDS:]
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to save drafter state: {e}")


# ---------------------------------------------------------------------------
# IMAP helpers (reuse gmail_bridge internals)
# ---------------------------------------------------------------------------

def _imap_connect():
    from gmail_bridge import _imap_connect as gmail_imap
    return gmail_imap()


def _extract_body(msg):
    from gmail_bridge import _extract_body as gmail_body
    return gmail_body(msg)


def _parse_header(raw_header: str) -> str:
    """Decode a potentially RFC2047-encoded header."""
    parts = email.header.decode_header(raw_header or "")
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _is_automated_sender(msg: email.message.Message) -> bool:
    """Check if an email is from an automated/noreply sender (address patterns only)."""
    from_addr = msg.get("From", "")
    return bool(_AUTOMATED_PATTERNS.search(from_addr))


def _is_bulk_mail(msg: email.message.Message) -> bool:
    """Check if an email has mailing list / bulk mail headers."""
    for header in _BULK_HEADERS:
        if msg.get(header):
            return True
    precedence = (msg.get("Precedence") or "").lower()
    return precedence in ("bulk", "list", "junk")


def _is_cc_only(msg: email.message.Message, my_email: str) -> bool:
    """Check if we're only in CC/BCC, not the To field."""
    to_raw = msg.get("To", "")
    if not my_email:
        return False
    my_lower = my_email.lower()
    # Parse all To addresses
    to_addrs = [addr.lower() for _, addr in email.utils.getaddresses([to_raw])]
    return my_lower not in to_addrs


def _is_2fa_email(msg: email.message.Message, from_addr: str,
                  subject: str, body: str) -> bool:
    """Detect 2FA/MFA/verification emails using multi-signal scoring."""
    sender_hit = bool(_2FA_SENDER_DOMAINS.search(from_addr))
    subj_hit = bool(_2FA_SUBJECT_RE.search(subject))
    body_code = bool(_2FA_BODY_CODE_RE.search(body[:2000]))
    body_expiry = bool(_2FA_EXPIRY_RE.search(body[:2000]))
    # Strong subject alone is sufficient (very specific keywords)
    if subj_hit:
        return True
    # Sender + any body signal
    if sender_hit and (body_code or body_expiry):
        return True
    # Body code pattern + expiry = high confidence even without sender/subject
    if body_code and body_expiry:
        return True
    return False


def _is_financial_email(msg: email.message.Message, from_addr: str,
                        subject: str, body: str) -> bool:
    """Detect financial/banking emails using sender domains, subject, and body PII patterns."""
    sender_hit = bool(_FINANCIAL_SENDER_DOMAINS.search(from_addr))
    subj_hit = bool(_FINANCIAL_SUBJECT_RE.search(subject))
    # Count body PII signals
    body_text = body[:3000]
    pii_hits = sum(bool(p.search(body_text)) for p in (
        _FINANCIAL_BODY_CC_RE, _FINANCIAL_BODY_SSN_RE,
        _FINANCIAL_BODY_ROUTING_RE, _FINANCIAL_BODY_ACCOUNT_RE,
    ))
    last4_hit = bool(_FINANCIAL_BODY_LAST4_RE.search(body_text))
    # Known financial sender = always skip (high confidence, these domains are customer-facing only)
    if sender_hit:
        return True
    # Financial subject keywords = always skip
    if subj_hit:
        return True
    # Body contains actual financial PII (CC#, SSN, routing#, account#)
    if pii_hits >= 1:
        return True
    # Last-4 pattern alone isn't enough (could be order numbers etc.)
    return False


def _fetch_thread_context(imap, msg: email.message.Message, current_num: bytes) -> list[dict]:
    """Fetch prior messages in the same email thread via In-Reply-To / References headers.

    Uses the existing readonly IMAP connection. Returns a list of dicts
    [{"from": str, "date": str, "body": str}, ...] sorted oldest-first.
    Returns an empty list if no thread is found or on error.
    """
    # Collect all message-ids referenced in this thread
    references_raw = msg.get("References", "")
    in_reply_to = msg.get("In-Reply-To", "")

    # Parse message-ids from References header (space-separated)
    ref_ids = re.findall(r"<[^>]+>", references_raw)
    if in_reply_to and in_reply_to not in ref_ids:
        ref_ids.append(in_reply_to)

    if not ref_ids:
        return []

    thread_messages = []
    seen_nums = {current_num}

    for ref_id in ref_ids[:MAX_THREAD_MESSAGES]:
        try:
            # Search by Message-ID header
            clean_id = ref_id.strip("<>")
            status, data = imap.search(None, f'HEADER Message-ID "<{clean_id}>"')
            if status != "OK" or not data[0]:
                continue

            for num in data[0].split():
                if num in seen_nums:
                    continue
                seen_nums.add(num)

                status2, msg_data = imap.fetch(num, "(BODY.PEEK[])")
                if status2 != "OK" or not msg_data or not msg_data[0]:
                    continue

                thread_msg = email.message_from_bytes(msg_data[0][1])
                thread_from = email.utils.parseaddr(thread_msg.get("From", ""))[1]
                thread_date = thread_msg.get("Date", "")
                thread_body = _extract_body(thread_msg)

                # Run injection guard on thread content
                try:
                    from injection_guard import scan_and_filter
                    thread_body = scan_and_filter(thread_body, source="email")
                except Exception:
                    pass

                thread_messages.append({
                    "from": thread_from,
                    "date": thread_date,
                    "body": thread_body[:MAX_THREAD_BODY_CHARS],
                })

                if len(thread_messages) >= MAX_THREAD_MESSAGES:
                    break

        except Exception as e:
            logger.debug(f"Thread fetch error for {ref_id}: {e}")
            continue

        if len(thread_messages) >= MAX_THREAD_MESSAGES:
            break

    # Sort by date (oldest first) for chronological context
    # Parse dates best-effort; fall back to original order
    def _parse_date(d):
        try:
            return email.utils.parsedate_to_datetime(d["date"])
        except Exception:
            import datetime as _dt
            return _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)

    thread_messages.sort(key=_parse_date)
    return thread_messages


async def _classify_spam(from_addr: str, subject: str, body: str,
                         message_id: str = "") -> dict:
    """Classify an email as spam/phishing or legitimate using AI.

    Returns {"is_spam": bool, "confidence": float, "reason": str}.
    Auto-classifies as spam if injection is detected in the content.
    Defaults to legitimate on parse errors or ambiguity.
    """
    # Pre-check: if injection guard already replaced content, treat as spam
    if "[CONTENT BLOCKED]" in subject or "[CONTENT BLOCKED]" in body:
        return {"is_spam": True, "confidence": 0.95,
                "reason": "injection/manipulation detected in email content"}

    truncated_body = body[:MAX_BODY_CHARS] if body else ""

    system_text = (
        "You are an email spam and phishing classifier. Analyze the email and respond "
        "with ONLY a JSON object, no other text. Format: "
        '{"classification": "spam"|"phishing"|"legitimate", "confidence": 0.0-1.0, "reason": "brief explanation"}\n\n'
        "Classification criteria:\n"
        "PHISHING signals: urgency tactics, credential/password/payment requests, suspicious links, "
        "sender impersonation, mismatched display name vs address, threats of account closure.\n"
        "SPAM signals: unsolicited commercial offers, mass-marketing language, excessive promotions, "
        "too-good-to-be-true offers, unknown sender with sales pitch.\n"
        "LEGITIMATE signals: expected correspondence, personal context, known sender patterns, "
        "professional tone from real organizations, replies to previous threads.\n\n"
        "When uncertain, ALWAYS classify as legitimate. False negatives are safer than false positives."
    )

    prompt = (
        f"Classify this email:\n\n"
        f"From: {from_addr}\n"
        f"Subject: {subject}\n\n"
        f"Body:\n{truncated_body}"
    )

    response = await _ai_call(prompt, system_text=system_text, timeout=30,
                              email_id=message_id)

    # Extract JSON from response (model may wrap in markdown code block)
    json_str = response.strip()
    if json_str.startswith("```"):
        # Strip markdown code fences
        lines = json_str.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        json_str = "\n".join(lines).strip()

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning(f"Spam classifier returned non-JSON: {response[:100]}")
        return {"is_spam": False, "confidence": 0.0, "reason": "classification parse error"}

    classification = parsed.get("classification", "legitimate").lower()
    confidence = float(parsed.get("confidence", 0.0))
    reason = str(parsed.get("reason", ""))[:200]

    return {
        "is_spam": classification in ("spam", "phishing"),
        "confidence": confidence,
        "reason": reason,
    }


def _imap_rewrite_subject(raw_bytes: bytes, new_subject: str, original_msg_num: bytes) -> bool:
    """Rewrite an email's subject via IMAP copy-and-replace.

    Opens a separate read-write IMAP connection. Parses the original message,
    replaces the Subject header, APPENDs the modified copy back to INBOX,
    then DELETEs and EXPUNGEs the original. Preserves all other headers,
    body, attachments, and MIME structure.

    Returns True on success, False on failure.
    """
    # Idempotency guard
    try:
        orig_msg = email.message_from_bytes(raw_bytes)
        orig_subject = _parse_header(orig_msg.get("Subject", ""))
        if orig_subject.upper().startswith("SPAM:"):
            return True
    except Exception:
        pass

    imap = None
    try:
        imap = _imap_connect()
        status, _ = imap.select("INBOX")  # read-write
        if status != "OK":
            logger.warning("IMAP rewrite: could not open INBOX read-write")
            return False

        # Parse and modify subject
        msg = email.message_from_bytes(raw_bytes)
        del msg["Subject"]
        msg["Subject"] = new_subject

        # Fetch original flags (exclude \Recent and \Deleted)
        status, flag_data = imap.fetch(original_msg_num, "(FLAGS)")
        flags_str = ""
        if status == "OK" and flag_data and flag_data[0]:
            import re as _re
            m = _re.search(rb"\(([^)]*)\)", flag_data[0] if isinstance(flag_data[0], bytes) else flag_data[0][0] if flag_data[0] else b"")
            if m:
                raw_flags = m.group(1).decode("utf-8", errors="replace")
                cleaned = [f for f in raw_flags.split() if f not in ("\\Recent", "\\Deleted")]
                flags_str = " ".join(cleaned)

        # APPEND modified copy
        status, resp = imap.append(
            "INBOX",
            flags_str if flags_str else None,
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        if status != "OK":
            logger.warning(f"IMAP rewrite: APPEND failed: {resp}")
            return False

        # DELETE original
        imap.store(original_msg_num, "+FLAGS", "\\Deleted")
        imap.expunge()

        logger.info(f"IMAP subject rewritten to: {new_subject[:60]}")
        return True

    except Exception as e:
        logger.warning(f"IMAP rewrite failed: {e}")
        return False
    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass


def _imap_move_to_folder(original_msg_num: bytes, folder: str) -> bool:
    """Move an email to a specified IMAP folder (e.g. '[Gmail]/Trash', '[Gmail]/Spam').

    Opens a separate read-write IMAP connection. Copies the message to the
    target folder, then deletes the original from INBOX.

    Returns True on success, False on failure.
    """
    imap = None
    try:
        imap = _imap_connect()
        status, _ = imap.select("INBOX")  # read-write
        if status != "OK":
            logger.warning(f"IMAP move: could not open INBOX read-write")
            return False

        # COPY to target folder
        status, resp = imap.copy(original_msg_num, folder)
        if status != "OK":
            logger.warning(f"IMAP move: COPY to {folder} failed: {resp}")
            return False

        # DELETE original from INBOX
        imap.store(original_msg_num, "+FLAGS", "\\Deleted")
        imap.expunge()

        logger.info(f"IMAP: moved message to {folder}")
        return True

    except Exception as e:
        logger.warning(f"IMAP move to {folder} failed: {e}")
        return False
    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass


def _matches_patterns(value: str, patterns: list[str]) -> str | None:
    """Check if value matches any wildcard pattern. Returns matched pattern or None."""
    val_lower = value.lower()
    for pattern in patterns:
        p = pattern.strip().lower().replace("*", ".*")
        if not p:
            continue
        try:
            if re.search(p, val_lower):
                return pattern
        except re.error:
            continue
    return None


def _check_filters(msg: email.message.Message, from_addr: str, subject: str,
                   body: str, my_email: str, filters: list[dict]) -> tuple[str, str] | None:
    """Run all enabled filters against a message.

    Returns (action, reason) if the message should be skipped, or None to proceed.
    The action string is used for history recording.
    """
    filters_by_id = {f["id"]: f for f in filters}

    # --- Builtin filters ---

    f = filters_by_id.get("automated_senders", {})
    if f.get("enabled", True) and _is_automated_sender(msg):
        return ("skipped_auto", "automated sender")

    f = filters_by_id.get("bulk_mail", {})
    if f.get("enabled", True) and _is_bulk_mail(msg):
        return ("skipped_auto", "bulk/mailing list")

    f = filters_by_id.get("empty_body", {})
    if f.get("enabled", True) and (not body or len(body.strip()) < 10):
        return ("skipped_empty", "empty body")

    f = filters_by_id.get("from_self", {})
    if f.get("enabled", True) and from_addr == my_email.lower():
        return ("skipped_auto", "from self")

    f = filters_by_id.get("cc_only", {})
    if f.get("enabled", True) and _is_cc_only(msg, my_email):
        return ("skipped_auto", "CC only (not in To)")

    # --- Sensitive content filters ---

    f = filters_by_id.get("security_codes", {})
    if f.get("enabled", True) and _is_2fa_email(msg, from_addr, subject, body):
        return ("skipped_sensitive", "2FA/verification email")

    f = filters_by_id.get("financial_content", {})
    if f.get("enabled", True) and _is_financial_email(msg, from_addr, subject, body):
        return ("skipped_sensitive", "financial/banking email")

    # --- Exclusion pattern filters ---

    f = filters_by_id.get("exclude_senders", {})
    if f.get("enabled", True):
        matched = _matches_patterns(from_addr, f.get("patterns", []))
        if matched:
            return ("skipped_excluded", f"excluded sender: {matched}")

    f = filters_by_id.get("exclude_subjects", {})
    if f.get("enabled", True):
        matched = _matches_patterns(subject, f.get("patterns", []))
        if matched:
            return ("skipped_excluded", f"excluded subject: {matched}")

    return None


# ---------------------------------------------------------------------------
# AI call — routes through the app's /api/chat endpoint so it uses
# whichever provider is actually connected (Claude Code, Anthropic API,
# OpenRouter, etc.) — same routing as the chat pane and new-worker tabs.
# ---------------------------------------------------------------------------

def _resolve_drafter_session_id(email_id: str = "", force_model_key: str = "") -> tuple[str, str]:
    """Pick a session ID that routes to the best available model.

    Args:
        email_id: Message-ID of the email being processed. Used for fresh-session hashing.
        force_model_key: Override model key (from config or caller).

    Returns:
        (session_id, model_key) tuple.

    Uses the same provider-detection logic the main app uses:
      - User-configured model  → tab-{model_key}-drafter
      - Claude Code bridge up  → tab-claude_sonnet-drafter
      - Anthropic API key      → tab-anthropic-drafter
      - Fallback               → tab-codex-drafter (OpenAI)
    """
    # 1. Resolve model key: caller override > config > auto-detect
    model_key = (force_model_key or "").strip()
    if not model_key:
        model_key = _get_config("drafter.model_key", "").strip()
    if not model_key:
        # Auto-detect: Claude Code bridge > Anthropic API > Codex
        try:
            from claude_bridge import get_claude_pool
            pool = get_claude_pool()
            if pool is not None:
                model_key = "claude_sonnet"
        except Exception:
            pass
        if not model_key and _get_api_key():
            model_key = "anthropic"
        if not model_key:
            model_key = "codex"

    # 2. Build session ID
    fresh = _get_config("drafter.fresh_session_per_email", "0") == "1"
    if fresh and email_id:
        import hashlib
        h = hashlib.sha256(email_id.encode()).hexdigest()[:8]
        session_id = f"tab-{model_key}-drf{h}"
    else:
        session_id = f"tab-{model_key}-drafter"

    return session_id, model_key


async def _ai_call(prompt: str, system_text: str = "", timeout: int = 120,
                   email_id: str = "", model_key: str = "") -> str:
    """AI call routed through the app's /api/chat endpoint.

    Sends a message to /api/chat, consumes the SSE stream, and returns
    the concatenated response text. Uses whichever AI provider is connected.

    Args:
        email_id: Message-ID for fresh-session hashing.
        model_key: Override model key for this call.
    """
    import httpx

    from config import PORT

    session_id, resolved_model = _resolve_drafter_session_id(email_id, model_key)
    logger.debug(f"AI call → session={session_id} model={resolved_model}")
    # Prepend system context to the prompt if provided
    full_prompt = prompt
    if system_text:
        full_prompt = f"[System: {system_text}]\n\n{prompt}"

    url = f"https://localhost:{PORT}/api/chat"
    payload = {"message": full_prompt, "session_id": session_id, "_internal": True}

    collected_text = []
    try:
        async with httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(timeout, connect=10),
        ) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code == 409:
                    raise RuntimeError("AI backend busy — another request in progress on drafter session")
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(f"AI call failed (HTTP {resp.status_code}): {body.decode('utf-8', errors='replace')[:200]}")

                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    # Parse SSE frames
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        data_lines = []
                        for line in frame.split("\n"):
                            if line.startswith("data: "):
                                data_lines.append(line[6:])
                            elif line.startswith("data:"):
                                data_lines.append(line[5:])
                        if not data_lines:
                            continue
                        try:
                            evt = json.loads("\n".join(data_lines))
                            if evt.get("type") in ("text", "chunk"):
                                collected_text.append(evt.get("text", ""))
                            if evt.get("type") == "error":
                                raise RuntimeError(f"AI error: {evt.get('text', 'unknown')}")
                            if evt.get("type") == "done":
                                break
                        except json.JSONDecodeError:
                            continue
    except httpx.TimeoutException:
        raise RuntimeError(f"AI call timed out after {timeout}s")
    except httpx.ConnectError:
        raise RuntimeError("Could not connect to local server for AI call")

    text = "".join(collected_text).strip()
    if not text or len(text) < 5:
        raise RuntimeError("AI returned empty response")
    return text


# ---------------------------------------------------------------------------
# Phase 1: Style Profile Builder
# ---------------------------------------------------------------------------

async def build_style_profile(force: bool = False) -> str:
    """Read sent emails and generate a writing style profile via AI.

    Returns the profile text. Caches to STYLE_PROFILE_PATH.
    """
    if not force and STYLE_PROFILE_PATH.exists():
        age_days = (time.time() - STYLE_PROFILE_PATH.stat().st_mtime) / 86400
        if age_days < PROFILE_MAX_AGE_DAYS:
            logger.info(f"Style profile is fresh ({age_days:.1f}d), skipping rebuild")
            return STYLE_PROFILE_PATH.read_text(encoding="utf-8")

    from gmail_bridge import check_permission
    check_permission("read_sent")

    logger.info(f"Building style profile from last {MAX_SENT_FOR_PROFILE} sent emails (fetching only recent)...")

    imap = _imap_connect()
    sent_bodies = []

    try:
        status, _ = imap.select('"[Gmail]/Sent Mail"', readonly=True)
        if status != "OK":
            raise RuntimeError("Could not open Sent Mail folder")

        status, data = imap.search(None, "ALL")
        if status != "OK" or not data[0]:
            raise RuntimeError("No sent emails found")

        msg_nums = data[0].split()
        logger.info(f"Found {len(msg_nums)} sent emails total")

        recent = msg_nums[-MAX_SENT_FOR_PROFILE:]
        recent.reverse()

        for num in recent:
            try:
                status, msg_data = imap.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                body = _extract_body(msg)
                subject = _parse_header(msg.get("Subject", ""))
                to_addr = msg.get("To", "")

                if body and len(body.strip()) > 20:
                    sent_bodies.append({
                        "to": to_addr[:100],
                        "subject": subject[:100],
                        "body": body[:500],
                    })
            except Exception:
                continue

        logger.info(f"Extracted {len(sent_bodies)} sent email bodies")
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    if len(sent_bodies) < 5:
        raise RuntimeError(f"Only found {len(sent_bodies)} sent emails — need at least 5")

    logger.info(f"Sending {len(sent_bodies)} emails to AI for style analysis...")

    sample_text = ""
    for i, s in enumerate(sent_bodies, 1):
        sample_text += f"\n--- Email {i} ---\n"
        sample_text += f"To: {s['to']}\nSubject: {s['subject']}\nBody:\n{s['body']}\n"

    prompt = f"""Analyze these {len(sent_bodies)} sent emails and produce a concise writing style profile.
Be very specific — quote actual phrases. This profile will be used to draft emails that sound exactly like the author.

Cover: tone, greetings, sign-offs, sentence style, common phrases, punctuation habits, typical response length.

{sample_text}

Output ONLY the style profile. No preamble, no commentary."""

    profile = await _ai_call(prompt, timeout=180)

    if len(profile.strip()) < 100:
        raise RuntimeError("AI returned a too-short style profile")

    STYLE_PROFILE_PATH.write_text(profile, encoding="utf-8")
    logger.info(f"Style profile saved ({len(profile)} chars)")

    state = _load_state()
    state["profile_built_at"] = int(time.time())
    _save_state(state)

    return profile


# ---------------------------------------------------------------------------
# Phase 2: Draft Generator
# ---------------------------------------------------------------------------

def _get_style_profile() -> str | None:
    """Load the style profile if it exists. Returns None if missing."""
    if STYLE_PROFILE_PATH.exists():
        return STYLE_PROFILE_PATH.read_text(encoding="utf-8")
    return None


async def _generate_draft_reply(style_profile: str, from_addr: str,
                                subject: str, body: str,
                                thread_context: list[dict] | None = None,
                                email_id: str = "", model_key: str = "") -> str:
    """Generate a draft reply using AI in the user's writing style."""
    # Build thread context section if available
    thread_section = ""
    if thread_context:
        thread_parts = []
        for i, tm in enumerate(thread_context, 1):
            thread_parts.append(
                f"--- Message {i} ---\n"
                f"From: {tm['from']}\n"
                f"Date: {tm['date']}\n"
                f"{tm['body']}"
            )
        thread_section = (
            "\n\nPRIOR THREAD CONTEXT (oldest first):\n"
            + "\n\n".join(thread_parts)
            + "\n\n--- End of thread context ---\n"
        )

    prompt = f"""Draft an email reply in the user's exact writing style.

STYLE PROFILE:
{style_profile}

RULES:
- Write ONLY the email body text. No Subject line, no metadata.
- Match the user's style exactly — tone, greetings, sign-offs, vocabulary.
- Address the content naturally. Keep length consistent with their patterns.
- No AI disclaimers. Write as if you ARE the user.
- If thread context is provided, use it to understand the full conversation and craft a contextually appropriate reply to the LATEST message.
{thread_section}
Reply to this email (LATEST message — reply to THIS one):

From: {from_addr}
Subject: {subject}
Body:
{body[:MAX_BODY_CHARS]}"""

    return await _ai_call(prompt, timeout=60, email_id=email_id, model_key=model_key)


async def generate_ai_reply(from_addr: str, subject: str, body: str,
                            message_id: str = "", model_key: str = "") -> dict:
    """Generate an AI reply for a single message on demand.

    Returns {ok, reply_text, subject} or {error}.
    Used by the inbox AI Reply button.
    """
    from gmail_bridge import check_permission

    check_permission("read_inbox")

    style_profile = _get_style_profile()
    if not style_profile:
        return {"error": "No style profile found. Build one in the Profile tab first."}

    reply_text = await _generate_draft_reply(
        style_profile=style_profile,
        from_addr=from_addr,
        subject=subject,
        body=body,
        email_id=message_id,
        model_key=model_key,
    )

    if not reply_text or not reply_text.strip():
        return {"error": "AI generated an empty reply. Try again."}

    re_subject = subject
    if not re.match(r"^Re:\s", subject, re.IGNORECASE):
        re_subject = f"Re: {subject}"

    return {"ok": True, "reply_text": reply_text.strip(), "subject": re_subject}


def _create_threaded_draft(from_email: str, original_from: str, subject: str,
                           body: str, message_id: str, signature_html: str = "") -> str | None:
    """Create a Gmail draft threaded as a reply. Returns the draft UID or None."""
    re_subject = subject
    if not re.match(r"^Re:\s", subject, re.IGNORECASE):
        re_subject = f"Re: {subject}"

    # Convert plain text to HTML
    body_html = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body_html = body_html.replace("\n", "<br>")

    html_parts = [f'<div dir="ltr"><div style="font-family:tahoma,sans-serif">{body_html}</div>']
    if signature_html:
        html_parts.append(f'<br clear="all"><div>{signature_html}</div>')
    html_parts.append('</div>')
    html_content = "".join(html_parts)

    msg = MIMEText(html_content, "html")
    msg["From"] = from_email
    msg["To"] = original_from
    msg["Subject"] = re_subject
    msg[X_DRAFTER_HEADER] = X_DRAFTER_VALUE
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id

    imap = _imap_connect()
    try:
        status, resp_data = imap.append(
            '"[Gmail]/Drafts"', "\\Draft",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        if status != "OK":
            logger.error(f"Failed to create draft: {status}")
            return None

        # Try to extract the UID from the APPENDUID response
        draft_uid = ""
        if resp_data and resp_data[0]:
            match = re.search(r"APPENDUID\s+\d+\s+(\d+)", resp_data[0].decode("utf-8", errors="replace"))
            if match:
                draft_uid = match.group(1)

        return draft_uid or "created"
    except Exception as e:
        logger.error(f"Failed to create draft: {e}")
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass


async def check_and_draft(dry_run: bool = False) -> dict:
    """Check inbox for new unread emails and draft replies.

    Returns summary dict with counts and details.
    """
    from gmail_bridge import check_permission

    # Verify permissions
    check_permission("read_inbox")
    check_permission("auto_draft")

    # Ensure history table exists
    _ensure_history_table()

    # Load style profile (must be pre-built via Profile tab)
    style_profile = _get_style_profile()
    if not style_profile:
        raise RuntimeError("No writing style profile — go to Profile tab and click 'Build Now' first")

    state = _load_state()
    processed_ids = set(state.get("processed_ids", []))
    from_email = _get_config("gmail.email", "")
    signature_html = _get_config("drafter.signature_html", DEFAULT_SIGNATURE_HTML)
    filters = _get_filters()

    max_per_run = int(_get_config("drafter.max_per_run", str(MAX_EMAILS_PER_RUN)))

    logger.info("Checking inbox for new unread emails...")

    imap = _imap_connect()
    results = {"drafted": 0, "skipped": 0, "spam": 0, "errors": 0, "details": []}

    try:
        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Could not open INBOX")

        status, data = imap.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            logger.info("No unread emails found")
            state["last_run_at"] = int(time.time())
            _save_state(state)
            _set_config("drafter.last_run_at", str(int(time.time())))
            _set_config("drafter.last_drafts_count", "0")
            return results

        msg_nums = data[0].split()
        logger.info(f"Found {len(msg_nums)} unread emails")

        msg_nums.reverse()
        msg_nums = msg_nums[:max_per_run]

        for num in msg_nums:
            try:
                status, msg_data = imap.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                message_id = msg.get("Message-ID", "")
                if not message_id:
                    message_id = f"<unknown-{num.decode()}@generated>"

                if message_id in processed_ids:
                    continue

                original_from = msg.get("From", "")
                from_addr_parsed = email.utils.parseaddr(original_from)[1].lower()
                subject = _parse_header(msg.get("Subject", "(no subject)"))
                body = _extract_body(msg)

                # Injection guard — scan subject + body before passing to AI
                try:
                    from injection_guard import scan_and_filter
                    subject = scan_and_filter(subject, source="email_subject")
                    body = scan_and_filter(body, source="email")
                except Exception as e:
                    logger.warning(f"Injection guard failed for {message_id}: {e}")

                # Run all enabled filters
                skip = _check_filters(msg, from_addr_parsed, subject, body, from_email, filters)
                if skip:
                    action, reason = skip
                    results["skipped"] += 1
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "skipped", "reason": reason,
                    })
                    _record_history(message_id, from_addr_parsed, subject, action, reason)
                    processed_ids.add(message_id)
                    continue

                # Spam/phishing detection (if enabled)
                spam_filter = next(
                    (f for f in filters if f["id"] == "spam_detection" and f.get("enabled", True)),
                    None,
                )
                if spam_filter:
                    try:
                        threshold = spam_filter.get("confidence_threshold", 0.6)
                        classification = await _classify_spam(from_addr_parsed, subject, body,
                                                               message_id=message_id)
                        if classification["is_spam"] and classification["confidence"] >= threshold:
                            spam_action = spam_filter.get("spam_action", "label")
                            action_desc = spam_action
                            if not dry_run:
                                if spam_action == "trash":
                                    _imap_move_to_folder(num, "[Gmail]/Trash")
                                    action_desc = "moved to Trash"
                                elif spam_action == "spam":
                                    _imap_move_to_folder(num, "[Gmail]/Spam")
                                    action_desc = "moved to Spam"
                                else:  # "label" — default: prepend SPAM: to subject
                                    new_subject = subject if subject.upper().startswith("SPAM:") else f"SPAM: {subject}"
                                    _imap_rewrite_subject(raw, new_subject, num)
                                    action_desc = "labeled SPAM:"
                            results["spam"] += 1
                            reason = f"spam detected ({classification['confidence']:.0%}, {action_desc}): {classification['reason']}"
                            results["details"].append({
                                "from": from_addr_parsed, "subject": subject[:80],
                                "action": "spam_detected", "reason": reason,
                            })
                            _record_history(message_id, from_addr_parsed, subject,
                                            "spam_detected", reason)
                            processed_ids.add(message_id)
                            continue
                    except Exception as e:
                        logger.warning(f"Spam classification failed for {subject[:40]}: {e}")
                        # Fail open — proceed with normal drafting

                logger.info(f"  Drafting reply to: {subject[:60]} (from {from_addr_parsed})")

                if dry_run:
                    results["drafted"] += 1
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "would_draft", "reason": "",
                    })
                    processed_ids.add(message_id)
                    continue

                # Fetch thread context if enabled
                thread_ctx = None
                thread_mode = _get_config("drafter.thread_context", "full_thread")
                if thread_mode == "full_thread":
                    try:
                        thread_ctx = _fetch_thread_context(imap, msg, num)
                        if thread_ctx:
                            logger.info(f"    Thread context: {len(thread_ctx)} prior message(s)")
                    except Exception as e:
                        logger.warning(f"Thread context fetch failed: {e}")

                # Generate draft
                draft_body = await _generate_draft_reply(
                    style_profile, original_from, subject, body,
                    thread_context=thread_ctx,
                    email_id=message_id,
                )

                if not draft_body or len(draft_body.strip()) < 5:
                    results["errors"] += 1
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "error", "reason": "AI returned empty draft",
                    })
                    _record_history(message_id, from_addr_parsed, subject,
                                    "error", "AI returned empty draft")
                    processed_ids.add(message_id)
                    continue

                # Sanitize draft content
                from email_sanitize import preflight_email
                passed, findings = preflight_email(subject, draft_body)
                if not passed:
                    results["errors"] += 1
                    reason = f"content blocked: {len(findings)} sensitive item(s)"
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "error", "reason": reason,
                    })
                    _record_history(message_id, from_addr_parsed, subject, "error", reason)
                    processed_ids.add(message_id)
                    continue

                # Create threaded draft
                draft_uid = _create_threaded_draft(
                    from_email, original_from, subject,
                    draft_body, message_id, signature_html,
                )

                if draft_uid:
                    results["drafted"] += 1
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "drafted", "reason": "",
                        "preview": draft_body[:200],
                    })
                    _record_history(message_id, from_addr_parsed, subject,
                                    "drafted", "", draft_body[:300], draft_uid)
                    logger.info(f"  Draft created successfully")
                else:
                    results["errors"] += 1
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "error", "reason": "IMAP append failed",
                    })
                    _record_history(message_id, from_addr_parsed, subject,
                                    "error", "IMAP append failed")

                processed_ids.add(message_id)

            except Exception as e:
                results["errors"] += 1
                logger.warning(f"  Error processing email {num}: {e}")
                continue

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    # Save state
    if not dry_run:
        state["last_run_at"] = int(time.time())
        state["processed_ids"] = list(processed_ids)
        _save_state(state)
        _set_config("drafter.last_run_at", str(int(time.time())))
        _set_config("drafter.last_drafts_count", str(results["drafted"]))

    logger.info(f"Done — {results['drafted']} drafted, {results['skipped']} skipped, {results['spam']} spam, {results['errors']} errors")
    return results


# ---------------------------------------------------------------------------
# Draft management (list / send / discard)
# ---------------------------------------------------------------------------

def list_drafts() -> list[dict]:
    """List ALL Gmail drafts (both AI-generated and manual).

    Uses two-phase IMAP fetch:
      Phase A — headers only to identify AI drafts (X-KukuiBot-Draft header)
      Phase B — full body for all drafts
    """
    from gmail_bridge import check_permission, _extract_body_both
    check_permission("read_inbox")

    imap = _imap_connect()
    drafts = []

    try:
        status, _ = imap.select('"[Gmail]/Drafts"', readonly=True)
        if status != "OK":
            return []

        status, data = imap.uid('search', None, "ALL")
        if status != "OK" or not data[0]:
            return []

        uids = data[0].split()
        uids.reverse()  # newest first

        # Phase A: fetch headers only, identify AI drafts
        ai_draft_uids = set()
        for uid in uids[:50]:  # cap at 50
            try:
                status2, hdr_data = imap.uid('fetch', uid, "(BODY.PEEK[HEADER])")
                if status2 != "OK" or not hdr_data or not hdr_data[0]:
                    continue
                hdr_msg = email.message_from_bytes(hdr_data[0][1])
                if hdr_msg.get(X_DRAFTER_HEADER) == X_DRAFTER_VALUE:
                    ai_draft_uids.add(uid)
            except Exception as e:
                logger.warning(f"Error reading draft header UID {uid}: {e}")
                continue

        # Phase B: fetch full body for ALL drafts
        for uid in uids[:50]:
            try:
                status2, msg_data = imap.uid('fetch', uid, "(BODY.PEEK[])")
                if status2 != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                subject = _parse_header(msg.get("Subject", "(no subject)"))
                to_addr = msg.get("To", "")
                in_reply_to = msg.get("In-Reply-To", "")

                # Extract both plain + HTML body
                body_plain, body_html = _extract_body_both(msg)

                # Build snippet: first 120 chars of plain body
                snippet = ""
                if body_plain:
                    snippet = body_plain.replace("\n", " ").replace("\r", " ").strip()[:120]

                # Parse display name from To header
                from_name = ""
                if to_addr:
                    m = re.match(r'^"?([^"<]+?)"?\s*<', to_addr)
                    if m:
                        from_name = m.group(1).strip()
                    else:
                        # Fallback to email local part
                        addr_part = email.utils.parseaddr(to_addr)[1]
                        from_name = addr_part.split("@")[0] if addr_part else to_addr

                drafts.append({
                    "uid": uid.decode(),
                    "to": to_addr,
                    "subject": subject,
                    "body": body_plain,
                    "body_html": body_html,
                    "snippet": snippet,
                    "from_name": from_name,
                    "in_reply_to": in_reply_to,
                    "date": msg.get("Date", ""),
                    "is_ai_draft": uid in ai_draft_uids,
                })
            except Exception as e:
                logger.warning(f"Error reading draft UID {uid}: {e}")
                continue

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return drafts


def get_original_for_draft(uid: str, message_id_hint: str = "") -> dict:
    """Fetch the original email that a draft is replying to, via In-Reply-To header.

    Checks the local SQLite cache first for instant results; falls back to IMAP
    only when the cache doesn't have the message body.

    Args:
        uid: Draft UID in [Gmail]/Drafts.
        message_id_hint: Optional In-Reply-To value from the client (avoids
            an IMAP round-trip to read the draft headers).

    Returns {"ok": True, "from": ..., "to": ..., "subject": ..., "date": ..., "body": ..., "body_html": ...}
    or {"ok": False, "error": "not_reply"} / {"ok": False, "not_found": True}.
    """
    from gmail_bridge import check_permission, _extract_body_both
    check_permission("read_inbox")

    # --- Resolve the In-Reply-To message ID ---
    original_message_id = (message_id_hint or "").strip()

    if not original_message_id:
        # Check if the draft is in the local cache with in_reply_to info
        # Fall back to IMAP to read just the draft header
        imap = _imap_connect()
        try:
            status, _ = imap.select('"[Gmail]/Drafts"', readonly=True)
            if status != "OK":
                return {"ok": False, "error": "Could not open Drafts folder"}
            status, msg_data = imap.uid('fetch', uid.encode(), "(BODY.PEEK[HEADER.FIELDS (In-Reply-To)])")
            if status != "OK" or not msg_data or not msg_data[0]:
                return {"ok": False, "error": f"Draft {uid} not found"}
            hdr_msg = email.message_from_bytes(msg_data[0][1])
            original_message_id = (hdr_msg.get("In-Reply-To") or "").strip()
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    if not original_message_id:
        return {"ok": False, "error": "not_reply"}

    # --- Try local SQLite cache first (instant) ---
    try:
        import email_cache
        db = email_cache._get_db()
        try:
            row = db.execute(
                "SELECT * FROM messages WHERE message_id = ? AND body_text IS NOT NULL AND body_text != '' LIMIT 1",
                (original_message_id,),
            ).fetchone()
            if row:
                d = email_cache._row_to_dict(row)
                logger.debug(f"Original for draft {uid} served from cache (message_id={original_message_id[:40]})")
                return {
                    "ok": True,
                    "from": d.get("from_addr", ""),
                    "to": d.get("to_addr", ""),
                    "subject": d.get("subject", ""),
                    "date": d.get("date", ""),
                    "body": d.get("body_text", ""),
                    "body_html": d.get("body_html"),
                }
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Cache lookup failed for original: {e}")

    # --- Cache miss — fall back to IMAP ---
    clean_id = original_message_id.strip("<>")
    search_folders = ["INBOX", '"[Gmail]/All Mail"', '"[Gmail]/Sent Mail"']

    imap = _imap_connect()
    try:
        for folder in search_folders:
            try:
                status, _ = imap.select(folder, readonly=True)
                if status != "OK":
                    continue
                status, data = imap.uid('search', None, f'HEADER Message-ID "<{clean_id}>"')
                if status != "OK" or not data[0]:
                    continue

                orig_uid = data[0].split()[0]
                status, orig_data = imap.uid('fetch', orig_uid, "(BODY.PEEK[])")
                if status != "OK" or not orig_data or not orig_data[0]:
                    continue

                orig_msg = email.message_from_bytes(orig_data[0][1])
                orig_from = orig_msg.get("From", "")
                orig_to = orig_msg.get("To", "")
                orig_subject = _parse_header(orig_msg.get("Subject", ""))
                orig_date = orig_msg.get("Date", "")
                orig_plain, orig_html = _extract_body_both(orig_msg)

                return {
                    "ok": True,
                    "from": orig_from,
                    "to": orig_to,
                    "subject": orig_subject,
                    "date": orig_date,
                    "body": orig_plain,
                    "body_html": orig_html,
                }
            except Exception as e:
                logger.debug(f"Original search error in {folder}: {e}")
                continue

        return {"ok": False, "not_found": True}

    finally:
        try:
            imap.logout()
        except Exception:
            pass


def update_draft(uid: str, body_html: str) -> dict:
    """Update a draft's body HTML in-place (IMAP has no edit; delete + append).

    Returns {"ok": True, "new_uid": "...", "old_uid": uid}.
    Works on both AI-generated and manual drafts.
    """
    from gmail_bridge import check_permission
    check_permission("create_drafts")

    imap = _imap_connect()
    try:
        status, _ = imap.select('"[Gmail]/Drafts"')  # read-write
        if status != "OK":
            raise RuntimeError("Could not open Drafts folder")

        status, msg_data = imap.uid('fetch', uid.encode(), "(BODY.PEEK[])")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"Draft {uid} not found")

        raw = msg_data[0][1]
        old_msg = email.message_from_bytes(raw)

        # Build new message preserving all original headers
        new_msg = MIMEText(body_html, "html")
        for hdr in ("From", "To", "Subject", "In-Reply-To", "References",
                     X_DRAFTER_HEADER, "Date", "Cc", "Bcc"):
            val = old_msg.get(hdr)
            if val:
                new_msg[hdr] = val

        # APPEND new draft
        status, resp_data = imap.append(
            '"[Gmail]/Drafts"', "\\Draft",
            imaplib.Time2Internaldate(time.time()),
            new_msg.as_bytes(),
        )
        if status != "OK":
            raise RuntimeError(f"APPEND failed: {resp_data}")

        # Extract new UID from APPENDUID response
        new_uid = ""
        if resp_data and resp_data[0]:
            match = re.search(r"APPENDUID\s+\d+\s+(\d+)",
                              resp_data[0].decode("utf-8", errors="replace"))
            if match:
                new_uid = match.group(1)

        # DELETE old draft
        imap.uid('store', uid.encode(), "+FLAGS", "\\Deleted")
        imap.expunge()

        logger.info(f"Draft updated: old_uid={uid} new_uid={new_uid}")
        return {"ok": True, "new_uid": new_uid or "unknown", "old_uid": uid}

    finally:
        try:
            imap.logout()
        except Exception:
            pass


def send_draft(uid: str) -> dict:
    """Send a draft email by UID. Reads from Drafts, sends via SMTP, trashes draft.

    Works on both AI-generated and manual drafts.
    """
    from gmail_bridge import check_permission, _get_config as gmail_config
    import smtplib

    check_permission("read_inbox")

    # Need at least one send permission
    from gmail_bridge import get_permissions
    perms = get_permissions()
    has_send_perm = (perms.get("send_owner_only") or perms.get("send_within_org")
                     or perms.get("send_anyone") or perms.get("manual_send"))
    if not has_send_perm:
        raise PermissionError("No send permission enabled — enable one in Settings > Gmail")

    email_addr = gmail_config("gmail.email", "")
    app_password = gmail_config("gmail.app_password", "")
    if not email_addr or not app_password:
        raise RuntimeError("Gmail credentials not configured")

    # Read the draft
    imap = _imap_connect()
    try:
        status, _ = imap.select('"[Gmail]/Drafts"')
        if status != "OK":
            raise RuntimeError("Could not open Drafts folder")

        status, msg_data = imap.uid('fetch', uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"Draft {uid} not found")

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        to_addr = msg.get("To", "")
        subject = _parse_header(msg.get("Subject", ""))

        if not to_addr:
            raise ValueError("Draft has no recipient")

        # Enforce send permissions
        to_lower = email.utils.parseaddr(to_addr)[1].lower()
        from gmail_bridge import _get_owner_emails
        owner_emails = _get_owner_emails()

        if perms.get("send_anyone"):
            pass
        elif perms.get("manual_send"):
            pass  # User manually sending a draft — no recipient restrictions
        elif perms.get("send_within_org"):
            owner_domain = email_addr.split('@')[-1] if '@' in email_addr else None
            to_domain = to_lower.split('@')[-1] if '@' in to_lower else None
            if not owner_domain or not to_domain or to_domain != owner_domain:
                raise PermissionError(f"Can only send within @{owner_domain}")
        elif perms.get("send_owner_only"):
            if to_lower not in owner_emails:
                raise PermissionError(f"Can only send to owner emails: {', '.join(sorted(owner_emails))}")

        # Send via SMTP
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(email_addr, app_password)
            server.sendmail(email_addr, [to_lower], raw)

        # Trash the draft
        imap.uid('store', uid.encode(), "+FLAGS", "\\Deleted")
        imap.expunge()

        logger.info(f"Draft sent: to={to_lower} subject={subject[:50]}")
        return {"ok": True, "to": to_addr, "subject": subject}

    finally:
        try:
            imap.logout()
        except Exception:
            pass


def discard_draft(uid: str) -> dict:
    """Delete a draft by UID. Works on both AI-generated and manual drafts."""
    from gmail_bridge import check_permission
    check_permission("read_inbox")

    imap = _imap_connect()
    try:
        status, _ = imap.select('"[Gmail]/Drafts"')
        if status != "OK":
            raise RuntimeError("Could not open Drafts folder")

        # Verify draft exists before deleting
        status, msg_data = imap.uid('fetch', uid.encode(), "(BODY.PEEK[HEADER])")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"Draft {uid} not found")

        imap.uid('store', uid.encode(), "+FLAGS", "\\Deleted")
        imap.expunge()

        logger.info(f"Draft discarded: uid={uid}")
        return {"ok": True, "uid": uid}

    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def get_status() -> dict:
    """Return drafter status for the UI."""
    from gmail_bridge import get_gmail_status

    gmail = get_gmail_status()
    state = _load_state()

    profile_exists = STYLE_PROFILE_PATH.exists()
    profile_age_days = None
    profile_size = 0
    if profile_exists:
        profile_age_days = round((time.time() - STYLE_PROFILE_PATH.stat().st_mtime) / 86400, 1)
        profile_size = len(STYLE_PROFILE_PATH.read_text(encoding="utf-8"))

    enabled = _get_config("drafter.enabled", "0") == "1"
    has_api_key = _has_ai_backend()
    has_auto_draft_perm = gmail.get("permissions", {}).get("auto_draft", False)

    return {
        "enabled": enabled,
        "gmail_connected": gmail.get("connected", False),
        "gmail_email": gmail.get("email", ""),
        "has_api_key": has_api_key,
        "has_auto_draft_perm": has_auto_draft_perm,
        "profile_exists": profile_exists,
        "profile_age_days": profile_age_days,
        "profile_size": profile_size,
        "profile_fresh": profile_exists and profile_age_days is not None and profile_age_days < PROFILE_MAX_AGE_DAYS,
        "last_run_at": state.get("last_run_at", 0),
        "last_drafts_count": int(_get_config("drafter.last_drafts_count", "0")),
        "check_interval_min": int(_get_config("drafter.check_interval_min", "15")),
        "max_per_run": int(_get_config("drafter.max_per_run", str(MAX_EMAILS_PER_RUN))),
        "processed_count": len(state.get("processed_ids", [])),
    }


def get_config_dict() -> dict:
    """Return drafter configuration."""
    return {
        "enabled": _get_config("drafter.enabled", "0") == "1",
        "check_interval_min": int(_get_config("drafter.check_interval_min", "15")),
        "max_per_run": int(_get_config("drafter.max_per_run", str(MAX_EMAILS_PER_RUN))),
        "signature_html": _get_config("drafter.signature_html", DEFAULT_SIGNATURE_HTML),
        "thread_context": _get_config("drafter.thread_context", "full_thread"),
        "sync_interval_sec": int(_get_config("gmail.sync_interval_sec", "180")),
        "fresh_session_per_email": _get_config("drafter.fresh_session_per_email", "0") == "1",
        "model_key": _get_config("drafter.model_key", ""),
        "filters": _get_filters(),
    }


def save_config_dict(cfg: dict):
    """Save drafter configuration."""
    if "enabled" in cfg:
        _set_config("drafter.enabled", "1" if cfg["enabled"] else "0")
    if "check_interval_min" in cfg:
        val = max(5, min(60, int(cfg["check_interval_min"])))
        _set_config("drafter.check_interval_min", str(val))
    if "max_per_run" in cfg:
        val = max(1, min(50, int(cfg["max_per_run"])))
        _set_config("drafter.max_per_run", str(val))
    if "signature_html" in cfg:
        _set_config("drafter.signature_html", cfg["signature_html"])
    if "thread_context" in cfg:
        val = cfg["thread_context"] if cfg["thread_context"] in ("full_thread", "latest_only") else "full_thread"
        _set_config("drafter.thread_context", val)
    if "sync_interval_sec" in cfg:
        val = max(30, min(600, int(cfg["sync_interval_sec"])))
        _set_config("gmail.sync_interval_sec", str(val))
    if "fresh_session_per_email" in cfg:
        _set_config("drafter.fresh_session_per_email", "1" if cfg["fresh_session_per_email"] else "0")
    if "model_key" in cfg:
        _set_config("drafter.model_key", str(cfg["model_key"]).strip())
    if "filters" in cfg:
        _save_filters(cfg["filters"])


# Init history table and migrate old exclusions on import
_ensure_history_table()
_migrate_exclusions_to_filters()
