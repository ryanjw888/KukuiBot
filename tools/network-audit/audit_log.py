"""audit_log.py — Incremental JSON log manager with per-phase writes."""

import json
from datetime import datetime
from pathlib import Path


class AuditLog:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.output_dir / "scan_results.json"
        self._data = {
            "audit_meta": {
                "client_name": "",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "start_time": datetime.now().isoformat(),
                "end_time": "",
                "duration_seconds": 0,
                "subnet": "",
                "gateway": "",
                "interface": "",
                "local_ip": "",
                "tools_available": {},
                "tools_versions": {},
            },
            "hosts": [],
            "nuclei_findings": [],
            "phase_logs": [],
            "summary_stats": {
                "total_hosts": 0,
                "total_open_ports": 0,
                "vulnerabilities_by_severity": {
                    "critical": 0,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                    "info": 0,
                },
                "device_categories": {},
            },
        }

    def update_meta(self, key: str, value):
        self._data["audit_meta"][key] = value

    def add_host(self, host_dict: dict):
        ip = host_dict.get("ip", "")
        for i, h in enumerate(self._data["hosts"]):
            if h.get("ip") == ip:
                # Merge: keep existing data, overlay new
                merged = {**h, **host_dict}
                # Don't let empty strings overwrite non-empty values
                for key in ("vendor", "category", "hostname", "discovery_method", "mac"):
                    if not host_dict.get(key) and h.get(key):
                        merged[key] = h[key]
                # Merge ports by port number
                existing_ports = {(p["port"], p["protocol"]): p for p in h.get("ports", [])}
                for p in host_dict.get("ports", []):
                    existing_ports[(p["port"], p["protocol"])] = p
                merged["ports"] = list(existing_ports.values())
                # Merge security probes
                existing_probes = h.get("security_probes", {})
                new_probes = host_dict.get("security_probes", {})
                merged["security_probes"] = {**existing_probes, **new_probes}
                # Merge vulnerabilities by id
                existing_vulns = {v.get("id", ""): v for v in h.get("vulnerabilities", [])}
                for v in host_dict.get("vulnerabilities", []):
                    existing_vulns[v.get("id", f"anon-{id(v)}")] = v
                merged["vulnerabilities"] = list(existing_vulns.values())
                # Merge hostnames
                existing_names = set(h.get("hostnames", []))
                existing_names.update(host_dict.get("hostnames", []))
                merged["hostnames"] = sorted(existing_names)
                self._data["hosts"][i] = merged
                return
        # New host
        if "ports" not in host_dict:
            host_dict["ports"] = []
        if "security_probes" not in host_dict:
            host_dict["security_probes"] = {}
        if "vulnerabilities" not in host_dict:
            host_dict["vulnerabilities"] = []
        if "hostnames" not in host_dict:
            host_dict["hostnames"] = []
        self._data["hosts"].append(host_dict)

    def get_host(self, ip: str) -> dict | None:
        for h in self._data["hosts"]:
            if h.get("ip") == ip:
                return h
        return None

    def get_live_hosts(self) -> list[dict]:
        return list(self._data["hosts"])

    def add_nuclei_finding(self, finding: dict):
        self._data["nuclei_findings"].append(finding)

    def log_phase(
        self,
        phase: int,
        name: str,
        start_time: str,
        end_time: str,
        duration: float,
        status: str = "completed",
        commands_run: list | None = None,
        hosts_discovered: int = 0,
        errors: list | None = None,
    ):
        self._data["phase_logs"].append({
            "phase": phase,
            "name": name,
            "start_time": start_time,
            "end_time": end_time,
            "duration_seconds": round(duration, 2),
            "status": status,
            "commands_run": commands_run or [],
            "hosts_discovered": hosts_discovered,
            "errors": errors or [],
        })

    def update_stats(self):
        hosts = self._data["hosts"]
        total_ports = sum(len(h.get("ports", [])) for h in hosts)
        vuln_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
        categories = {}

        for h in hosts:
            cat = h.get("category", "Unknown")
            categories[cat] = categories.get(cat, 0) + 1
            for v in h.get("vulnerabilities", []):
                sev = v.get("severity", "info").lower()
                if sev in vuln_counts:
                    vuln_counts[sev] += 1

        for f in self._data.get("nuclei_findings", []):
            sev = f.get("severity", "info").lower()
            if sev in vuln_counts:
                vuln_counts[sev] += 1

        self._data["summary_stats"] = {
            "total_hosts": len(hosts),
            "total_open_ports": total_ports,
            "vulnerabilities_by_severity": vuln_counts,
            "device_categories": categories,
        }

    def finalize(self):
        self._data["audit_meta"]["end_time"] = datetime.now().isoformat()
        start = self._data["audit_meta"].get("start_time", "")
        if start:
            try:
                s = datetime.fromisoformat(start)
                e = datetime.now()
                self._data["audit_meta"]["duration_seconds"] = round(
                    (e - s).total_seconds(), 2
                )
            except ValueError:
                pass
        self.update_stats()

    def save(self):
        self.update_stats()
        self._path.write_text(json.dumps(self._data, indent=2, default=str))

    @classmethod
    def load(cls, path: str | Path) -> "AuditLog":
        p = Path(path)
        log = cls(p.parent)
        log._path = p
        log._data = json.loads(p.read_text())
        return log

    def to_dict(self) -> dict:
        return dict(self._data)

    @property
    def path(self) -> Path:
        return self._path
