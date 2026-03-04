"""
gmail_bridge.py — Gmail integration via IMAP/SMTP with App Password.

Setup: User enters their Gmail address + App Password in Settings. Done.
No Google Cloud Console, no OAuth, no client_secret.json.

How to get an App Password:
  1. Enable 2FA on your Google account
  2. Go to myaccount.google.com/apppasswords
  3. Generate a password for "Mail"
  4. Paste it into KukuiBot Settings

Permissions (stored as config keys):
  gmail.perm.read_inbox, gmail.perm.read_sent, gmail.perm.create_drafts,
  gmail.perm.send_owner_only, gmail.perm.send_anyone, gmail.perm.manual_send, gmail.perm.trash

Security:
  - App password stored in DB config table (same as other API keys)
  - Every operation checks its permission toggle before executing
  - All outbound content passes through email_sanitize.preflight_email()
  - Inbound message bodies scanned via email_sanitize.scan()
"""

import base64
import email
import email.header
import email.utils
import imaplib
import logging
import re as _re_module
import smtplib
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import KUKUIBOT_HOME

logger = logging.getLogger("kukuibot.gmail")

# --- Constants ---
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

ALL_PERMS = [
    "read_inbox", "read_sent", "create_drafts",
    "send_owner_only", "send_within_org", "send_anyone", "manual_send",
    "trash", "auto_draft",
]

# --- Helpers ---

def _get_config(key: str, default: str = "") -> str:
    from auth import get_config
    return get_config(key, default)


def _set_config(key: str, value: str):
    from auth import set_config
    set_config(key, value)


def get_permissions() -> dict[str, bool]:
    """Return dict of all Gmail permissions and their on/off state."""
    return {p: _get_config(f"gmail.perm.{p}", "0") == "1" for p in ALL_PERMS}


def set_permission(perm: str, enabled: bool):
    if perm not in ALL_PERMS:
        raise ValueError(f"Unknown permission: {perm}")
    _set_config(f"gmail.perm.{perm}", "1" if enabled else "0")


def set_permissions(perms: dict[str, bool]):
    for p, v in perms.items():
        set_permission(p, v)
    _sync_tools_md()


def _sync_tools_md():
    """Update the ## Gmail section in TOOLS.md to reflect current permissions."""
    tools_path = KUKUIBOT_HOME / "TOOLS.md"
    try:
        content = tools_path.read_text() if tools_path.exists() else ""
    except Exception:
        content = ""

    # Build the Gmail section
    email_addr = _get_config("gmail.email", "")
    connected = bool(email_addr and _get_config("gmail.app_password", ""))
    perms = get_permissions()
    enabled = [p for p, v in perms.items() if v]

    if connected and enabled:
        owner_emails = _get_owner_emails()
        owner_list = ", ".join(sorted(owner_emails)) if owner_emails else email_addr
        owner_domain = email_addr.split('@')[-1] if '@' in email_addr else ""
        perm_labels = {
            "read_inbox": "Read inbox messages",
            "read_sent": "Read sent messages",
            "create_drafts": "Create email drafts",
            "send_owner_only": f"Send email (to owner only: {owner_list})",
            "send_within_org": f"Send email (within @{owner_domain} only)" if owner_domain else "Send within organization",
            "send_anyone": "Send email to anyone",
            "manual_send": "Manual send by user (drafts only, not AI)",
            "trash": "Move messages to trash",
        }
        lines = [
            "## Gmail Integration",
            "",
            f"Gmail is connected as **{email_addr}**. Available email capabilities:",
            "",
        ]
        for p in enabled:
            lines.append(f"- {perm_labels.get(p, p)}")
        lines.append("")
        lines.append("All outbound email is scanned by `email_sanitize.preflight_email()` before sending.")
        lines.append("Inbound message bodies are scanned for sensitive content on read.")
        lines.append("")
        gmail_section = "\n".join(lines)
    else:
        gmail_section = ""

    # Replace or append the section
    marker_start = "## Gmail Integration"
    if marker_start in content:
        # Find the section and replace it (up to next ## or end of file)
        import re
        pattern = r"## Gmail Integration\n.*?(?=\n## |\Z)"
        content = re.sub(pattern, gmail_section.rstrip(), content, count=1, flags=re.DOTALL)
    elif gmail_section:
        # Append to end
        content = content.rstrip() + "\n\n" + gmail_section

    try:
        tools_path.write_text(content.rstrip() + "\n")
        logger.info(f"TOOLS.md Gmail section updated: {len(enabled)} permissions enabled")
    except Exception as e:
        logger.warning(f"Failed to update TOOLS.md: {e}")


def check_permission(perm: str) -> bool:
    if perm not in ALL_PERMS:
        raise ValueError(f"Unknown Gmail permission: {perm}")
    if _get_config(f"gmail.perm.{perm}", "0") != "1":
        raise PermissionError(f"Gmail permission '{perm}' is not enabled")
    return True


