"""
routes/scheduler.py — Scheduled Tasks (Cron Job Management)
Extracted from server.py — SQLite-backed scheduler with stable UUIDs.
"""

import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cron_manager import CronManager
from config import DB_PATH as _SCHEDULER_DB_PATH

logger = logging.getLogger("kukuibot.scheduler")

router = APIRouter()

_scheduler: CronManager | None = None


def _get_scheduler() -> CronManager:
    """Lazy-init the CronManager singleton."""
    global _scheduler
    if _scheduler is None:
        _scheduler = CronManager(_SCHEDULER_DB_PATH)
    return _scheduler


# --- New Scheduler API routes ---


@router.get("/api/scheduler/jobs")
async def api_scheduler_list_jobs(req: Request):
    """List all scheduled jobs with optional filters."""
    mgr = _get_scheduler()
    category = req.query_params.get("category")
    tag = req.query_params.get("tag")
    enabled_str = req.query_params.get("enabled")
    enabled = None
    if enabled_str is not None:
        enabled = enabled_str.lower() in ("1", "true", "yes")
    jobs = mgr.list_jobs(category=category, tag=tag, enabled=enabled)
    return {"ok": True, "jobs": jobs}


@router.get("/api/scheduler/jobs/{job_id}")
async def api_scheduler_get_job(job_id: str):
    """Get a single job by UUID or slug."""
    mgr = _get_scheduler()
    job = mgr.get_job(job_id)
    if not job:
        return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)
    return {"ok": True, "job": job}


@router.post("/api/scheduler/jobs")
async def api_scheduler_create_job(req: Request):
    """Create a new scheduled job."""
    mgr = _get_scheduler()
    body = await req.json()

    name = (body.get("name") or "").strip()
    command = (body.get("command") or "").strip()
    cron_expr = (body.get("cron_expr") or "").strip()

    if not name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    if not command:
        return JSONResponse({"ok": False, "error": "command is required"}, status_code=400)
    if not cron_expr:
        return JSONResponse({"ok": False, "error": "cron_expr is required"}, status_code=400)

    try:
        job = mgr.create_job(
            name=name,
            command=command,
            cron_expr=cron_expr,
            slug=body.get("slug"),
            description=body.get("description", ""),
            category=body.get("category", ""),
            tags=body.get("tags"),
            enabled=body.get("enabled", True),
            timeout_seconds=body.get("timeout_seconds", 1800),
            timezone=body.get("timezone", "America/Los_Angeles"),
            concurrency_policy=body.get("concurrency_policy", "skip"),
        )
        return {"ok": True, "job": job}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Failed to create scheduled job: {e}")
        return JSONResponse({"ok": False, "error": "Internal error"}, status_code=500)


@router.put("/api/scheduler/jobs/{job_id}")
async def api_scheduler_update_job(job_id: str, req: Request):
    """Update a scheduled job's fields (partial update — only provided fields change)."""
    mgr = _get_scheduler()
    body = await req.json()

    try:
        job = mgr.update_job(job_id, **body)
        if not job:
            return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)
        return {"ok": True, "job": job}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Failed to update scheduled job: {e}")
        return JSONResponse({"ok": False, "error": "Internal error"}, status_code=500)


@router.post("/api/scheduler/jobs/{job_id}/toggle")
async def api_scheduler_toggle_job(job_id: str):
    """Toggle a job's enabled/disabled state."""
    mgr = _get_scheduler()
    job = mgr.toggle_job(job_id)
    if not job:
        return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)
    return {"ok": True, "job": job}


@router.delete("/api/scheduler/jobs/{job_id}")
async def api_scheduler_delete_job(job_id: str):
    """Delete a scheduled job."""
    mgr = _get_scheduler()
    deleted = mgr.delete_job(job_id)
    if not deleted:
        return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)
    return {"ok": True}


@router.post("/api/scheduler/validate")
async def api_scheduler_validate(req: Request):
    """Validate a cron expression and return next run times."""
    mgr = _get_scheduler()
    body = await req.json()
    cron_expr = (body.get("cron_expr") or "").strip()
    if not cron_expr:
        return JSONResponse({"ok": False, "error": "cron_expr is required"}, status_code=400)
    result = mgr.validate_schedule(cron_expr)
    return {"ok": True, **result}


@router.get("/api/scheduler/presets")
async def api_scheduler_presets():
    """Return the preset schedule catalog."""
    mgr = _get_scheduler()
    return {"ok": True, "presets": mgr.get_presets()}


@router.post("/api/scheduler/import-legacy")
async def api_scheduler_import_legacy():
    """One-time import of legacy plists into the scheduler."""
    mgr = _get_scheduler()
    try:
        imported = mgr.import_from_legacy()
        return {"ok": True, "imported": len(imported), "jobs": imported}
    except Exception as e:
        logger.error(f"Failed to import legacy jobs: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# --- Phase 2B: Run history + manual trigger ---


@router.post("/api/scheduler/jobs/{job_id}/report-run")
async def api_scheduler_report_run(job_id: str, req: Request):
    """Called by scheduler_runner.py after job completes."""
    mgr = _get_scheduler()
    body = await req.json()
    mgr.record_run(
        job_id,
        exit_code=body.get("exit_code", -1),
        duration_ms=body.get("duration_ms", 0),
        status=body.get("status", "unknown"),
        output_tail=body.get("output_tail", "")[:65536],
        trigger_source=body.get("trigger_source", "cron"),
    )
    return {"ok": True}


@router.get("/api/scheduler/jobs/{job_id}/runs")
async def api_scheduler_job_runs(job_id: str, limit: int = 20):
    """Return recent runs for a job, newest first."""
    mgr = _get_scheduler()
    runs = mgr.get_run_history(job_id, limit=min(limit, 100))
    return {"ok": True, "runs": runs}


@router.post("/api/scheduler/jobs/{job_id}/run-now")
async def api_scheduler_run_now(job_id: str):
    """Launch a job immediately (non-blocking)."""
    mgr = _get_scheduler()
    result = mgr.trigger_job_now(job_id)
    if not result:
        return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)
    return {"ok": True, "message": "Job triggered"}


