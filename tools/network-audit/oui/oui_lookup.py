"""oui_lookup.py — MAC address to vendor lookup using IEEE OUI database."""

import re
import time
import urllib.request
from pathlib import Path

from ..config import CACHE_DIR, OUI_MAX_AGE_DAYS

OUI_URL = "https://standards-oui.ieee.org/oui/oui.txt"
OUI_CACHE_PATH = CACHE_DIR / "oui.txt"

_oui_db: dict[str, str] | None = None


def _needs_refresh() -> bool:
    if not OUI_CACHE_PATH.exists():
        return True
    age_days = (time.time() - OUI_CACHE_PATH.stat().st_mtime) / 86400
    return age_days > OUI_MAX_AGE_DAYS


def _download_oui():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(OUI_URL, headers={"User-Agent": "KukuiBot-Audit/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        OUI_CACHE_PATH.write_bytes(data)
    except Exception:
        pass


def _load_oui_db() -> dict[str, str]:
    global _oui_db
    if _oui_db is not None:
        return _oui_db

    if _needs_refresh():
        _download_oui()

    db = {}
    if OUI_CACHE_PATH.exists():
        try:
            text = OUI_CACHE_PATH.read_text(errors="replace")
            # Format: "AA-BB-CC   (hex)\t\tVendor Name"
            for line in text.splitlines():
                if "(hex)" in line:
                    m = re.match(r"([0-9A-F]{2}-[0-9A-F]{2}-[0-9A-F]{2})\s+\(hex\)\s+(.*)", line)
                    if m:
                        prefix = m.group(1).replace("-", ":").upper()
                        vendor = m.group(2).strip()
                        db[prefix] = vendor
        except Exception:
            pass

    _oui_db = db
    return db


def get_vendor(mac: str) -> str:
    """Look up vendor from MAC address. Returns 'Unknown' if not found."""
    if not mac:
        return "Unknown"
    # Normalize MAC to XX:XX:XX format (first 3 octets)
    clean = mac.upper().replace("-", ":").replace(".", ":")
    # Handle formats like AABB.CCDD.EEFF
    if len(clean.replace(":", "")) >= 6:
        hex_only = re.sub(r"[^0-9A-F]", "", clean)
        if len(hex_only) >= 6:
            prefix = f"{hex_only[0:2]}:{hex_only[2:4]}:{hex_only[4:6]}"
            db = _load_oui_db()
            return db.get(prefix, "Unknown")
    return "Unknown"


def preload():
    """Pre-download and parse the OUI database."""
    _load_oui_db()