# --- Connection ---

def save_gmail_credentials(email_addr: str, app_password: str):
    """Save Gmail email + app password to config DB."""
    _set_config("gmail.email", email_addr.strip())
    _set_config("gmail.app_password", app_password.strip())
    _set_config("gmail.connected_at", str(int(time.time())))
    _sync_tools_md()


def clear_gmail_credentials():
    """Clear all Gmail config."""
    for key in ("gmail.email", "gmail.app_password", "gmail.connected_at"):
        _set_config(key, "")
    for p in ALL_PERMS:
        _set_config(f"gmail.perm.{p}", "0")
    _sync_tools_md()
    logger.info("Gmail disconnected")


def get_send_whitelist_domains() -> list[str]:
    """Return the list of whitelisted send domains."""
    raw = _get_config("gmail.send_whitelist_domains", "")
    if not raw.strip():
        return []
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def set_send_whitelist_domains(domains: list[str]):
    """Save the whitelist of allowed send domains."""
    cleaned = sorted(set(d.strip().lower() for d in domains if d.strip()))
    _set_config("gmail.send_whitelist_domains", ",".join(cleaned))
    _sync_tools_md()


def get_gmail_status() -> dict:
    """Return Gmail connection status."""
    email_addr = _get_config("gmail.email", "")
    app_password = _get_config("gmail.app_password", "")
    connected_at = _get_config("gmail.connected_at", "")
    perms = get_permissions()
    return {
        "connected": bool(email_addr and app_password),
        "email": email_addr,
        "connected_at": int(connected_at) if connected_at else 0,
        "permissions": perms,
        "send_whitelist_domains": get_send_whitelist_domains(),
    }


