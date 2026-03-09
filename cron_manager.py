"""
cron_manager.py — Scheduled Tasks management for KukuiBot.

SQLite-backed job registry with stable UUIDs, cron expression validation,
launchd sync, and preset schedules. Jobs execute through macOS launchd
(LaunchAgents); all metadata lives in SQLite (source of truth).

Each enabled job gets a plist at ~/Library/LaunchAgents/com.kukuibot.job.<slug>.plist.
Disabled/deleted jobs have their plists unloaded and removed.

Legacy crontab support has been removed — macOS TCC blocks crontab writes.
"""

import json
import logging
import os
import platform
import re
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if platform.system() != "Windows":
    import plistlib
else:
    plistlib = None  # type: ignore[assignment]

logger = logging.getLogger("kukuibot.scheduler")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# launchd constants
LAUNCHD_PREFIX = "com.kukuibot.job."
if platform.system() != "Windows":
    LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
else:
    LAUNCHD_DIR = None  # type: ignore[assignment]
KUKUIBOT_HOME = Path.home() / ".kukuibot"
if platform.system() != "Windows":
    LAUNCHD_ENV = {
        "HOME": str(Path.home()),
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
else:
    LAUNCHD_ENV = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", ""),
    }

PRESETS = {
    "every_5_min":        {"schedule": "*/5 * * * *",  "label": "Every 5 minutes"},
    "every_15_min":       {"schedule": "*/15 * * * *", "label": "Every 15 minutes"},
    "hourly":             {"schedule": "0 * * * *",    "label": "Every hour"},
    "daily_midnight":     {"schedule": "0 0 * * *",    "label": "Daily at midnight"},
    "daily_3am":          {"schedule": "0 3 * * *",    "label": "Daily at 3:00 AM"},
    "weekly_mon_9am":     {"schedule": "0 9 * * 1",    "label": "Weekly Monday 9 AM"},
    "weekly_sun_midnight": {"schedule": "0 0 * * 0",   "label": "Weekly Sunday midnight"},
    "monthly_1st":        {"schedule": "0 0 1 * *",    "label": "Monthly 1st at midnight"},
}

# Default timezone for new jobs
DEFAULT_TIMEZONE = "US/Hawaii"

# Cron field ranges: (min_val, max_val)
_FIELD_RANGES = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day-of-month
    (1, 12),   # month
    (0, 7),    # day-of-week (0 and 7 are both Sunday)
]

_FIELD_NAMES = ["minute", "hour", "day-of-month", "month", "day-of-week"]

_DOW_MAP = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3,
    "thu": 4, "fri": 5, "sat": 6,
}

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DOW_NAMES = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


# ---------------------------------------------------------------------------
# Pure-Python cron expression parser & validator
# ---------------------------------------------------------------------------

def _parse_field(field: str, min_val: int, max_val: int, name: str) -> tuple[bool, set[int] | None, str | None]:
    """Parse a single cron field into a set of valid integer values.

    Returns (ok, values_set, error_message).
    """
    values: set[int] = set()

    for part in field.split(","):
        part = part.strip()
        if not part:
            return False, None, f"{name}: empty element in comma-separated list"

        # Handle step: */N or N-M/S
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                return False, None, f"{name}: invalid step '/{step_str}'"
            if step < 1:
                return False, None, f"{name}: step must be >= 1"
            part = base

        if part == "*":
            values.update(range(min_val, max_val + 1, step))
            continue

        # Handle range: N-M
        if "-" in part:
            parts = part.split("-", 1)
            try:
                lo = int(parts[0])
                hi = int(parts[1])
            except ValueError:
                return False, None, f"{name}: invalid range '{part}'"
            if lo < min_val or lo > max_val:
                return False, None, f"{name}: {lo} out of range ({min_val}-{max_val})"
            if hi < min_val or hi > max_val:
                return False, None, f"{name}: {hi} out of range ({min_val}-{max_val})"
            if lo > hi:
                return False, None, f"{name}: range start {lo} > end {hi}"
            values.update(range(lo, hi + 1, step))
            continue

        # Single value
        try:
            val = int(part)
        except ValueError:
            return False, None, f"{name}: invalid value '{part}'"
        if val < min_val or val > max_val:
            return False, None, f"{name}: {val} out of range ({min_val}-{max_val})"
        values.add(val)

    return True, values, None


def _normalize_cron_field(field: str, field_idx: int) -> str:
    """Replace 3-letter month/dow names with numbers."""
    if field_idx == 3:  # month
        for name, num in _MONTH_MAP.items():
            field = re.sub(rf"\b{name}\b", str(num), field, flags=re.IGNORECASE)
    elif field_idx == 4:  # dow
        for name, num in _DOW_MAP.items():
            field = re.sub(rf"\b{name}\b", str(num), field, flags=re.IGNORECASE)
    return field


