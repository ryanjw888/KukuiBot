#!/usr/bin/env python3
"""audit.py — Network Audit CLI entry point.

Usage:
  # Run full audit (auto-detect subnet)
  python3 audit.py

  # Run full audit with specific subnet
  python3 audit.py --subnet 192.168.1.0/24

  # Run with client name and custom output
  python3 audit.py --subnet 192.168.1.0/24 --client "Acme Corp" --output /tmp/audit

  # Run specific phase only
  python3 audit.py --subnet 192.168.1.0/24 --phase discovery

  # Render report from existing scan data + AI analysis
  python3 audit.py --render --scan-data /path/scan.json --analysis /path/analysis.json

  # Run without sudo (limited scan quality)
  python3 audit.py --no-sudo
"""

import argparse
import asyncio
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Allow running directly or as a module
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    import types

    pkg_dir = Path(__file__).parent
    pkg_name = "network_audit"

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(pkg_dir)]
    pkg.__package__ = pkg_name
    sys.modules[pkg_name] = pkg
    sys.modules[__name__].__package__ = pkg_name

from .config import AuditConfig, DEFAULT_REPORT_DIR
from .audit_log import AuditLog
from .progress import ProgressTracker
from .phases.setup import run_setup
from .phases.discovery import run_discovery
from .phases.scanning import run_scanning
from .phases.probes import run_probes
from .phases.classify import run_classify
from .phases.vulns import run_vulns
from .report.renderer import render_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kukui IT Network Security Audit Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--subnet", type=str, default="",
        help="Target subnet in CIDR notation (e.g., 192.168.1.0/24). Auto-detects if omitted.",
    )
    parser.add_argument(
        "--output", type=str, default="",
        help=f"Output directory (default: {DEFAULT_REPORT_DIR}/<timestamp>/)",
    )
    parser.add_argument(
        "--client", type=str, default="",
        help="Client name for report header",
    )
    parser.add_argument(
        "--phase", type=str, default="",
        choices=["", "setup", "discovery", "scanning", "probes", "classify", "vulns"],
        help="Run a single phase only",
    )
    parser.add_argument(
        "--render", action="store_true",
        help="Render mode: generate HTML report from scan data + analysis JSON",
    )
    parser.add_argument(
        "--scan-data", type=str, default="",
        help="Path to scan_results.json (for --render mode)",
    )
    parser.add_argument(
        "--analysis", type=str, default="",
        help="Path to analysis.json (for --render mode)",
    )
    parser.add_argument(
        "--no-sudo", action="store_true",
        help="Disable sudo (limits scan quality but allows unprivileged runs)",
    )
    parser.add_argument(
        "--max-hosts", type=int, default=8,
        help="Max concurrent host scans (default: 8)",
    )
    return parser.parse_args()


async def run_full_audit(config: AuditConfig) -> Path:
    """Run all audit phases with progress tracking."""
    print("=" * 60, flush=True)
    print("  Kukui IT — Network Security Audit", flush=True)
    print("=" * 60, flush=True)
    print(flush=True)

    # Phase 0: Setup
    audit_log = await run_setup(config)
    if not config.subnet:
        print("\nERROR: Could not determine subnet. Use --subnet to specify.", flush=True)
        sys.exit(1)

    # Initialize progress tracker (after setup creates output_dir)
    progress = ProgressTracker(config.output_dir)
    progress.complete_phase(0, "Pre-Audit Setup", 0.1, "Setup complete")
    print(flush=True)

    # Phase 1: Discovery
    progress.start_phase(1, "Discovery", "Discovering live hosts on the network...")
    t1 = time.monotonic()
    live_hosts = await run_discovery(config, audit_log)
    d1 = time.monotonic() - t1
    host_count = len(live_hosts)
    progress.set_stats(hosts=host_count)
    progress.complete_phase(1, "Discovery", d1, f"Found {host_count} live hosts")
    if not live_hosts:
        print("\nWARNING: No live hosts discovered. Check subnet and permissions.", flush=True)
    print(flush=True)

    # Phase 2: Port Scanning + Phase 4: Classification can overlap
    # Scan ports first, then classify while probes run
    progress.start_phase(2, "Port Scanning", f"Scanning {host_count} hosts for open ports...")
    t2 = time.monotonic()
    await run_scanning(config, audit_log)
    d2 = time.monotonic() - t2
    total_ports = sum(len(h.get("ports", [])) for h in audit_log.get_live_hosts())
    progress.set_stats(ports=total_ports)
    progress.complete_phase(2, "Port Scanning", d2, f"Found {total_ports} open ports")
    print(flush=True)

    # Phase 3 + 4 + 5 in parallel:
    #   - Phase 3: Targeted probes (nmap scripts + Nuclei misconfig)
    #   - Phase 4: Classification (pure Python, instant)
    #   - Phase 5: Nuclei CVE scan
    # These are independent — probes use nmap scripts, vulns use Nuclei CVEs,
    # classify is pure OUI lookup. Run them all at once.
    progress.start_phase(3, "Probes + Classification + Vuln Scan",
                         "Running security probes, device classification, and vulnerability scan in parallel...")
    t345 = time.monotonic()

    results_345 = await asyncio.gather(
        _run_phase_safe(run_probes, config, audit_log, "probes"),
        _run_phase_safe(run_classify, config, audit_log, "classify"),
        _run_phase_safe(run_vulns, config, audit_log, "vulns"),
        return_exceptions=True,
    )

    d345 = time.monotonic() - t345

    # Log any errors from parallel phases
    for i, (name, r) in enumerate(zip(["probes", "classify", "vulns"], results_345)):
        if isinstance(r, Exception):
            err_msg = f"Phase {name} failed: {r}"
            progress.add_error(err_msg)
            print(f"  WARNING: {err_msg}", flush=True)

    # Update final stats
    audit_log.update_stats()
    stats = audit_log.to_dict().get("summary_stats", {})
    total_ports = stats.get("total_open_ports", 0)
    vuln_counts = stats.get("vulnerabilities_by_severity", {})
    total_vulns = sum(vuln_counts.values())
    progress.set_stats(ports=total_ports, vulns=total_vulns)

    progress.complete_phase(3, "Probes", d345, "Security probes complete")
    progress.complete_phase(4, "Classification", 0, "Device classification complete")
    progress.complete_phase(5, "Vulnerability Assessment", 0,
                           f"Found {total_vulns} vulnerability findings")
    print(flush=True)

    # Finalize
    audit_log.finalize()
    audit_log.save()

    vuln_summary = ", ".join(f"{v} {k}" for k, v in vuln_counts.items() if v)
    cat_summary = ", ".join(
        f"{v} {k}" for k, v in sorted(
            stats.get("device_categories", {}).items(), key=lambda x: -x[1]
        )
    )

    print("=" * 60, flush=True)
    print(f"  Audit complete!", flush=True)
    print(f"  Results: {audit_log.path}", flush=True)
    print(f"  Hosts:   {stats.get('total_hosts', 0)}", flush=True)
    print(f"  Ports:   {total_ports}", flush=True)
    if cat_summary:
        print(f"  Devices: {cat_summary}", flush=True)
    if vuln_summary:
        print(f"  Vulns:   {vuln_summary}", flush=True)
    total_elapsed = audit_log.to_dict()["audit_meta"].get("duration_seconds", 0)
    print(f"  Time:    {total_elapsed:.0f}s", flush=True)
    print("=" * 60, flush=True)

    progress.complete(
        message=f"Audit complete: {stats.get('total_hosts', 0)} hosts, "
                f"{total_ports} ports, {total_vulns} findings",
        scan_results_path=str(audit_log.path),
    )

    return audit_log.path


