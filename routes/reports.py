"""
routes/reports.py — Nightly Report Config + Report Manager API
Extracted from server.py — report generation, listing, sending, deletion.
"""

import logging
import re
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from auth import db_connection, get_config, set_config
from config import KUKUIBOT_HOME

logger = logging.getLogger("kukuibot.reports")

router = APIRouter()


# --- Nightly Report Config + Report Manager API ---

_NIGHTLY_DEFAULTS = {
    "enabled": "1",
    "schedule": "0 3 * * *",
    "to_email": "",
    "send_email": "1",
}

_REPORT_DEFAULTS = {
    "time": "03:00",
    "timezone": "America/Los_Angeles",
    "send_email": "1",
    "to_email": "",
}

_REPORT_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_REPORT_FILE_RE = re.compile(r"^[\w.-]+\.html$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _extract_report_date(name: str) -> str:
    if "-nightly" in name:
        return name.split("-nightly", 1)[0]
    if name.endswith(".html"):
        return name[:-5]
    return name


def _default_owner_email() -> str:
    user_md = KUKUIBOT_HOME / "USER.md"
    if not user_md.exists():
        return ""
    emails = []
    for m in re.finditer(r"[\w.+-]+@[\w.-]+\.\w+", user_md.read_text()):
        emails.append(m.group())
    return emails[0] if emails else ""


def _gmail_capabilities() -> dict:
    """Normalized Gmail capability snapshot for report APIs."""
    email_addr = (get_config("gmail.email", "") or "").strip()
    app_password = (get_config("gmail.app_password", "") or "").strip()
    connected = bool(email_addr and app_password)
    perms = {
        "send_owner_only": _truthy(get_config("gmail.perm.send_owner_only", "0")),
        "send_anyone": _truthy(get_config("gmail.perm.send_anyone", "0")),
    }
    reason = ""

    try:
        from gmail_bridge import get_gmail_status
        st = get_gmail_status() or {}
        connected = bool(st.get("connected", connected))
        email_addr = (st.get("email") or email_addr).strip()
        status_perms = st.get("permissions") or {}
        if isinstance(status_perms, dict):
            perms["send_owner_only"] = bool(status_perms.get("send_owner_only", perms["send_owner_only"]))
            perms["send_anyone"] = bool(status_perms.get("send_anyone", perms["send_anyone"]))
    except Exception as e:
        reason = f"Gmail status lookup degraded: {e}"

    can_send = bool(perms.get("send_owner_only") or perms.get("send_anyone"))
    if not reason:
        if not connected:
            reason = "Gmail is not connected"
        elif not can_send:
            reason = "Gmail send permission is disabled"

    return {
        "connected": connected,
        "can_send": can_send,
        "email": email_addr,
        "reason": "" if (connected and can_send) else reason,
    }


def _time_to_cron(value: str) -> str | None:
    """Convert HH:MM to minute/hour cron or return None for off/invalid."""
    value = (value or "").strip().lower()
    if value == "off":
        return None
    m = _REPORT_TIME_RE.match(value)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    return f"{mm} {hh} * * *"


def _cron_to_time(expr: str) -> str:
    """Convert simple daily cron to HH:MM; fallback to default time."""
    expr = (expr or "").strip()
    if not expr:
        return "off"
    parts = expr.split()
    if len(parts) == 5 and parts[2:] == ["*", "*", "*"]:
        try:
            minute = int(parts[0])
            hour = int(parts[1])
            if 0 <= minute <= 59 and 0 <= hour <= 23:
                return f"{hour:02d}:{minute:02d}"
        except Exception:
            pass
    return _REPORT_DEFAULTS["time"]


def _is_valid_email(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return True
    return bool(_EMAIL_RE.match(value))


_NIGHTLY_REPORT_SLUG = "nightly-activity-report"


def _get_scheduler():
    """Get the scheduler singleton — imported at call time to avoid circular imports."""
    from routes.scheduler import _get_scheduler as _sched
    return _sched()


def _sync_nightly_report_cron_job():
    """Ensure a scheduled_jobs entry exists (or is updated) for the nightly report.

    Reads current report_manager / nightly_report config and creates, updates,
    or disables the corresponding scheduled job so sync_launchd() writes it out.
    """
    try:
        mgr = _get_scheduler()

        # Determine desired state from config
        time_value = (get_config("report_manager.time", "") or "").strip().lower()
        if not time_value or time_value == "off":
            enabled = False
            cron_expr = get_config("nightly_report.schedule", _NIGHTLY_DEFAULTS["schedule"])
        else:
            enabled = True
            cron_expr = _time_to_cron(time_value) or _NIGHTLY_DEFAULTS["schedule"]

        send_email = _truthy(get_config("report_manager.send_email", get_config("nightly_report.send_email", "1")))
        to_email = (get_config("report_manager.to_email", get_config("nightly_report.to_email", "")) or "").strip()

        # Build the command
        script = Path(__file__).parent.parent / "nightly_activity_report.py"
        cmd_parts = [sys.executable, str(script), "--window", "yesterday"]
        if to_email:
            cmd_parts += ["--to", to_email]
        if send_email:
            cmd_parts.append("--send-email")
        command = " ".join(cmd_parts)
        log_path = str(KUKUIBOT_HOME / "logs" / "nightly-report.log")
        command_full = f"{command} >> {log_path} 2>&1"

        # Check if job already exists
        existing = mgr.get_job(_NIGHTLY_REPORT_SLUG)
        if existing:
            mgr.update_job(existing["id"],
                           cron_expr=cron_expr,
                           command=command_full,
                           enabled=enabled)
            logger.info(f"Updated nightly report cron job: enabled={enabled}, cron={cron_expr}")
        else:
            mgr.create_job(
                name="Nightly Activity Report",
                slug=_NIGHTLY_REPORT_SLUG,
                command=command_full,
                cron_expr=cron_expr,
                description="Generate and email the daily KukuiBot activity report",
                category="report",
                enabled=enabled,
                source="system",
                timezone="America/Los_Angeles",
            )
            logger.info(f"Created nightly report cron job: enabled={enabled}, cron={cron_expr}")
    except Exception as e:
        logger.error(f"Failed to sync nightly report cron job: {e}")


def _ensure_launchd_sync():
    """Ensure all DB scheduled jobs have launchd plists on startup."""
    import platform
    if platform.system() == "Windows":
        logger.info("Skipping launchd sync on Windows")
        return
    try:
        mgr = _get_scheduler()
        mgr.sync_launchd()
        logger.info("Startup launchd sync complete")
    except Exception as e:
        logger.error(f"Startup launchd sync failed: {e}")


def _ensure_report_history_table():
    with db_connection() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS report_history (
              report_name TEXT PRIMARY KEY,
              report_date TEXT NOT NULL,
              size_bytes INTEGER NOT NULL DEFAULT 0,
              generated_at INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'generated',
              last_sent_at INTEGER,
              last_sent_to TEXT,
              last_error TEXT DEFAULT '',
              updated_at INTEGER NOT NULL
            )
            """
        )
        db.commit()


def _upsert_report_history(report_name: str, report_date: str, size_bytes: int, status: str = "generated", *,
                           generated_at: int | None = None, last_error: str = ""):
    _ensure_report_history_table()
    now = int(time.time())
    generated = int(generated_at or now)
    with db_connection() as db:
        db.execute(
            """
            INSERT INTO report_history
              (report_name, report_date, size_bytes, generated_at, status, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_name) DO UPDATE SET
              report_date=excluded.report_date,
              size_bytes=excluded.size_bytes,
              generated_at=excluded.generated_at,
              status=excluded.status,
              last_error=excluded.last_error,
              updated_at=excluded.updated_at
            """,
            (report_name, report_date, int(size_bytes), generated, status, (last_error or "")[:2000], now),
        )
        db.commit()


def _mark_report_sent(report_name: str, to_email: str):
    _ensure_report_history_table()
    now = int(time.time())
    path = KUKUIBOT_HOME / "daily_reports" / report_name
    size = path.stat().st_size if path.exists() else 0
    with db_connection() as db:
        db.execute(
            """
            INSERT INTO report_history
              (report_name, report_date, size_bytes, generated_at, status, last_sent_at, last_sent_to, last_error, updated_at)
            VALUES (?, ?, ?, ?, 'sent', ?, ?, '', ?)
            ON CONFLICT(report_name) DO UPDATE SET
              status='sent',
              size_bytes=excluded.size_bytes,
              last_sent_at=excluded.last_sent_at,
              last_sent_to=excluded.last_sent_to,
              last_error='',
              updated_at=excluded.updated_at
            """,
            (
                report_name,
                _extract_report_date(report_name),
                int(size),
                now,
                now,
                (to_email or "")[:320],
                now,
            ),
        )
        db.commit()


def _delete_report_history(report_name: str):
    _ensure_report_history_table()
    with db_connection() as db:
        db.execute("DELETE FROM report_history WHERE report_name = ?", (report_name,))
        db.commit()


def _extract_saved_report_path(output: str) -> str:
    for line in (output or "").splitlines():
        if line.startswith("saved_report="):
            return line.split("=", 1)[1].strip()
    return ""


def _build_reports_config() -> dict:
    legacy_enabled = _truthy(get_config("nightly_report.enabled", _NIGHTLY_DEFAULTS["enabled"]))
    legacy_cron = get_config("nightly_report.schedule", _NIGHTLY_DEFAULTS["schedule"]).strip() or _NIGHTLY_DEFAULTS["schedule"]

    time_value = (get_config("report_manager.time", "") or "").strip().lower()
    if time_value and time_value != "off" and not _REPORT_TIME_RE.match(time_value):
        time_value = ""
    if not time_value:
        time_value = _cron_to_time(legacy_cron)
        if not legacy_enabled:
            time_value = "off"

    timezone = (get_config("report_manager.timezone", "") or "").strip() or _REPORT_DEFAULTS["timezone"]

    send_email = _truthy(get_config(
        "report_manager.send_email",
        get_config("nightly_report.send_email", _NIGHTLY_DEFAULTS["send_email"]),
    ))

    to_email = (get_config(
        "report_manager.to_email",
        get_config("nightly_report.to_email", _NIGHTLY_DEFAULTS["to_email"]),
    ) or "").strip()
    if not to_email:
        to_email = _default_owner_email()

    enabled = (time_value != "off")
    gmail = _gmail_capabilities()

    return {
        "ok": True,
        "schedule": {
            "time": time_value,
            "timezone": timezone,
            "enabled": enabled,
            "legacy_cron": _time_to_cron(time_value) if enabled else legacy_cron,
        },
        "delivery": {
            "send_email": bool(send_email),
            "to_email": to_email,
        },
        "gmail": gmail,
    }


@router.get("/api/nightly-report/config")
async def api_nightly_config_get():
    """Get legacy nightly report configuration (backward compatible)."""
    cfg = {}
    for key, default in _NIGHTLY_DEFAULTS.items():
        cfg[key] = get_config(f"nightly_report.{key}", default)

    if not cfg["to_email"]:
        cfg["to_email"] = _default_owner_email()

    gmail = _gmail_capabilities()
    cfg["gmail_connected"] = gmail["connected"]
    cfg["gmail"] = gmail
    cfg["gmail_status"] = "connected" if gmail["connected"] else "disconnected"

    reports_dir = KUKUIBOT_HOME / "daily_reports"
    reports = []
    if reports_dir.is_dir():
        for f in sorted(reports_dir.glob("*.html"), reverse=True)[:30]:
            reports.append({"name": f.name, "date": _extract_report_date(f.name), "size": f.stat().st_size})
    cfg["reports"] = reports
    return cfg


@router.post("/api/nightly-report/config")
async def api_nightly_config_set(req: Request):
    """Save legacy nightly report configuration and sync report_manager keys."""
    body = await req.json()
    for key in _NIGHTLY_DEFAULTS:
        if key in body:
            set_config(f"nightly_report.{key}", str(body[key]))

    # Keep new report_manager keys in sync for backward compatibility.
    enabled = _truthy(get_config("nightly_report.enabled", _NIGHTLY_DEFAULTS["enabled"]))
    legacy_cron = get_config("nightly_report.schedule", _NIGHTLY_DEFAULTS["schedule"])
    time_value = _cron_to_time(legacy_cron)
    if not enabled:
        time_value = "off"
    set_config("report_manager.time", time_value)
    set_config("report_manager.timezone", get_config("report_manager.timezone", _REPORT_DEFAULTS["timezone"]))
    set_config("report_manager.send_email", "1" if _truthy(get_config("nightly_report.send_email", "1")) else "0")
    set_config("report_manager.to_email", get_config("nightly_report.to_email", ""))

    # Sync the scheduled_jobs entry so launchd actually runs the report
    _sync_nightly_report_cron_job()

    return {"ok": True}


@router.post("/api/nightly-report/run")
async def api_nightly_report_run(req: Request):
    """Run the nightly report now (legacy endpoint, still supported)."""
    import subprocess as _sp
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    dry_run = body.get("dry_run", False)
    script = Path(__file__).parent.parent / "nightly_activity_report.py"
    if not script.exists():
        return JSONResponse({"ok": False, "error": "Script not found"}, status_code=404)
    to_email = get_config("nightly_report.to_email", "")
    send = get_config("nightly_report.send_email", "1") == "1"
    cmd = [sys.executable, str(script), "--window", "yesterday"]
    if to_email:
        cmd += ["--to", to_email]
    if send and not dry_run:
        cmd.append("--send-email")
    if dry_run:
        cmd.append("--dry-run")
    try:
        result = _sp.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(KUKUIBOT_HOME))
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            return JSONResponse({"ok": False, "error": (result.stderr or "").strip() or "Script failed", "output": output}, status_code=500)

        saved_path = _extract_saved_report_path(output)
        if saved_path:
            p = Path(saved_path)
            if p.exists():
                _upsert_report_history(p.name, _extract_report_date(p.name), p.stat().st_size, status="generated")
        return {"ok": True, "output": output}
    except _sp.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "Script timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/nightly-report/view/{filename}")
async def api_nightly_report_view(filename: str):
    """Serve a saved nightly report HTML file."""
    if not _REPORT_FILE_RE.match(filename):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    path = KUKUIBOT_HOME / "daily_reports" / filename
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/api/reports/config")
async def api_reports_config_get():
    """Canonical report manager config endpoint."""
    try:
        return _build_reports_config()
    except Exception as e:
        logger.warning(f"Reports config get failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/reports/config")
async def api_reports_config_set(req: Request):
    """Save report manager schedule + delivery (time-picker format)."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    time_value = str(body.get("time", "")).strip().lower()
    if not time_value:
        return JSONResponse({"ok": False, "error": "time is required (off or HH:MM)"}, status_code=400)
    if time_value != "off" and not _REPORT_TIME_RE.match(time_value):
        return JSONResponse({"ok": False, "error": "time must be 'off' or HH:MM (24-hour)"}, status_code=400)

    timezone_value = str(body.get("timezone", "")).strip() or _REPORT_DEFAULTS["timezone"]
    send_email = _truthy(body.get("send_email", get_config("report_manager.send_email", "1")))
    to_email = str(body.get("to_email", "")).strip()
    if not _is_valid_email(to_email):
        return JSONResponse({"ok": False, "error": "Invalid to_email"}, status_code=400)

    # Canonical report_manager keys
    set_config("report_manager.time", time_value)
    set_config("report_manager.timezone", timezone_value)
    set_config("report_manager.send_email", "1" if send_email else "0")
    set_config("report_manager.to_email", to_email)

    # Legacy nightly_report key sync
    enabled = (time_value != "off")
    legacy_cron = _time_to_cron(time_value) if enabled else get_config("nightly_report.schedule", _NIGHTLY_DEFAULTS["schedule"])
    set_config("nightly_report.enabled", "1" if enabled else "0")
    set_config("nightly_report.schedule", legacy_cron or _NIGHTLY_DEFAULTS["schedule"])
    set_config("nightly_report.send_email", "1" if send_email else "0")
    set_config("nightly_report.to_email", to_email)

    # Sync the scheduled_jobs entry so launchd actually runs the report
    _sync_nightly_report_cron_job()

    cfg = _build_reports_config()
    return {
        "ok": True,
        "schedule": cfg["schedule"],
        "delivery": cfg["delivery"],
        "gmail": cfg["gmail"],
    }


@router.post("/api/reports/run")
async def api_reports_run(req: Request):
    """On-demand report generation using nightly_activity_report.py."""
    import subprocess as _sp

    body = {}
    try:
        body = await req.json()
    except Exception:
        pass

    dry_run = bool(body.get("dry_run", False))
    send_override = body.get("send_email", None)
    send_email = _truthy(send_override) if send_override is not None else _truthy(
        get_config("report_manager.send_email", get_config("nightly_report.send_email", "1"))
    )

    to_email = str(body.get("to_email", "")).strip() or get_config(
        "report_manager.to_email", get_config("nightly_report.to_email", "")
    ).strip()
    if to_email and not _is_valid_email(to_email):
        return JSONResponse({"ok": False, "error": "Invalid to_email"}, status_code=400)

    script = Path(__file__).parent.parent / "nightly_activity_report.py"
    if not script.exists():
        return JSONResponse({"ok": False, "error": "Script not found"}, status_code=404)

    cmd = [sys.executable, str(script), "--window", "yesterday", "--ignore-enabled"]
    if to_email:
        cmd += ["--to", to_email]
    if send_email and not dry_run:
        cmd.append("--send-email")
    if dry_run:
        cmd.append("--dry-run")

    try:
        result = _sp.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(KUKUIBOT_HOME))
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            err = (result.stderr or "").strip() or "Script failed"
            return JSONResponse({"ok": False, "error": err, "output": output}, status_code=500)

        saved_path = _extract_saved_report_path(output)
        report_name = ""
        size_bytes = 0
        if saved_path:
            p = Path(saved_path)
            if p.exists():
                report_name = p.name
                size_bytes = p.stat().st_size
                _upsert_report_history(report_name, _extract_report_date(report_name), size_bytes, status="generated")

        return {
            "ok": True,
            "report_name": report_name,
            "saved_path": saved_path,
            "size_bytes": size_bytes,
            "output": output,
        }
    except _sp.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "Script timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/reports/list")
