"""Integration routes: Gmail, UniFi, Cloudflare, and cert management."""

import asyncio
import logging
import os
import shutil
import subprocess
from datetime import datetime

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.responses import Response

from auth import get_config, set_config, db_connection, is_localhost, get_request_user
from config import KUKUIBOT_HOME, SSL_CERT, SSL_KEY

router = APIRouter()
logger = logging.getLogger("kukuibot.server")


# --- Gmail Integration ---

@router.get("/api/gmail/status")
async def api_gmail_status():
    """Gmail connection status + permissions."""
    from gmail_bridge import get_gmail_status
    return get_gmail_status()


@router.post("/api/gmail/permissions")
async def api_gmail_permissions(req: Request):
    """Set Gmail permissions."""
    from gmail_bridge import set_permissions, get_permissions
    body = await req.json()
    perms = body.get("permissions", {})
    if not isinstance(perms, dict):
        return JSONResponse({"error": "permissions must be a dict"}, status_code=400)
    try:
        set_permissions(perms)
        return {"ok": True, "permissions": get_permissions()}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/api/gmail/send-whitelist")
async def api_gmail_send_whitelist(req: Request):
    """Set the whitelisted send domains."""
    from gmail_bridge import set_send_whitelist_domains, get_send_whitelist_domains
    body = await req.json()
    domains = body.get("domains")
    if not isinstance(domains, list):
        return JSONResponse({"error": "domains must be a list of strings"}, status_code=400)
    set_send_whitelist_domains(domains)
    return {"ok": True, "domains": get_send_whitelist_domains()}


@router.post("/api/gmail/connect")
async def api_gmail_connect(req: Request):
    """Save Gmail credentials (email + app password) and test connection."""
    from gmail_bridge import save_gmail_credentials, test_gmail_connection
    body = await req.json()
    email_addr = (body.get("email") or "").strip()
    app_password = (body.get("app_password") or "").strip()
    if not email_addr or not app_password:
        return JSONResponse({"error": "Email and app password are required"}, status_code=400)
    save_gmail_credentials(email_addr, app_password)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, test_gmail_connection)
    if not result["ok"]:
        # Credentials are saved but test failed — warn but don't clear
        return {"ok": True, "email": email_addr, "warning": result["error"]}
    return {"ok": True, "email": email_addr}


@router.post("/api/gmail/test")
async def api_gmail_test():
    """Test the saved Gmail connection."""
    from gmail_bridge import test_gmail_connection
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, test_gmail_connection)


@router.post("/api/gmail/send-test")
async def api_gmail_send_test():
    """Send a test email to the owner's Gmail address."""
    from gmail_bridge import get_gmail_status, _get_config, _get_owner_emails
    import smtplib
    from email.mime.text import MIMEText

    email_addr = _get_config("gmail.email", "")
    app_password = _get_config("gmail.app_password", "")
    if not email_addr or not app_password:
        return JSONResponse({"error": "Gmail not configured"}, status_code=400)

    # Prefer sending to a different owner email (Gmail hides self-sent emails from inbox)
    owner_emails = _get_owner_emails()
    send_to = email_addr
    for oe in sorted(owner_emails):
        if oe != email_addr.strip().lower():
            send_to = oe
            break

    def _do_send():
        msg = MIMEText(
            "This is a test email sent from KukuiBot.\n\n"
            "If you received this, your Gmail integration is working correctly.\n\n"
            f"Sent from: {email_addr}\n"
            f"Sent to: {send_to}\n\n"
            "— KukuiBot"
        )
        msg["From"] = email_addr
        msg["To"] = send_to
        msg["Subject"] = "KukuiBot Test Email"

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(email_addr, app_password)
            server.sendmail(email_addr, [send_to], msg.as_string())

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_send)
        logger.info(f"Gmail test email sent to {send_to}")
        return {"ok": True, "to": send_to}
    except Exception as e:
        logger.warning(f"Gmail test email failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/gmail/disconnect")
async def api_gmail_disconnect():
    """Disconnect Gmail — clear credentials and permissions."""
    from gmail_bridge import clear_gmail_credentials
    clear_gmail_credentials()
    return {"ok": True}


