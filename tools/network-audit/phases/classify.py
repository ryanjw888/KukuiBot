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
        print("[Phase 4] No hosts to classify")
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
    print(f"[Phase 4] Classification complete in {elapsed:.1f}s — {summary}")


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

    return "Unknown"
