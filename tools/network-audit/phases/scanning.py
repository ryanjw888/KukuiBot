"""Phase 2 — Port Scanning & Service Enumeration.

Strategy (tiered by device type):
  1. Pre-scan triage: use MAC vendor to categorize hosts into scan tiers
     - QUICK tier (IoT, printers, cameras, phones, smart home): nmap top-100
     - FULL tier (computers, NAS, network equipment, gateways, unknown): nmap top-1000
  2. Run both tiers in parallel (batched nmap calls)
  3. If RustScan is available, run full-65535 sweep on "interesting" FULL-tier hosts
     (gateways, servers, NAS, hosts with 5+ open ports)
  4. nmap service-enum on any newly discovered ports from RustScan

This saves significant time — IoT/printers/cameras have predictable ports, so
scanning their top-1000 wastes ~90% of the scan time on ports that will never
be open. Computers/NAS/servers need the full top-1000 because they run
arbitrary services.
"""

import asyncio
import time
from datetime import datetime
from pathlib import Path

from ..audit_log import AuditLog
from ..config import (
    AuditConfig,
    NMAP_TOP_PORTS,
    NMAP_QUICK_PORTS,
    RUSTSCAN_BATCH_SIZE,
    RUSTSCAN_TIMEOUT_MS,
    SCAN_TIMEOUT_PER_HOST,
)
from ..executor import run_command
from ..parallel import run_parallel
from ..parsers.nmap_parser import parse_nmap_xml
from ..parsers.rustscan_parser import parse_rustscan_output

# How many hosts to include in a single nmap call
NMAP_BATCH_SIZE_QUICK = 24  # Simple devices, fewer ports → bigger batches
NMAP_BATCH_SIZE_FULL = 16   # More ports but still LAN-local
MAX_CONCURRENT_BATCHES = 4  # nmap processes running at once (LAN has plenty of bandwidth)

# Thresholds for deciding which hosts get a RustScan deep sweep
RUSTSCAN_MIN_OPEN_PORTS = 5  # Hosts with this many+ open ports are "interesting"
RUSTSCAN_INTERESTING_PORTS = {22, 80, 443, 8080, 8443, 548, 445, 5000, 5001}

# Device categories that get a quick scan (top-100 only)
QUICK_SCAN_CATEGORIES = {
    "IoT", "Smart Home", "Printer", "Camera", "Phone/Tablet",
}

# Vendor keywords that map directly to quick-scan (used in triage before
# full Phase 4 classification — just needs the vendor string from OUI)
QUICK_SCAN_VENDORS = {
    # Smart Home
    "google": "Smart Home",
    "amazon": "Smart Home",
    "sonos": "Smart Home",
    "ring": "Smart Home",
    "nest": "Smart Home",
    "ecobee": "Smart Home",
    "philips": "Smart Home",
    # Printers
    "brother": "Printer",
    "canon": "Printer",
    "epson": "Printer",
    "xerox": "Printer",
    "lexmark": "Printer",
    # Cameras
    "hikvision": "Camera",
    "dahua": "Camera",
    "axis": "Camera",
    "reolink": "Camera",
    # IoT
    "raspberry": "IoT",
    "espressif": "IoT",
    # Phones/Tablets
    "samsung": "Phone/Tablet",
    "oneplus": "Phone/Tablet",
    "xiaomi": "Phone/Tablet",
    "huawei": "Phone/Tablet",
}

# Vendor keywords that map to full-scan tier
FULL_SCAN_VENDORS = {
    "apple", "dell", "lenovo", "hewlett", "intel", "microsoft", "hp inc",
    "synology", "qnap", "western digital",
    "ubiquiti", "cisco", "netgear", "tp-link", "aruba", "ruckus", "meraki",
    "mikrotik",
}