async def _run_phase_safe(phase_fn, config, audit_log, name):
    """Run a phase function, catching and returning exceptions."""
    try:
        return await phase_fn(config, audit_log)
    except Exception as e:
        print(f"  ERROR in {name}: {e}", flush=True)
        traceback.print_exc()
        raise


async def run_single_phase(config: AuditConfig, phase: str) -> Path:
    """Run a single audit phase."""
    audit_log = await run_setup(config)

    if phase == "setup":
        pass
    elif phase == "discovery":
        await run_discovery(config, audit_log)
    elif phase == "scanning":
        await run_discovery(config, audit_log)
        await run_scanning(config, audit_log)
    elif phase == "probes":
        await run_discovery(config, audit_log)
        await run_scanning(config, audit_log)
        await run_probes(config, audit_log)
    elif phase == "classify":
        await run_discovery(config, audit_log)
        await run_classify(config, audit_log)
    elif phase == "vulns":
        await run_discovery(config, audit_log)
        await run_scanning(config, audit_log)
        await run_vulns(config, audit_log)

    audit_log.finalize()
    audit_log.save()
    return audit_log.path


def render_mode(args: argparse.Namespace) -> Path:
    """Render HTML report from existing scan data + analysis."""
    if not args.scan_data:
        print("ERROR: --scan-data is required in render mode")
        sys.exit(1)
    if not args.analysis:
        print("ERROR: --analysis is required in render mode")
        sys.exit(1)

    scan_path = Path(args.scan_data)
    analysis_path = Path(args.analysis)

    if not scan_path.exists():
        print(f"ERROR: Scan data not found: {scan_path}")
        sys.exit(1)
    if not analysis_path.exists():
        print(f"ERROR: Analysis file not found: {analysis_path}")
        sys.exit(1)

    scan_data = json.loads(scan_path.read_text())
    analysis = json.loads(analysis_path.read_text())

    if args.output:
        output_path = Path(args.output)
        if output_path.is_dir() or not output_path.suffix:
            output_path = output_path / "report.html"
    else:
        output_path = scan_path.parent / "report.html"

    report_path = render_report(scan_data, analysis, output_path)
    print(f"Report generated: {report_path}")
    return report_path


def main():
    args = parse_args()

    if args.render:
        render_mode(args)
        return

    config = AuditConfig(
        subnet=args.subnet,
        client_name=args.client,
        use_sudo=not args.no_sudo,
        max_hosts=args.max_hosts,
    )

    if args.output:
        config.output_dir = Path(args.output)

    try:
        if args.phase:
            result_path = asyncio.run(run_single_phase(config, args.phase))
        else:
            result_path = asyncio.run(run_full_audit(config))

        print(f"\nScan results saved to: {result_path}", flush=True)
    except KeyboardInterrupt:
        print("\nAudit interrupted by user.", flush=True)
        sys.exit(130)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