def test_gmail_connection() -> dict:
    """Test the Gmail IMAP connection. Returns {ok, error?}."""
    email_addr = _get_config("gmail.email", "")
    app_password = _get_config("gmail.app_password", "")
    if not email_addr or not app_password:
        return {"ok": False, "error": "No Gmail credentials saved"}
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(email_addr, app_password)
        imap.logout()
        return {"ok": True}
    except imaplib.IMAP4.error as e:
        return {"ok": False, "error": f"Login failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- IMAP Connection Pool ---

_pool_lock = threading.Lock()
_pool: list[tuple[imaplib.IMAP4_SSL, float]] = []  # [(conn, created_at), ...]
_POOL_MAX_SIZE = 2
_POOL_MAX_AGE = 300  # 5 minutes


def _imap_connect_raw() -> imaplib.IMAP4_SSL:
    """Create a fresh authenticated IMAP connection (no pooling)."""
    email_addr = _get_config("gmail.email", "")
    app_password = _get_config("gmail.app_password", "")
    if not email_addr or not app_password:
        raise RuntimeError("No Gmail credentials — add email + app password in Settings")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(email_addr, app_password)
    return imap


def _get_imap() -> imaplib.IMAP4_SSL:
    """Get an IMAP connection from the pool, or create a new one.

    The returned connection must be returned via _return_imap() after use,
    or discarded via _discard_imap() on error.
    """
    with _pool_lock:
        now = time.time()
        while _pool:
            conn, created_at = _pool.pop()
            if now - created_at > _POOL_MAX_AGE:
                # Too old, close and skip
                try:
                    conn.logout()
                except Exception:
                    pass
                continue
            # Check if connection is still alive
            try:
                status, _ = conn.noop()
                if status == "OK":
                    return conn
            except Exception:
                pass
            # Dead connection, discard
            try:
                conn.logout()
            except Exception:
                pass
    # No pooled connection available — create new
    return _imap_connect_raw()


def _return_imap(conn: imaplib.IMAP4_SSL):
    """Return a connection to the pool for reuse."""
    with _pool_lock:
        if len(_pool) < _POOL_MAX_SIZE:
            _pool.append((conn, time.time()))
        else:
            try:
                conn.logout()
            except Exception:
                pass


def _discard_imap(conn: imaplib.IMAP4_SSL):
    """Discard a connection that errored (don't return to pool)."""
    try:
        conn.logout()
    except Exception:
        pass


# Legacy name kept for test_gmail_connection which needs a throwaway connection
def _imap_connect() -> imaplib.IMAP4_SSL:
    """Return an authenticated IMAP connection (not pooled, caller must logout)."""
    return _imap_connect_raw()


# --- IMAP helpers ---


def _parse_email_headers(msg: email.message.Message) -> dict:
    """Extract common headers from an email message."""
    subject, encoding = email.header.decode_header(msg.get("Subject", ""))[0]
    if isinstance(subject, bytes):
        subject = subject.decode(encoding or "utf-8", errors="replace")
    return {
        "from": msg.get("From", ""),
        "to": msg.get("To", ""),
        "cc": msg.get("Cc", ""),
        "subject": subject or "(no subject)",
        "date": msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
    }


def _extract_body(msg: email.message.Message) -> str:
    """Extract text body from email. Prefers text/plain, falls back to HTML."""
    if msg.is_multipart():
        plain = ""
        html = ""
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not plain:
                payload = part.get_payload(decode=True)
                if payload:
                    plain = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ct == "text/html" and not html:
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if plain:
            return plain
        if html:
            return _strip_html(html)
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if not payload:
            return ""
        text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            return _strip_html(text)
        return text


def _resolve_cid_images(html: str, msg: email.message.Message) -> str:
    """Replace cid: references in HTML with inline data: URIs from MIME parts."""
    import re

    if not msg.is_multipart() or "cid:" not in html.lower():
        return html

    # Build map of Content-ID -> data URI
    cid_map: dict[str, str] = {}
    for part in msg.walk():
        content_id = part.get("Content-ID")
        if not content_id:
            continue
        ct = part.get_content_type() or ""
        if not ct.startswith("image/"):
            continue
        # Strip angle brackets: <image001.png@01DA1234> -> image001.png@01DA1234
        cid = content_id.strip("<>").strip()
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        # Cap at 2MB per image to avoid bloating the response
        if len(payload) > 2 * 1024 * 1024:
            continue
        b64 = base64.b64encode(payload).decode("ascii")
        cid_map[cid] = f"data:{ct};base64,{b64}"

    if not cid_map:
        return html

    # Replace cid: references (handles both quoted and unquoted, case-insensitive)
    def _replace_cid(match):
        cid_ref = match.group(1)
        return cid_map.get(cid_ref, match.group(0))

    # Match src="cid:xxx", src='cid:xxx', and bare cid:xxx in url()
    html = re.sub(r'cid:([^\s"\'<>]+)', _replace_cid, html, flags=re.IGNORECASE)
    return html


def _inline_external_images(html: str) -> str:
    """Fetch external http(s) images in <img> tags and replace with data: URIs.
    srcdoc iframes have a null origin and cannot load external images directly."""
    import re
    import urllib.request

    img_pattern = re.compile(
        r'(<img\b[^>]*\bsrc\s*=\s*["\'])(https?://[^"\']+)(["\'])',
        re.IGNORECASE,
    )
    urls = set(m.group(2) for m in img_pattern.finditer(html))
    if not urls:
        return html

    url_map: dict[str, str] = {}
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "KukuiBot/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                ct = resp.headers.get("Content-Type", "")
                if not ct.startswith("image/"):
                    continue
                data = resp.read(2 * 1024 * 1024)  # cap 2MB per image
                if len(data) >= 2 * 1024 * 1024:
                    continue  # too large, skip
                b64 = base64.b64encode(data).decode("ascii")
                url_map[url] = f"data:{ct.split(';')[0]};base64,{b64}"
        except Exception:
            continue  # network error — leave original URL

    if not url_map:
        return html

    def _replace_url(match):
        pre, url, post = match.group(1), match.group(2), match.group(3)
        return pre + url_map.get(url, url) + post

    return img_pattern.sub(_replace_url, html)


def _extract_body_both(msg: email.message.Message) -> tuple[str, str | None]:
    """Extract both plain text and HTML body from email.
    Returns (plain_text, html_or_none). Resolves cid: inline images to data URIs."""
    if msg.is_multipart():
        plain = ""
        html = ""
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not plain:
                payload = part.get_payload(decode=True)
                if payload:
                    plain = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ct == "text/html" and not html:
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        # Resolve cid: inline images, then fetch external images as data URIs
        if html:
            html = _resolve_cid_images(html, msg)
            html = _inline_external_images(html)
        if plain:
            return plain, html or None
        if html:
            return _strip_html(html), html
        return "", None
    else:
        payload = msg.get_payload(decode=True)
        if not payload:
            return "", None
        text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            text = _inline_external_images(text)
            return _strip_html(text), text
        return text, None


def _extract_attachments(msg: email.message.Message) -> list[dict]:
    """Walk MIME parts and return list of {filename, content_type, size} for attachments."""
    attachments = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        if not filename:
            continue
        # Has a filename — it's an attachment (inline or regular)
        content_type = part.get_content_type() or "application/octet-stream"
        payload = part.get_payload(decode=True)
        size = len(payload) if payload else 0
        attachments.append({
            "filename": filename,
            "content_type": content_type,
            "size": size,
        })
    return attachments


def _strip_html(html: str) -> str:
    """Simple HTML tag stripper."""
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _html_to_plain(html: str) -> str:
    """Convert HTML to readable plain text, preserving links and structure."""
    import re
    text = html
    # Remove style/script blocks
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert links: <a href="url">text</a> -> text (url)
    text = re.sub(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                  lambda m: f"{m.group(2).strip()} ({m.group(1)})" if m.group(2).strip() != m.group(1) else m.group(1),
                  text, flags=re.IGNORECASE | re.DOTALL)
    # List items
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    # Block elements to newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<(p|div|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode entities
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#\d+;", "", text)
    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _resolve_folder(folder: str) -> str:
    """Map shorthand folder names to Gmail IMAP folder paths."""
    upper = folder.upper()
    if upper == "SENT":
        return "[Gmail]/Sent Mail"
    elif upper == "TRASH":
        return "[Gmail]/Trash"
    elif upper == "DRAFTS":
        return "[Gmail]/Drafts"
    return folder


def _build_imap_search(query: str) -> str | tuple:
    """Convert a user-friendly search string into IMAP search criteria.

    Uses Gmail's X-GM-RAW extension which accepts the same query syntax as
    Gmail web search (full-text across all headers and body, supports
    operators like from:, to:, subject:, has:attachment, before:, after:, etc).

    Returns a tuple for imap.search() when using X-GM-RAW, or a string
    for standard IMAP criteria.

    Examples:
      "daniel"          -> X-GM-RAW "daniel"         (full Gmail search)
      "from:alice"      -> X-GM-RAW "from:alice"     (Gmail operator)
      "UNSEEN"          -> UNSEEN                    (raw IMAP pass-through)
    """
    if not query or not query.strip():
        return "ALL"
    q = query.strip()

    # If it already looks like raw IMAP criteria (starts with known keyword), pass through
    imap_keywords = ("FROM", "SUBJECT", "BODY", "TO", "CC", "BCC", "TEXT",
                     "SINCE", "BEFORE", "ON", "SEEN", "UNSEEN", "ALL",
                     "FLAGGED", "UNFLAGGED", "OR", "NOT", "HEADER")
    first_word = q.split()[0].upper()
    if first_word in imap_keywords:
        return q

    # Use Gmail X-GM-RAW for full-text search (same as Gmail web search bar)
    # Escape internal double quotes
    safe = q.replace('\\', '\\\\').replace('"', '\\"')
    return ("X-GM-RAW", f'"{safe}"')


# --- Read Operations ---

def list_messages(folder: str = "INBOX", max_results: int = 20, search: str = "", offset: int = 0) -> list[dict]:
    """
    List messages from a Gmail folder via IMAP.

    Args:
        folder: IMAP folder — "INBOX", "[Gmail]/Sent Mail", "[Gmail]/Trash", etc.
        max_results: Max messages to return (capped at 50)
        search: IMAP search criteria (e.g. 'FROM "alice"', 'SUBJECT "hello"')

    Returns list of message summaries.
    """
    if "sent" in folder.lower():
        check_permission("read_sent")
    else:
        check_permission("read_inbox")

    max_results = min(max_results, 50)
    imap = _get_imap()
    errored = False

    try:
        # Select folder
        imap_folder = _resolve_folder(folder)

        status, _ = imap.select(f'"{imap_folder}"', readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open folder: {imap_folder}")

        # Search — convert user query to IMAP criteria
        criteria = _build_imap_search(search)
        if isinstance(criteria, tuple):
            status, data = imap.search(None, *criteria)
        else:
            status, data = imap.search(None, criteria)
        if status != "OK":
            return []

        msg_nums = data[0].split()
        if not msg_nums:
            return []

        # Get the most recent N messages (with offset for pagination)
        total = len(msg_nums)
        end_idx = total - offset
        start_idx = max(0, end_idx - max_results)
        if end_idx <= 0:
            return []
        msg_nums = msg_nums[start_idx:end_idx]
        msg_nums.reverse()  # newest first

        from injection_guard import scan_and_filter

        # Batch fetch: headers + flags only (one BODY section = reliable imaplib parsing)
        # BODY.PEEK does NOT set \Seen flag (critical for inbox listing)
        # Snippets come from cache or are left empty — avoids imaplib multi-section bugs
        msg_set = b",".join(msg_nums)
        fetch_spec = "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] FLAGS)"
        status, batch_data = imap.fetch(msg_set, fetch_spec)
        if status != "OK":
            return []

        # Parse batch response — with single BODY section, each message is one tuple:
        #   (b'NUM (FLAGS (...) BODY[HEADER.FIELDS ...] {size}', b'header data')
        summaries = []
        parsed = _parse_batch_fetch(batch_data, msg_nums)

        # Load cached snippets so we don't need TEXT in the batch fetch
        snippet_map = {}
        try:
            import email_cache
            db = email_cache._get_db()
            try:
                placeholders = ",".join("?" for _ in msg_nums)
                uid_strs = [n.decode() for n in msg_nums]
                rows = db.execute(
                    f"SELECT uid, snippet FROM messages WHERE folder = ? AND uid IN ({placeholders})",
                    [folder] + uid_strs,
                ).fetchall()
                for r in rows:
                    snippet_map[str(r[0])] = r[1] or ""
            finally:
                db.close()
        except Exception:
            pass  # No cache available — snippets will be empty

        for num in msg_nums:
            num_str = num.decode()
            entry = parsed.get(num_str)
            if not entry:
                continue
            try:
                header_bytes = entry.get("headers", b"")
                flags_str = entry.get("flags_str", b"")

                # Parse headers
                msg = email.message_from_bytes(header_bytes)
                headers = _parse_email_headers(msg)
                headers["uid"] = num_str
                headers["folder"] = folder
                headers["is_read"] = b"\\Seen" in flags_str
                headers["is_starred"] = b"\\Flagged" in flags_str
                headers["snippet"] = snippet_map.get(num_str, "")

                # Scan subject for injection attempts
                original_subject = headers.get("subject", "")
                headers["subject"] = scan_and_filter(original_subject, source="email_subject")
                if headers["subject"] != original_subject:
                    headers["injection_filtered"] = True
                    logger.warning(f"Gmail list: injection filtered in subject of msg {num_str}")
                summaries.append(headers)
            except Exception as e:
                logger.warning(f"Failed to parse message {num_str}: {e}")
                continue

        return summaries
    except Exception:
        errored = True
        raise
    finally:
        if errored:
            _discard_imap(imap)
        else:
            _return_imap(imap)


def _parse_batch_fetch(batch_data: list, msg_nums: list[bytes]) -> dict:
    """Parse the flat batch response from imap.fetch() into per-message dicts.

    Returns: {msg_num_str: {"headers": bytes, "flags_str": bytes}}

    With a single BODY section fetch (HEADER.FIELDS + FLAGS), each message
    produces one tuple: (b'NUM (FLAGS (...) BODY[HEADER.FIELDS ...] {size}', b'data')
    followed by b')'.

    The descriptor contains the message sequence number, FLAGS, and the BODY spec.
    We track the "current message number" to handle continuation tuples that
    don't start with a message number (e.g. multi-section responses).
    """
    result = {}
    valid_nums = {num.decode() for num in msg_nums}

    # Pre-initialize entries for all requested message numbers
    for num_str in valid_nums:
        result[num_str] = {"headers": b"", "flags_str": b""}

    current_msg = None  # track current message for continuation tuples

    for item in batch_data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        descriptor = item[0]
        payload = item[1]

        # Extract message number from start of descriptor
        desc_str = descriptor.decode("ascii", errors="replace")
        parts = desc_str.split(None, 1)
        if not parts:
            continue

        # Check if first token is a message number (digits only)
        first_token = parts[0]
        if first_token.isdigit() and first_token in valid_nums:
            current_msg = first_token
        elif current_msg is None:
            continue  # can't associate this tuple with a message

        msg_num = current_msg
        desc_upper = desc_str.upper()

        # Store the full descriptor for FLAGS parsing (contains FLAGS inline)
        if "FLAGS" in desc_upper:
            result[msg_num]["flags_str"] = descriptor

        if "HEADER.FIELDS" in desc_upper or (first_token.isdigit() and b"BODY[" in descriptor):
            result[msg_num]["headers"] = payload

    return result


def get_message(folder: str, uid: str) -> dict:
    """
    Get full message content via IMAP.

    Returns: {from, to, subject, date, body, uid, folder, sensitive_findings}
    """
    check_permission("read_inbox")

    imap = _get_imap()
    errored = False
    try:
        imap_folder = _resolve_folder(folder)

        status, _ = imap.select(f'"{imap_folder}"', readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open folder: {imap_folder}")

        status, msg_data = imap.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"Message {uid} not found in {folder}")

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        headers = _parse_email_headers(msg)
        body, body_html = _extract_body_both(msg)
        attachments = _extract_attachments(msg)

        # Scan for sensitive content (egress-style)
        from email_sanitize import scan
        findings = scan(body)
        if findings:
            logger.info(f"Gmail message {uid}: {len(findings)} sensitive finding(s) on read")

        # Injection guard — same two-stage pipeline as web_fetch
        from injection_guard import scan_and_filter
        original_body = body
        original_subject = headers.get("subject", "")
        body = scan_and_filter(body, source="email")
        subject_clean = scan_and_filter(original_subject, source="email_subject")
        if body_html is not None:
            body_html = scan_and_filter(body_html, source="email")
        injection_filtered = (body != original_body) or (subject_clean != original_subject)
        if injection_filtered:
            logger.warning(f"Gmail message {uid}: injection content filtered")
            headers["subject"] = subject_clean

        return {
            **headers,
            "body": body,
            "body_html": body_html,
            "uid": uid,
            "folder": folder,
            "sensitive_findings": len(findings),
            "injection_filtered": injection_filtered,
            "attachments": attachments,
            "has_attachments": bool(attachments),
        }
    except Exception:
        errored = True
        raise
    finally:
        if errored:
            _discard_imap(imap)
        else:
            _return_imap(imap)


def list_folders() -> list[str]:
    """List available IMAP folders."""
    check_permission("read_inbox")

    imap = _get_imap()
    errored = False
    try:
        status, folders = imap.list()
        if status != "OK":
            return []
        result = []
        for f in folders:
            # Parse IMAP folder list response
            parts = f.decode().split(' "/" ')
            if len(parts) >= 2:
                name = parts[1].strip('"')
                result.append(name)
        return result
    except Exception:
        errored = True
        raise
    finally:
        if errored:
            _discard_imap(imap)
        else:
            _return_imap(imap)


def get_attachment(folder: str, uid: str, filename: str) -> tuple[bytes, str, str]:
    """
    Download a specific attachment from a message.

    Returns: (content_bytes, content_type, filename)
    Raises RuntimeError if attachment not found.
    """
    check_permission("read_inbox")

    imap = _get_imap()
    errored = False
    try:
        imap_folder = _resolve_folder(folder)

        status, _ = imap.select(f'"{imap_folder}"', readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open folder: {imap_folder}")

        status, msg_data = imap.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"Message {uid} not found in {folder}")

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        for part in msg.walk():
            part_filename = part.get_filename()
            if part_filename and part_filename == filename:
                content_type = part.get_content_type() or "application/octet-stream"
                payload = part.get_payload(decode=True)
                if payload is None:
                    raise RuntimeError(f"Attachment '{filename}' has no content")
                return payload, content_type, filename

        raise RuntimeError(f"Attachment '{filename}' not found in message {uid}")
    except Exception:
        errored = True
        raise
    finally:
        if errored:
            _discard_imap(imap)
        else:
            _return_imap(imap)


# --- Write Operations ---

def _get_owner_emails() -> set[str]:
    """Return all owner email addresses: Gmail connected email + any emails in USER.md."""
    emails = set()
    gmail_email = _get_config("gmail.email", "")
    if gmail_email:
        emails.add(gmail_email.strip().lower())
    # Parse emails from USER.md
    import re
    user_md = KUKUIBOT_HOME / "USER.md"
    try:
        text = user_md.read_text() if user_md.exists() else ""
        for match in re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', text):
            emails.add(match.lower())
    except Exception:
        pass
    return emails


def _enforce_send_permissions(to: str, perms: dict, owner_emails: set[str]):
    """Shared permission enforcement for all send operations.

    Raises PermissionError if the recipient is not allowed by the current permission level.
    """
    to_lower = to.strip().lower()

    if not (perms.get("send_owner_only") or perms.get("send_within_org") or perms.get("send_anyone")):
        raise PermissionError("No send permission enabled")

    if perms.get("send_anyone"):
        return  # No restrictions

    # Check domain whitelist — if recipient's domain is whitelisted, allow
    whitelist = get_send_whitelist_domains()
    if whitelist and '@' in to_lower:
        to_domain = to_lower.split('@')[-1]
        if to_domain in whitelist:
            return  # Domain is whitelisted

    if perms.get("send_within_org"):
        gmail_email = _get_config("gmail.email", "")
        owner_domain = gmail_email.split('@')[-1] if '@' in gmail_email else None
        to_domain = to_lower.split('@')[-1] if '@' in to_lower else None
        if not owner_domain or not to_domain or to_domain != owner_domain:
            raise PermissionError(
                f"Send within organization only: recipient must be @{owner_domain}, not {to}"
            )
    elif perms.get("send_owner_only"):
        if to_lower not in owner_emails:
            raise PermissionError(
                f"Send to owner only: allowed recipients are {', '.join(sorted(owner_emails))}, not {to}"
            )


def _smtp_send(to: str, subject: str, body_text: str, body_html: str | None = None, subtype: str = "plain"):
    """Send an email via SMTP. Builds multipart/alternative when body_html is provided."""
    email_addr = _get_config("gmail.email", "")
    app_password = _get_config("gmail.app_password", "")
    if not email_addr or not app_password:
        raise RuntimeError("No Gmail credentials")

    if body_html:
        # Auto-generate plain text fallback if body_text is empty
        plain = body_text if body_text.strip() else _html_to_plain(body_html)
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(body_html, "html"))
    else:
        msg = MIMEText(body_text, subtype)

    msg["From"] = email_addr
    msg["To"] = to
    msg["Subject"] = subject

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(email_addr, app_password)
        server.sendmail(email_addr, [to], msg.as_string())


def create_draft(to: str, subject: str, body: str, body_html: str | None = None) -> dict:
    """
    Create a Gmail draft via IMAP APPEND to [Gmail]/Drafts.
    Requires create_drafts permission. Content sanitized first.
    """
    check_permission("create_drafts")

    from email_sanitize import preflight_email
    passed, findings = preflight_email(subject, body, body_html=body_html)
    if not passed:
        finding_strs = [f"[{f['severity']}] {f['rule']}: \"{f['match']}\"" for f in findings[:5]]
        raise ValueError(
            f"Content blocked — {len(findings)} sensitive item(s) detected:\n" +
            "\n".join(finding_strs)
        )

    email_addr = _get_config("gmail.email", "")
    if body_html:
        plain = body if body.strip() else _html_to_plain(body_html)
        msg = MIMEMultipart("alternative")
        msg["From"] = email_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(body_html, "html"))
    else:
        msg = MIMEText(body)
        msg["From"] = email_addr
        msg["To"] = to
        msg["Subject"] = subject

    imap = _get_imap()
    errored = False
    try:
        status, _ = imap.append(
            '"[Gmail]/Drafts"', "\\Draft",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes()
        )
        if status != "OK":
            raise RuntimeError("Failed to create draft")
        logger.info(f"Gmail draft created: to={to} subject={subject[:50]}")
        return {"ok": True, "to": to, "subject": subject}
    except Exception:
        errored = True
        raise
    finally:
        if errored:
            _discard_imap(imap)
        else:
            _return_imap(imap)


