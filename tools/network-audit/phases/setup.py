"""Phase 0 — Pre-Audit Setup: detect network, create dirs, check tools."""

import time
from datetime import datetime
from pathlib import Path

from ..audit_log import AuditLog
from ..config import (
    DEFAULT_REPORT_DIR,
    AuditConfig,
    detect_all_tools,
    detect_network,
)


async def run_setup(config: AuditConfig) -> AuditLog:
    """Initialize the audit: detect network, tools, create output directory."""
    start = time.monotonic()
    start_time = datetime.now().isoformat()
    errors = []

    # Detect network
    network = detect_network()
    config.network = network

    # Use auto-detected subnet if not provided
    if not config.subnet and network.subnet:
        config.subnet = network.subnet
    elif not config.subnet:
        errors.append("Could not auto-detect subnet. Use --subnet to specify.")

    # Detect tools
    tools = detect_all_tools()
    config.tools = tools

    if not tools.get("nmap", None) or not tools["nmap"].available:
        errors.append("nmap not found — required for scanning")

    # Create timestamped output directory
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if config.output_dir == DEFAULT_REPORT_DIR:
        config.output_dir = config.output_dir / timestamp
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize audit log
    audit_log = AuditLog(config.output_dir)
    audit_log.update_meta("subnet", config.subnet)
    audit_log.update_meta("gateway", network.gateway)
    audit_log.update_meta("interface", network.interface)
    audit_log.update_meta("local_ip", network.local_ip)
    audit_log.update_meta("client_name", config.client_name)
    audit_log.update_meta("tools_available", {
        name: info.available for name, info in tools.items()
    })
    audit_log.update_meta("tools_versions", {
        name: info.version for name, info in tools.items() if info.available
    })

    elapsed = time.monotonic() - start
    audit_log.log_phase(
        phase=0,
        name="Pre-Audit Setup",
        start_time=start_time,
        end_time=datetime.now().isoformat(),
        duration=elapsed,
        status="completed" if not errors else "completed_with_errors",
        commands_run=[
            {"cmd": "route -n get default", "exit_code": 0, "duration": 0},
            {"cmd": f"ifconfig {network.interface}", "exit_code": 0, "duration": 0},
        ],
        errors=errors,
    )
    audit_log.save()

    print(f"[Phase 0] Setup complete in {elapsed:.1f}s")
    print(f"  Interface: {network.interface}")
    print(f"  Local IP:  {network.local_ip}")
    print(f"  Gateway:   {network.gateway}")
    print(f"  Subnet:    {config.subnet}")
    print(f"  Output:    {config.output_dir}")
    for name, info in tools.items():
        status = f"v{info.version}" if info.available else "NOT FOUND"
        print(f"  {name}: {status}")
    if errors:
        for e in errors:
            print(f"  WARNING: {e}")

    return audit_log