def triage_hosts(live_hosts: list[dict]) -> dict[str, list[str]]:
    """Categorize hosts into scan tiers based on vendor and gateway status.

    Returns {"quick": [ip, ...], "full": [ip, ...]}.
    Called from audit.py after early OUI lookup so vendor is available.
    """
    quick_ips = []
    full_ips = []

    for host in live_hosts:
        ip = host["ip"]
        vendor = host.get("vendor", "").lower()

        # Gateways always get full scan
        if host.get("is_gateway"):
            full_ips.append(ip)
            continue

        # If already classified (e.g. from a prior partial run), use category
        category = host.get("category", "")
        if category in QUICK_SCAN_CATEGORIES:
            quick_ips.append(ip)
            continue
        if category and category != "Unknown":
            full_ips.append(ip)
            continue

        # Triage by vendor keyword
        tier = None
        if vendor:
            for keyword in QUICK_SCAN_VENDORS:
                if keyword in vendor:
                    tier = "quick"
                    break
            if tier is None:
                for keyword in FULL_SCAN_VENDORS:
                    if keyword in vendor:
                        tier = "full"
                        break

        if tier == "quick":
            quick_ips.append(ip)
        else:
            # Unknown vendor → full scan (safer to scan more than miss something)
            full_ips.append(ip)

    return {"quick": quick_ips, "full": full_ips}


async def run_scanning(config: AuditConfig, audit_log: AuditLog) -> None:
    """Port scan and service enumeration on all discovered hosts."""
    start = time.monotonic()
    start_time = datetime.now().isoformat()
    errors = []
    commands_run = []

    live_hosts = audit_log.get_live_hosts()
    if not live_hosts:
        print("[Phase 2] No live hosts to scan", flush=True)
        audit_log.log_phase(
            phase=2, name="Port Scanning & Service Enumeration",
            start_time=start_time, end_time=datetime.now().isoformat(),
            duration=0, status="skipped", errors=["No live hosts"],
        )
        audit_log.save()
        return

    has_rustscan = config.tools.get("rustscan") and config.tools["rustscan"].available

    # Create XML output directory
    xml_dir = config.output_dir / "nmap-xml"
    xml_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Triage hosts into scan tiers ──
    tiers = triage_hosts(live_hosts)
    quick_ips = tiers["quick"]
    full_ips = tiers["full"]

    # ── Step 2: Run quick and full nmap scans in parallel ──
    all_batch_tasks = []

    if quick_ips:
        quick_batch_count = (len(quick_ips) + NMAP_BATCH_SIZE_QUICK - 1) // NMAP_BATCH_SIZE_QUICK
        print(f"[Phase 2] Quick scan: {len(quick_ips)} devices in {quick_batch_count} batches "
              f"(nmap top-{NMAP_QUICK_PORTS}, no scripts)...", flush=True)
        for batch_ips in _batch_list(quick_ips, NMAP_BATCH_SIZE_QUICK):
            all_batch_tasks.append(
                _scan_batch_nmap(batch_ips, xml_dir, config, audit_log,
                                 top_ports=NMAP_QUICK_PORTS, label="quick",
                                 use_scripts=False, host_timeout=60)
            )

    if full_ips:
        full_batch_count = (len(full_ips) + NMAP_BATCH_SIZE_FULL - 1) // NMAP_BATCH_SIZE_FULL
        print(f"[Phase 2] Full scan:  {len(full_ips)} devices in {full_batch_count} batches "
              f"(nmap top-{NMAP_TOP_PORTS})...", flush=True)
        for batch_ips in _batch_list(full_ips, NMAP_BATCH_SIZE_FULL):
            all_batch_tasks.append(
                _scan_batch_nmap(batch_ips, xml_dir, config, audit_log,
                                 top_ports=NMAP_TOP_PORTS, label="full")
            )

    if all_batch_tasks:
        batch_results = await run_parallel(all_batch_tasks, max_workers=MAX_CONCURRENT_BATCHES)
        for r in batch_results:
            if isinstance(r, dict):
                errors.extend(r.get("errors", []))
                commands_run.extend(r.get("commands", []))

    # ── Step 3: RustScan deep sweep on "interesting" FULL-tier hosts only ──
    if has_rustscan and full_ips:
        interesting = _pick_interesting_hosts(audit_log, full_ips)
        if interesting:
            print(f"[Phase 2] RustScan deep sweep on {len(interesting)} interesting hosts: "
                  f"{', '.join(interesting)}...", flush=True)

            rs_tasks = [_rustscan_deep(ip, config) for ip in interesting]
            rs_results = await run_parallel(rs_tasks, max_workers=4)

            # Collect any NEW ports not already known from nmap
            extra_ports: dict[str, list[int]] = {}
            for ip, rs in zip(interesting, rs_results):
                if not isinstance(rs, dict):
                    continue
                commands_run.extend(rs.get("commands", []))
                errors.extend(rs.get("errors", []))
                rs_ports = set(rs.get("ports", []))
                if not rs_ports:
                    continue

                # Compare against what nmap already found
                host_data = audit_log.get_host(ip)
                known_ports = set()
                if host_data:
                    known_ports = {
                        p["port"] for p in host_data.get("ports", [])
                        if p.get("state") == "open"
                    }
                new_ports = rs_ports - known_ports
                if new_ports:
                    extra_ports[ip] = sorted(new_ports)
                    print(f"    {ip}: RustScan found {len(new_ports)} new ports: "
                          f"{', '.join(str(p) for p in sorted(new_ports))}", flush=True)

            # Record RustScan ports immediately (nmap -sV may fail to connect)
            for ip, ports in extra_ports.items():
                audit_log.add_host({
                    "ip": ip,
                    "ports": [{
                        "port": p,
                        "protocol": "tcp",
                        "state": "open",
                        "service": "",
                        "version": "",
                        "banner": "",
                        "scripts": {},
                        "source": "rustscan",
                    } for p in ports],
                })

            # Step 4: nmap service-enum on just the NEW ports
            if extra_ports:
                print(f"  nmap service-enum on {len(extra_ports)} hosts with new ports...",
                      flush=True)
                extra_tasks = []
                for batch in _batch_hosts_with_ports(extra_ports, NMAP_BATCH_SIZE_FULL):
                    extra_tasks.append(
                        _scan_batch_nmap_specific(batch, xml_dir, config, audit_log,
                                                  suffix="_deep")
                    )
                extra_results = await run_parallel(extra_tasks, max_workers=2)
                for r in extra_results:
                    if isinstance(r, dict):
                        errors.extend(r.get("errors", []))
                        commands_run.extend(r.get("commands", []))
            else:
                print("    No new ports found beyond nmap top-1000", flush=True)

    # ── Step 4: Retry pass for "silent" hosts ──
    # Hosts that responded to ARP/ping during discovery but returned 0 open
    # ports from the batched nmap scan.  These are typically slow or flaky
    # devices (e.g. pool controllers, legacy IoT) whose service-detection
    # probes cause nmap to hit its per-host timeout when batched with 15
    # other hosts.  Re-scan them individually with lighter parameters.
    silent_ips = _find_silent_hosts(audit_log)
    if silent_ips:
        print(f"[Phase 2] Retry pass: {len(silent_ips)} silent hosts identified, "
              f"rescanning individually...", flush=True)
        retry_tasks = [
            _retry_silent_host(ip, xml_dir, config, audit_log)
            for ip in silent_ips
        ]
        # Run retries with high parallelism — each host gets its own nmap
        # process so slow devices don't hold up others.  Most will finish
        # quickly (genuinely portless) so effective wall-clock is low.
        retry_results = await run_parallel(retry_tasks, max_workers=8)
        retry_found = 0
        for r in retry_results:
            if isinstance(r, dict):
                errors.extend(r.get("errors", []))
                commands_run.extend(r.get("commands", []))
                retry_found += r.get("ports_found", 0)
        if retry_found:
            print(f"    Retry pass recovered {retry_found} open port(s)", flush=True)
        else:
            print(f"    Retry pass: no additional ports found (hosts may be offline)",
                  flush=True)

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
    print(f"[Phase 2] Scanning complete in {elapsed:.1f}s — {total_ports} open ports found",
          flush=True)


