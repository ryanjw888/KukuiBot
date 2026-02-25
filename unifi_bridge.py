"""
unifi_bridge.py — UniFi Dream Machine integration via REST API.

Setup: User enters their UDM host/IP and API key in Settings.
The API key is generated in the UDM UI under Settings → Admins → API Keys.

Config keys (stored in kukuibot.db via auth.get_config/set_config):
  unifi.host          — UDM IP or hostname (e.g. 10.0.6.1)
  unifi.api_key       — API key from UDM admin panel
  unifi.verify_ssl    — "1" to verify TLS cert, "0" to skip (default: 0)
  unifi.site          — UniFi site name (default: "default")

Capabilities:
  - Connection test
  - List connected clients (which AP, signal, IP, MAC)
  - List access points (name, MAC, model)
  - List firewall rules
  - Create/update/delete firewall rules
  - Track device location by AP association
"""

import logging
import urllib3

import httpx

from config import KUKUIBOT_HOME

logger = logging.getLogger("kukuibot.unifi")

# Suppress InsecureRequestWarning when verify_ssl is off
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Config helpers (DB-backed, same pattern as gmail_bridge)
# ---------------------------------------------------------------------------

def _get_config(key: str, default: str = "") -> str:
    from auth import get_config
    return get_config(key, default)


def _set_config(key: str, value: str):
    from auth import set_config
    set_config(key, value)


def save_credentials(host: str, api_key: str, verify_ssl: bool = False, site: str = "default"):
    """Save UniFi UDM credentials to the config store."""
    _set_config("unifi.host", host)
    _set_config("unifi.api_key", api_key)
    _set_config("unifi.verify_ssl", "1" if verify_ssl else "0")
    _set_config("unifi.site", site)
    logger.info(f"UniFi credentials saved for host={host}")


def clear_credentials():
    """Remove all UniFi credentials from the config store."""
    for key in ("unifi.host", "unifi.api_key", "unifi.verify_ssl", "unifi.site"):
        _set_config(key, "")
    logger.info("UniFi credentials cleared")


def get_credentials() -> dict:
    """Return current UniFi credentials (API key masked)."""
    host = _get_config("unifi.host", "")
    api_key = _get_config("unifi.api_key", "")
    verify_ssl = _get_config("unifi.verify_ssl", "0") == "1"
    site = _get_config("unifi.site", "default") or "default"
    return {
        "host": host,
        "has_api_key": bool(api_key),
        "verify_ssl": verify_ssl,
        "site": site,
    }


# ---------------------------------------------------------------------------
# API client (stateless — API key auth, no session management needed)
# ---------------------------------------------------------------------------

def _get_base_url() -> str:
    host = _get_config("unifi.host", "")
    if not host:
        raise ValueError("UniFi host not configured")
    if not host.startswith("http"):
        host = f"https://{host}"
    return host.rstrip("/")


def _get_headers() -> dict:
    api_key = _get_config("unifi.api_key", "")
    if not api_key:
        raise ValueError("UniFi API key not configured")
    return {"X-API-Key": api_key}


def _get_verify() -> bool:
    return _get_config("unifi.verify_ssl", "0") == "1"