def send_email(to: str, subject: str, body: str, body_html: str | None = None) -> dict:
    """
    Send an email via Gmail SMTP.
    Requires send_owner_only, send_within_org, or send_anyone permission.
    Enforces recipient restrictions server-side. Content sanitized first.
    """
    perms = get_permissions()
    owner_emails = _get_owner_emails()
    _enforce_send_permissions(to, perms, owner_emails)

    from email_sanitize import preflight_email
    passed, findings = preflight_email(subject, body, body_html=body_html)
    if not passed:
        finding_strs = [f"[{f['severity']}] {f['rule']}: \"{f['match']}\"" for f in findings[:5]]
        raise ValueError(
            f"Content blocked — {len(findings)} sensitive item(s) detected:\n" +
            "\n".join(finding_strs)
        )

    # Signature injection for HTML emails
    if body_html:
        sig_html = _get_config("drafter.signature_html", "")
        if sig_html:
            body_html += f'<br><div class="signature">{sig_html}</div>'
            # Append plain text signature
            sig_plain = _html_to_plain(sig_html)
            if sig_plain:
                body = (body or "") + f"\n-- \n{sig_plain}"

    _smtp_send(to, subject, body, body_html=body_html)
    logger.info(f"Gmail sent: to={to} subject={subject[:50]}")
    return {"ok": True, "to": to, "subject": subject}


