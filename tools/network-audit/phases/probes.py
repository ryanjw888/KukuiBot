"""Phase 3 — Targeted Security Probes: SSH, TLS, HTTP, SMB/AFP + Nuclei.

Optimization: batches hosts into groups for nmap script scanning (one nmap
process per batch, not per host). Nuclei runs in parallel with all nmap probes.
"""

import asyncio
import time
from datetime import datetime
from pathlib import Path

from ..audit_log import AuditLog
from ..config import AuditConfig, SCAN_TIMEOUT_PER_HOST
from ..executor import run_command
from ..parallel import run_parallel
from ..parsers.nmap_parser import parse_nmap_xml
from ..parsers.nuclei_parser import parse_nuclei_jsonl

# Port groups for protocol-specific probes
SSH_PORTS = {22, 2222}
TLS_PORTS = {443, 8443, 993, 995, 465, 636}
HTTP_PORTS = {80, 443, 8080, 8443, 8000, 8888, 3000, 5000}
SMB_PORTS = {445, 139}
AFP_PORTS = {548}

# Scripts per protocol — only scripts NOT already in -sC default set
PROTO_SCRIPTS = {
    "ssh": "ssh2-enum-algos",
    "tls": "ssl-cert,ssl-enum-ciphers",
    "http": "http-title,http-server-header,http-auth,http-methods",
    "smb": "smb-security-mode,smb-enum-shares,smb2-security-mode",
    "afp": "afp-serverinfo",
}

# How many hosts per nmap probe batch
PROBE_BATCH_SIZE = 8


