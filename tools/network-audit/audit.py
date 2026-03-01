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
from datetime import datetime
from pathlib import Path

# Allow running directly or as a module
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    # Adjust for relative imports when running directly
    import importlib
    import types

    pkg_dir = Path(__file__).parent
    pkg_name = "network_audit"

    # Create a fake package so relative imports work
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(pkg_dir)]
    pkg.__package__ = pkg_name
    sys.modules[pkg_name] = pkg

    # Re-map this module's package
    sys.modules[__name__].__package__ = pkg_name

from .config import AuditConfig, DEFAULT_REPORT_DIR
from .audit_log import AuditLog
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
    """Run all audit phases sequentially."""
    print("=" * 60)
    print("  Kukui IT — Network Security Audit")
    print("=" * 60)
    print()

    # Phase 0: Setup
    audit_log = await run_setup(config)
    if not config.subnet:
        print("\nERROR: Could not determine subnet. Use --subnet to specify.")
        sys.exit(1)
    print()

    # Phase 1: Discovery
    live_hosts = await run_discovery(config, audit_log)
    if not live_hosts:
        print("\nWARNING: No live hosts discovered. Check subnet and permissions.")
    print()

    # Phase 2: Port Scanning & Service Enumeration
    await run_scanning(config, audit_log)
    print()

    # Phase 3: Targeted Security Probes
    await run_probes(config, audit_log)
    print()

    # Phase 4: Vendor ID & Device Classification
    await run_classify(config, audit_log)
    print()

    # Phase 5: Vulnerability Assessment
    await run_vulns(config, audit_log)
    print()

    # Finalize
    audit_log.finalize()
    audit_log.save()

    print("=" * 60)
    print(f"  Audit complete!")
    print(f"  Results: {audit_log.path}")
    print(f"  Hosts:   {len(audit_log.get_live_hosts())}")
    stats = audit_log.to_dict().get("summary_stats", {})
    print(f"  Ports:   {stats.get('total_open_ports', 0)}")
    vulns = stats.get("vulnerabilities_by_severity", {})
    vuln_summary = ", ".join(f"{v} {k}" for k, v in vulns.items() if v)
    if vuln_summary:
        print(f"  Vulns:   {vuln_summary}")
    print("=" * 60)

    return audit_log.path


async def run_single_phase(config: AuditConfig, phase: str) -> Path:
    """Run a single audit phase."""
    audit_log = await run_setup(config)

    if phase == "setup":
        pass  # Already done
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

    # Determine output path
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

    if args.phase:
        result_path = asyncio.run(run_single_phase(config, args.phase))
    else:
        result_path = asyncio.run(run_full_audit(config))

    print(f"\nScan results saved to: {result_path}")


if __name__ == "__main__":
    main()