def send_html_report(to: str, subject: str, html_path: str) -> dict:
    """
    Send a local HTML file as an email.
    Skips egress sanitizer — this is for static local reports, not AI-generated content.
    Requires send_owner_only, send_within_org, or send_anyone permission.
    """
    import os
    perms = get_permissions()
    owner_emails = _get_owner_emails()
    _enforce_send_permissions(to, perms, owner_emails)

    # Validate the file exists and is under KUKUIBOT_HOME
    abs_path = os.path.abspath(html_path)
    if not abs_path.startswith(str(KUKUIBOT_HOME)):
        raise PermissionError(f"HTML report must be under KukuiBot home directory")
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"Report file not found: {html_path}")

    with open(abs_path, "r") as f:
        html_content = f.read()

    _smtp_send(to, subject, html_content, subtype="html")
    logger.info(f"Gmail HTML report sent: to={to} subject={subject[:50]}")
    return {"ok": True, "to": to, "subject": subject}


def draft_html_report(to: str, subject: str, html_path: str) -> dict:
    """
    Save a local HTML file as a Gmail draft.
    Skips egress sanitizer — this is for static local reports, not AI-generated content.
    Requires create_drafts permission.
    """
    import os
    check_permission("create_drafts")

    abs_path = os.path.abspath(html_path)
    if not abs_path.startswith(str(KUKUIBOT_HOME)):
        raise PermissionError("HTML report must be under KukuiBot home directory")
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"Report file not found: {html_path}")

    with open(abs_path, "r") as f:
        html_content = f.read()

    email_addr = _get_config("gmail.email", "")
    msg = MIMEText(html_content, "html")
    msg["From"] = email_addr
    msg["To"] = to
    msg["Subject"] = subject

    imap = _get_imap()
    errored = False
    try:
        status, _ = imap.append(
            '"[Gmail]/Drafts"', "\\Draft",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes()
        )
        if status != "OK":
            raise RuntimeError("Failed to create draft")
        logger.info(f"Gmail HTML draft created: to={to} subject={subject[:50]}")
        return {"ok": True, "to": to, "subject": subject}
    except Exception:
        errored = True
        raise
    finally:
        if errored:
            _discard_imap(imap)
        else:
            _return_imap(imap)


