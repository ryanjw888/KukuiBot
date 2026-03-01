"""Phase 1 — Reconnaissance & Discovery: find live hosts on the network."""

import re
import time
from datetime import datetime

from ..audit_log import AuditLog
from ..config import AuditConfig, DISCOVERY_TIMEOUT, MDNS_TIMEOUT
from ..executor import run_command, run_shell
from ..parallel import run_parallel_dict


async def run_discovery(config: AuditConfig, audit_log: AuditLog) -> list[dict]:
    """Discover live hosts via ARP, ping, mDNS, IPv6. Returns list of host dicts."""
    start = time.monotonic()
    start_time = datetime.now().isoformat()
    commands_run = []
    errors = []

    # Run discovery methods in parallel
    tasks = {
        "arp": _discover_arp(),
        "ping_sweep": _discover_ping_sweep(config),
    }

    if config.tools.get("nmap") and config.tools["nmap"].available:
        tasks["nmap_ping"] = _discover_nmap_ping(config)

    if config.network.interface:
        tasks["mdns"] = _discover_mdns()
        tasks["ipv6"] = _discover_ipv6(config.network.interface)

    results = await run_parallel_dict(tasks, max_workers=5)

    # Merge all discovered hosts by IP
    hosts_by_ip: dict[str, dict] = {}

    for method_name, result in results.items():
        if isinstance(result, dict) and "error" in result:
            errors.append(f"{method_name}: {result['error']}")
            continue
        if not isinstance(result, list):
            continue
        for host in result:
            ip = host.get("ip", "")
            if not ip:
                continue
            if ip in hosts_by_ip:
                existing = hosts_by_ip[ip]
                # Merge MACs (prefer non-empty)
                if not existing.get("mac") and host.get("mac"):
                    existing["mac"] = host["mac"]
                # Merge hostnames
                names = set(existing.get("hostnames", []))
                names.update(host.get("hostnames", []))
                existing["hostnames"] = sorted(names)
                if not existing.get("hostname") and host.get("hostname"):
                    existing["hostname"] = host["hostname"]
                # Keep first discovery method
            else:
                hosts_by_ip[ip] = {
                    "ip": ip,
                    "mac": host.get("mac", ""),
                    "hostname": host.get("hostname", ""),
                    "hostnames": host.get("hostnames", []),
                    "discovery_method": method_name,
                    "is_gateway": ip == config.network.gateway,
                    "vendor": "",
                    "category": "",
                }

    # Add all hosts to audit log
    for host in hosts_by_ip.values():
        audit_log.add_host(host)

    elapsed = time.monotonic() - start
    audit_log.log_phase(
        phase=1,
        name="Reconnaissance & Discovery",
        start_time=start_time,
        end_time=datetime.now().isoformat(),
        duration=elapsed,
        hosts_discovered=len(hosts_by_ip),
        errors=errors,
    )
    audit_log.save()

    print(f"[Phase 1] Discovery complete in {elapsed:.1f}s — {len(hosts_by_ip)} hosts found")
    return list(hosts_by_ip.values())


async def _discover_arp() -> list[dict]:
    """Parse existing ARP cache."""
    result = await run_command(["arp", "-a"], timeout=DISCOVERY_TIMEOUT)
    hosts = []
    if result.exit_code != 0:
        return hosts

    for line in result.stdout.splitlines():
        # Format: hostname (ip) at mac on interface [ifscope ...]
        m = re.match(r"(\S+)\s+\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+(\S+)", line)
        if m:
            hostname = m.group(1) if m.group(1) != "?" else ""
            ip = m.group(2)
            mac = m.group(3)
            if mac == "(incomplete)":
                mac = ""
            hosts.append({
                "ip": ip,
                "mac": mac,
                "hostname": hostname,
                "hostnames": [hostname] if hostname else [],
            })
    return hosts


async def _discover_ping_sweep(config: AuditConfig) -> list[dict]:
    """Broadcast ping sweep."""
    broadcast = config.network.broadcast
    if not broadcast:
        # Calculate broadcast from subnet
        if config.subnet:
            parts = config.subnet.split("/")
            if len(parts) == 2:
                ip_parts = [int(x) for x in parts[0].split(".")]
                cidr = int(parts[1])
                host_bits = 32 - cidr
                mask = (0xFFFFFFFF >> host_bits) << host_bits
                ip_int = sum(ip_parts[i] << (24 - i * 8) for i in range(4))
                bcast_int = ip_int | (~mask & 0xFFFFFFFF)
                broadcast = ".".join(str((bcast_int >> (24 - i * 8)) & 0xFF) for i in range(4))

    if not broadcast:
        return []

    result = await run_command(
        ["ping", "-c", "1", "-W", "1", "-t", "2", broadcast],
        timeout=DISCOVERY_TIMEOUT,
    )
    # Broadcast ping may not return hosts directly on macOS;
    # the ARP cache gets populated though. Re-read ARP after ping.
    return await _discover_arp()


async def _discover_nmap_ping(config: AuditConfig) -> list[dict]:
    """nmap ping sweep (-sn)."""
    if not config.subnet:
        return []

    result = await run_command(
        ["nmap", "-sn", config.subnet],
        timeout=30,
    )
    hosts = []
    if result.exit_code != 0:
        return hosts

    current_ip = ""
    current_mac = ""
    current_hostname = ""

    for line in result.stdout.splitlines():
        # "Nmap scan report for hostname (ip)" or "Nmap scan report for ip"
        m = re.match(r"Nmap scan report for (\S+)\s*(?:\((\d+\.\d+\.\d+\.\d+)\))?", line)
        if m:
            if current_ip:
                hosts.append({
                    "ip": current_ip,
                    "mac": current_mac,
                    "hostname": current_hostname,
                    "hostnames": [current_hostname] if current_hostname else [],
                })
            if m.group(2):
                current_hostname = m.group(1)
                current_ip = m.group(2)
            else:
                current_ip = m.group(1)
                current_hostname = ""
            current_mac = ""
            continue

        # "MAC Address: XX:XX:XX:XX:XX:XX (Vendor)"
        m = re.match(r"MAC Address:\s+(\S+)\s*(?:\((.+)\))?", line)
        if m:
            current_mac = m.group(1)

    if current_ip:
        hosts.append({
            "ip": current_ip,
            "mac": current_mac,
            "hostname": current_hostname,
            "hostnames": [current_hostname] if current_hostname else [],
        })

    return hosts


async def _discover_mdns() -> list[dict]:
    """mDNS/DNS-SD service browsing."""
    result = await run_shell(
        "dns-sd -B _services._dns-sd._udp local. 2>/dev/null",
        timeout=MDNS_TIMEOUT,
    )
    # dns-sd doesn't resolve to IPs directly — just notes service existence
    # The real value is in populating hostnames. Return empty for now;
    # mDNS hostnames are better captured via nmap -sn which reports .local names
    return []


async def _discover_ipv6(interface: str) -> list[dict]:
    """IPv6 link-local neighbor discovery."""
    result = await run_command(
        ["ping6", "-c", "2", "-I", interface, "ff02::1"],
        timeout=DISCOVERY_TIMEOUT,
    )
    hosts = []
    if result.exit_code != 0:
        return hosts

    # Parse responses for unique IPv6 addresses
    seen = set()
    for line in result.stdout.splitlines():
        m = re.search(r"from\s+([0-9a-fA-F:]+)", line)
        if m:
            ipv6 = m.group(1)
            if ipv6 not in seen:
                seen.add(ipv6)
                # IPv6 link-local addresses aren't directly useful for the port scan
                # but indicate device presence
    return hosts