# --- Phase 2C: Natural Language to Cron ---


@router.post("/api/scheduler/nl-to-cron")
async def api_scheduler_nl_to_cron(req: Request):
    """Convert natural language schedule description to cron expression using AI."""
    from cron_manager import validate_cron_expr as _validate_cron
    from anthropic_bridge import anthropic_chat, DEFAULT_MODEL as _ANTHROPIC_DEFAULT

    body = await req.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text is required"}, status_code=400)

    # Import _anthropic_api_key from server at call time to avoid circular import
    from server import _anthropic_api_key
    api_key = _anthropic_api_key()
    if not api_key:
        return JSONResponse({"ok": False, "error": "No AI API key configured"}, status_code=503)

    system_prompt = """You are a cron expression parser. Convert natural language schedule descriptions to 5-field cron expressions.

RESPOND WITH ONLY A JSON OBJECT. No markdown, no explanation, no code blocks.

Format:
{
  "cron_expr": "5-field cron expression",
  "name": "Short job name (3-5 words, title case)",
  "schedule_label": "Human readable description like 'Every weekday at 9:00 AM'",
  "assumptions": ["List of any assumptions made, e.g. 'Interpreted morning as 9:00 AM'"]
}

If you cannot parse a schedule, return: {"error": "Could not parse: <reason>"}

Rules:
- Use standard 5-field cron: minute hour day-of-month month day-of-week
- Day of week: 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat
- Use ranges for consecutive days: 1-5 for Mon-Fri
- "weekday" = 1-5 (Mon-Fri), "weekend" = 0,6 (Sat-Sun)
- "morning" = 9:00 AM unless specified
- "night" / "nightly" = midnight (0:00) unless specified
- "every hour" = 0 * * * *
- Only extract the schedule, not the command"""

    try:
        # Use Haiku for speed + cost efficiency on this simple task
        result = await anthropic_chat(
            messages=[{"role": "user", "content": f"Schedule description: {text}"}],
            system=[{"type": "text", "text": system_prompt}],
            model="claude-haiku-4-5-20251001",
            api_key=api_key,
            max_tokens=512,
            temperature=0.0,
            timeout_s=15,
            use_prompt_caching=False,
        )

        if not result.get("ok"):
            return JSONResponse({"ok": False, "error": result.get("error", "AI call failed")}, status_code=500)

        output_text = result.get("text", "")
        if not output_text:
            return JSONResponse({"ok": False, "error": "No response from AI"}, status_code=500)

        parsed = json.loads(output_text)

        if "error" in parsed:
            return JSONResponse({"ok": False, "error": parsed["error"]}, status_code=400)

        cron_expr = parsed.get("cron_expr", "")
        validation = _validate_cron(cron_expr)
        if not validation["valid"]:
            return JSONResponse({"ok": False, "error": f"AI generated invalid cron: {validation['error']}"}, status_code=400)

        return {
            "ok": True,
            "cron_expr": cron_expr,
            "name": parsed.get("name", ""),
            "schedule_label": validation["schedule_label"],
            "next_runs": validation["next_runs"],
            "assumptions": parsed.get("assumptions", []),
        }

    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Could not parse AI response"}, status_code=500)
    except Exception as e:
        logger.error(f"nl-to-cron failed: {e}")
        return JSONResponse({"ok": False, "error": "Internal error"}, status_code=500)


# --- Legacy compatibility shim (deprecated) ---


@router.get("/api/cron")
async def api_cron_list_compat():
    """DEPRECATED: Legacy cron list endpoint. Use GET /api/scheduler/jobs instead."""
    mgr = _get_scheduler()
    jobs = mgr.list_jobs()
    # Map new format to old response shape for backwards compat
    legacy_jobs = []
    for i, job in enumerate(jobs):
        cmd = job["command"]
        # Separate command from redirect (for old UI compat)
        redirect = ""
        for sep in [">>", ">"]:
            idx = cmd.find(f" {sep} ")
            if idx > 0:
                redirect = cmd[idx:].strip()
                cmd = cmd[:idx].strip()
                break
        script_path = cmd.split()[0] if cmd else ""
        legacy_jobs.append({
            "id": i,
            "raw": f"{job['cron_expr']} {job['command']}",
            "schedule": job["cron_expr"],
            "schedule_label": job.get("schedule_label", job["cron_expr"]),
            "command": cmd,
            "redirect": redirect,
            "label": job["name"],
            "script_exists": os.path.isfile(script_path) if script_path else False,
            "disabled": not job["enabled"],
            # New fields for gradual migration
            "_uuid": job["id"],
            "_slug": job.get("slug", ""),
            "_deprecated": True,
        })
    return {"ok": True, "jobs": legacy_jobs, "_deprecated": "Use GET /api/scheduler/jobs instead"}
