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
  gmail.perm.send_owner_only, gmail.perm.send_anyone, gmail.perm.trash

Security:
  - App password stored in DB config table (same as other API keys)
  - Every operation checks its permission toggle before executing
  - All outbound content passes through email_sanitize.preflight_email()
  - Inbound message bodies scanned via email_sanitize.scan()
"""

import email
import email.utils
import imaplib
import logging
import smtplib
import time
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
    "send_owner_only", "send_within_org", "send_anyone", "trash",
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


# --- IMAP helpers ---

def _imap_connect() -> imaplib.IMAP4_SSL:
    """Return an authenticated IMAP connection."""
    email_addr = _get_config("gmail.email", "")
    app_password = _get_config("gmail.app_password", "")
    if not email_addr or not app_password:
        raise RuntimeError("No Gmail credentials — add email + app password in Settings")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(email_addr, app_password)
    return imap


def _parse_email_headers(msg: email.message.Message) -> dict:
    """Extract common headers from an email message."""
    subject, encoding = email.header.decode_header(msg.get("Subject", ""))[0]
    if isinstance(subject, bytes):
        subject = subject.decode(encoding or "utf-8", errors="replace")
    return {
        "from": msg.get("From", ""),
        "to": msg.get("To", ""),
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


# --- Read Operations ---

def list_messages(folder: str = "INBOX", max_results: int = 20, search: str = "") -> list[dict]:
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
    imap = _imap_connect()

    try:
        # Select folder
        imap_folder = _resolve_folder(folder)

        status, _ = imap.select(f'"{imap_folder}"', readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open folder: {imap_folder}")

        # Search
        criteria = search if search else "ALL"
        status, data = imap.search(None, criteria)
        if status != "OK":
            return []

        msg_nums = data[0].split()
        if not msg_nums:
            return []

        # Get the most recent N messages
        msg_nums = msg_nums[-max_results:]
        msg_nums.reverse()  # newest first

        from injection_guard import scan_and_filter

        summaries = []
        for num in msg_nums:
            try:
                status, msg_data = imap.fetch(num, "(RFC822.HEADER)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                headers = _parse_email_headers(msg)
                headers["uid"] = num.decode()
                headers["folder"] = folder
                # Scan subject for injection attempts
                original_subject = headers.get("subject", "")
                headers["subject"] = scan_and_filter(original_subject, source="email_subject")
                if headers["subject"] != original_subject:
                    headers["injection_filtered"] = True
                    logger.warning(f"Gmail list: injection filtered in subject of msg {num.decode()}")
                summaries.append(headers)
            except Exception as e:
                logger.warning(f"Failed to fetch message {num}: {e}")
                continue

        return summaries
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def get_message(folder: str, uid: str) -> dict:
    """
    Get full message content via IMAP.

    Returns: {from, to, subject, date, body, uid, folder, sensitive_findings}
    """
    check_permission("read_inbox")

    imap = _imap_connect()
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
        body = _extract_body(msg)

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
        injection_filtered = (body != original_body) or (subject_clean != original_subject)
        if injection_filtered:
            logger.warning(f"Gmail message {uid}: injection content filtered")
            headers["subject"] = subject_clean

        return {
            **headers,
            "body": body,
            "uid": uid,
            "folder": folder,
            "sensitive_findings": len(findings),
            "injection_filtered": injection_filtered,
        }
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def list_folders() -> list[str]:
    """List available IMAP folders."""
    check_permission("read_inbox")

    imap = _imap_connect()
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
    finally:
        try:
            imap.logout()
        except Exception:
            pass


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


def _smtp_send(to: str, subject: str, body: str, subtype: str = "plain"):
    """Send an email via SMTP. subtype can be 'plain' or 'html'."""
    email_addr = _get_config("gmail.email", "")
    app_password = _get_config("gmail.app_password", "")
    if not email_addr or not app_password:
        raise RuntimeError("No Gmail credentials")

    msg = MIMEText(body, subtype)
    msg["From"] = email_addr
    msg["To"] = to
    msg["Subject"] = subject

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(email_addr, app_password)
        server.sendmail(email_addr, [to], msg.as_string())


def create_draft(to: str, subject: str, body: str) -> dict:
    """
    Create a Gmail draft via IMAP APPEND to [Gmail]/Drafts.
    Requires create_drafts permission. Content sanitized first.
    """
    check_permission("create_drafts")

    from email_sanitize import preflight_email
    passed, findings = preflight_email(subject, body)
    if not passed:
        finding_strs = [f"[{f['severity']}] {f['rule']}: \"{f['match']}\"" for f in findings[:5]]
        raise ValueError(
            f"Content blocked — {len(findings)} sensitive item(s) detected:\n" +
            "\n".join(finding_strs)
        )

    email_addr = _get_config("gmail.email", "")
    msg = MIMEText(body)
    msg["From"] = email_addr
    msg["To"] = to
    msg["Subject"] = subject

    imap = _imap_connect()
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
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def send_email(to: str, subject: str, body: str) -> dict:
    """
    Send an email via Gmail SMTP.
    Requires send_owner_only, send_within_org, or send_anyone permission.
    Enforces recipient restrictions server-side. Content sanitized first.
    """
    perms = get_permissions()
    to_lower = to.strip().lower()

    # Check if ANY send permission is enabled
    if not (perms.get("send_owner_only") or perms.get("send_within_org") or perms.get("send_anyone")):
        raise PermissionError("No send permission enabled")

    # Enforce recipient restrictions based on enabled permissions
    owner_emails = _get_owner_emails()

    # Most permissive: send_anyone
    if perms.get("send_anyone"):
        pass  # No restrictions
    # Medium: send_within_org
    elif perms.get("send_within_org"):
        gmail_email = _get_config("gmail.email", "")
        owner_domain = gmail_email.split('@')[-1] if '@' in gmail_email else None
        to_domain = to_lower.split('@')[-1] if '@' in to_lower else None

        if not owner_domain or not to_domain or to_domain != owner_domain:
            raise PermissionError(
                f"Send within organization only: recipient must be @{owner_domain}, not {to}"
            )
    # Most restrictive: send_owner_only
    elif perms.get("send_owner_only"):
        if to_lower not in owner_emails:
            raise PermissionError(
                f"Send to owner only: allowed recipients are {', '.join(sorted(owner_emails))}, not {to}"
            )

    from email_sanitize import preflight_email
    passed, findings = preflight_email(subject, body)
    if not passed:
        finding_strs = [f"[{f['severity']}] {f['rule']}: \"{f['match']}\"" for f in findings[:5]]
        raise ValueError(
            f"Content blocked — {len(findings)} sensitive item(s) detected:\n" +
            "\n".join(finding_strs)
        )

    subtype = "html" if body.strip().startswith(("<!", "<html", "<HTML")) else "plain"
    _smtp_send(to, subject, body, subtype=subtype)
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
    to_lower = to.strip().lower()

    # Check if ANY send permission is enabled
    if not (perms.get("send_owner_only") or perms.get("send_within_org") or perms.get("send_anyone")):
        raise PermissionError("No send permission enabled")

    # Enforce recipient restrictions based on enabled permissions
    owner_emails = _get_owner_emails()

    # Most permissive: send_anyone
    if perms.get("send_anyone"):
        pass  # No restrictions
    # Medium: send_within_org
    elif perms.get("send_within_org"):
        gmail_email = _get_config("gmail.email", "")
        owner_domain = gmail_email.split('@')[-1] if '@' in gmail_email else None
        to_domain = to_lower.split('@')[-1] if '@' in to_lower else None

        if not owner_domain or not to_domain or to_domain != owner_domain:
            raise PermissionError(
                f"Send within organization only: recipient must be @{owner_domain}, not {to}"
            )
    # Most restrictive: send_owner_only
    elif perms.get("send_owner_only"):
        if to_lower not in owner_emails:
            raise PermissionError(
                f"Send to owner only: allowed recipients are {', '.join(sorted(owner_emails))}, not {to}"
            )

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

    imap = _imap_connect()
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
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# --- Trash Operations ---

def trash_message(folder: str, uid: str) -> dict:
    """Move a message to trash via IMAP. Requires trash permission."""
    check_permission("trash")

    imap = _imap_connect()
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
    finally:
        try:
            imap.logout()
        except Exception:
            pass
