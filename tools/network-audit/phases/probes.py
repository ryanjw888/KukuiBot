"""Phase 3 — Targeted Security Probes: SSH, TLS, HTTP, SMB/AFP + Nuclei."""

import tempfile
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


async def run_probes(config: AuditConfig, audit_log: AuditLog) -> None:
    """Run targeted security probes on open ports + Nuclei batch scan."""
    start = time.monotonic()
    start_time = datetime.now().isoformat()
    errors = []
    commands_run = []

    live_hosts = audit_log.get_live_hosts()
    if not live_hosts:
        print("[Phase 3] No hosts to probe")
        audit_log.log_phase(
            phase=3, name="Targeted Security Probes",
            start_time=start_time, end_time=datetime.now().isoformat(),
            duration=0, status="skipped",
        )
        audit_log.save()
        return

    xml_dir = config.output_dir / "probe-xml"
    xml_dir.mkdir(parents=True, exist_ok=True)

    # Build per-host probe tasks based on open ports from Phase 2
    probe_tasks = []
    nuclei_targets = []

    for host in live_hosts:
        ip = host["ip"]
        open_ports = {p["port"] for p in host.get("ports", []) if p.get("state") == "open"}

        if not open_ports:
            continue

        nuclei_targets.append(ip)

        # Determine which protocol-specific probes to run
        # Only run probes for protocols NOT already covered by Phase 2's -sC scripts
        probe_scripts = []

        ssh_open = open_ports & SSH_PORTS
        if ssh_open:
            # ssh2-enum-algos and ssh-hostkey may already be in -sC, but we want full detail
            probe_scripts.append(("ssh", sorted(ssh_open), "ssh2-enum-algos,ssh-hostkey"))

        tls_open = open_ports & TLS_PORTS
        if tls_open:
            probe_scripts.append(("tls", sorted(tls_open), "ssl-cert,ssl-enum-ciphers"))

        http_open = open_ports & HTTP_PORTS
        if http_open:
            probe_scripts.append(("http", sorted(http_open),
                                  "http-title,http-server-header,http-auth,http-methods"))

        smb_open = open_ports & SMB_PORTS
        if smb_open:
            probe_scripts.append(("smb", sorted(smb_open),
                                  "smb-security-mode,smb-enum-shares,smb2-security-mode"))

        afp_open = open_ports & AFP_PORTS
        if afp_open:
            probe_scripts.append(("afp", sorted(afp_open), "afp-serverinfo"))

        if probe_scripts:
            probe_tasks.append(
                _probe_host(ip, probe_scripts, xml_dir, config, audit_log)
            )

    # Run per-host probes in parallel
    if probe_tasks:
        print(f"[Phase 3] Running protocol probes on {len(probe_tasks)} hosts...")
        results = await run_parallel(probe_tasks, max_workers=config.max_hosts)
        for r in results:
            if isinstance(r, dict):
                errors.extend(r.get("errors", []))
                commands_run.extend(r.get("commands", []))

    # Nuclei batch scan
    has_nuclei = config.tools.get("nuclei") and config.tools["nuclei"].available
    if has_nuclei and nuclei_targets:
        print(f"[Phase 3] Running Nuclei scan on {len(nuclei_targets)} targets...")
        nuclei_result = await _run_nuclei_batch(
            nuclei_targets, config, audit_log,
            templates=["default-logins", "exposed-panels", "misconfiguration"],
            severity="critical,high,medium",
        )
        errors.extend(nuclei_result.get("errors", []))
        commands_run.extend(nuclei_result.get("commands", []))

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

    print(f"[Phase 3] Probes complete in {elapsed:.1f}s")


async def _probe_host(
    ip: str,
    probe_scripts: list[tuple],
    xml_dir: Path,
    config: AuditConfig,
    audit_log: AuditLog,
) -> dict:
    """Run nmap protocol-specific scripts on a single host."""
    errors = []
    commands = []

    for proto, ports, scripts in probe_scripts:
        ports_str = ",".join(str(p) for p in ports)
        xml_path = xml_dir / f"{ip.replace('.', '_')}_{proto}.xml"

        nmap_cmd = []
        if config.use_sudo:
            nmap_cmd = ["sudo", "nmap"]
        else:
            nmap_cmd = ["nmap"]

        nmap_cmd.extend([
            "-p", ports_str,
            "--script", scripts,
            "--host-timeout", f"{SCAN_TIMEOUT_PER_HOST}s",
            "-oX", str(xml_path),
            ip,
        ])

        result = await run_command(nmap_cmd, timeout=SCAN_TIMEOUT_PER_HOST + 10)
        commands.append({
            "cmd": result.command,
            "exit_code": result.exit_code,
            "duration": round(result.duration, 2),
        })

        if result.exit_code != 0:
            errors.append(f"Probe {proto} failed for {ip}: {result.stderr[:200]}")
            continue

        if xml_path.exists():
            parsed = parse_nmap_xml(xml_path)
            for ph in parsed:
                if ph.get("ip") == ip:
                    audit_log.add_host(ph)

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

    # Write targets file
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
        if not targets_urls or ip not in str(targets_urls):
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

    # Parse results
    if output_file.exists():
        findings = parse_nuclei_jsonl(output_file)
        for f in findings:
            audit_log.add_nuclei_finding(f)
            # Also add to host's vulnerabilities
            host_ip = f.get("host", "")
            if host_ip:
                host = audit_log.get_host(host_ip)
                if host:
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
