"""nmap_parser.py — Parse nmap XML output into structured dicts."""

import xml.etree.ElementTree as ET
from pathlib import Path


def parse_nmap_xml(xml_path: str | Path) -> list[dict]:
    """Parse nmap XML output file, return list of host dicts."""
    try:
        tree = ET.parse(str(xml_path))
    except (ET.ParseError, FileNotFoundError) as e:
        return []

    root = tree.getroot()
    hosts = []

    for host_el in root.findall("host"):
        if host_el.find("status") is not None:
            state = host_el.find("status").get("state", "")
            if state != "up":
                continue

        host = {
            "ip": "",
            "mac": "",
            "vendor": "",
            "hostname": "",
            "hostnames": [],
            "ports": [],
            "security_probes": {},
        }

        # Addresses
        for addr in host_el.findall("address"):
            atype = addr.get("addrtype", "")
            if atype == "ipv4":
                host["ip"] = addr.get("addr", "")
            elif atype == "mac":
                host["mac"] = addr.get("addr", "")
                host["vendor"] = addr.get("vendor", "")

        # Hostnames
        hostnames_el = host_el.find("hostnames")
        if hostnames_el is not None:
            for hn in hostnames_el.findall("hostname"):
                name = hn.get("name", "")
                if name:
                    host["hostnames"].append(name)
            if host["hostnames"]:
                host["hostname"] = host["hostnames"][0]

        # Ports
        ports_el = host_el.find("ports")
        if ports_el is not None:
            for port_el in ports_el.findall("port"):
                state_el = port_el.find("state")
                if state_el is None:
                    continue
                port_state = state_el.get("state", "")
                if port_state not in ("open", "open|filtered"):
                    continue

                port_info = {
                    "port": int(port_el.get("portid", "0")),
                    "protocol": port_el.get("protocol", "tcp"),
                    "state": port_state,
                    "service": "",
                    "version": "",
                    "banner": "",
                    "scripts": {},
                }

                service_el = port_el.find("service")
                if service_el is not None:
                    port_info["service"] = service_el.get("name", "")
                    product = service_el.get("product", "")
                    version = service_el.get("version", "")
                    extra = service_el.get("extrainfo", "")
                    version_parts = [p for p in (product, version, extra) if p]
                    port_info["version"] = " ".join(version_parts)

                # Scripts
                for script_el in port_el.findall("script"):
                    script_id = script_el.get("id", "")
                    script_output = script_el.get("output", "")
                    if script_id:
                        port_info["scripts"][script_id] = script_output

                host["ports"].append(port_info)

        # Host scripts
        hostscript_el = host_el.find("hostscript")
        if hostscript_el is not None:
            for script_el in hostscript_el.findall("script"):
                script_id = script_el.get("id", "")
                script_output = script_el.get("output", "")
                if script_id:
                    _merge_script_to_probes(host, script_id, script_output)

        # Extract security probes from port scripts
        _extract_probes_from_ports(host)

        if host["ip"]:
            hosts.append(host)

    return hosts


def _merge_script_to_probes(host: dict, script_id: str, output: str):
    probes = host.setdefault("security_probes", {})
    if "ssh" in script_id:
        ssh = probes.setdefault("ssh", {"algorithms": {}, "host_key_types": []})
        if "enum-algos" in script_id:
            ssh["algorithms"] = _parse_ssh_algos(output)
        elif "hostkey" in script_id:
            ssh["host_key_types"] = _parse_ssh_hostkeys(output)
    elif "ssl" in script_id or "tls" in script_id:
        tls = probes.setdefault("tls", {"certificates": [], "protocols": [], "ciphers": []})
        if "ssl-cert" in script_id:
            tls["certificates"].append(output.strip())
        elif "ssl-enum-ciphers" in script_id:
            tls["ciphers"].append(output.strip())
    elif "smb" in script_id:
        smb = probes.setdefault("smb", {"signing": "", "version": "", "shares": []})
        if "security-mode" in script_id:
            smb["signing"] = output.strip()
        elif "enum-shares" in script_id:
            smb["shares"].append(output.strip())
    elif "afp" in script_id:
        afp = probes.setdefault("afp", {"version": "", "info": ""})
        afp["info"] = output.strip()
    elif "http" in script_id:
        http = probes.setdefault("http", {
            "title": "", "server": "", "auth_required": False, "redirects_to": "",
        })
        if "http-title" in script_id:
            http["title"] = output.strip()
        elif "http-server-header" in script_id:
            http["server"] = output.strip()
        elif "http-auth" in script_id:
            http["auth_required"] = True


def _extract_probes_from_ports(host: dict):
    probes = host.setdefault("security_probes", {})
    for port in host.get("ports", []):
        for script_id, output in port.get("scripts", {}).items():
            _merge_script_to_probes(host, script_id, output)


def _parse_ssh_algos(output: str) -> dict:
    algos = {}
    current_section = ""
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith(":"):
            current_section = line.rstrip(":").strip()
            algos[current_section] = []
        elif current_section:
            algos[current_section].append(line)
    return algos


def _parse_ssh_hostkeys(output: str) -> list[str]:
    keys = []
    for line in output.splitlines():
        line = line.strip()
        if line and not line.startswith("("):
            parts = line.split()
            if len(parts) >= 2:
                keys.append(parts[-1] if "(" in parts[-1] else parts[0])
    return keys


def parse_nmap_xml_string(xml_string: str) -> list[dict]:
    """Parse nmap XML from a string."""
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return []

    # Write to temp and reuse file parser — simpler than duplicating logic
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
        f.write(xml_string)
        tmp_path = f.name
    try:
        return parse_nmap_xml(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