def _pick_interesting_hosts(audit_log: AuditLog, full_ips: list[str]) -> list[str]:
    """Select full-tier hosts worth a full-65535 RustScan sweep.

    Criteria (any one qualifies):
    - Gateway/router
    - 5+ open ports from nmap (likely a server or NAS)
    - Has server-like services (SSH + HTTP, or NAS ports like 548/5000)
    """
    interesting = []
    for ip in full_ips:
        host_data = audit_log.get_host(ip)
        if not host_data:
            continue

        open_ports = {
            p["port"] for p in host_data.get("ports", [])
            if p.get("state") == "open"
        }

        # Gateway — always interesting
        if host_data.get("is_gateway"):
            interesting.append(ip)
            continue

        # Many open ports — likely a server
        if len(open_ports) >= RUSTSCAN_MIN_OPEN_PORTS:
            interesting.append(ip)
            continue

        # Has server/NAS signature ports
        if len(open_ports & RUSTSCAN_INTERESTING_PORTS) >= 3:
            interesting.append(ip)
            continue

    return interesting


def _batch_list(items: list, size: int) -> list[list]:
    """Split a list into batches of the given size."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _batch_hosts_with_ports(
    host_ports: dict[str, list[int]], size: int,
) -> list[dict[str, list[int]]]:
    """Split a {ip: [ports]} dict into batches of the given size."""
    items = list(host_ports.items())
    batches = []
    for i in range(0, len(items), size):
        batches.append(dict(items[i:i + size]))
    return batches


async def _rustscan_deep(ip: str, config: AuditConfig) -> dict:
    """RustScan full-65535 port sweep on a single host."""
    errors = []
    commands = []

    rs_result = await run_command(
        [
            "rustscan", "-a", ip,
            "--range", "1-65535",
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

    return {"ports": ports_found, "commands": commands, "errors": errors}


async def _scan_batch_nmap_specific(
    host_ports: dict[str, list[int]],
    xml_dir: Path,
    config: AuditConfig,
    audit_log: AuditLog,
    suffix: str = "",
) -> dict:
    """nmap service-enum on a batch of hosts with known open ports."""
    errors = []
    commands = []

    ips = list(host_ports.keys())
    all_ports = set()
    for ports in host_ports.values():
        all_ports.update(ports)
    ports_str = ",".join(str(p) for p in sorted(all_ports))

    batch_name = f"batch_{ips[0].replace('.', '_')}_{len(ips)}{suffix}"
    xml_path = xml_dir / f"{batch_name}.xml"

    nmap_cmd = []
    if config.use_sudo:
        nmap_cmd = ["sudo", "nmap"]
    else:
        nmap_cmd = ["nmap"]

    nmap_cmd.extend([
        "-sV", "-sC",
        "-p", ports_str,
        "-T4",
        "--host-timeout", f"{SCAN_TIMEOUT_PER_HOST}s",
        "-oX", str(xml_path),
        *ips,
    ])

    batch_timeout = SCAN_TIMEOUT_PER_HOST + (len(ips) - 1) * 30 + 10
    result = await run_command(nmap_cmd, timeout=batch_timeout, retries=1)
    commands.append({
        "cmd": result.command,
        "exit_code": result.exit_code,
        "duration": round(result.duration, 2),
    })

    if result.exit_code != 0 and not result.timed_out:
        errors.append(f"nmap batch scan failed: {result.stderr[:200]}")

    if xml_path.exists():
        parsed_hosts = parse_nmap_xml(xml_path)
        for ph in parsed_hosts:
            audit_log.add_host(ph)
        print(f"    Batch done: {len(ips)} hosts, "
              f"{sum(len(ph.get('ports', [])) for ph in parsed_hosts)} ports "
              f"({result.duration:.0f}s)", flush=True)

    return {"errors": errors, "commands": commands}


async def _scan_batch_nmap(
    ips: list[str],
    xml_dir: Path,
    config: AuditConfig,
    audit_log: AuditLog,
    top_ports: int = NMAP_TOP_PORTS,
    label: str = "",
    use_scripts: bool = True,
    host_timeout: int = SCAN_TIMEOUT_PER_HOST,
) -> dict:
    """nmap top-ports scan on a batch of hosts.

    Args:
        use_scripts: If False, skip -sC (default scripts). Saves ~10-20s per
                     host on simple devices where scripts add no value.
        host_timeout: Per-host timeout in seconds. Quick-tier uses 60s,
                      full-tier uses 90s (the default).
    """
    errors = []
    commands = []

    suffix = f"_{label}" if label else ""
    batch_name = f"batch_{ips[0].replace('.', '_')}_{len(ips)}{suffix}"
    xml_path = xml_dir / f"{batch_name}.xml"

    nmap_cmd = []
    if config.use_sudo:
        nmap_cmd = ["sudo", "nmap", "-sS"]
    else:
        nmap_cmd = ["nmap", "-sT"]

    nmap_cmd.append("-sV")
    if use_scripts:
        nmap_cmd.append("-sC")

    nmap_cmd.extend([
        "--top-ports", str(top_ports),
        "-T4",
        "--host-timeout", f"{host_timeout}s",
        "-oX", str(xml_path),
        *ips,
    ])

    batch_timeout = host_timeout + (len(ips) - 1) * 20 + 10
    result = await run_command(nmap_cmd, timeout=batch_timeout, retries=1)
    commands.append({
        "cmd": result.command,
        "exit_code": result.exit_code,
        "duration": round(result.duration, 2),
    })

    if result.exit_code != 0 and not result.timed_out:
        errors.append(f"nmap batch scan failed: {result.stderr[:200]}")

    if xml_path.exists():
        parsed_hosts = parse_nmap_xml(xml_path)
        for ph in parsed_hosts:
            audit_log.add_host(ph)
        print(f"    [{label or 'scan'}] Batch done: {len(ips)} hosts, "
              f"{sum(len(ph.get('ports', [])) for ph in parsed_hosts)} ports "
              f"({result.duration:.0f}s)", flush=True)

    return {"errors": errors, "commands": commands}


# ── Retry pass helpers ──────────────────────────────────────────────


def _find_silent_hosts(audit_log: AuditLog) -> list[str]:
    """Return IPs of hosts that were discovered alive but have 0 open ports.

    These are candidates for a retry scan — they likely timed out during
    the batched nmap scan due to slow network stacks (legacy IoT, pool
    controllers, etc.).

    Filters out:
      - Multicast/broadcast addresses (224.x, 239.x, 255.x)
      - Link-local addresses (169.254.x)
      - Ping-sweep-only discoveries with no vendor (likely dead IPs
        responding to broadcast)
      - Hosts discovered via ARP with a known vendor are the best
        retry candidates — they are real devices
    """
    silent = []
    for host in audit_log.get_live_hosts():
        ip = host["ip"]

        # Skip multicast, broadcast, link-local
        first_octet = int(ip.split(".")[0])
        if first_octet in (224, 239, 255) or ip.startswith("169.254."):
            continue

        ports = host.get("ports", [])
        if ports:
            continue  # Already has results

        # Prioritise ARP-confirmed devices with a known vendor
        discovery = host.get("discovery_method", "")
        vendor = host.get("vendor", "")

        if discovery == "arp" and vendor:
            # Real device confirmed by ARP + OUI lookup — always retry
            silent.append(ip)
        elif discovery == "arp":
            # ARP-confirmed but unknown vendor — still retry
            silent.append(ip)
        # Skip ping_sweep-only with no vendor/MAC — high false-positive rate

    return silent


RETRY_HOST_TIMEOUT = 120  # Give slow devices extra time when scanned alone
RETRY_TOP_PORTS = 200     # Broader than quick-scan, narrower than full


async def _retry_silent_host(
    ip: str,
    xml_dir: Path,
    config: AuditConfig,
    audit_log: AuditLog,
) -> dict:
    """Retry a single silent host with lighter nmap parameters.

    Strategy:
      - Scan one host at a time (no batching competition)
      - No -sC scripts (service probes only — scripts cause the timeouts)
      - Top-200 ports (covers all common services without excessive probing)
      - 120s host timeout (vs 90s in the batch scan)
      - T3 timing (gentler than T4 — reduces probe retransmission storms)
    """
    errors = []
    commands = []

    xml_path = xml_dir / f"retry_{ip.replace('.', '_')}.xml"

    nmap_cmd = []
    if config.use_sudo:
        nmap_cmd = ["sudo", "nmap", "-sS"]
    else:
        nmap_cmd = ["nmap", "-sT"]

    nmap_cmd.extend([
        "-sV",                    # Service version detection only (no scripts)
        "--top-ports", str(RETRY_TOP_PORTS),
        "-T3",                    # Gentler timing for slow devices
        "--host-timeout", f"{RETRY_HOST_TIMEOUT}s",
        "--max-retries", "2",     # Fewer retries to avoid overwhelming device
        "-oX", str(xml_path),
        ip,
    ])

    result = await run_command(nmap_cmd, timeout=RETRY_HOST_TIMEOUT + 30, retries=0)
    commands.append({
        "cmd": result.command,
        "exit_code": result.exit_code,
        "duration": round(result.duration, 2),
    })

    ports_found = 0
    if result.exit_code != 0 and not result.timed_out:
        errors.append(f"Retry scan failed for {ip}: {result.stderr[:200]}")

    if xml_path.exists():
        parsed_hosts = parse_nmap_xml(xml_path)
        for ph in parsed_hosts:
            ph_ports = len(ph.get("ports", []))
            if ph_ports > 0:
                audit_log.add_host(ph)
                ports_found += ph_ports
                print(f"    {ip}: retry found {ph_ports} port(s)", flush=True)

    return {"errors": errors, "commands": commands, "ports_found": ports_found}
