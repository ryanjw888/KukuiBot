#!/usr/bin/env python3
"""
Monitor service - manages the continuous monitoring process.
This runs as a background service separate from the main server.
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

MONITOR_HOME = Path(os.path.expanduser("~/.kukuibot"))
LOGS_DIR = MONITOR_HOME / "logs"
MONITOR_SCRIPT = MONITOR_HOME / "src" / "monitor.py"
MONITOR_PID_FILE = MONITOR_HOME / ".monitor.pid"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "monitor_service.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def start_monitor():
    """Start the monitor process"""
    if MONITOR_PID_FILE.exists():
        try:
            pid = int(MONITOR_PID_FILE.read_text().strip())
            # Check if process is still running
            if os.path.exists(f"/proc/{pid}") or subprocess.run(
                ["kill", "-0", str(pid)], capture_output=True
            ).returncode == 0:
                logger.info(f"Monitor already running (PID: {pid})")
                return True
        except (ValueError, FileNotFoundError):
            pass
    
    logger.info("Starting monitor process...")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(MONITOR_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detach from parent
        )
        MONITOR_PID_FILE.write_text(str(proc.pid))
        logger.info(f"Monitor started (PID: {proc.pid})")
        return True
    except Exception as e:
        logger.error(f"Failed to start monitor: {e}")
        return False


def stop_monitor():
    """Stop the monitor process"""
    if not MONITOR_PID_FILE.exists():
        logger.info("Monitor not running")
        return True
    
    try:
        pid = int(MONITOR_PID_FILE.read_text().strip())
        logger.info(f"Stopping monitor (PID: {pid})...")
        os.kill(pid, 15)  # SIGTERM
        time.sleep(2)
        MONITOR_PID_FILE.unlink()
        logger.info("Monitor stopped")
        return True
    except (ValueError, ProcessLookupError, FileNotFoundError) as e:
        logger.error(f"Failed to stop monitor: {e}")
        MONITOR_PID_FILE.unlink(missing_ok=True)
        return False


def status_monitor():
    """Check monitor status"""
    if not MONITOR_PID_FILE.exists():
        return {"running": False, "pid": None}
    
    try:
        pid = int(MONITOR_PID_FILE.read_text().strip())
        # Check if process is still running
        if subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0:
            return {"running": True, "pid": pid}
        else:
            MONITOR_PID_FILE.unlink()
            return {"running": False, "pid": None}
    except (ValueError, FileNotFoundError):
        return {"running": False, "pid": None}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: monitor_service.py [start|stop|status]")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "start":
        sys.exit(0 if start_monitor() else 1)
    elif command == "stop":
        sys.exit(0 if stop_monitor() else 1)
    elif command == "status":
        status = status_monitor()
        print(json.dumps(status, indent=2))
        sys.exit(0 if status["running"] else 1)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