@router.post("/api/gmail/search")
async def api_gmail_search(req: Request):
    """Search Gmail messages — cache-first for browsing, IMAP-direct for search queries."""
    from gmail_bridge import list_messages
    body = await req.json()
    folder = body.get("folder", "INBOX")
    max_results = min(int(body.get("max_results", 20)), 50)
    search = body.get("search", "")
    offset = int(body.get("offset", 0))
    use_cache = body.get("cache", True)
    loop = asyncio.get_event_loop()

    # When user is actively searching, always go to IMAP to find older emails
    # not in the local cache. Cache is only used for browsing (no search query).
    if search:
        try:
            messages = await loop.run_in_executor(
                None, lambda: list_messages(folder=folder, max_results=max_results, search=search, offset=offset))
            # Cache the results
            try:
                import email_cache
                email_cache.upsert_messages(messages, folder)
            except Exception:
                pass
            return {"ok": True, "messages": messages, "count": len(messages), "source": "imap", "has_more": len(messages) == max_results}
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except Exception as e:
            logger.warning(f"Gmail search failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # No search query — try cache first for fast browsing
    if use_cache:
        try:
            import email_cache
            cached = email_cache.get_cached_messages(folder, max_results, search, offset)
            if cached:
                # Remap field names to match IMAP response format
                messages = []
                for c in cached:
                    messages.append({
                        "from": c.get("from_addr", ""),
                        "to": c.get("to_addr", ""),
                        "subject": c.get("subject", ""),
                        "date": c.get("date", ""),
                        "message_id": c.get("message_id", ""),
                        "uid": c.get("uid", ""),
                        "folder": c.get("folder", folder),
                        "is_read": c.get("is_read", False),
                        "snippet": c.get("snippet", ""),
                        "has_attachments": c.get("has_attachments", False),
                    })
                return {"ok": True, "messages": messages, "count": len(messages), "source": "cache", "has_more": len(messages) == max_results}
        except Exception as e:
            logger.warning(f"Gmail cache read failed, falling back to IMAP: {e}")

    # IMAP fallback
    try:
        messages = await loop.run_in_executor(
            None, lambda: list_messages(folder=folder, max_results=max_results, search=search, offset=offset))
        # Cache the results in background
        try:
            import email_cache
            email_cache.upsert_messages(messages, folder)
        except Exception:
            pass
        return {"ok": True, "messages": messages, "count": len(messages), "source": "imap", "has_more": len(messages) == max_results}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        logger.warning(f"Gmail search failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/gmail/message")
async def api_gmail_message(req: Request):
    """Get a single Gmail message — cache-first, IMAP fallback."""
    from gmail_bridge import get_message
    body = await req.json()
    folder = body.get("folder", "INBOX")
    uid = body.get("uid", "")
    if not uid:
        return JSONResponse({"error": "uid is required"}, status_code=400)

    # Try cache first (only if it has the full body)
    try:
        import email_cache
        cached = email_cache.get_cached_message(folder, uid)
        if cached:
            body_html = cached.get("body_html") or ""
            # Skip cache if HTML has unresolved cid: or external image URLs
            # (srcdoc iframes can't load external images — they must be data URIs)
            import re
            has_stale = ("cid:" in body_html.lower()
                         or re.search(r'<img\b[^>]+src\s*=\s*["\']https?://', body_html, re.IGNORECASE))
            if has_stale:
                logger.info(f"Gmail cache: uninlined images in {uid}, re-fetching from IMAP")
                cached = None
        if cached:
            from injection_guard import scan_and_filter
            body_text = scan_and_filter(cached.get("body_text", ""), source="email")
            body_html = cached.get("body_html")
            if body_html:
                body_html = scan_and_filter(body_html, source="email")
            message = {
                "from": cached.get("from_addr", ""),
                "to": cached.get("to_addr", ""),
                "subject": cached.get("subject", ""),
                "date": cached.get("date", ""),
                "message_id": cached.get("message_id", ""),
                "body": body_text,
                "body_html": body_html,
                "uid": cached.get("uid", uid),
                "folder": cached.get("folder", folder),
                "sensitive_findings": 0,
                "injection_filtered": False,
                "attachments": cached.get("attachment_info", []),
                "has_attachments": cached.get("has_attachments", False),
                "source": "cache",
            }
            return {"ok": True, "message": message}
    except Exception as e:
        logger.warning(f"Gmail cache message read failed, falling back to IMAP: {e}")

    # IMAP fallback
    try:
        loop = asyncio.get_event_loop()
        message = await loop.run_in_executor(None, lambda: get_message(folder, uid))
        # Cache the full message for next time
        try:
            import email_cache
            email_cache.upsert_full_message(message, folder)
        except Exception:
            pass
        return {"ok": True, "message": message}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        logger.warning(f"Gmail get message failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/gmail/folders")
async def api_gmail_folders():
    """List available Gmail IMAP folders."""
    from gmail_bridge import list_folders
    try:
        loop = asyncio.get_event_loop()
        folders = await loop.run_in_executor(None, list_folders)
        return {"ok": True, "folders": folders}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        logger.warning(f"Gmail list folders failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/gmail/draft")
async def api_gmail_draft(req: Request):
    """Create a Gmail draft via IMAP. Content sanitized before creating."""
    from gmail_bridge import create_draft
    body = await req.json()
    to = body.get("to", "")
    subject = body.get("subject", "")
    email_body = body.get("body", "")
    body_html = body.get("body_html") or None
    if not to or not subject:
        return JSONResponse({"error": "to and subject are required"}, status_code=400)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: create_draft(to, subject, email_body, body_html=body_html))
        return {"ok": True, **result}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e), "blocked": True}, status_code=422)
    except Exception as e:
        logger.warning(f"Gmail draft failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/gmail/send")