# --- Trash Operations ---

def trash_message(folder: str, uid: str) -> dict:
    """Move a message to trash via IMAP. Requires trash permission."""
    check_permission("trash")

    imap = _get_imap()
    errored = False
    try:
        imap_folder = _resolve_folder(folder)

        status, _ = imap.select(f'"{imap_folder}"')
        if status != "OK":
            raise RuntimeError(f"Could not open folder: {imap_folder}")

        # Copy to trash, then mark as deleted
        status, _ = imap.copy(uid.encode(), '"[Gmail]/Trash"')
        if status != "OK":
            raise RuntimeError(f"Failed to move message {uid} to trash")

        imap.store(uid.encode(), "+FLAGS", "\\Deleted")
        imap.expunge()

        logger.info(f"Gmail message trashed: {uid} from {folder}")
        return {"ok": True, "uid": uid}
    except Exception:
        errored = True
        raise
    finally:
        if errored:
            _discard_imap(imap)
        else:
            _return_imap(imap)


def redirect_email(folder: str, uid: str, to: str, subject_override: str | None = None) -> dict:
    """
    Redirect (bounce) an email to a new recipient as-is.

    Fetches the original raw message, rewrites From/To/Subject headers,
    and sends via SMTP preserving the full MIME structure (HTML, attachments).
    Requires at least send_within_org permission. Content sanitized.
    """
    # Enforce send permissions
    perms = get_permissions()
    owner_emails = _get_owner_emails()
    _enforce_send_permissions(to, perms, owner_emails)

    email_addr = _get_config("gmail.email", "")
    app_password = _get_config("gmail.app_password", "")
    if not email_addr or not app_password:
        raise RuntimeError("No Gmail credentials — add email + app password in Settings")

    imap = _get_imap()
    errored = False
    try:
        imap_folder = _resolve_folder(folder)
        status, _ = imap.select(f'"{imap_folder}"', readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open folder: {imap_folder}")

        status, msg_data = imap.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"Message {uid} not found in {folder}")

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        # Rewrite headers — delete originals, set new ones
        del msg["To"]
        del msg["Cc"]
        del msg["Bcc"]
        del msg["From"]
        del msg["Reply-To"]
        msg["From"] = email_addr
        msg["To"] = to

        if subject_override is not None and subject_override.strip():
            del msg["Subject"]
            msg["Subject"] = subject_override

        # Sanitize redirected content
        redirect_body = _extract_body(msg)
        redirect_subject = msg.get("Subject", "")
        from email_sanitize import preflight_email
        passed, findings = preflight_email(redirect_subject, redirect_body)
        if not passed:
            finding_strs = [f"[{f['severity']}] {f['rule']}: \"{f['match']}\"" for f in findings[:5]]
            raise ValueError(
                f"Redirect blocked — {len(findings)} sensitive item(s) detected:\n" +
                "\n".join(finding_strs)
            )

        # Send via SMTP with full MIME intact
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(email_addr, app_password)
            server.sendmail(email_addr, [to], msg.as_bytes())

        original_subject = msg.get("Subject", "(no subject)")
        logger.info(f"Gmail redirect: to={to} subject={original_subject[:50]}")
        return {"ok": True, "to": to, "subject": original_subject}
    except Exception:
        errored = True
        raise
    finally:
        if errored:
            _discard_imap(imap)
        else:
            _return_imap(imap)


