#!/usr/bin/env python3
"""list_networks.py — List connected networks with subnets for audit target selection.

Outputs a numbered list of active network interfaces with IP/subnet info.
Designed to be called by IT Admin workers before starting an audit.
"""

import json
import re
import subprocess
import sys


def get_connected_networks() -> list[dict]:
    """Detect all active network interfaces with IP addresses."""
    networks = []

    # Get hardware port mapping
    port_map = {}
    try:
        r = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=5,
        )
        current_name = ""
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("Hardware Port:"):
                current_name = line.split(":", 1)[1].strip()
            elif line.startswith("Device:"):
                device = line.split(":", 1)[1].strip()
                if current_name:
                    port_map[device] = current_name
    except Exception:
        pass

    # Get default gateway interface
    default_iface = ""
    default_gw = ""
    try:
        r = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("interface:"):
                default_iface = line.split(":", 1)[1].strip()
            elif line.startswith("gateway:"):
                default_gw = line.split(":", 1)[1].strip()
    except Exception:
        pass

    # Get Wi-Fi SSID if connected
    wifi_ssid = ""
    try:
        # Try the modern macOS command first
        r = subprocess.run(
            ["networksetup", "-getairportnetwork", "en0"],
            capture_output=True, text=True, timeout=5,
        )
        if "Current Wi-Fi Network" in r.stdout or "You are associated" in r.stdout:
            wifi_ssid = r.stdout.split(":", 1)[-1].strip()
        # Also try en1 (some Macs use en1 for Wi-Fi)
        if not wifi_ssid:
            r = subprocess.run(
                ["networksetup", "-getairportnetwork", "en1"],
                capture_output=True, text=True, timeout=5,
            )
            if "Current Wi-Fi Network" in r.stdout or "You are associated" in r.stdout:
                wifi_ssid = r.stdout.split(":", 1)[-1].strip()
    except Exception:
        pass

    # Parse ifconfig for active interfaces with IPv4 addresses
    try:
        r = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
        current_iface = ""
        current_flags = ""

        for line in r.stdout.splitlines():
            # Interface header line
            m = re.match(r"^(\w+):\s+flags=\d+<([^>]*)>", line)
            if m:
                current_iface = m.group(1)
                current_flags = m.group(2)
                continue

            # IPv4 address line
            if current_iface and "inet " in line and "127.0.0.1" not in line:
                line = line.strip()
                parts = line.split()
                if len(parts) < 4:
                    continue

                ip = parts[1]
                # Skip link-local
                if ip.startswith("169.254."):
                    continue

                # Parse netmask
                subnet = ""
                nm_idx = parts.index("netmask") + 1 if "netmask" in parts else -1
                if nm_idx > 0 and nm_idx < len(parts):
                    hex_mask = parts[nm_idx]
                    try:
                        mask_int = int(hex_mask, 16)
                        cidr = bin(mask_int).count("1")
                        ip_parts = [int(x) for x in ip.split(".")]
                        mask_parts = [
                            (mask_int >> (24 - i * 8)) & 0xFF for i in range(4)
                        ]
                        net_parts = [ip_parts[i] & mask_parts[i] for i in range(4)]
                        subnet = ".".join(str(x) for x in net_parts) + f"/{cidr}"
                    except ValueError:
                        pass

                # Parse broadcast
                broadcast = ""
                if "broadcast" in parts:
                    b_idx = parts.index("broadcast") + 1
                    if b_idx < len(parts):
                        broadcast = parts[b_idx]

                # Skip non-UP interfaces and loopback/tunnel interfaces
                if "UP" not in current_flags or "RUNNING" not in current_flags:
                    continue
                if current_iface.startswith(("lo", "gif", "stf", "anpi", "awdl",
                                              "llw", "ap", "bridge", "utun",
                                              "ipsec", "ppp")):
                    continue

                # Determine friendly name
                friendly = port_map.get(current_iface, current_iface)
                is_default = current_iface == default_iface

                # Add Wi-Fi SSID to name if applicable
                if wifi_ssid and ("wi-fi" in friendly.lower() or "wifi" in friendly.lower()
                                  or "airport" in friendly.lower()):
                    friendly = f"{friendly} ({wifi_ssid})"

                # Determine connection type
                conn_type = "Ethernet"
                fl = friendly.lower()
                if "wi-fi" in fl or "wifi" in fl or "airport" in fl:
                    conn_type = "Wi-Fi"
                elif "thunderbolt" in fl:
                    conn_type = "Thunderbolt"
                elif "usb" in fl:
                    conn_type = "USB"
                elif "vpn" in fl or "tun" in current_iface:
                    conn_type = "VPN"

                gateway = default_gw if is_default else ""

                networks.append({
                    "interface": current_iface,
                    "name": friendly,
                    "type": conn_type,
                    "ip": ip,
                    "subnet": subnet,
                    "broadcast": broadcast,
                    "gateway": gateway,
                    "is_default": is_default,
                    "ssid": wifi_ssid if conn_type == "Wi-Fi" else "",
                })

    except Exception:
        pass

    # Sort: default interface first, then by interface name
    networks.sort(key=lambda n: (not n["is_default"], n["interface"]))
    return networks


def main():
    networks = get_connected_networks()

    if "--json" in sys.argv:
        print(json.dumps(networks, indent=2))
        return

    if not networks:
        print("No active network connections found.")
        return

    print("Connected Networks:")
    print("-" * 70)
    for i, net in enumerate(networks, 1):
        default_marker = " [DEFAULT]" if net["is_default"] else ""
        ssid_info = f" (SSID: {net['ssid']})" if net.get("ssid") else ""
        gw_info = f"  GW: {net['gateway']}" if net.get("gateway") else ""

        print(f"  {i}. {net['name']}{default_marker}")
        print(f"     Type: {net['type']}{ssid_info}")
        print(f"     Interface: {net['interface']}  IP: {net['ip']}")
        print(f"     Subnet: {net['subnet']}{gw_info}")
        print()


if __name__ == "__main__":
    main()