async def api_gmail_send(req: Request):
    """Send an email via Gmail SMTP. Content sanitized and permission-checked."""
    from gmail_bridge import send_email
    body = await req.json()
    to = body.get("to", "")
    subject = body.get("subject", "")
    email_body = body.get("body", "")
    body_html = body.get("body_html") or None
    if not to or not subject:
        return JSONResponse({"error": "to and subject are required"}, status_code=400)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: send_email(to, subject, email_body, body_html=body_html))
        return {"ok": True, **result}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e), "blocked": True}, status_code=422)
    except Exception as e:
        logger.warning(f"Gmail send failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/gmail/redirect")
async def api_gmail_redirect(req: Request):
    """Redirect (bounce) an email to a new recipient as-is.
    Preserves full MIME (HTML, attachments). Permission-checked and sanitized."""
    from gmail_bridge import redirect_email
    body = await req.json()
    folder = body.get("folder", "INBOX")
    uid = body.get("uid", "")
    to = body.get("to", "")
    subject = body.get("subject")  # None means keep original
    if not uid or not to:
        return JSONResponse({"error": "uid and to are required"}, status_code=400)
    if not str(uid).isdigit():
        return JSONResponse({"error": "uid must be a single numeric message ID"}, status_code=400)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: redirect_email(folder, uid, to, subject))
        return {"ok": True, **result}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e), "blocked": True}, status_code=422)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.warning(f"Gmail redirect failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/gmail/trash")
async def api_gmail_trash(req: Request):
    """Move a message to trash via IMAP. One message at a time only."""
    from gmail_bridge import trash_message
    body = await req.json()
    folder = body.get("folder", "INBOX")
    uid = body.get("uid", "")
    if not uid:
        return JSONResponse({"error": "uid is required"}, status_code=400)
    # Safety: only allow a single numeric UID — block ranges (1:*), wildcards, comma-lists
    if not str(uid).isdigit():
        return JSONResponse({"error": "uid must be a single numeric message ID — bulk trash is not allowed"}, status_code=400)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: trash_message(folder, uid))
        # Also remove from cache
        try:
            import email_cache
            email_cache.delete_message(folder, uid)
        except Exception:
            pass
        return {"ok": True, **result}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        logger.warning(f"Gmail trash failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/gmail/flags")
async def api_gmail_flags(req: Request):
    """Set flags on a message (e.g. mark read/unread)."""
    from gmail_bridge import set_message_flags
    body = await req.json()
    folder = body.get("folder", "INBOX")
    uid = body.get("uid", "")
    flags = body.get("flags", {})
    if not uid:
        return JSONResponse({"error": "uid is required"}, status_code=400)
    if not str(uid).isdigit():
        return JSONResponse({"error": "uid must be a single numeric message ID"}, status_code=400)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: set_message_flags(folder, uid, flags))
        # Also update cache
        try:
            import email_cache
            if "seen" in flags:
                email_cache.update_message_flag(folder, uid, bool(flags["seen"]))
        except Exception:
            pass
        return {"ok": True, **result}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        logger.warning(f"Gmail flags failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/gmail/attachment")