async def api_reports_list(limit: int = 20, offset: int = 0, sort: str = "date", dir: str = "desc"):
    """List saved reports with metadata (file system + report_history join)."""
    try:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid limit/offset"}, status_code=400)

    sort = (sort or "date").strip().lower()
    if sort not in ("date", "size"):
        sort = "date"
    dir = (dir or "desc").strip().lower()
    reverse = (dir != "asc")

    reports_dir = KUKUIBOT_HOME / "daily_reports"
    files = list(reports_dir.glob("*.html")) if reports_dir.is_dir() else []

    _ensure_report_history_table()
    hist = {}
    with db_connection() as db:
        rows = db.execute(
            "SELECT report_name, report_date, size_bytes, generated_at, status, last_sent_at, last_sent_to, last_error FROM report_history"
        ).fetchall()
        for row in rows:
            hist[row[0]] = {
                "report_date": row[1],
                "size_bytes": int(row[2] or 0),
                "generated_at": int(row[3] or 0),
                "status": row[4] or "generated",
                "last_sent_at": row[5],
                "last_sent_to": row[6] or "",
                "last_error": row[7] or "",
            }

    reports = []
    for f in files:
        st = f.stat()
        m = hist.get(f.name, {})
        generated_at = int(m.get("generated_at") or int(st.st_mtime))
        size_bytes = int(m.get("size_bytes") or st.st_size)
        report_date = m.get("report_date") or _extract_report_date(f.name)
        reports.append({
            "name": f.name,
            "date": report_date,
            "size_bytes": size_bytes,
            "status": m.get("status") or "generated",
            "generated_at": generated_at,
            "last_sent_at": m.get("last_sent_at"),
            "last_sent_to": m.get("last_sent_to") or "",
            "last_error": m.get("last_error") or "",
        })

    if sort == "size":
        reports.sort(key=lambda r: int(r.get("size_bytes") or 0), reverse=reverse)
    else:
        reports.sort(key=lambda r: int(r.get("generated_at") or 0), reverse=reverse)

    total = len(reports)
    page = reports[offset: offset + limit]
    return {
        "ok": True,
        "reports": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
    }