def set_message_flags(folder: str, uid: str, flags: dict) -> dict:
    """Set IMAP flags on a message. Requires read_inbox permission.

    Args:
        folder: IMAP folder name
        uid: Message sequence number
        flags: Dict of flags to set, e.g. {"seen": True} or {"seen": False}

    Returns: {"ok": True, "uid": uid}
    """
    check_permission("read_inbox")

    imap = _get_imap()
    errored = False
    try:
        imap_folder = _resolve_folder(folder)

        status, _ = imap.select(f'"{imap_folder}"')
        if status != "OK":
            raise RuntimeError(f"Could not open folder: {imap_folder}")

        if "seen" in flags:
            if flags["seen"]:
                imap.store(uid.encode(), "+FLAGS", "\\Seen")
            else:
                imap.store(uid.encode(), "-FLAGS", "\\Seen")

        if "flagged" in flags:
            if flags["flagged"]:
                imap.store(uid.encode(), "+FLAGS", "\\Flagged")
            else:
                imap.store(uid.encode(), "-FLAGS", "\\Flagged")

        logger.info(f"Gmail flags updated: uid={uid} folder={folder} flags={flags}")
        return {"ok": True, "uid": uid}
    except Exception:
        errored = True
        raise
    finally:
        if errored:
            _discard_imap(imap)
        else:
            _return_imap(imap)
