"""Phase 2 — Port Scanning & Service Enumeration."""

import tempfile
import time
from datetime import datetime
from pathlib import Path

from ..audit_log import AuditLog
from ..config import (
    AuditConfig,
    NMAP_TOP_PORTS,
    RUSTSCAN_BATCH_SIZE,
    RUSTSCAN_TIMEOUT_MS,
    SCAN_TIMEOUT_PER_HOST,
)
from ..executor import run_command
from ..parallel import run_parallel
from ..parsers.nmap_parser import parse_nmap_xml
from ..parsers.rustscan_parser import parse_rustscan_output


async def run_scanning(config: AuditConfig, audit_log: AuditLog) -> None:
    """Port scan and service enumeration on all discovered hosts."""
    start = time.monotonic()
    start_time = datetime.now().isoformat()
    errors = []
    commands_run = []

    live_hosts = audit_log.get_live_hosts()
    if not live_hosts:
        print("[Phase 2] No live hosts to scan")
        audit_log.log_phase(
            phase=2, name="Port Scanning & Service Enumeration",
            start_time=start_time, end_time=datetime.now().isoformat(),
            duration=0, status="skipped", errors=["No live hosts"],
        )
        audit_log.save()
        return

    has_rustscan = config.tools.get("rustscan") and config.tools["rustscan"].available
    use_sudo = config.use_sudo

    # Create XML output directory
    xml_dir = config.output_dir / "nmap-xml"
    xml_dir.mkdir(parents=True, exist_ok=True)

    print(f"[Phase 2] Scanning {len(live_hosts)} hosts "
          f"({'RustScan + nmap' if has_rustscan else 'nmap only'})...")

    # Build scan tasks
    tasks = []
    for host in live_hosts:
        ip = host["ip"]
        if has_rustscan:
            tasks.append(_scan_host_rustscan(ip, xml_dir, config, audit_log))
        else:
            tasks.append(_scan_host_nmap_only(ip, xml_dir, config, audit_log))

    # Run scans in parallel
    results = await run_parallel(tasks, max_workers=config.max_hosts)

    for r in results:
        if isinstance(r, dict):
            if r.get("errors"):
                errors.extend(r["errors"])
            if r.get("commands"):
                commands_run.extend(r["commands"])

    elapsed = time.monotonic() - start
    audit_log.log_phase(
        phase=2,
        name="Port Scanning & Service Enumeration",
        start_time=start_time,
        end_time=datetime.now().isoformat(),
        duration=elapsed,
        commands_run=commands_run,
        errors=errors,
    )
    audit_log.save()

    total_ports = sum(len(h.get("ports", [])) for h in audit_log.get_live_hosts())
    print(f"[Phase 2] Scanning complete in {elapsed:.1f}s — {total_ports} open ports found")


async def _scan_host_rustscan(
    ip: str, xml_dir: Path, config: AuditConfig, audit_log: AuditLog,
) -> dict:
    """RustScan for port discovery, then nmap for service enumeration."""
    errors = []
    commands = []

    # Step 1: RustScan for fast port discovery
    rs_result = await run_command(
        [
            "rustscan", "-a", ip, "--top",
            "--timeout", str(RUSTSCAN_TIMEOUT_MS),
            "-b", str(RUSTSCAN_BATCH_SIZE),
        ],
        timeout=SCAN_TIMEOUT_PER_HOST,
        retries=1,
    )
    commands.append({
        "cmd": rs_result.command,
        "exit_code": rs_result.exit_code,
        "duration": round(rs_result.duration, 2),
    })

    ports_found = []
    if rs_result.exit_code == 0:
        port_map = parse_rustscan_output(rs_result.stdout)
        ports_found = port_map.get(ip, [])

    if not ports_found:
        # Fallback to nmap top ports
        return await _scan_host_nmap_only(ip, xml_dir, config, audit_log)

    # Step 2: nmap service enum on discovered ports only
    ports_str = ",".join(str(p) for p in sorted(ports_found))
    xml_path = xml_dir / f"{ip.replace('.', '_')}.xml"

    # ONE nmap pass: -sV -sC merges service detection + default scripts
    nmap_cmd = ["nmap"]
    if config.use_sudo:
        nmap_cmd = ["sudo", "nmap"]

    nmap_cmd.extend([
        "-sV", "-sC",
        "-p", ports_str,
        "--host-timeout", f"{SCAN_TIMEOUT_PER_HOST}s",
        "-oX", str(xml_path),
        ip,
    ])

    nm_result = await run_command(nmap_cmd, timeout=SCAN_TIMEOUT_PER_HOST + 10, retries=1)
    commands.append({
        "cmd": nm_result.command,
        "exit_code": nm_result.exit_code,
        "duration": round(nm_result.duration, 2),
    })

    if nm_result.exit_code != 0 and not nm_result.timed_out:
        errors.append(f"nmap failed for {ip}: {nm_result.stderr[:200]}")

    # Parse results
    if xml_path.exists():
        parsed_hosts = parse_nmap_xml(xml_path)
        for ph in parsed_hosts:
            if ph.get("ip") == ip:
                audit_log.add_host(ph)

    return {"errors": errors, "commands": commands}


async def _scan_host_nmap_only(
    ip: str, xml_dir: Path, config: AuditConfig, audit_log: AuditLog,
) -> dict:
    """Full nmap scan when RustScan is unavailable."""
    errors = []
    commands = []

    xml_path = xml_dir / f"{ip.replace('.', '_')}.xml"

    nmap_cmd = ["nmap"]
    if config.use_sudo:
        nmap_cmd = ["sudo", "nmap", "-sS"]
    else:
        nmap_cmd = ["nmap", "-sT"]

    nmap_cmd.extend([
        "-sV", "-sC",
        "--top-ports", str(NMAP_TOP_PORTS),
        "-T4",
        "--host-timeout", f"{SCAN_TIMEOUT_PER_HOST}s",
        "-oX", str(xml_path),
        ip,
    ])

    result = await run_command(nmap_cmd, timeout=SCAN_TIMEOUT_PER_HOST + 10, retries=1)
    commands.append({
        "cmd": result.command,
        "exit_code": result.exit_code,
        "duration": round(result.duration, 2),
    })

    if result.exit_code != 0 and not result.timed_out:
        errors.append(f"nmap failed for {ip}: {result.stderr[:200]}")

    if xml_path.exists():
        parsed_hosts = parse_nmap_xml(xml_path)
        for ph in parsed_hosts:
            if ph.get("ip") == ip:
                audit_log.add_host(ph)

    return {"errors": errors, "commands": commands}
