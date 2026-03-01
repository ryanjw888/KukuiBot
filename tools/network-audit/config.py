"""config.py — Tool detection, sudo checks, macOS-specific settings."""

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_REPORT_DIR = Path.home() / ".kukuibot" / "reports" / "network-audits"
CACHE_DIR = Path.home() / ".kukuibot" / "cache"
SCAN_TIMEOUT_PER_HOST = 90
MAX_CONCURRENT_HOSTS = 8
RUSTSCAN_BATCH_SIZE = 512
RUSTSCAN_TIMEOUT_MS = 500
NMAP_TOP_PORTS = 1000
DISCOVERY_TIMEOUT = 10
MDNS_TIMEOUT = 3
OUI_MAX_AGE_DAYS = 30


@dataclass
class ToolInfo:
    available: bool = False
    path: str = ""
    version: str = ""


@dataclass
class NetworkInfo:
    interface: str = ""
    local_ip: str = ""
    subnet: str = ""
    gateway: str = ""
    broadcast: str = ""


@dataclass
class AuditConfig:
    subnet: str = ""
    output_dir: Path = DEFAULT_REPORT_DIR
    client_name: str = ""
    use_sudo: bool = True
    max_hosts: int = MAX_CONCURRENT_HOSTS
    tools: dict = field(default_factory=dict)
    network: NetworkInfo = field(default_factory=NetworkInfo)


def detect_tool(name: str) -> ToolInfo:
    path = shutil.which(name)
    if not path:
        return ToolInfo()
    try:
        if name == "nmap":
            r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if "Nmap version" in line:
                    version = line.split("version")[1].strip().split()[0]
                    return ToolInfo(available=True, path=path, version=version)
            return ToolInfo(available=True, path=path, version="unknown")
        elif name == "rustscan":
            r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
            version = r.stdout.strip().split()[-1] if r.stdout.strip() else "unknown"
            return ToolInfo(available=True, path=path, version=version)
        elif name == "nuclei":
            r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=5)
            out = r.stdout.strip() or r.stderr.strip()
            version = "unknown"
            for line in out.splitlines():
                if "nuclei" in line.lower():
                    parts = line.strip().split()
                    for p in parts:
                        if p and p[0].isdigit():
                            version = p.rstrip(",")
                            break
                    break
            return ToolInfo(available=True, path=path, version=version)
    except Exception:
        return ToolInfo(available=True, path=path, version="unknown")
    return ToolInfo(available=True, path=path, version="unknown")


def detect_all_tools() -> dict[str, ToolInfo]:
    return {name: detect_tool(name) for name in ("nmap", "rustscan", "nuclei")}


def detect_network() -> NetworkInfo:
    info = NetworkInfo()
    try:
        r = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("gateway:"):
                info.gateway = line.split(":", 1)[1].strip()
            elif line.startswith("interface:"):
                info.interface = line.split(":", 1)[1].strip()
    except Exception:
        pass

    if info.interface:
        try:
            r = subprocess.run(
                ["ifconfig", info.interface],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet ") and "netmask" in line:
                    parts = line.split()
                    info.local_ip = parts[1]
                    # Parse hex netmask to CIDR
                    nm_idx = parts.index("netmask") + 1 if "netmask" in parts else -1
                    if nm_idx > 0 and nm_idx < len(parts):
                        hex_mask = parts[nm_idx]
                        try:
                            mask_int = int(hex_mask, 16)
                            cidr = bin(mask_int).count("1")
                            # Calculate network address
                            ip_parts = [int(x) for x in info.local_ip.split(".")]
                            mask_parts = [
                                (mask_int >> (24 - i * 8)) & 0xFF for i in range(4)
                            ]
                            net_parts = [ip_parts[i] & mask_parts[i] for i in range(4)]
                            info.subnet = ".".join(str(x) for x in net_parts) + f"/{cidr}"
                        except ValueError:
                            pass
                    if "broadcast" in parts:
                        b_idx = parts.index("broadcast") + 1
                        if b_idx < len(parts):
                            info.broadcast = parts[b_idx]
        except Exception:
            pass

    return info


def check_sudo() -> bool:
    try:
        r = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False
