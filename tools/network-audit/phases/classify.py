"""Phase 4 — Vendor ID & Device Classification."""

import time
from datetime import datetime

from ..audit_log import AuditLog
from ..config import AuditConfig
from ..oui.oui_lookup import get_vendor, preload


# Port-based classification heuristics
CATEGORY_RULES = [
    # (port_set, category, priority) — higher priority wins
    ({631, 9100, 515}, "Printer", 10),
    ({548}, "NAS", 8),
    ({5000, 5001}, "NAS", 7),  # Synology
    ({8200, 1900}, "Smart Home", 6),  # DLNA/UPnP
    ({554, 8554}, "Camera", 9),  # RTSP
    ({1883, 8883}, "IoT", 5),  # MQTT
    ({5353}, "IoT", 3),  # mDNS common on IoT
    ({3689}, "Computer", 4),  # DAAP (iTunes)
]

# Vendor-based classification
VENDOR_CATEGORIES = {
    "ubiquiti": "Network Equipment",
    "cisco": "Network Equipment",
    "netgear": "Network Equipment",
    "tp-link": "Network Equipment",
    "aruba": "Network Equipment",
    "ruckus": "Network Equipment",
    "meraki": "Network Equipment",
    "mikrotik": "Network Equipment",
    "apple": "Computer",
    "dell": "Computer",
    "lenovo": "Computer",
    "hewlett": "Computer",
    "intel": "Computer",
    "microsoft": "Computer",
    "hp inc": "Computer",
    "samsung": "Phone/Tablet",
    "google": "Smart Home",
    "amazon": "Smart Home",
    "sonos": "Smart Home",
    "ring": "Smart Home",
    "nest": "Smart Home",
    "ecobee": "Smart Home",
    "philips": "Smart Home",
    "synology": "NAS",
    "qnap": "NAS",
    "western digital": "NAS",
    "brother": "Printer",
    "canon": "Printer",
    "epson": "Printer",
    "xerox": "Printer",
    "lexmark": "Printer",
    "hikvision": "Camera",
    "dahua": "Camera",
    "axis": "Camera",
    "reolink": "Camera",
    "raspberry": "IoT",
    "espressif": "IoT",
}


async def run_classify(config: AuditConfig, audit_log: AuditLog) -> None:
    """Classify devices by vendor and open ports."""
    start = time.monotonic()
    start_time = datetime.now().isoformat()

    # Pre-load OUI database
    preload()

    hosts = audit_log.get_live_hosts()
    if not hosts:
        print("[Phase 4] No hosts to classify", flush=True)
        audit_log.log_phase(
            phase=4, name="Vendor ID & Device Classification",
            start_time=start_time, end_time=datetime.now().isoformat(),
            duration=0, status="skipped",
        )
        audit_log.save()
        return

    for host in hosts:
        ip = host["ip"]

        # OUI vendor lookup
        mac = host.get("mac", "")
        if mac and not host.get("vendor"):
            vendor = get_vendor(mac)
            audit_log.add_host({"ip": ip, "vendor": vendor})
        else:
            vendor = host.get("vendor", "")

        # Classify device
        category = _classify_device(host, vendor)
        audit_log.add_host({"ip": ip, "category": category})

    elapsed = time.monotonic() - start
    audit_log.log_phase(
        phase=4,
        name="Vendor ID & Device Classification",
        start_time=start_time,
        end_time=datetime.now().isoformat(),
        duration=elapsed,
    )
    audit_log.save()

    categories = {}
    for h in audit_log.get_live_hosts():
        cat = h.get("category", "Unknown")
        categories[cat] = categories.get(cat, 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(categories.items(), key=lambda x: -x[1]))
    print(f"[Phase 4] Classification complete in {elapsed:.1f}s — {summary}", flush=True)


SERVICE_BANNER_CATEGORIES = {
    "synology": "NAS", "qnap": "NAS", "dsm": "NAS",
    "unifi": "Network Equipment", "ubiquiti": "Network Equipment",
    "home assistant": "Smart Home", "homebridge": "Smart Home",
    "plex": "Media Server", "emby": "Media Server", "jellyfin": "Media Server",
    "pihole": "DNS/Ad Blocker", "adguard": "DNS/Ad Blocker",
    "proxmox": "Server", "esxi": "Server", "vmware": "Server",
    "cups": "Printer", "ipp": "Printer",
    "rtsp": "Camera", "ipcam": "Camera",
    "mosquitto": "IoT", "mqtt": "IoT",
}


def _classify_device(host: dict, vendor: str) -> str:
    """Determine device category from ports, vendor, and other signals."""
    open_ports = {p["port"] for p in host.get("ports", []) if p.get("state") == "open"}

    # Gateway detection
    if host.get("is_gateway"):
        return "Router/Gateway"

    # Port-based classification (check highest priority first)
    best_category = None
    best_priority = -1

    for port_set, category, priority in CATEGORY_RULES:
        if open_ports & port_set and priority > best_priority:
            best_category = category
            best_priority = priority

    # Vendor-based classification
    vendor_category = None
    if vendor:
        vendor_lower = vendor.lower()
        for key, cat in VENDOR_CATEGORIES.items():
            if key in vendor_lower:
                vendor_category = cat
                break

    # Combine signals
    if best_category and best_priority >= 8:
        return best_category  # High-confidence port match
    if vendor_category:
        if best_category and best_priority >= 5:
            return best_category  # Medium port match overrides vendor
        return vendor_category
    if best_category:
        return best_category

    # Heuristic fallbacks
    if open_ports & {22, 80, 443, 8080}:
        if len(open_ports) > 5:
            return "Server"
        return "Computer"

    # Banner/probe-based classification — check HTTP titles, server headers,
    # TLS certs, and service version strings for device identification
    banner_category = _classify_from_banners(host)
    if banner_category:
        return banner_category

    return "Unknown"


def _classify_from_banners(host: dict) -> str | None:
    """Check security_probes and service banners for device identification."""
    text_signals = []

    # Collect HTTP title and server header from probes
    probes = host.get("security_probes", {})
    http_probe = probes.get("http", {})
    if isinstance(http_probe, dict):
        if http_probe.get("title"):
            text_signals.append(http_probe["title"])
        if http_probe.get("server"):
            text_signals.append(http_probe["server"])

    # Collect TLS certificate CN/org
    tls_probe = probes.get("tls", {})
    if isinstance(tls_probe, dict):
        certs = tls_probe.get("certificates", [])
        if isinstance(certs, list):
            for cert in certs:
                if isinstance(cert, dict):
                    for field in ("subject", "issuer", "commonName"):
                        if cert.get(field):
                            text_signals.append(str(cert[field]))
        # Also check as a string/dict directly
        if isinstance(certs, str) and certs:
            text_signals.append(certs)

    # Collect service+version strings from port data
    for p in host.get("ports", []):
        if p.get("version"):
            text_signals.append(p["version"])
        if p.get("service"):
            text_signals.append(p["service"])
        if p.get("banner"):
            text_signals.append(p["banner"])

    # Match against known banner keywords
    combined = " ".join(text_signals).lower()
    for keyword, category in SERVICE_BANNER_CATEGORIES.items():
        if keyword in combined:
            return category

    return None
