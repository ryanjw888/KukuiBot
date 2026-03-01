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

DRAFTER_MODEL = "claude-haiku-4-5-20251001"

MAX_EMAILS_PER_RUN = 10
MAX_SENT_FOR_PROFILE = 1000
PROFILE_SAMPLE_SIZE = 100
PROFILE_MAX_AGE_DAYS = 7
MAX_BODY_CHARS = 3000
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

# Default signature (can be overridden via config)
DEFAULT_SIGNATURE_HTML = ""


# ---------------------------------------------------------------------------
# Config helpers (reuse existing auth module)
# ---------------------------------------------------------------------------

def _get_config(key: str, default: str = "") -> str:
    from auth import get_config
    return get_config(key, default)


def _set_config(key: str, value: str):
    from auth import set_config
    set_config(key, value)


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


def _is_automated(msg: email.message.Message) -> bool:
    """Check if an email is from an automated/noreply sender."""
    from_addr = msg.get("From", "")
    if _AUTOMATED_PATTERNS.search(from_addr):
        return True
    for header in _BULK_HEADERS:
        if msg.get(header):
            return True
    precedence = (msg.get("Precedence") or "").lower()
    if precedence in ("bulk", "list", "junk"):
        return True
    return False


def _matches_exclusion(addr: str, subject: str) -> str | None:
    """Check user-configured exclusion patterns. Returns reason or None."""
    raw = _get_config("drafter.exclusions", "")
    if not raw:
        return None
    try:
        exclusions = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    addr_lower = addr.lower()
    for pattern in exclusions.get("senders", []):
        p = pattern.lower().replace("*", ".*")
        if re.match(p, addr_lower):
            return f"excluded sender: {pattern}"
    for pattern in exclusions.get("subjects", []):
        p = pattern.lower().replace("*", ".*")
        if re.search(p, subject.lower()):
            return f"excluded subject: {pattern}"
    return None


# ---------------------------------------------------------------------------
# AI call (async — uses anthropic_bridge.anthropic_chat)
# ---------------------------------------------------------------------------