def validate_cron_expr(cron_expr: str) -> dict:
    """Validate a 5-field cron expression.

    Returns:
        {
            "valid": bool,
            "error": str | None,
            "schedule_label": str,
            "next_runs": [str, ...],    # ISO-format datetimes
            "fields": {minute: [...], hour: [...], ...} | None
        }
    """
    cron_expr = cron_expr.strip()
    parts = cron_expr.split()
    if len(parts) != 5:
        return {
            "valid": False,
            "error": f"Expected 5 fields, got {len(parts)}",
            "schedule_label": "",
            "next_runs": [],
            "fields": None,
        }

    parsed_sets: list[set[int]] = []
    for i, (field_str, (lo, hi), fname) in enumerate(zip(parts, _FIELD_RANGES, _FIELD_NAMES)):
        normed = _normalize_cron_field(field_str, i)
        ok, vals, err = _parse_field(normed, lo, hi, fname)
        if not ok:
            return {
                "valid": False,
                "error": err,
                "schedule_label": "",
                "next_runs": [],
                "fields": None,
            }
        parsed_sets.append(vals)

    label = _build_schedule_label(cron_expr, parsed_sets)
    next_runs = _compute_next_runs(parsed_sets, count=5)

    return {
        "valid": True,
        "error": None,
        "schedule_label": label,
        "next_runs": next_runs,
        "fields": {
            _FIELD_NAMES[i]: sorted(parsed_sets[i]) for i in range(5)
        },
    }


def _build_schedule_label(cron_expr: str, sets: list[set[int]]) -> str:
    """Generate a human-readable label from parsed cron sets."""
    parts = cron_expr.split()
    minutes, hours, doms, months, dows = sets

    # Every minute
    if parts[0] == "*" and parts[1] == "*":
        return "Every minute"

    # */N minute patterns
    if parts[0].startswith("*/") and parts[1] == "*":
        try:
            step = int(parts[0].split("/")[1])
            return f"Every {step} minutes"
        except (ValueError, IndexError):
            pass

    # Every hour at :MM
    if len(minutes) == 1 and parts[1] == "*" and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
        m = next(iter(minutes))
        return f"Every hour at :{m:02d}"

    # Hourly step patterns: 0 */N * * *
    if parts[1].startswith("*/") and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
        try:
            step = int(parts[1].split("/")[1])
            m = next(iter(minutes)) if len(minutes) == 1 else 0
            return f"Every {step} hours at :{m:02d}"
        except (ValueError, IndexError):
            pass

    # Specific hour(s), day of week
    if len(hours) >= 1 and len(minutes) == 1 and parts[4] != "*":
        m = next(iter(minutes))
        h = next(iter(hours))
        dow_labels = ",".join(_DOW_NAMES.get(d, str(d)) for d in sorted(dows))
        h12 = h % 12 or 12
        ampm = "AM" if h < 12 else "PM"
        return f"{dow_labels} at {h12}:{m:02d} {ampm}"

    # Daily at specific time
    if len(hours) == 1 and len(minutes) == 1 and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
        m = next(iter(minutes))
        h = next(iter(hours))
        h12 = h % 12 or 12
        ampm = "AM" if h < 12 else "PM"
        return f"Daily at {h12}:{m:02d} {ampm}"

    # Monthly on specific day
    if len(doms) == 1 and parts[3] == "*" and parts[4] == "*" and len(hours) == 1 and len(minutes) == 1:
        m = next(iter(minutes))
        h = next(iter(hours))
        d = next(iter(doms))
        h12 = h % 12 or 12
        ampm = "AM" if h < 12 else "PM"
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(d if d < 20 else d % 10, "th")
        return f"Monthly on the {d}{suffix} at {h12}:{m:02d} {ampm}"

    # Fallback: show the expression
    return cron_expr


def _compute_next_runs(sets: list[set[int]], count: int = 5, max_days: int = 400) -> list[str]:
    """Find the next N cron-matching times by walking forward smartly.

    Skips ahead by day when the day/month/dow doesn't match, and by hour
    when the hour doesn't match. Only walks minute-by-minute within
    matching hours. Handles up to max_days into the future.
    Returns ISO-format strings.
    """
    now = datetime.now()
    cursor = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = now + timedelta(days=max_days)

    cron_minutes, cron_hours, cron_doms, cron_months, cron_dows = sets
    py_dows = _convert_cron_dow_to_python({d % 7 for d in cron_dows})

    results: list[str] = []

    while cursor < end and len(results) < count:
        # Check month
        if cursor.month not in cron_months:
            # Skip to 1st of next month
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                cursor = cursor.replace(month=cursor.month + 1, day=1, hour=0, minute=0)
            continue

        # Check day-of-month and day-of-week
        if cursor.day not in cron_doms or cursor.weekday() not in py_dows:
            cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        # Check hour
        if cursor.hour not in cron_hours:
            cursor = cursor.replace(minute=0) + timedelta(hours=1)
            continue

        # Check minute
        if cursor.minute in cron_minutes:
            results.append(cursor.strftime("%Y-%m-%dT%H:%M:00"))
        cursor += timedelta(minutes=1)

    return results


def _convert_cron_dow_to_python(cron_dows: set[int]) -> set[int]:
    """Convert cron day-of-week (0=Sun) to Python weekday (0=Mon).

    Cron: 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat
    Python: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    """
    result: set[int] = set()
    for d in cron_dows:
        if d == 0:  # Sun
            result.add(6)
        else:
            result.add(d - 1)
    return result


# ---------------------------------------------------------------------------
# Cron-to-launchd conversion
# ---------------------------------------------------------------------------


