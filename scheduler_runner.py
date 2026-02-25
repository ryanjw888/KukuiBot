#!/usr/bin/env python3
"""
scheduler_runner.py — KukuiBot job execution wrapper.

Invoked by launchd or manually:
    python3 scheduler_runner.py --job-id <uuid>

Handles:
- Concurrency locking (skip policy via lockfile)
- Timeout enforcement
- Output capture (last 64KB)
- Reports back to KukuiBot /api/scheduler/jobs/{id}/report-run
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

KUKUIBOT_HOME = Path(os.environ.get("KUKUIBOT_HOME", str(Path.home() / ".kukuibot")))
_PORT = os.environ.get("KUKUIBOT_PORT", "7000")
KUKUIBOT_URL = os.environ.get("KUKUIBOT_URL", f"https://localhost:{_PORT}")
LOCK_DIR = KUKUIBOT_HOME / "var" / "locks"
MAX_OUTPUT = 65536


def main():
    parser = argparse.ArgumentParser(description="KukuiBot scheduled job runner")
    parser.add_argument("--job-id", required=True, help="UUID of the job to run")
    parser.add_argument("--trigger-source", default="cron", help="Trigger source (cron or manual)")
    args = parser.parse_args()

    job_id = args.job_id
    trigger_source = args.trigger_source

    # Load job from DB
    db_path = KUKUIBOT_HOME / "kukuibot.db"
    if not db_path.exists():
        sys.exit(1)

    db = sqlite3.connect(str(db_path), timeout=5.0)
    db.execute("PRAGMA busy_timeout=5000")
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
    db.close()

    if not row:
        sys.exit(1)

    command = row["command"]
    timeout_s = row["timeout_seconds"] or 1800
    concurrency = row["concurrency_policy"] or "skip"

    # Concurrency: skip if already running
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_DIR / f"job-{job_id}.lock"

    if concurrency == "skip" and lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            os.kill(pid, 0)  # raises if not running
            sys.exit(0)  # skip — still running
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock, proceed

    lock_file.write_text(str(os.getpid()))
    run_id = str(uuid.uuid4())
    started_at = time.time()

    try:
        proc = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            out_bytes, _ = proc.communicate(timeout=timeout_s)
            exit_code = proc.returncode
            status = "success" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            proc.kill()
            out_bytes, _ = proc.communicate()
            exit_code = -1
            status = "timeout"
    except Exception as e:
        out_bytes = str(e).encode()
        exit_code = -1
        status = "failed"
    finally:
        lock_file.unlink(missing_ok=True)

    finished_at = time.time()
    duration_ms = int((finished_at - started_at) * 1000)
    output_tail = out_bytes[-MAX_OUTPUT:].decode("utf-8", errors="replace") if out_bytes else ""

    # Report back to KukuiBot API
    payload = json.dumps({
        "run_id": run_id,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "status": status,
        "output_tail": output_tail,
        "trigger_source": trigger_source,
    })
    try:
        subprocess.run(
            ["curl", "-sk", "-X", "POST",
             f"{KUKUIBOT_URL}/api/scheduler/jobs/{job_id}/report-run",
             "-H", "Content-Type: application/json",
             "-d", payload],
            timeout=10, capture_output=True,
        )
    except Exception:
        pass  # Best-effort reporting


if __name__ == "__main__":
    main()