@router.post("/api/reports/send")
async def api_reports_send(req: Request):
    """Send a specific saved report via Gmail bridge."""
    body = await req.json()
    report_name = str(body.get("report_name", "")).strip()
    if not report_name or not _REPORT_FILE_RE.match(report_name):
        return JSONResponse({"ok": False, "error": "report_name is required"}, status_code=400)

    to_email = str(body.get("to", "")).strip()
    if not to_email:
        to_email = get_config("report_manager.to_email", get_config("nightly_report.to_email", "")).strip()
    if not to_email or not _is_valid_email(to_email):
        return JSONResponse({"ok": False, "error": "Valid 'to' email is required"}, status_code=400)

    report_path = KUKUIBOT_HOME / "daily_reports" / report_name
    if not report_path.exists():
        return JSONResponse({"ok": False, "error": "Report not found"}, status_code=404)

    subject = str(body.get("subject", "")).strip()
    if not subject:
        subject = f"Nightly KukuiBot Report — {_extract_report_date(report_name)}"

    try:
        from gmail_bridge import send_html_report
        send_html_report(to_email, subject, str(report_path))
        _mark_report_sent(report_name, to_email)
        return {
            "ok": True,
            "report_name": report_name,
            "to": to_email,
            "message": "sent",
        }
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=403)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e), "blocked": True}, status_code=422)
    except Exception as e:
        logger.warning(f"Reports send failed: {e}")
        _upsert_report_history(report_name, _extract_report_date(report_name), report_path.stat().st_size if report_path.exists() else 0,
                               status="failed", last_error=str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/reports/{report_name}/delete")
async def api_reports_delete(report_name: str):
    """Delete a saved report file and history metadata."""
    if not report_name or not _REPORT_FILE_RE.match(report_name):
        return JSONResponse({"ok": False, "error": "Invalid report name"}, status_code=400)

    report_path = KUKUIBOT_HOME / "daily_reports" / report_name
    if not report_path.exists():
        return JSONResponse({"ok": False, "error": "Report not found"}, status_code=404)

    try:
        report_path.unlink()
        _delete_report_history(report_name)
        return {
            "ok": True,
            "report_name": report_name,
            "message": "deleted",
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
