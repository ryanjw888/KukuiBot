"""rustscan_parser.py — Parse RustScan output for open ports per host."""

import re


def parse_rustscan_output(output: str) -> dict[str, list[int]]:
    """Parse RustScan stdout. Returns {ip: [port1, port2, ...]}."""
    results: dict[str, list[int]] = {}

    for line in output.splitlines():
        line = line.strip()
        # RustScan outputs "Open <ip>:<port>" lines
        m = re.match(r"Open\s+(\d+\.\d+\.\d+\.\d+):(\d+)", line)
        if m:
            ip = m.group(1)
            port = int(m.group(2))
            results.setdefault(ip, []).append(port)
            continue

        # Also handle the summary format: "<ip> -> [port1, port2, ...]"
        m = re.match(r"(\d+\.\d+\.\d+\.\d+)\s*->\s*\[([^\]]*)\]", line)
        if m:
            ip = m.group(1)
            ports_str = m.group(2)
            ports = []
            for p in ports_str.split(","):
                p = p.strip()
                if p.isdigit():
                    ports.append(int(p))
            if ports:
                existing = results.get(ip, [])
                existing.extend(p for p in ports if p not in existing)
                results[ip] = existing

    return results
