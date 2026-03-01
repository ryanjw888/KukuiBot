"""progress.py — Real-time progress file for worker polling."""

import json
import time
from datetime import datetime
from pathlib import Path


class ProgressTracker:
    """Writes a progress.json file that workers can poll to track audit status."""

    def __init__(self, output_dir: Path):
        self._path = output_dir / "progress.json"
        self._start = time.monotonic()
        self._data = {
            "status": "starting",
            "started_at": datetime.now().isoformat(),
            "elapsed_seconds": 0,
            "current_phase": 0,
            "current_phase_name": "Initializing",
            "phases_completed": [],
            "total_hosts": 0,
            "total_ports": 0,
            "total_vulns": 0,
            "errors": [],
            "last_update": datetime.now().isoformat(),
            "message": "Audit starting...",
        }
        self._save()

    def start_phase(self, phase: int, name: str, message: str = ""):
        self._data["status"] = "running"
        self._data["current_phase"] = phase
        self._data["current_phase_name"] = name
        self._data["message"] = message or f"Phase {phase}: {name}..."
        self._update()

    def update_phase(self, message: str, **kwargs):
        """Update progress within a phase."""
        self._data["message"] = message
        for k, v in kwargs.items():
            if k in self._data:
                self._data[k] = v
        self._update()

    def complete_phase(self, phase: int, name: str, duration: float, summary: str = ""):
        self._data["phases_completed"].append({
            "phase": phase,
            "name": name,
            "duration_seconds": round(duration, 1),
            "summary": summary,
        })
        self._data["message"] = summary or f"Phase {phase} complete"
        self._update()

    def set_stats(self, hosts: int = 0, ports: int = 0, vulns: int = 0):
        if hosts:
            self._data["total_hosts"] = hosts
        if ports:
            self._data["total_ports"] = ports
        if vulns:
            self._data["total_vulns"] = vulns
        self._update()

    def add_error(self, error: str):
        self._data["errors"].append(error)
        self._update()

    def complete(self, message: str = "Audit complete", scan_results_path: str = ""):
        self._data["status"] = "completed"
        self._data["message"] = message
        if scan_results_path:
            self._data["scan_results"] = scan_results_path
        self._update()

    def fail(self, message: str):
        self._data["status"] = "failed"
        self._data["message"] = message
        self._update()

    def _update(self):
        self._data["elapsed_seconds"] = round(time.monotonic() - self._start, 1)
        self._data["last_update"] = datetime.now().isoformat()
        self._save()

    def _save(self):
        try:
            self._path.write_text(json.dumps(self._data, indent=2, default=str))
        except Exception:
            pass

    @property
    def path(self) -> Path:
        return self._path