async def _ai_call(prompt: str, system_text: str = "", timeout: int = 120) -> str:
    """Non-streaming AI call via Anthropic Messages API."""
    from anthropic_bridge import anthropic_chat

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("No Anthropic API key configured — add one in Settings > Anthropic API")

    messages = [{"role": "user", "content": prompt}]
    system = [{"type": "text", "text": system_text}] if system_text else None

    result = await anthropic_chat(
        messages=messages,
        system=system,
        model=DRAFTER_MODEL,
        api_key=api_key,
        max_tokens=4096,
        temperature=0.7,
        timeout_s=timeout,
        use_prompt_caching=False,
    )

    if not result.get("ok"):
        raise RuntimeError(f"AI error: {result.get('error', 'unknown')}")

    text = result.get("text", "").strip()
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

    logger.info(f"Building style profile from last {MAX_SENT_FOR_PROFILE} sent emails...")

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

    # Sample for the AI prompt
    if len(sent_bodies) > PROFILE_SAMPLE_SIZE:
        step = len(sent_bodies) // (PROFILE_SAMPLE_SIZE // 2)
        evenly_spaced = sent_bodies[::max(step, 1)][:PROFILE_SAMPLE_SIZE // 2]
        recent_extra = sent_bodies[:PROFILE_SAMPLE_SIZE // 2]
        seen = set()
        samples = []
        for s in recent_extra + evenly_spaced:
            key = (s["subject"], s["body"][:100])
            if key not in seen:
                seen.add(key)
                samples.append(s)
        samples = samples[:PROFILE_SAMPLE_SIZE]
    else:
        samples = sent_bodies

    logger.info(f"Sending {len(samples)} samples to AI for style analysis...")

    sample_text = ""
    for i, s in enumerate(samples, 1):
        sample_text += f"\n--- Email {i} ---\n"
        sample_text += f"To: {s['to']}\nSubject: {s['subject']}\nBody:\n{s['body']}\n"

    prompt = f"""Analyze these {len(samples)} sent emails and produce a concise writing style profile.
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
                                subject: str, body: str) -> str:
    """Generate a draft reply using AI in the user's writing style."""
    prompt = f"""Draft an email reply in the user's exact writing style.

STYLE PROFILE:
{style_profile}

RULES:
- Write ONLY the email body text. No Subject line, no metadata.
- Match the user's style exactly — tone, greetings, sign-offs, vocabulary.
- Address the content naturally. Keep length consistent with their patterns.
- No AI disclaimers. Write as if you ARE the user.

Reply to this email:

From: {from_addr}
Subject: {subject}
Body:
{body[:MAX_BODY_CHARS]}"""

    return await _ai_call(prompt, timeout=60)


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

    # Load or build style profile
    style_profile = _get_style_profile()
    if not style_profile:
        style_profile = await build_style_profile()

    state = _load_state()
    processed_ids = set(state.get("processed_ids", []))
    from_email = _get_config("gmail.email", "")
    signature_html = _get_config("drafter.signature_html", DEFAULT_SIGNATURE_HTML)

    max_per_run = int(_get_config("drafter.max_per_run", str(MAX_EMAILS_PER_RUN)))

    logger.info("Checking inbox for new unread emails...")

    imap = _imap_connect()
    results = {"drafted": 0, "skipped": 0, "errors": 0, "details": []}

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

                # Skip automated senders
                if _is_automated(msg):
                    results["skipped"] += 1
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "skipped", "reason": "automated sender",
                    })
                    _record_history(message_id, from_addr_parsed, subject,
                                    "skipped_auto", "automated sender")
                    processed_ids.add(message_id)
                    continue

                # Skip user-configured exclusions
                exclusion_reason = _matches_exclusion(from_addr_parsed, subject)
                if exclusion_reason:
                    results["skipped"] += 1
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "skipped", "reason": exclusion_reason,
                    })
                    _record_history(message_id, from_addr_parsed, subject,
                                    "skipped_excluded", exclusion_reason)
                    processed_ids.add(message_id)
                    continue

                # Skip empty bodies
                if not body or len(body.strip()) < 10:
                    results["skipped"] += 1
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "skipped", "reason": "empty body",
                    })
                    _record_history(message_id, from_addr_parsed, subject,
                                    "skipped_empty", "empty body")
                    processed_ids.add(message_id)
                    continue

                # Skip emails from self
                if from_addr_parsed == from_email.lower():
                    processed_ids.add(message_id)
                    continue

                logger.info(f"  Drafting reply to: {subject[:60]} (from {from_addr_parsed})")

                if dry_run:
                    results["drafted"] += 1
                    results["details"].append({
                        "from": from_addr_parsed, "subject": subject[:80],
                        "action": "would_draft", "reason": "",
                    })
                    processed_ids.add(message_id)
                    continue

                # Generate draft
                draft_body = await _generate_draft_reply(
                    style_profile, original_from, subject, body,
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

    logger.info(f"Done — {results['drafted']} drafted, {results['skipped']} skipped, {results['errors']} errors")
    return results


# ---------------------------------------------------------------------------
# Draft management (list / send / discard)
# ---------------------------------------------------------------------------

def list_drafts() -> list[dict]:
    """List Gmail drafts created by the auto-drafter (identified by X-KukuiBot-Draft header)."""
    from gmail_bridge import check_permission
    check_permission("read_inbox")

    imap = _imap_connect()
    drafts = []

    try:
        status, _ = imap.select('"[Gmail]/Drafts"', readonly=True)
        if status != "OK":
            return []

        status, data = imap.search(None, "ALL")
        if status != "OK" or not data[0]:
            return []

        msg_nums = data[0].split()
        msg_nums.reverse()  # newest first

        for num in msg_nums[:50]:  # cap at 50
            try:
                status, msg_data = imap.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                # Only include our auto-drafted emails
                if msg.get(X_DRAFTER_HEADER) != X_DRAFTER_VALUE:
                    continue

                subject = _parse_header(msg.get("Subject", "(no subject)"))
                to_addr = msg.get("To", "")
                # The "To" of the draft = the person we're replying to
                # Get the original sender from In-Reply-To context
                in_reply_to = msg.get("In-Reply-To", "")

                body = _extract_body(msg)

                drafts.append({
                    "uid": num.decode(),
                    "to": to_addr,
                    "subject": subject,
                    "body": body,
                    "in_reply_to": in_reply_to,
                    "date": msg.get("Date", ""),
                })
            except Exception as e:
                logger.warning(f"Error reading draft {num}: {e}")
                continue

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return drafts


def send_draft(uid: str) -> dict:
    """Send an auto-drafted email by UID. Reads from Drafts, sends via SMTP, trashes draft."""
    from gmail_bridge import check_permission, _get_config as gmail_config
    import smtplib

    check_permission("auto_draft")

    # Need at least one send permission
    from gmail_bridge import get_permissions
    perms = get_permissions()
    if not (perms.get("send_owner_only") or perms.get("send_within_org") or perms.get("send_anyone")):
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

        status, msg_data = imap.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"Draft {uid} not found")

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        # Verify it's our draft
        if msg.get(X_DRAFTER_HEADER) != X_DRAFTER_VALUE:
            raise PermissionError("This draft was not created by the auto-drafter")

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
        imap.store(uid.encode(), "+FLAGS", "\\Deleted")
        imap.expunge()

        logger.info(f"Draft sent: to={to_lower} subject={subject[:50]}")
        return {"ok": True, "to": to_addr, "subject": subject}

    finally:
        try:
            imap.logout()
        except Exception:
            pass


def discard_draft(uid: str) -> dict:
    """Delete a draft by UID."""
    from gmail_bridge import check_permission
    check_permission("auto_draft")

    imap = _imap_connect()
    try:
        status, _ = imap.select('"[Gmail]/Drafts"')
        if status != "OK":
            raise RuntimeError("Could not open Drafts folder")

        # Verify it's our draft before deleting
        status, msg_data = imap.fetch(uid.encode(), "(BODY.PEEK[HEADER])")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"Draft {uid} not found")

        raw_header = msg_data[0][1]
        msg = email.message_from_bytes(raw_header)
        if msg.get(X_DRAFTER_HEADER) != X_DRAFTER_VALUE:
            raise PermissionError("This draft was not created by the auto-drafter")

        imap.store(uid.encode(), "+FLAGS", "\\Deleted")
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
    exclusions_raw = _get_config("drafter.exclusions", "")
    try:
        exclusions = json.loads(exclusions_raw) if exclusions_raw else {"senders": [], "subjects": []}
    except (json.JSONDecodeError, TypeError):
        exclusions = {"senders": [], "subjects": []}

    return {
        "enabled": _get_config("drafter.enabled", "0") == "1",
        "check_interval_min": int(_get_config("drafter.check_interval_min", "15")),
        "max_per_run": int(_get_config("drafter.max_per_run", str(MAX_EMAILS_PER_RUN))),
        "signature_html": _get_config("drafter.signature_html", DEFAULT_SIGNATURE_HTML),
        "exclusions": exclusions,
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
    if "exclusions" in cfg:
        _set_config("drafter.exclusions", json.dumps(cfg["exclusions"]))


# Init history table on import
_ensure_history_table()