async def api_gmail_attachment(folder: str = "", uid: str = "", filename: str = ""):
    """Download an email attachment by folder, uid, and filename."""
    from gmail_bridge import get_attachment
    if not folder or not uid or not filename:
        return JSONResponse({"error": "folder, uid, and filename are required"}, status_code=400)
    if not str(uid).isdigit():
        return JSONResponse({"error": "uid must be numeric"}, status_code=400)
    try:
        loop = asyncio.get_event_loop()
        content_bytes, content_type, fname = await loop.run_in_executor(
            None, lambda: get_attachment(folder, uid, filename))
        return Response(
            content=content_bytes,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.warning(f"Gmail attachment download failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/gmail/sync-status")
async def api_gmail_sync_status():
    """Return email cache sync status."""
    try:
        import email_cache
        return {"ok": True, **email_cache.get_sync_status()}
    except Exception as e:
        logger.warning(f"Gmail sync status failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/gmail/send-report")
async def api_gmail_send_report(req: Request):
    """Send a local HTML report file as an email."""
    from gmail_bridge import send_html_report
    body = await req.json()
    to = body.get("to", "")
    subject = body.get("subject", "KukuiBot Report")
    html_path = body.get("html_path", "")
    if not to or not html_path:
        return JSONResponse({"error": "to and html_path are required"}, status_code=400)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: send_html_report(to, subject, html_path))
        return {"ok": True, **result}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.warning(f"Gmail send-report failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/gmail/draft-report")