async def run_probes(config: AuditConfig, audit_log: AuditLog) -> None:
    """Run targeted security probes on open ports + Nuclei batch scan."""
    start = time.monotonic()
    start_time = datetime.now().isoformat()
    errors = []
    commands_run = []

    live_hosts = audit_log.get_live_hosts()
    if not live_hosts:
        print("[Phase 3] No hosts to probe", flush=True)
        audit_log.log_phase(
            phase=3, name="Targeted Security Probes",
            start_time=start_time, end_time=datetime.now().isoformat(),
            duration=0, status="skipped",
        )
        audit_log.save()
        return

    xml_dir = config.output_dir / "probe-xml"
    xml_dir.mkdir(parents=True, exist_ok=True)

    # Collect per-host probe requirements
    host_probes: dict[str, dict] = {}  # ip -> {"ports": set, "scripts": set}
    nuclei_targets = []

    for host in live_hosts:
        ip = host["ip"]
        open_ports = {p["port"] for p in host.get("ports", []) if p.get("state") == "open"}
        if not open_ports:
            continue

        nuclei_targets.append(ip)

        all_ports = set()
        all_scripts = set()

        if open_ports & SSH_PORTS:
            all_ports.update(open_ports & SSH_PORTS)
            all_scripts.update(PROTO_SCRIPTS["ssh"].split(","))
        if open_ports & TLS_PORTS:
            all_ports.update(open_ports & TLS_PORTS)
            all_scripts.update(PROTO_SCRIPTS["tls"].split(","))
        if open_ports & HTTP_PORTS:
            all_ports.update(open_ports & HTTP_PORTS)
            all_scripts.update(PROTO_SCRIPTS["http"].split(","))
        if open_ports & SMB_PORTS:
            all_ports.update(open_ports & SMB_PORTS)
            all_scripts.update(PROTO_SCRIPTS["smb"].split(","))
        if open_ports & AFP_PORTS:
            all_ports.update(open_ports & AFP_PORTS)
            all_scripts.update(PROTO_SCRIPTS["afp"].split(","))

        # Dynamic detection: probe any port with HTTP/TLS/SSH/SMB service
        # regardless of port number (catches non-standard ports)
        for p in host.get("ports", []):
            if p.get("state") != "open":
                continue
            svc = (p.get("service", "") or "").lower()
            port_num = p["port"]
            if "http" in svc and port_num not in all_ports:
                all_ports.add(port_num)
                all_scripts.update(PROTO_SCRIPTS["http"].split(","))
                if "https" in svc or "ssl" in svc:
                    all_scripts.update(PROTO_SCRIPTS["tls"].split(","))
            elif ("ssl" in svc or "tls" in svc) and port_num not in all_ports:
                all_ports.add(port_num)
                all_scripts.update(PROTO_SCRIPTS["tls"].split(","))
            elif "ssh" in svc and port_num not in all_ports:
                all_ports.add(port_num)
                all_scripts.update(PROTO_SCRIPTS["ssh"].split(","))
            elif ("smb" in svc or "microsoft-ds" in svc) and port_num not in all_ports:
                all_ports.add(port_num)
                all_scripts.update(PROTO_SCRIPTS["smb"].split(","))

        if all_ports and all_scripts:
            host_probes[ip] = {"ports": all_ports, "scripts": all_scripts}

    # Batch hosts into groups for nmap
    probe_batches = _batch_probe_hosts(host_probes, PROBE_BATCH_SIZE)

    # Run nmap probe batches and Nuclei IN PARALLEL
    has_nuclei = config.tools.get("nuclei") and config.tools["nuclei"].available
    all_tasks = []

    if probe_batches:
        total_hosts = sum(len(b) for b in probe_batches)
        print(f"[Phase 3] Running nmap probes on {total_hosts} hosts "
              f"in {len(probe_batches)} batches...", flush=True)
        all_tasks.append(_run_probe_batches(probe_batches, xml_dir, config, audit_log))

    if has_nuclei and nuclei_targets:
        print(f"[Phase 3] Running Nuclei scan on {len(nuclei_targets)} targets (parallel)...",
              flush=True)
        all_tasks.append(_run_nuclei_batch(nuclei_targets, config, audit_log,
                                           templates=["default-logins", "exposed-panels",
                                                      "misconfiguration"],
                                           severity="critical,high,medium"))

    if all_tasks:
        results = await asyncio.gather(*all_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                errors.append(f"Probe task failed: {r}")
            elif isinstance(r, dict):
                errors.extend(r.get("errors", []))
                commands_run.extend(r.get("commands", []))

    elapsed = time.monotonic() - start
    audit_log.log_phase(
        phase=3,
        name="Targeted Security Probes",
        start_time=start_time,
        end_time=datetime.now().isoformat(),
        duration=elapsed,
        commands_run=commands_run,
        errors=errors,
    )
    audit_log.save()

    print(f"[Phase 3] Probes complete in {elapsed:.1f}s", flush=True)


def _batch_probe_hosts(
    host_probes: dict[str, dict],
    batch_size: int,
) -> list[dict[str, dict]]:
    """Split host probes into batches."""
    items = list(host_probes.items())
    batches = []
    for i in range(0, len(items), batch_size):
        batches.append(dict(items[i:i + batch_size]))
    return batches


async def _run_probe_batches(
    batches: list[dict[str, dict]],
    xml_dir: Path,
    config: AuditConfig,
    audit_log: AuditLog,
) -> dict:
    """Run all probe batches concurrently (2 at a time), return merged results."""
    errors = []
    commands = []

    tasks = [
        _probe_batch(batch, idx, xml_dir, config, audit_log)
        for idx, batch in enumerate(batches)
    ]

    results = await run_parallel(tasks, max_workers=2)
    for r in results:
        if isinstance(r, dict):
            errors.extend(r.get("errors", []))
            commands.extend(r.get("commands", []))

    return {"errors": errors, "commands": commands}


async def _probe_batch(
    host_probes: dict[str, dict],
    batch_idx: int,
    xml_dir: Path,
    config: AuditConfig,
    audit_log: AuditLog,
) -> dict:
    """Run ONE nmap call for a batch of hosts with merged ports and scripts."""
    errors = []
    commands = []

    ips = list(host_probes.keys())

    # Merge all ports and scripts across all hosts in the batch
    all_ports = set()
    all_scripts = set()
    for info in host_probes.values():
        all_ports.update(info["ports"])
        all_scripts.update(info["scripts"])

    ports_str = ",".join(str(p) for p in sorted(all_ports))
    scripts_str = ",".join(sorted(all_scripts))
    xml_path = xml_dir / f"probe_batch_{batch_idx}.xml"

    nmap_cmd = []
    if config.use_sudo:
        nmap_cmd = ["sudo", "nmap"]
    else:
        nmap_cmd = ["nmap"]

    nmap_cmd.extend([
        "-p", ports_str,
        "--script", scripts_str,
        "--host-timeout", f"{SCAN_TIMEOUT_PER_HOST}s",
        "-oX", str(xml_path),
        *ips,
    ])

    # Timeout scales with batch size
    batch_timeout = SCAN_TIMEOUT_PER_HOST + (len(ips) - 1) * 30 + 10
    result = await run_command(nmap_cmd, timeout=batch_timeout, retries=1)
    commands.append({
        "cmd": result.command,
        "exit_code": result.exit_code,
        "duration": round(result.duration, 2),
    })

    if result.exit_code != 0 and not result.timed_out:
        errors.append(f"Probe batch {batch_idx} failed: {result.stderr[:200]}")
    elif xml_path.exists():
        parsed = parse_nmap_xml(xml_path)
        for ph in parsed:
            audit_log.add_host(ph)
        print(f"    Probe batch {batch_idx} done: {len(ips)} hosts ({result.duration:.0f}s)",
              flush=True)

    return {"errors": errors, "commands": commands}


async def _run_nuclei_batch(
    targets: list[str],
    config: AuditConfig,
    audit_log: AuditLog,
    templates: list[str] | None = None,
    severity: str = "critical,high,medium",
) -> dict:
    """Run Nuclei against a batch of targets."""
    errors = []
    commands = []

    targets_file = config.output_dir / "nuclei_targets.txt"
    targets_urls = []
    for ip in targets:
        host = audit_log.get_host(ip)
        if host:
            http_ports = {p["port"] for p in host.get("ports", [])
                         if p.get("state") == "open" and p["port"] in HTTP_PORTS}
            for port in sorted(http_ports):
                scheme = "https" if port in (443, 8443) else "http"
                targets_urls.append(f"{scheme}://{ip}:{port}")
        if not any(ip in u for u in targets_urls):
            targets_urls.append(f"http://{ip}")

    targets_file.write_text("\n".join(targets_urls))
    output_file = config.output_dir / "nuclei_results.jsonl"

    nuclei_cmd = [
        "nuclei",
        "-l", str(targets_file),
        "-severity", severity,
        "-jsonl",
        "-o", str(output_file),
        "-silent",
        "-timeout", "10",
        "-retries", "1",
    ]

    if templates:
        for t in templates:
            nuclei_cmd.extend(["-t", t])

    result = await run_command(nuclei_cmd, timeout=300)
    commands.append({
        "cmd": result.command,
        "exit_code": result.exit_code,
        "duration": round(result.duration, 2),
    })

    if result.exit_code != 0 and not result.timed_out:
        errors.append(f"Nuclei batch scan error: {result.stderr[:300]}")

    if output_file.exists():
        findings = parse_nuclei_jsonl(output_file)
        for f in findings:
            audit_log.add_nuclei_finding(f)
            host_ip = f.get("host", "")
            if host_ip:
                audit_log.add_host({
                    "ip": host_ip,
                    "vulnerabilities": [{
                        "id": f.get("template_id", ""),
                        "source": "nuclei",
                        "severity": f.get("severity", "info"),
                        "name": f.get("name", ""),
                        "description": f.get("description", ""),
                        "evidence": f.get("matched_at", ""),
                        "template": f.get("template_id", ""),
                        "matched_at": f.get("matched_at", ""),
                    }],
                })

    return {"errors": errors, "commands": commands}