def _cron_to_launchd_schedule(cron_expr: str) -> dict:
    """Convert a 5-field cron expression to launchd schedule config.

    Returns a dict with either:
      {"type": "calendar", "intervals": [dict, ...]}  — for StartCalendarInterval
      {"type": "interval", "seconds": int}             — for StartInterval (*/N patterns)

    launchd keys: Minute, Hour, Day (day-of-month), Month, Weekday (0=Sun, same as cron).
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5 cron fields, got {len(parts)}")

    minute_str, hour_str, dom_str, month_str, dow_str = parts

    # Special case: */N minute with wildcard everything else → StartInterval
    if (minute_str.startswith("*/") and hour_str == "*"
            and dom_str == "*" and month_str == "*" and dow_str == "*"):
        try:
            step = int(minute_str.split("/")[1])
            return {"type": "interval", "seconds": step * 60}
        except (ValueError, IndexError):
            pass

    # Special case: */N hour with fixed minute → StartInterval
    if (hour_str.startswith("*/") and dom_str == "*"
            and month_str == "*" and dow_str == "*"):
        try:
            hour_step = int(hour_str.split("/")[1])
            return {"type": "interval", "seconds": hour_step * 3600}
        except (ValueError, IndexError):
            pass

    # Parse each field into a set of values
    vr = validate_cron_expr(cron_expr)
    if not vr["valid"]:
        raise ValueError(f"Invalid cron expression: {vr['error']}")

    fields = vr["fields"]
    minutes = fields["minute"]
    hours = fields["hour"]
    doms = fields["day-of-month"]
    months = fields["month"]
    dows = fields["day-of-week"]

    # Determine which fields are constrained (not full range)
    all_minutes = (len(minutes) == 60)
    all_hours = (len(hours) == 24)
    all_doms = (len(doms) == 31)
    all_months = (len(months) == 12)
    all_dows = (len(dows) >= 7)  # 0-6 or 0-7

    # Build base dict with only constrained fields
    def _build_entry(**overrides) -> dict:
        entry = {}
        if not all_minutes or "Minute" in overrides:
            entry["Minute"] = overrides.get("Minute", minutes[0] if len(minutes) == 1 else minutes[0])
        if not all_hours or "Hour" in overrides:
            entry["Hour"] = overrides.get("Hour", hours[0] if len(hours) == 1 else hours[0])
        if not all_doms:
            entry["Day"] = overrides.get("Day", doms[0] if len(doms) == 1 else doms[0])
        if not all_months:
            entry["Month"] = overrides.get("Month", months[0] if len(months) == 1 else months[0])
        if not all_dows:
            entry["Weekday"] = overrides.get("Weekday", (dows[0] % 7))
        return entry

    # Generate one entry per combination of constrained multi-value fields
    # For simple cases (single value per field), just one entry
    intervals = []

    # Expand combinations: iterate over multi-value constrained fields
    iter_minutes = minutes if not all_minutes and len(minutes) > 1 else [None]
    iter_hours = hours if not all_hours and len(hours) > 1 else [None]
    iter_doms = doms if not all_doms and len(doms) > 1 else [None]
    iter_months = months if not all_months and len(months) > 1 else [None]
    iter_dows = [d % 7 for d in dows] if not all_dows and len(dows) > 1 else [None]
    # Deduplicate dows (cron 0 and 7 both = Sunday)
    if iter_dows != [None]:
        iter_dows = sorted(set(iter_dows))

    for m in iter_minutes:
        for h in iter_hours:
            for dom in iter_doms:
                for mo in iter_months:
                    for dw in iter_dows:
                        entry = {}
                        if m is not None:
                            entry["Minute"] = m
                        elif not all_minutes:
                            entry["Minute"] = minutes[0]
                        if h is not None:
                            entry["Hour"] = h
                        elif not all_hours:
                            entry["Hour"] = hours[0]
                        if dom is not None:
                            entry["Day"] = dom
                        elif not all_doms:
                            entry["Day"] = doms[0]
                        if mo is not None:
                            entry["Month"] = mo
                        elif not all_months:
                            entry["Month"] = months[0]
                        if dw is not None:
                            entry["Weekday"] = dw
                        elif not all_dows:
                            entry["Weekday"] = dows[0] % 7
                        if entry:
                            intervals.append(entry)

    if not intervals:
        # Fallback: every minute (no constraints)
        intervals = [{}]

    return {"type": "calendar", "intervals": intervals}


def _build_job_plist(slug: str, command: str, cron_expr: str, enabled: bool = True) -> bytes:
    """Build a launchd plist for a scheduled job. Returns plist XML as bytes."""
    label = f"{LAUNCHD_PREFIX}{slug}"
    log_path = str(KUKUIBOT_HOME / "logs" / f"job-{slug}.log")

    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", "-c", command],
        "WorkingDirectory": str(KUKUIBOT_HOME),
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": LAUNCHD_ENV,
    }

    schedule = _cron_to_launchd_schedule(cron_expr)
    if schedule["type"] == "interval":
        plist["StartInterval"] = schedule["seconds"]
    else:
        intervals = schedule["intervals"]
        if len(intervals) == 1:
            plist["StartCalendarInterval"] = intervals[0]
        else:
            plist["StartCalendarInterval"] = intervals

    return plistlib.dumps(plist, fmt=plistlib.FMT_XML)


# ---------------------------------------------------------------------------
# CronManager — main class
# ---------------------------------------------------------------------------

class CronManager:
    """Manages scheduled jobs in SQLite with launchd sync."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._create_tables()

    def _get_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=5.0)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = sqlite3.Row
        return db

    def _create_tables(self):
        db = self._get_db()
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    slug TEXT UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    command TEXT NOT NULL,
                    cron_expr TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'US/Hawaii',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    category TEXT DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'api',
                    template_key TEXT,
                    timeout_seconds INTEGER NOT NULL DEFAULT 1800,
                    max_output_bytes INTEGER NOT NULL DEFAULT 65536,
                    max_history_runs INTEGER NOT NULL DEFAULT 100,
                    concurrency_policy TEXT NOT NULL DEFAULT 'skip',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_run_at INTEGER,
                    last_status TEXT,
                    last_exit_code INTEGER,
                    last_duration_ms INTEGER
                );

                CREATE TABLE IF NOT EXISTS scheduled_job_tags (
                    job_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (job_id, tag),
                    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_sched_jobs_enabled ON scheduled_jobs(enabled);
                CREATE INDEX IF NOT EXISTS idx_sched_jobs_category ON scheduled_jobs(category);
            """)
            db.commit()

            existing_cols = {row[1] for row in db.execute("PRAGMA table_info(scheduled_jobs)").fetchall()}
            new_columns = [
                ("job_type", "TEXT NOT NULL DEFAULT 'shell'"),
                ("prompt_text", "TEXT"),
                ("next_run_at", "INTEGER"),
                ("last_scheduled_for", "INTEGER"),
                ("run_missed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("parse_confidence", "REAL"),
                ("notify_email", "TEXT DEFAULT ''"),
                ("notify_on", "TEXT NOT NULL DEFAULT 'failure'"),
            ]
            for col_name, col_def in new_columns:
                if col_name not in existing_cols:
                    db.execute(f"ALTER TABLE scheduled_jobs ADD COLUMN {col_name} {col_def}")

            db.executescript("""
                CREATE TABLE IF NOT EXISTS job_runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL DEFAULT 'schedule',
                    scheduled_for INTEGER,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    duration_ms INTEGER,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    output_snippet TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    prompt_sent TEXT,
                    response_summary TEXT,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_job_runs_job_started ON job_runs(job_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status);

                CREATE TABLE IF NOT EXISTS scheduler_parse_drafts (
                    id TEXT PRIMARY KEY,
                    user_input TEXT NOT NULL,
                    parsed_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_parse_drafts_expires ON scheduler_parse_drafts(expires_at);
            """)
            db.commit()
        finally:
            db.close()

    # -------------------------------------------------------------------
    # Validation
    # -------------------------------------------------------------------

    def validate_schedule(self, cron_expr: str) -> dict:
        """Validate a cron expression and return parse results."""
        return validate_cron_expr(cron_expr)

    # -------------------------------------------------------------------
    # Presets
    # -------------------------------------------------------------------

    def get_presets(self) -> dict:
        """Return the preset schedule catalog."""
        return PRESETS

    # -------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row, db: sqlite3.Connection | None = None) -> dict:
        """Convert a DB row to a plain dict with tags and computed fields."""
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        # Attach tags
        if db:
            tags_rows = db.execute(
                "SELECT tag FROM scheduled_job_tags WHERE job_id = ? ORDER BY tag",
                (d["id"],),
            ).fetchall()
            d["tags"] = [r["tag"] for r in tags_rows]
        else:
            d["tags"] = []
        # Compute schedule label
        d["schedule_label"] = validate_cron_expr(d["cron_expr"]).get("schedule_label", d["cron_expr"])
        return d

    def create_job(
        self,
        name: str,
        command: str,
        cron_expr: str,
        *,
        slug: str | None = None,
        description: str = "",
        category: str = "",
        tags: list[str] | None = None,
        enabled: bool = True,
        source: str = "api",
        template_key: str | None = None,
        timeout_seconds: int = 1800,
        timezone: str = DEFAULT_TIMEZONE,
        concurrency_policy: str = "skip",
        job_type: str = "shell",
        prompt_text: str = "",
        notify_email: str = "",
        notify_on: str = "failure",
        _skip_sync: bool = False,
    ) -> dict:
        """Create a new scheduled job. Validates cron, inserts to DB, syncs launchd."""
        # Validate
        vr = validate_cron_expr(cron_expr)
        if not vr["valid"]:
            raise ValueError(f"Invalid cron expression: {vr['error']}")

        if not name or not name.strip():
            raise ValueError("Job name is required")
        if not command or not command.strip():
            raise ValueError("Command is required")
        if concurrency_policy not in ("skip", "parallel"):
            raise ValueError("concurrency_policy must be 'skip' or 'parallel'")

        job_id = str(uuid.uuid4())
        now = int(time.time())

        # Generate slug from name if not provided
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            # Ensure uniqueness
            slug = self._unique_slug(slug)

        db = self._get_db()
        try:
            db.execute(
                """INSERT INTO scheduled_jobs
                   (id, slug, name, description, command, cron_expr, timezone,
                    enabled, category, source, template_key, timeout_seconds,
                    max_output_bytes, max_history_runs, concurrency_policy,
                    job_type, prompt_text, notify_email, notify_on,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 65536, 100, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, slug, name.strip(), description.strip(), command.strip(),
                 cron_expr.strip(), timezone, int(enabled), category.strip(),
                 source, template_key, timeout_seconds, concurrency_policy,
                 (job_type or "shell").strip(), (prompt_text or "").strip(),
                 (notify_email or "").strip(), (notify_on or "failure").strip(),
                 now, now),
            )
            # Insert tags
            if tags:
                for tag in tags:
                    t = tag.strip().lower()
                    if t:
                        db.execute(
                            "INSERT OR IGNORE INTO scheduled_job_tags (job_id, tag) VALUES (?, ?)",
                            (job_id, t),
                        )
            db.commit()

            row = db.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
            result = self._row_to_dict(row, db)
        finally:
            db.close()

        if not _skip_sync:
            self.sync_launchd()
        logger.info(f"Created scheduled job: {name!r} ({job_id})")
        return result

    def _unique_slug(self, base_slug: str) -> str:
        """Ensure a slug is unique by appending a suffix if needed."""
        db = self._get_db()
        try:
            slug = base_slug
            suffix = 0
            while True:
                row = db.execute("SELECT 1 FROM scheduled_jobs WHERE slug = ?", (slug,)).fetchone()
                if not row:
                    return slug
                suffix += 1
                slug = f"{base_slug}-{suffix}"
        finally:
            db.close()

    def get_job(self, job_id: str) -> dict | None:
        """Get a single job by UUID or slug."""
        db = self._get_db()
        try:
            row = db.execute(
                "SELECT * FROM scheduled_jobs WHERE id = ? OR slug = ?",
                (job_id, job_id),
            ).fetchone()
            if not row:
                return None
            return self._row_to_dict(row, db)
        finally:
            db.close()

    def list_jobs(
        self,
        category: str | None = None,
        tag: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict]:
        """List jobs with optional filters."""
        db = self._get_db()
        try:
            query = "SELECT * FROM scheduled_jobs WHERE 1=1"
            params: list[Any] = []

            if category is not None:
                query += " AND category = ?"
                params.append(category)
            if enabled is not None:
                query += " AND enabled = ?"
                params.append(int(enabled))
            if tag is not None:
                query += " AND id IN (SELECT job_id FROM scheduled_job_tags WHERE tag = ?)"
                params.append(tag.strip().lower())

            query += " ORDER BY name"
            rows = db.execute(query, params).fetchall()
            return [self._row_to_dict(r, db) for r in rows]
        finally:
            db.close()

    def update_job(self, job_id: str, **fields) -> dict | None:
        """Update a job's fields. Validates cron_expr if changed. Syncs launchd."""
        db = self._get_db()
        try:
            row = db.execute(
                "SELECT * FROM scheduled_jobs WHERE id = ? OR slug = ?",
                (job_id, job_id),
            ).fetchone()
            if not row:
                return None

            real_id = row["id"]

            # Validate cron if being changed
            if "cron_expr" in fields and fields["cron_expr"]:
                vr = validate_cron_expr(fields["cron_expr"])
                if not vr["valid"]:
                    raise ValueError(f"Invalid cron expression: {vr['error']}")

            # Validate concurrency_policy if being changed
            if "concurrency_policy" in fields:
                if fields["concurrency_policy"] not in ("skip", "parallel"):
                    raise ValueError("concurrency_policy must be 'skip' or 'parallel'")

            # Build update
            allowed = {
                "name", "description", "command", "cron_expr", "timezone",
                "enabled", "category", "slug", "timeout_seconds",
                "concurrency_policy", "job_type", "prompt_text",
                "notify_email", "notify_on",
            }
            updates: list[str] = []
            params: list[Any] = []
            for k, v in fields.items():
                if k == "tags":
                    continue  # Handle separately
                if k not in allowed:
                    continue
                if k == "enabled":
                    v = int(bool(v))
                updates.append(f"{k} = ?")
                params.append(v)

            if updates:
                updates.append("updated_at = ?")
                params.append(int(time.time()))
                params.append(real_id)
                db.execute(
                    f"UPDATE scheduled_jobs SET {', '.join(updates)} WHERE id = ?",
                    params,
                )

            # Handle tags
            if "tags" in fields:
                db.execute("DELETE FROM scheduled_job_tags WHERE job_id = ?", (real_id,))
                for tag in (fields["tags"] or []):
                    t = tag.strip().lower()
                    if t:
                        db.execute(
                            "INSERT OR IGNORE INTO scheduled_job_tags (job_id, tag) VALUES (?, ?)",
                            (real_id, t),
                        )

            db.commit()
            row = db.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (real_id,)).fetchone()
            result = self._row_to_dict(row, db)
        finally:
            db.close()

        self.sync_launchd()
        logger.info(f"Updated scheduled job: {real_id}")
        return result

    def delete_job(self, job_id: str) -> bool:
        """Delete a job by UUID or slug. Returns True if deleted."""
        db = self._get_db()
        try:
            row = db.execute(
                "SELECT id, name FROM scheduled_jobs WHERE id = ? OR slug = ?",
                (job_id, job_id),
            ).fetchone()
            if not row:
                return False
            real_id = row["id"]
            name = row["name"]
            db.execute("DELETE FROM scheduled_jobs WHERE id = ?", (real_id,))
            db.commit()
        finally:
            db.close()

        self.sync_launchd()
        logger.info(f"Deleted scheduled job: {name!r} ({real_id})")
        return True

    def toggle_job(self, job_id: str) -> dict | None:
        """Toggle a job's enabled state. Returns the updated job."""
        db = self._get_db()
        try:
            row = db.execute(
                "SELECT * FROM scheduled_jobs WHERE id = ? OR slug = ?",
                (job_id, job_id),
            ).fetchone()
            if not row:
                return None
            real_id = row["id"]
            new_enabled = 0 if row["enabled"] else 1
            db.execute(
                "UPDATE scheduled_jobs SET enabled = ?, updated_at = ? WHERE id = ?",
                (new_enabled, int(time.time()), real_id),
            )
            db.commit()
            row = db.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (real_id,)).fetchone()
            result = self._row_to_dict(row, db)
        finally:
            db.close()

        self.sync_launchd()
        state = "enabled" if result["enabled"] else "disabled"
        logger.info(f"Toggled scheduled job {result['name']!r} -> {state}")
        return result

    # -------------------------------------------------------------------
    # launchd sync
    # -------------------------------------------------------------------

    def sync_launchd(self):
        """Sync all scheduled jobs to launchd plists.

        For each enabled shell job: write plist, launchctl load.
        For disabled/missing jobs: launchctl unload, remove plist.
        """
        if platform.system() == "Windows":
            logger.warning("launchd not available on Windows — skipping sync")
            return
        LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)

        db = self._get_db()
        try:
            rows = db.execute(
                "SELECT id, slug, cron_expr, command, enabled, name FROM scheduled_jobs "
                "WHERE job_type = 'shell' OR job_type = '' OR job_type IS NULL ORDER BY name"
            ).fetchall()
        finally:
            db.close()

        # Track which plists we manage
        managed_slugs: set[str] = set()

        for row in rows:
            slug = row["slug"]
            managed_slugs.add(slug)
            plist_path = LAUNCHD_DIR / f"{LAUNCHD_PREFIX}{slug}.plist"
            label = f"{LAUNCHD_PREFIX}{slug}"

            if row["enabled"]:
                try:
                    plist_data = _build_job_plist(slug, row["command"], row["cron_expr"])

                    # Check if plist already exists and is identical
                    if plist_path.exists():
                        existing_data = plist_path.read_bytes()
                        if existing_data == plist_data:
                            # No change needed, ensure it's loaded
                            self._ensure_launchd_loaded(label, plist_path)
                            continue
                        # Content changed — unload old, write new, load new
                        self._launchctl_unload(label, plist_path)

                    plist_path.write_bytes(plist_data)
                    self._launchctl_load(label, plist_path)
                    logger.info(f"Synced launchd job: {row['name']} ({slug})")
                except Exception as e:
                    logger.error(f"Failed to sync launchd job {slug}: {e}")
            else:
                # Disabled — unload and remove plist
                if plist_path.exists():
                    self._launchctl_unload(label, plist_path)
                    plist_path.unlink(missing_ok=True)
                    logger.info(f"Disabled launchd job: {row['name']} ({slug})")

        # Clean up orphaned plists (jobs deleted from DB but plist remains)
        for plist_file in LAUNCHD_DIR.glob(f"{LAUNCHD_PREFIX}*.plist"):
            slug_from_file = plist_file.stem.removeprefix(LAUNCHD_PREFIX)
            if slug_from_file not in managed_slugs:
                label = f"{LAUNCHD_PREFIX}{slug_from_file}"
                self._launchctl_unload(label, plist_file)
                plist_file.unlink(missing_ok=True)
                logger.info(f"Removed orphaned launchd plist: {plist_file.name}")

    def _launchctl_load(self, label: str, plist_path: Path):
        """Load a launchd agent."""
        if platform.system() == "Windows":
            logger.warning("launchd not available on Windows")
            return
        try:
            subprocess.run(
                ["launchctl", "load", str(plist_path)],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            logger.error(f"launchctl load failed for {label}: {e}")

    def _launchctl_unload(self, label: str, plist_path: Path):
        """Unload a launchd agent."""
        if platform.system() == "Windows":
            logger.warning("launchd not available on Windows")
            return
        try:
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            logger.error(f"launchctl unload failed for {label}: {e}")

    def _ensure_launchd_loaded(self, label: str, plist_path: Path):
        """Check if agent is loaded; load if not."""
        if platform.system() == "Windows":
            logger.warning("launchd not available on Windows")
            return
        try:
            result = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                # Not loaded — load it
                self._launchctl_load(label, plist_path)
        except Exception:
            pass

    # -------------------------------------------------------------------
    # Missing methods (referenced by server.py API endpoints)
    # -------------------------------------------------------------------

    def record_run(self, job_id: str, exit_code: int = -1, duration_ms: int = 0,
                   status: str = "unknown", output_tail: str = "", trigger_source: str = "cron"):
        """Record a completed job run (called by scheduler_runner.py via API)."""
        run_id = str(uuid.uuid4())
        now = int(time.time())
        db = self._get_db()
        try:
            db.execute(
                """INSERT INTO job_runs
                   (id, job_id, trigger_type, started_at, finished_at, duration_ms,
                    status, exit_code, output_snippet, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, job_id, trigger_source, now - (duration_ms // 1000),
                 now, duration_ms, status, exit_code,
                 (output_tail or "")[:500], now),
            )
            db.execute(
                """UPDATE scheduled_jobs SET last_run_at=?, last_status=?, last_exit_code=?,
                   last_duration_ms=?, updated_at=? WHERE id=?""",
                (now, status, exit_code, duration_ms, now, job_id),
            )
            db.commit()
        finally:
            db.close()
        logger.info(f"Recorded run for job {job_id}: status={status}, exit={exit_code}")

    def get_run_history(self, job_id: str, limit: int = 20) -> list[dict]:
        """Return recent runs for a job, newest first."""
        db = self._get_db()
        try:
            rows = db.execute(
                "SELECT * FROM job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def trigger_job_now(self, job_id: str) -> dict | None:
        """Launch a job immediately in the background. Returns job dict or None."""
        job = self.get_job(job_id)
        if not job:
            return None

        command = job["command"]
        run_id = str(uuid.uuid4())
        now = int(time.time())

        # Record the run as starting
        db = self._get_db()
        try:
            db.execute(
                """INSERT INTO job_runs
                   (id, job_id, trigger_type, started_at, status, created_at)
                   VALUES (?, ?, 'manual', ?, 'running', ?)""",
                (run_id, job["id"], now, now),
            )
            db.commit()
        finally:
            db.close()

        # Launch in background
        try:
            proc = subprocess.Popen(
                command, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(KUKUIBOT_HOME),
            )
            # Don't wait — fire and forget. The scheduler_runner or a watcher
            # will report completion if set up. For now, record immediately.
            logger.info(f"Triggered job {job['name']} (pid={proc.pid})")
        except Exception as e:
            # Record failure
            finished = int(time.time())
            db = self._get_db()
            try:
                db.execute(
                    """UPDATE job_runs SET status='failed', finished_at=?, duration_ms=?,
                       error_message=?, exit_code=-1 WHERE id=?""",
                    (finished, (finished - now) * 1000, str(e), run_id),
                )
                db.execute(
                    """UPDATE scheduled_jobs SET last_run_at=?, last_status='failed',
                       last_exit_code=-1, updated_at=? WHERE id=?""",
                    (finished, finished, job["id"]),
                )
                db.commit()
            finally:
                db.close()
            logger.error(f"Failed to trigger job {job['name']}: {e}")

        return job

    # -------------------------------------------------------------------
    # Run history
    # -------------------------------------------------------------------

    def create_job_run(self, job_id: str, trigger_type: str = "schedule", scheduled_for: int | None = None) -> dict:
        """Insert a new job_runs row with status='running'. Returns the run dict."""
        run_id = str(uuid.uuid4())
        now = int(time.time())
        db = self._get_db()
        try:
            db.execute(
                """INSERT INTO job_runs
                   (id, job_id, trigger_type, scheduled_for, started_at, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'running', ?)""",
                (run_id, job_id, trigger_type, scheduled_for, now, now),
            )
            db.commit()
            row = db.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row)
        finally:
            db.close()

    def finish_job_run(self, run_id: str, status: str, *, exit_code: int | None = None,
                       duration_ms: int | None = None, output_snippet: str = "",
                       error_message: str = "", response_summary: str = "") -> dict | None:
        """Update a job_runs row to terminal state. Also updates scheduled_jobs last_* fields."""
        now = int(time.time())
        db = self._get_db()
        try:
            row = db.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            job_id = row["job_id"]
            started_at = row["started_at"]
            if duration_ms is None:
                duration_ms = (now - started_at) * 1000
            db.execute(
                """UPDATE job_runs SET status=?, finished_at=?, duration_ms=?, exit_code=?,
                   output_snippet=?, error_message=?, response_summary=? WHERE id=?""",
                (status, now, duration_ms, exit_code,
                 (output_snippet or "")[:500], error_message or "", response_summary or "", run_id),
            )
            db.execute(
                """UPDATE scheduled_jobs SET last_run_at=?, last_status=?, last_exit_code=?,
                   last_duration_ms=?, updated_at=? WHERE id=?""",
                (now, status, exit_code, duration_ms, now, job_id),
            )
            db.commit()
            row = db.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row)
        finally:
            db.close()

    def list_job_runs(self, job_id: str, limit: int = 5, offset: int = 0) -> dict:
        """Return { items: [...], total: int } for a job's run history."""
        db = self._get_db()
        try:
            total = db.execute("SELECT COUNT(*) FROM job_runs WHERE job_id = ?", (job_id,)).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()
            return {"items": [dict(r) for r in rows], "total": total}
        finally:
            db.close()

    def update_job_last_run(self, job_id: str, status: str, exit_code: int | None, duration_ms: int | None):
        """Update last_run_at, last_status, last_exit_code, last_duration_ms on scheduled_jobs."""
        now = int(time.time())
        db = self._get_db()
        try:
            db.execute(
                """UPDATE scheduled_jobs SET last_run_at=?, last_status=?, last_exit_code=?,
                   last_duration_ms=?, updated_at=? WHERE id=?""",
                (now, status, exit_code, duration_ms, now, job_id),
            )
            db.commit()
        finally:
            db.close()

    def reconcile_stale_runs(self, threshold_seconds: int = 300):
        """On startup: find job_runs with status='running' older than threshold, mark as failure."""
        cutoff = int(time.time()) - threshold_seconds
        db = self._get_db()
        try:
            stale = db.execute(
                "SELECT id, job_id FROM job_runs WHERE status = 'running' AND started_at < ?",
                (cutoff,),
            ).fetchall()
            now = int(time.time())
            for row in stale:
                db.execute(
                    "UPDATE job_runs SET status='failure', error_message='Reconciled on startup (stale run)', finished_at=? WHERE id=?",
                    (now, row["id"]),
                )
                db.execute(
                    "UPDATE scheduled_jobs SET last_status='failure', updated_at=? WHERE id=?",
                    (now, row["job_id"]),
                )
            if stale:
                db.commit()
                logger.info(f"Reconciled {len(stale)} stale running job_runs on startup")
        finally:
            db.close()

    def list_categories_with_counts(self) -> list[dict]:
        """Return [{ category: str, count: int }, ...] for all jobs, sorted by count desc."""
        db = self._get_db()
        try:
            rows = db.execute(
                """SELECT COALESCE(NULLIF(category,''), 'uncategorized') as category, COUNT(*) as count
                   FROM scheduled_jobs GROUP BY category ORDER BY count DESC""",
            ).fetchall()
            return [{"category": r["category"], "count": r["count"]} for r in rows]
        finally:
            db.close()

    # -------------------------------------------------------------------
    # Parse drafts
    # -------------------------------------------------------------------

    def save_parse_draft(self, user_input: str, parsed_json: str) -> str:
        """Save a parse draft with 15min expiry. Returns draft_id."""
        draft_id = str(uuid.uuid4())
        now = int(time.time())
        expires_at = now + 900  # 15 minutes
        db = self._get_db()
        try:
            db.execute(
                "INSERT INTO scheduler_parse_drafts (id, user_input, parsed_json, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (draft_id, user_input, parsed_json, now, expires_at),
            )
            db.commit()
            return draft_id
        finally:
            db.close()

    def get_parse_draft(self, draft_id: str) -> dict | None:
        """Get a non-expired parse draft by ID. Returns None if missing or expired."""
        now = int(time.time())
        db = self._get_db()
        try:
            row = db.execute(
                "SELECT * FROM scheduler_parse_drafts WHERE id = ? AND expires_at > ?",
                (draft_id, now),
            ).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def expire_parse_drafts(self):
        """Delete expired parse drafts."""
        now = int(time.time())
        db = self._get_db()
        try:
            db.execute("DELETE FROM scheduler_parse_drafts WHERE expires_at <= ?", (now,))
            db.commit()
        finally:
            db.close()

    # -------------------------------------------------------------------
    # Legacy import
    # -------------------------------------------------------------------

    def import_from_crontab(self) -> list[dict]:
        """Legacy alias — delegates to import_from_legacy()."""
        return self.import_from_legacy()

    def import_from_legacy(self) -> list[dict]:
        """One-time import: detect orphaned launchd plists and DB gaps.

        Only runs if no jobs exist in the DB yet. Scans existing
        com.kukuibot.job.* plists in LaunchAgents and imports them.

        Returns list of imported jobs.
        """
        db = self._get_db()
        try:
            count = db.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0]
            if count > 0:
                logger.info("Import skipped: jobs already exist in DB")
                return []
        finally:
            db.close()

        imported: list[dict] = []

        # Scan existing com.kukuibot.job.* plists
        for plist_file in LAUNCHD_DIR.glob(f"{LAUNCHD_PREFIX}*.plist"):
            try:
                with open(plist_file, "rb") as f:
                    pdata = plistlib.load(f)
                label = pdata.get("Label", "")
                slug = label.removeprefix(LAUNCHD_PREFIX)
                if not slug:
                    continue

                # Extract command
                prog_args = pdata.get("ProgramArguments", [])
                if len(prog_args) >= 3 and prog_args[0] == "/bin/bash" and prog_args[1] == "-c":
                    command = prog_args[2]
                else:
                    command = " ".join(prog_args)

                # Determine schedule — try StartCalendarInterval first
                cron_expr = "0 * * * *"  # fallback: hourly
                sci = pdata.get("StartCalendarInterval")
                if sci:
                    if isinstance(sci, dict):
                        sci = [sci]
                    # Simple single-interval conversion
                    iv = sci[0] if sci else {}
                    minute = iv.get("Minute", "*")
                    hour = iv.get("Hour", "*")
                    dom = iv.get("Day", "*")
                    month = iv.get("Month", "*")
                    dow = iv.get("Weekday", "*")
                    cron_expr = f"{minute} {hour} {dom} {month} {dow}"
                elif "StartInterval" in pdata:
                    secs = pdata["StartInterval"]
                    if secs % 60 == 0:
                        cron_expr = f"*/{secs // 60} * * * *"

                name = slug.replace("-", " ").replace("_", " ").title()

                job = self.create_job(
                    name=name,
                    slug=slug,
                    command=command,
                    cron_expr=cron_expr,
                    source="import",
                    enabled=True,
                    _skip_sync=True,
                )
                imported.append(job)
            except Exception as e:
                logger.error(f"Failed to import plist {plist_file.name}: {e}")

        if imported:
            self.sync_launchd()
            logger.info(f"Imported {len(imported)} jobs from legacy plists")
        return imported