def _get_site() -> str:
    return _get_config("unifi.site", "default") or "default"


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Make an authenticated request to the UDM API."""
    base_url = _get_base_url()
    headers = {**_get_headers(), **kwargs.pop("headers", {})}
    verify = _get_verify()
    with httpx.Client(verify=verify, timeout=15.0) as client:
        return client.request(method, f"{base_url}{path}", headers=headers, **kwargs)


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------

def get_status() -> dict:
    """Get UniFi connection status."""
    creds = get_credentials()
    connected = False
    error = ""
    udm_info = {}

    if creds["host"] and creds["has_api_key"]:
        try:
            site = _get_site()
            r = _request("GET", f"/proxy/network/api/s/{site}/stat/sysinfo")
            if r.status_code == 200:
                connected = True
                data = r.json()
                if isinstance(data, dict) and "data" in data and data["data"]:
                    info = data["data"][0]
                    udm_info = {
                        "version": info.get("version", ""),
                        "hostname": info.get("hostname", ""),
                    }
            else:
                error = f"API returned HTTP {r.status_code}"
        except Exception as e:
            error = str(e)
    else:
        error = "Not configured"

    return {
        "connected": connected,
        "host": creds["host"],
        "verify_ssl": creds["verify_ssl"],
        "site": creds["site"],
        "error": error,
        "udm_info": udm_info,
    }


def test_connection() -> dict:
    """Test the UniFi connection and return result."""
    try:
        site = _get_site()
        r = _request("GET", f"/proxy/network/api/s/{site}/stat/sysinfo")
        if r.status_code == 200:
            data = r.json()
            info = {}
            if isinstance(data, dict) and "data" in data and data["data"]:
                d = data["data"][0]
                info = {"version": d.get("version", ""), "hostname": d.get("hostname", "")}
            return {"ok": True, "info": info}
        return {"ok": False, "error": f"API returned HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_clients() -> list[dict]:
    """List all connected clients with AP association info."""
    site = _get_site()
    r = _request("GET", f"/proxy/network/api/s/{site}/stat/sta")
    r.raise_for_status()
    data = r.json()
    clients = []
    for c in data.get("data", []):
        clients.append({
            "mac": c.get("mac", ""),
            "ip": c.get("ip", ""),
            "hostname": c.get("hostname", c.get("name", "")),
            "oui": c.get("oui", ""),
            "ap_mac": c.get("ap_mac", ""),
            "essid": c.get("essid", ""),
            "channel": c.get("channel"),
            "signal": c.get("signal"),
            "rssi": c.get("rssi"),
            "tx_rate": c.get("tx_rate"),
            "rx_rate": c.get("rx_rate"),
            "uptime": c.get("uptime"),
            "is_wired": c.get("is_wired", False),
        })
    return clients


def list_access_points() -> list[dict]:
    """List all UniFi access points with name, MAC, model."""
    site = _get_site()
    r = _request("GET", f"/proxy/network/api/s/{site}/stat/device")
    r.raise_for_status()
    data = r.json()
    aps = []
    for d in data.get("data", []):
        if d.get("type") in ("uap", "udm"):
            aps.append({
                "mac": d.get("mac", ""),
                "name": d.get("name", d.get("hostname", "")),
                "model": d.get("model", ""),
                "type": d.get("type", ""),
                "ip": d.get("ip", ""),
                "version": d.get("version", ""),
                "uptime": d.get("uptime"),
                "num_sta": d.get("num_sta", 0),
                "state": d.get("state", 0),
            })
    return aps


def list_firewall_rules() -> list[dict]:
    """List all firewall rules."""
    site = _get_site()
    r = _request("GET", f"/proxy/network/api/s/{site}/rest/firewallrule")
    r.raise_for_status()
    data = r.json()
    rules = []
    for rule in data.get("data", []):
        rules.append({
            "_id": rule.get("_id", ""),
            "name": rule.get("name", ""),
            "enabled": rule.get("enabled", False),
            "action": rule.get("action", ""),
            "protocol": rule.get("protocol", ""),
            "ruleset": rule.get("ruleset", ""),
            "rule_index": rule.get("rule_index"),
            "src_address": rule.get("src_address", ""),
            "src_networkconf_id": rule.get("src_networkconf_id", ""),
            "dst_address": rule.get("dst_address", ""),
            "dst_networkconf_id": rule.get("dst_networkconf_id", ""),
            "dst_port": rule.get("dst_port", ""),
            "protocol_match_excepted": rule.get("protocol_match_excepted", False),
        })
    return rules


def create_firewall_rule(rule: dict) -> dict:
    """Create a new firewall rule. Returns the created rule."""
    site = _get_site()
    r = _request("POST", f"/proxy/network/api/s/{site}/rest/firewallrule", json=rule)
    r.raise_for_status()
    data = r.json()
    return data.get("data", [{}])[0] if data.get("data") else {}


def update_firewall_rule(rule_id: str, rule: dict) -> dict:
    """Update an existing firewall rule."""
    site = _get_site()
    r = _request("PUT", f"/proxy/network/api/s/{site}/rest/firewallrule/{rule_id}", json=rule)
    r.raise_for_status()
    data = r.json()
    return data.get("data", [{}])[0] if data.get("data") else {}


def delete_firewall_rule(rule_id: str) -> bool:
    """Delete a firewall rule by ID."""
    site = _get_site()
    r = _request("DELETE", f"/proxy/network/api/s/{site}/rest/firewallrule/{rule_id}")
    r.raise_for_status()
    return True


def get_client_ap_location(mac: str) -> dict | None:
    """Given a client MAC, return which AP it's connected to with room mapping."""
    clients = list_clients()
    aps = list_access_points()
    ap_map = {ap["mac"]: ap for ap in aps}

    for client in clients:
        if client["mac"].lower() == mac.lower():
            ap_mac = client.get("ap_mac", "")
            ap_info = ap_map.get(ap_mac, {})
            return {
                "client_mac": client["mac"],
                "client_ip": client["ip"],
                "client_hostname": client["hostname"],
                "ap_mac": ap_mac,
                "ap_name": ap_info.get("name", "Unknown AP"),
                "essid": client.get("essid", ""),
                "signal": client.get("signal"),
                "rssi": client.get("rssi"),
                "is_wired": client.get("is_wired", False),
            }
    return None