async def api_gmail_draft_report(req: Request):
    """Save a local HTML report file as a Gmail draft."""
    from gmail_bridge import draft_html_report
    body = await req.json()
    to = body.get("to", "")
    subject = body.get("subject", "KukuiBot Report")
    html_path = body.get("html_path", "")
    if not to or not html_path:
        return JSONResponse({"error": "to and html_path are required"}, status_code=400)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: draft_html_report(to, subject, html_path))
        return {"ok": True, **result}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.warning(f"Gmail draft-report failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# --- UniFi UDM Integration ---

@router.get("/api/unifi/status")
async def api_unifi_status():
    """UniFi connection status."""
    from unifi_bridge import get_status
    return get_status()


@router.post("/api/unifi/connect")
async def api_unifi_connect(req: Request):
    """Save UniFi UDM credentials and test connection."""
    from unifi_bridge import save_credentials, test_connection
    body = await req.json()
    host = (body.get("host") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    verify_ssl = bool(body.get("verify_ssl", False))
    site = (body.get("site") or "default").strip()
    if not host or not api_key:
        return JSONResponse({"error": "Host and API key are required"}, status_code=400)
    save_credentials(host, api_key, verify_ssl, site)
    result = test_connection()
    if not result["ok"]:
        return {"ok": True, "host": host, "warning": result["error"]}
    return {"ok": True, "host": host, "info": result.get("info", {})}


@router.post("/api/unifi/test")
async def api_unifi_test():
    """Test the saved UniFi connection."""
    from unifi_bridge import test_connection
    return test_connection()


@router.post("/api/unifi/disconnect")
async def api_unifi_disconnect():
    """Disconnect UniFi — clear credentials."""
    from unifi_bridge import clear_credentials
    clear_credentials()
    return {"ok": True}


@router.get("/api/unifi/clients")
async def api_unifi_clients():
    """List all connected UniFi clients."""
    from unifi_bridge import list_clients
    try:
        clients = list_clients()
        return {"ok": True, "clients": clients, "count": len(clients)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/unifi/access-points")
async def api_unifi_access_points():
    """List all UniFi access points."""
    from unifi_bridge import list_access_points
    try:
        aps = list_access_points()
        return {"ok": True, "access_points": aps, "count": len(aps)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/unifi/firewall-rules")
async def api_unifi_firewall_rules():
    """List all firewall rules."""
    from unifi_bridge import list_firewall_rules
    try:
        rules = list_firewall_rules()
        return {"ok": True, "rules": rules, "count": len(rules)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/unifi/firewall-rules")
async def api_unifi_create_firewall_rule(req: Request):
    """Create a new firewall rule."""
    from unifi_bridge import create_firewall_rule
    body = await req.json()
    rule = body.get("rule", {})
    if not rule:
        return JSONResponse({"error": "rule object is required"}, status_code=400)
    try:
        created = create_firewall_rule(rule)
        return {"ok": True, "rule": created}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/unifi/firewall-rules/{rule_id}")
async def api_unifi_update_firewall_rule(rule_id: str, req: Request):
    """Update an existing firewall rule."""
    from unifi_bridge import update_firewall_rule
    body = await req.json()
    rule = body.get("rule", {})
    if not rule:
        return JSONResponse({"error": "rule object is required"}, status_code=400)
    try:
        updated = update_firewall_rule(rule_id, rule)
        return {"ok": True, "rule": updated}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/api/unifi/firewall-rules/{rule_id}")
async def api_unifi_delete_firewall_rule(rule_id: str):
    """Delete a firewall rule."""
    from unifi_bridge import delete_firewall_rule
    try:
        delete_firewall_rule(rule_id)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/unifi/locate/{mac}")
async def api_unifi_locate(mac: str):
    """Find which AP a device (by MAC) is connected to."""
    from unifi_bridge import get_client_ap_location
    try:
        result = get_client_ap_location(mac)
        if result is None:
            return JSONResponse({"error": "Device not found or not connected"}, status_code=404)
        return {"ok": True, **result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================
# CLOUDFLARE INTEGRATION
# =============================================

CF_API_BASE = "https://api.cloudflare.com/client/v4"


def _cf_token() -> str:
    return get_config("cloudflare.api_token", "")


def _cf_headers() -> dict:
    token = _cf_token()
    if not token:
        raise ValueError("Cloudflare API token not configured")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@router.get("/api/cloudflare/status")
async def api_cf_status():
    """Check Cloudflare connection and list zones."""
    token = _cf_token()
    if not token:
        return {"connected": False, "error": "Not configured", "zones": []}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{CF_API_BASE}/zones?per_page=50", headers=_cf_headers())
            data = r.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            msg = errors[0].get("message", "Unknown error") if errors else "Auth failed"
            return {"connected": False, "error": msg, "zones": []}
        zones = [{"id": z["id"], "name": z["name"], "status": z["status"]} for z in data.get("result", [])]
        return {"connected": True, "error": "", "zones": zones}
    except Exception as e:
        return {"connected": False, "error": str(e), "zones": []}


@router.post("/api/cloudflare/connect")
async def api_cf_connect(request: Request):
    """Save Cloudflare API token and test connection."""
    body = await request.json()
    token = body.get("api_token", "").strip()
    if not token:
        return JSONResponse({"error": "API token is required"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{CF_API_BASE}/zones?per_page=5",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            data = r.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            msg = errors[0].get("message", "Invalid token") if errors else "Auth failed"
            return JSONResponse({"error": msg}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Connection failed: {e}"}, status_code=400)
    set_config("cloudflare.api_token", token)
    zones = [{"id": z["id"], "name": z["name"], "status": z["status"]} for z in data.get("result", [])]
    return {"ok": True, "zones": zones}


@router.post("/api/cloudflare/disconnect")
async def api_cf_disconnect():
    """Remove Cloudflare API token."""
    set_config("cloudflare.api_token", "")
    return {"ok": True}


# --- Cloudflare DNS Records Management ---

@router.get("/api/cloudflare/dns")
async def api_cf_dns_list(zone_id: str = ""):
    """List DNS records for a zone."""
    if not zone_id:
        return JSONResponse({"error": "zone_id is required"}, status_code=400)
    try:
        records = []
        page = 1
        while True:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{CF_API_BASE}/zones/{zone_id}/dns_records?per_page=100&page={page}",
                    headers=_cf_headers()
                )
                data = r.json()
            if not data.get("success"):
                errors = data.get("errors", [])
                return JSONResponse({"error": errors[0].get("message") if errors else "Failed"}, status_code=400)
            for rec in data.get("result", []):
                records.append({
                    "id": rec["id"], "type": rec["type"], "name": rec["name"],
                    "content": rec["content"], "ttl": rec["ttl"],
                    "proxied": rec.get("proxied", False), "priority": rec.get("priority"),
                })
            info = data.get("result_info", {})
            if page >= info.get("total_pages", 1):
                break
            page += 1
        return {"ok": True, "records": records, "count": len(records)}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/cloudflare/dns")
async def api_cf_dns_create(request: Request):
    """Create a DNS record."""
    body = await request.json()
    zone_id = body.get("zone_id", "").strip()
    rec_type = body.get("type", "").strip().upper()
    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    ttl = body.get("ttl", 1)
    proxied = body.get("proxied", False)
    priority = body.get("priority")

    if not zone_id or not rec_type or not name or not content:
        return JSONResponse({"error": "zone_id, type, name, and content are required"}, status_code=400)

    payload = {"type": rec_type, "name": name, "content": content, "ttl": ttl}
    if rec_type in ("A", "AAAA", "CNAME"):
        payload["proxied"] = proxied
    if rec_type == "MX" and priority is not None:
        payload["priority"] = int(priority)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{CF_API_BASE}/zones/{zone_id}/dns_records",
                headers=_cf_headers(), json=payload
            )
            data = r.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            return JSONResponse({"error": errors[0].get("message") if errors else "Failed"}, status_code=400)
        rec = data["result"]
        return {"ok": True, "record": {
            "id": rec["id"], "type": rec["type"], "name": rec["name"],
            "content": rec["content"], "ttl": rec["ttl"],
            "proxied": rec.get("proxied", False), "priority": rec.get("priority"),
        }}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/cloudflare/dns/{record_id}")
async def api_cf_dns_update(record_id: str, request: Request):
    """Update a DNS record."""
    body = await request.json()
    zone_id = body.get("zone_id", "").strip()
    if not zone_id:
        return JSONResponse({"error": "zone_id is required"}, status_code=400)

    payload = {}
    for field in ("type", "name", "content", "ttl", "proxied", "priority"):
        if field in body:
            val = body[field]
            if field == "type":
                val = val.strip().upper()
            if field == "priority" and val is not None:
                val = int(val)
            payload[field] = val

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.put(
                f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}",
                headers=_cf_headers(), json=payload
            )
            data = r.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            return JSONResponse({"error": errors[0].get("message") if errors else "Failed"}, status_code=400)
        rec = data["result"]
        return {"ok": True, "record": {
            "id": rec["id"], "type": rec["type"], "name": rec["name"],
            "content": rec["content"], "ttl": rec["ttl"],
            "proxied": rec.get("proxied", False), "priority": rec.get("priority"),
        }}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/api/cloudflare/dns/{record_id}")
async def api_cf_dns_delete(record_id: str, zone_id: str = ""):
    """Delete a DNS record."""
    if not zone_id:
        return JSONResponse({"error": "zone_id query param required"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.delete(
                f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}",
                headers=_cf_headers()
            )
            data = r.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            return JSONResponse({"error": errors[0].get("message") if errors else "Failed"}, status_code=400)
        return {"ok": True}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================
# SSL CERTIFICATE MANAGEMENT
# =============================================

LETSENCRYPT_CONFIG = os.path.expanduser("~/letsencrypt/config")
LETSENCRYPT_WORK = os.path.expanduser("~/letsencrypt/work")
LETSENCRYPT_LOGS = os.path.expanduser("~/letsencrypt/logs")


def _ensure_certbot_dirs():
    """Ensure certbot directories exist and log file is writable."""
    for d in (LETSENCRYPT_CONFIG, LETSENCRYPT_WORK, LETSENCRYPT_LOGS):
        os.makedirs(d, exist_ok=True)
    log_file = os.path.join(LETSENCRYPT_LOGS, "letsencrypt.log")
    if os.path.exists(log_file) and not os.access(log_file, os.W_OK):
        try:
            os.chmod(log_file, 0o644)
        except OSError:
            raise PermissionError(
                f"Cannot write to {log_file} — it may be owned by root. "
                f"Run: sudo chown {os.getenv('USER', 'jarvis')}:staff {log_file}"
            )


def _ensure_cf_creds_file() -> str:
    """Write a temp cloudflare.ini from DB-stored token for certbot."""
    token = _cf_token()
    if not token:
        raise ValueError("Cloudflare API token not configured — set it in Settings > Cloudflare")
    creds_path = os.path.join(LETSENCRYPT_CONFIG, ".cloudflare-credentials.ini")
    os.makedirs(os.path.dirname(creds_path), exist_ok=True)
    with open(creds_path, "w") as f:
        f.write(f"dns_cloudflare_api_token = {token}\n")
    os.chmod(creds_path, 0o600)
    return creds_path


@router.get("/api/certs/list")
async def api_certs_list():
    """List all managed Let's Encrypt certificates."""
    live_dir = os.path.join(LETSENCRYPT_CONFIG, "live")
    if not os.path.isdir(live_dir):
        return {"ok": True, "certs": []}
    certs = []
    for name in sorted(os.listdir(live_dir)):
        cert_path = os.path.join(live_dir, name, "fullchain.pem")
        if not os.path.isfile(cert_path):
            continue
        try:
            r = subprocess.run(
                ["openssl", "x509", "-in", cert_path, "-noout", "-subject", "-enddate", "-ext", "subjectAltName"],
                capture_output=True, text=True, timeout=5
            )
            subject = ""
            expiry = ""
            sans = []
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("subject="):
                    subject = line.split("=", 1)[1].strip()
                elif line.startswith("notAfter="):
                    expiry = line.split("=", 1)[1].strip()
                elif line.startswith("DNS:"):
                    sans = [s.strip() for s in line.split(",")]
            r2 = subprocess.run(
                ["openssl", "x509", "-in", cert_path, "-noout", "-text"],
                capture_output=True, text=True, timeout=5
            )
            key_type = "unknown"
            for line in r2.stdout.splitlines():
                if "Public Key Algorithm" in line:
                    if "rsa" in line.lower():
                        key_type = "RSA"
                    elif "ec" in line.lower():
                        key_type = "ECDSA"
                    break
            certs.append({
                "name": name, "subject": subject, "expiry": expiry,
                "sans": sans, "key_type": key_type,
            })
        except Exception as e:
            certs.append({"name": name, "error": str(e)})
    return {"ok": True, "certs": certs}


@router.post("/api/certs/generate")
async def api_certs_generate(request: Request):
    """Generate a Let's Encrypt cert for a given domain (wildcard supported)."""
    body = await request.json()
    domain = body.get("domain", "").strip()
    key_type = body.get("key_type", "rsa").lower()
    cert_name = body.get("cert_name", "").strip()

    if not domain:
        return JSONResponse({"error": "domain is required"}, status_code=400)

    try:
        _ensure_certbot_dirs()
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    try:
        creds_path = _ensure_cf_creds_file()
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if not cert_name:
        cert_name = domain.replace("*.", "wildcard-").replace(".", "-")
    domains = [domain]
    if domain.startswith("*."):
        bare = domain[2:]
        domains.append(bare)

    cmd = [
        "certbot", "certonly",
        "--dns-cloudflare",
        "--dns-cloudflare-credentials", creds_path,
        "--config-dir", LETSENCRYPT_CONFIG,
        "--work-dir", LETSENCRYPT_WORK,
        "--logs-dir", LETSENCRYPT_LOGS,
        "--key-type", key_type,
        "--cert-name", cert_name,
        "--non-interactive", "--agree-tos",
    ]
    if key_type == "rsa":
        cmd.extend(["--rsa-key-size", "2048"])
    for d in domains:
        cmd.extend(["-d", d])

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return JSONResponse({"error": f"certbot failed: {r.stderr}"}, status_code=500)
        return {
            "ok": True, "cert_name": cert_name, "domains": domains,
            "key_type": key_type, "message": r.stdout.strip(),
        }
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "certbot timed out (120s)"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


_cert_renew_jobs: dict[str, dict] = {}


@router.post("/api/certs/renew")
async def api_certs_renew(request: Request):
    """Kick off a cert renewal in the background."""
    body = await request.json()
    cert_name = body.get("cert_name", "").strip()
    if not cert_name:
        return JSONResponse({"error": "cert_name is required"}, status_code=400)
    safe_name = os.path.basename(cert_name)
    cert_dir = os.path.join(LETSENCRYPT_CONFIG, "live", safe_name)
    if not os.path.isdir(cert_dir):
        return JSONResponse({"error": f"Certificate '{safe_name}' not found"}, status_code=404)
    if _cert_renew_jobs.get(safe_name, {}).get("status") == "running":
        return {"ok": True, "status": "running", "cert_name": safe_name}
    try:
        _ensure_certbot_dirs()
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    try:
        creds_path = _ensure_cf_creds_file()
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    _cert_renew_jobs[safe_name] = {"status": "running", "message": "", "error": ""}

    async def _run_renew():
        cmd = [
            "certbot", "renew",
            "--cert-name", safe_name,
            "--dns-cloudflare",
            "--dns-cloudflare-credentials", creds_path,
            "--config-dir", LETSENCRYPT_CONFIG,
            "--work-dir", LETSENCRYPT_WORK,
            "--logs-dir", LETSENCRYPT_LOGS,
            "--non-interactive", "--force-renewal",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                _cert_renew_jobs[safe_name] = {"status": "error", "message": "", "error": stderr.decode().strip()}
            else:
                _cert_renew_jobs[safe_name] = {"status": "done", "message": stdout.decode().strip(), "error": ""}
        except asyncio.TimeoutError:
            _cert_renew_jobs[safe_name] = {"status": "error", "message": "", "error": "certbot timed out (120s)"}
        except Exception as e:
            _cert_renew_jobs[safe_name] = {"status": "error", "message": "", "error": str(e)}

    asyncio.create_task(_run_renew())
    return {"ok": True, "status": "running", "cert_name": safe_name}


@router.get("/api/certs/renew/status")
async def api_certs_renew_status(name: str = ""):
    """Poll renewal status for a cert."""
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    safe_name = os.path.basename(name)
    job = _cert_renew_jobs.get(safe_name)
    if not job:
        return {"status": "none"}
    return job


@router.post("/api/certs/install")
async def api_certs_install(request: Request):
    """Install a managed cert as KukuiBot's HTTPS cert, back up existing, and restart."""
    body = await request.json()
    cert_name = body.get("cert_name", "").strip()
    if not cert_name:
        return JSONResponse({"error": "cert_name is required"}, status_code=400)
    safe_name = os.path.basename(cert_name)
    live_path = os.path.join(LETSENCRYPT_CONFIG, "live", safe_name)
    if not os.path.isdir(live_path):
        return JSONResponse({"error": f"Certificate '{safe_name}' not found"}, status_code=404)
    fullchain = os.path.join(live_path, "fullchain.pem")
    privkey = os.path.join(live_path, "privkey.pem")
    if not os.path.isfile(fullchain) or not os.path.isfile(privkey):
        return JSONResponse({"error": "fullchain.pem or privkey.pem missing"}, status_code=400)
    certs_dir = SSL_CERT.parent
    certs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if SSL_CERT.exists():
        shutil.copy2(SSL_CERT, certs_dir / f"kukuibot.pem.bak-{ts}")
    if SSL_KEY.exists():
        shutil.copy2(SSL_KEY, certs_dir / f"kukuibot-key.pem.bak-{ts}")
    shutil.copy2(fullchain, SSL_CERT)
    shutil.copy2(privkey, SSL_KEY)
    logger.info(f"Installed cert '{safe_name}' -> {SSL_CERT}, {SSL_KEY} (backed up with ts {ts})")

    async def _delayed_restart():
        await asyncio.sleep(1.5)
        logger.info("Restarting after cert install...")
        os._exit(0)

    asyncio.create_task(_delayed_restart())
    return {"ok": True, "cert_name": safe_name, "backup_ts": ts}


@router.get("/api/certs/download")
async def api_certs_download(name: str = "", file: str = "zip"):
    """Download cert files. file=zip|fullchain|privkey|cert"""
    if not name:
        return JSONResponse({"error": "Specify ?name=cert-name"}, status_code=400)
    safe_name = os.path.basename(name)
    live_path = os.path.join(LETSENCRYPT_CONFIG, "live", safe_name)
    if not os.path.isdir(live_path):
        return JSONResponse({"error": f"Certificate '{safe_name}' not found"}, status_code=404)

    if file == "zip":
        import tempfile
        import zipfile
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in ("fullchain.pem", "privkey.pem", "cert.pem", "chain.pem"):
                fpath = os.path.join(live_path, fname)
                if os.path.isfile(fpath):
                    real_path = os.path.realpath(fpath)
                    zf.write(real_path, fname)
        return FileResponse(tmp.name, media_type="application/zip", filename=f"{safe_name}-certs.zip")
    else:
        fname_map = {"fullchain": "fullchain.pem", "privkey": "privkey.pem", "cert": "cert.pem", "chain": "chain.pem"}
        fname = fname_map.get(file)
        if not fname:
            return JSONResponse({"error": "file must be zip|fullchain|privkey|cert|chain"}, status_code=400)
        fpath = os.path.realpath(os.path.join(live_path, fname))
        if not os.path.isfile(fpath):
            return JSONResponse({"error": f"{fname} not found"}, status_code=404)
        return FileResponse(fpath, media_type="application/x-pem-file", filename=f"{safe_name}-{fname}")
