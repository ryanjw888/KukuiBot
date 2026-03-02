"""analyzer.py — Automated security analysis of scan results.

Extracts findings, positive practices, and priority actions from
scan_results.json data. Produces an analysis dict compatible with
the report generator (generator.py).

This replaces the manual step of the AI writing analysis.json.
The AI can still review/override the output if needed.
"""

import re
from collections import Counter
from datetime import datetime


# ── Detection rules ──────────────────────────────────────────────────

def analyze(scan_data: dict, client_name: str = "") -> dict:
    """Analyze scan results and return a structured analysis dict.

    Args:
        scan_data: Parsed scan_results.json dict
        client_name: Client name for the report (optional)

    Returns:
        Analysis dict with executive_summary, key_findings,
        positive_practices, priority_actions, and grades.
    """
    meta = scan_data.get("audit_meta", {})
    hosts = scan_data.get("hosts", [])
    client = client_name or meta.get("client_name", "Network")
    subnet = meta.get("subnet", "")
    date_str = meta.get("date", datetime.now().strftime("%Y-%m-%d"))

    hosts_with_ports = [h for h in hosts if h.get("ports")]
    total_ports = sum(len(h.get("ports", [])) for h in hosts_with_ports)

    findings = []
    positives = []
    finding_id = 0

    # ── Run all detectors ────────────────────────────────────────
    for h in hosts:
        ip = h.get("ip", "")
        hostname = h.get("hostname", "")
        vendor = h.get("vendor", "")
        category = h.get("category", "")
        ports = h.get("ports", [])
        probes = h.get("security_probes", {})

        host_label = f"{ip}"
        if hostname:
            host_label = f"{ip} ({hostname})"

        for p in ports:
            port_num = p.get("port", 0)
            service = p.get("service", "")
            version = p.get("version", "")
            scripts = p.get("scripts", {})

            # ── TELNET (unencrypted) ─────────────────────────────
            if service == "telnet" or port_num == 23:
                finding_id += 1
                findings.append({
                    "id": f"F-{finding_id:03d}",
                    "severity": "high",
                    "title": f"Unencrypted Telnet on {vendor or 'Device'}",
                    "description": (
                        f"The device at {ip} exposes Telnet on port {port_num} "
                        f"with no encryption. Credentials and commands are "
                        f"transmitted in plaintext."
                    ),
                    "affected_device": f"{host_label} ({vendor})" if vendor else host_label,
                    "port": str(port_num),
                    "category": "Encryption",
                    "vendor": vendor,
                    "evidence": f"Port {port_num}/tcp open, service: telnet. No SSH alternative detected.",
                    "business_impact": (
                        "Credentials could be intercepted by anyone on the network. "
                        "An attacker could take control of the device."
                    ),
                    "remediation": (
                        "Isolate the device on a dedicated IoT VLAN with strict firewall rules. "
                        "Check for firmware updates that replace Telnet with SSH."
                    ),
                    "cve": "N/A",
                })

            # ── LEGACY TLS (1.0 / 1.1) ──────────────────────────
            cipher_info = scripts.get("ssl-enum-ciphers", "")
            if cipher_info and ("TLSv1.0" in cipher_info or "TLSv1.1" in cipher_info):
                finding_id += 1
                has_10 = "TLSv1.0" in cipher_info
                has_11 = "TLSv1.1" in cipher_info
                versions = []
                if has_10:
                    versions.append("1.0")
                if has_11:
                    versions.append("1.1")
                ver_str = " and ".join(versions)
                findings.append({
                    "id": f"F-{finding_id:03d}",
                    "severity": "high",
                    "title": f"Legacy TLS {ver_str} on {vendor or 'Device'}",
                    "description": (
                        f"The device at {ip}:{port_num} accepts TLS {ver_str} connections. "
                        f"These deprecated protocol versions are vulnerable to BEAST, "
                        f"POODLE, and other downgrade attacks."
                    ),
                    "affected_device": f"{host_label} ({vendor})" if vendor else host_label,
                    "port": str(port_num),
                    "category": "Encryption",
                    "vendor": vendor,
                    "evidence": f"TLS {ver_str} cipher suites accepted on port {port_num}.",
                    "business_impact": (
                        "An attacker on the local network could intercept or modify "
                        "communications using known TLS downgrade attacks."
                    ),
                    "remediation": (
                        "Update firmware to disable TLS 1.0/1.1. If unavailable, "
                        "isolate the device on a dedicated VLAN with restricted access."
                    ),
                    "cve": "N/A",
                    "_merge_key": "legacy-tls",
                })

            # ── RPCBIND EXPOSED ──────────────────────────────────
            if service == "rpcbind" or (port_num == 111 and "rpcinfo" in scripts):
                finding_id += 1
                findings.append({
                    "id": f"F-{finding_id:03d}",
                    "severity": "high",
                    "title": f"rpcbind Exposed on {vendor or 'Device'}",
                    "description": (
                        f"The device at {ip} exposes rpcbind on port 111. "
                        f"This service is often unnecessary and expands the attack surface. "
                        f"rpcbind has historically been a target for remote exploits."
                    ),
                    "affected_device": f"{host_label} ({vendor})" if vendor else host_label,
                    "port": "111",
                    "category": "Exposure",
                    "vendor": vendor,
                    "evidence": scripts.get("rpcinfo", f"Port 111/tcp open, rpcbind service detected."),
                    "business_impact": (
                        "rpcbind can be used to enumerate services and has been the "
                        "target of multiple CVEs."
                    ),
                    "remediation": (
                        "Disable rpcbind if not needed. Use firewall rules to block "
                        "port 111 from non-management networks."
                    ),
                    "cve": "N/A",
                    "_merge_key": "rpcbind-exposed",
                })

            # ── UNAUTHENTICATED SOCKS PROXY ──────────────────────
            if "socks" in service.lower():
                auth_info = scripts.get("socks-auth-info", "")
                if "No authentication" in auth_info:
                    finding_id += 1
                    findings.append({
                        "id": f"F-{finding_id:03d}",
                        "severity": "medium",
                        "title": f"Unauthenticated SOCKS Proxy on {vendor or 'Device'}",
                        "description": (
                            f"The device at {ip} exposes a SOCKS proxy on port {port_num} "
                            f"with no authentication."
                        ),
                        "affected_device": f"{host_label} ({vendor})" if vendor else host_label,
                        "port": str(port_num),
                        "category": "Exposure",
                        "vendor": vendor,
                        "evidence": f"Port {port_num}/tcp: {service}. Auth: {auth_info.strip()}",
                        "business_impact": (
                            "An unauthenticated proxy could be used for network pivoting "
                            "or bypassing firewall rules."
                        ),
                        "remediation": (
                            "Ensure the device is on an IoT VLAN with restricted "
                            "inter-VLAN routing."
                        ),
                        "cve": "N/A",
                    })

            # ── UNAUTHENTICATED HTTP ON IOT ──────────────────────
            # Skip devices that use physical button auth (e.g., Philips Hue)
            _is_hue = "Philips" in vendor or "Signify" in vendor or "hue" in hostname.lower()
            if (service in ("http", "http-alt") and
                    category in ("IoT", "Smart Home") and
                    port_num == 80 and not _is_hue):
                http_title = scripts.get("http-title", "")
                http_probe = probes.get("http", {})
                auth_required = http_probe.get("auth_required", True)
                if not auth_required and version:
                    finding_id += 1
                    findings.append({
                        "id": f"F-{finding_id:03d}",
                        "severity": "medium",
                        "title": f"Unauthenticated IoT Device ({http_title.split(chr(10))[0].strip() or vendor})",
                        "description": (
                            f"The IoT device at {ip} runs {version} on port {port_num} "
                            f"with no authentication required."
                        ),
                        "affected_device": f"{host_label} ({vendor})" if vendor else host_label,
                        "port": str(port_num),
                        "category": "Auth",
                        "vendor": vendor,
                        "evidence": (
                            f"HTTP port {port_num} open, {version}, no auth challenge. "
                            f"Title: {http_title.split(chr(10))[0].strip()}"
                        ),
                        "business_impact": (
                            "Anyone on the network can control this device without "
                            "authentication."
                        ),
                        "remediation": (
                            "Enable authentication via the device's web interface. "
                            "Update firmware and place on a dedicated IoT VLAN."
                        ),
                        "cve": "N/A",
                        "_merge_key": "unauth-iot",
                    })

            # ── CBC CIPHER SUITES ────────────────────────────────
            if cipher_info and "CBC" in cipher_info:
                # Only flag if not already flagged for legacy TLS on same port
                already_flagged = any(
                    f.get("affected_device", "").startswith(ip) and
                    f.get("port") == str(port_num) and
                    "Legacy TLS" in f.get("title", "")
                    for f in findings
                )
                if not already_flagged:
                    # Only flag on non-standard ports or if it's notable
                    if port_num not in (443, 80):
                        finding_id += 1
                        findings.append({
                            "id": f"F-{finding_id:03d}",
                            "severity": "medium" if port_num < 10000 else "low",
                            "title": f"CBC Cipher Suites on {vendor or 'Device'} (port {port_num})",
                            "description": (
                                f"The device at {ip} offers CBC cipher suites on port {port_num}. "
                                f"CBC mode ciphers are susceptible to padding oracle attacks."
                            ),
                            "affected_device": f"{host_label} ({vendor})" if vendor else host_label,
                            "port": str(port_num),
                            "category": "Encryption",
                            "vendor": vendor,
                            "evidence": f"CBC cipher suites detected on port {port_num}.",
                            "business_impact": (
                                "Theoretical vulnerability to padding oracle attacks "
                                "(e.g., Lucky 13)."
                            ),
                            "remediation": (
                                "Update to use GCM or ChaCha20 cipher suites only."
                            ),
                            "cve": "N/A",
                            "_merge_key": "cbc-ciphers",
                        })

            # ── WEAK SSH ALGORITHMS ──────────────────────────────
            ssh_algos = scripts.get("ssh2-enum-algos", "")
            if ssh_algos:
                kex_section = ""
                if "kex_algorithms:" in ssh_algos:
                    kex_section = ssh_algos.split("kex_algorithms:")[1].split("server_host_key")[0]

                has_sha1_kex = any(
                    weak in kex_section
                    for weak in ["diffie-hellman-group14-sha1",
                                 "diffie-hellman-group1-sha1",
                                 "diffie-hellman-group-exchange-sha1"]
                )
                if has_sha1_kex:
                    # Collect and deduplicate later
                    finding_id += 1
                    findings.append({
                        "id": f"F-{finding_id:03d}",
                        "severity": "medium",
                        "title": f"Legacy SHA-1 SSH Key Exchange",
                        "description": (
                            f"The device at {ip} offers SHA-1 based key exchange algorithms. "
                            f"SHA-1 is deprecated and allows potential downgrade attacks."
                        ),
                        "affected_device": f"{host_label} ({vendor})" if vendor else host_label,
                        "port": "22",
                        "category": "Encryption",
                        "vendor": vendor,
                        "evidence": "SSH kex_algorithms includes diffie-hellman-group14-sha1.",
                        "business_impact": (
                            "Low immediate risk since stronger algorithms are preferred, "
                            "but SHA-1 is deprecated."
                        ),
                        "remediation": (
                            "Update device firmware to remove SHA-1 key exchange algorithms."
                        ),
                        "cve": "N/A",
                        "_merge_key": "weak-ssh-kex",
                    })

    # ── Merge similar findings ───────────────────────────────────
    findings = _merge_similar_findings(findings)

    # ── Detect positive practices ────────────────────────────────
    positives = _detect_positives(hosts)

    # ── Build priority actions from findings ─────────────────────
    actions = _build_actions(findings)

    # ── Grade the network ────────────────────────────────────────
    grade, grade_explanation = _compute_grade(findings, positives, len(hosts_with_ports), total_ports)

    # ── Build executive summary ──────────────────────────────────
    summary = _build_summary(
        client, subnet, date_str, hosts, hosts_with_ports,
        total_ports, findings, positives, grade
    )

    return {
        "executive_summary": summary,
        "overall_grade": grade,
        "overall_grade_explanation": grade_explanation,
        "key_findings": findings,
        "positive_practices": positives,
        "priority_actions": actions,
    }


def _merge_similar_findings(findings: list) -> list:
    """Merge findings with the same _merge_key into consolidated entries."""
    merged = []
    merge_groups: dict[str, list] = {}

    for f in findings:
        key = f.pop("_merge_key", None)
        if key:
            merge_groups.setdefault(key, []).append(f)
        else:
            merged.append(f)

    for key, group in merge_groups.items():
        if len(group) == 1:
            merged.append(group[0])
            continue

        # Consolidate: keep first finding's structure, merge affected devices
        base = group[0].copy()
        all_ips = []
        all_ports = set()
        all_vendors = set()
        for f in group:
            ip_match = re.match(r"([\d.]+)", f.get("affected_device", ""))
            if ip_match:
                all_ips.append(ip_match.group(1))
            all_ports.add(f.get("port", ""))
            v = f.get("vendor", "")
            if v:
                all_vendors.add(v)

        ip_list = ", ".join(all_ips[:6])
        if len(all_ips) > 6:
            ip_list += f" and {len(all_ips) - 6} more"
        vendor_str = ", ".join(all_vendors) if all_vendors else "various devices"

        base["affected_device"] = ip_list
        port_str = ", ".join(sorted(all_ports - {""}))
        if port_str:
            base["port"] = port_str

        # Rewrite description and evidence generically
        original_title = base.get("title", "")
        base["title"] = re.sub(
            r" on .+$", f" on {len(group)} Devices",
            original_title,
        ) if " on " in original_title else f"{original_title} ({len(group)} devices)"

        base["description"] = (
            f"{len(group)} devices are affected: {ip_list}. "
            f"{group[0].get('description', '').split('.')[0]}."
        )
        base["evidence"] = (
            f"Detected on {len(group)} devices ({vendor_str}): {ip_list}"
        )
        base["remediation"] = (
            f"{group[0].get('remediation', '')} "
            f"Applies to all {len(group)} affected devices."
        ).strip()

        merged.append(base)

    # Re-number findings by severity
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    merged.sort(key=lambda f: sev_order.get(f.get("severity", "info").lower(), 5))
    for i, f in enumerate(merged, 1):
        f["id"] = f"F-{i:03d}"

    return merged


def _detect_positives(hosts: list) -> list:
    """Detect positive security practices from scan data."""
    positives = []
    pq_hosts = []
    grade_a_tls_hosts = []
    redirect_hosts = []
    tls13_only = []

    for h in hosts:
        ip = h.get("ip", "")
        vendor = h.get("vendor", "")
        hostname = h.get("hostname", "")

        for p in h.get("ports", []):
            scripts = p.get("scripts", {})

            # Post-quantum SSH
            ssh_algos = scripts.get("ssh2-enum-algos", "")
            if "sntrup761" in ssh_algos or "mlkem768" in ssh_algos:
                pq_hosts.append(f"{ip} ({hostname or vendor})")

            # Grade-A TLS
            cipher_info = scripts.get("ssl-enum-ciphers", "")
            if "least strength: A" in cipher_info and p.get("port") in (443, 8443):
                grade_a_tls_hosts.append(ip)

            # TLS 1.3-only
            if cipher_info and "TLSv1.3" in cipher_info:
                if "TLSv1.2" not in cipher_info and "TLSv1.1" not in cipher_info and "TLSv1.0" not in cipher_info:
                    tls13_only.append(f"{ip} ({vendor})")

            # HTTP→HTTPS redirects
            http_title = scripts.get("http-title", "")
            if "redirect to https" in http_title.lower():
                redirect_hosts.append(ip)

            # Let's Encrypt cert
            ssl_cert = scripts.get("ssl-cert", "")
            if "Let's Encrypt" in ssl_cert:
                if not any("Let's Encrypt" in pp.get("title", "") for pp in positives):
                    # Extract expiry
                    expiry_match = re.search(r"Not valid after:\s*(\S+)", ssl_cert)
                    expiry = expiry_match.group(1) if expiry_match else "unknown"
                    positives.append({
                        "title": "Valid Let's Encrypt Certificate",
                        "description": (
                            f"The network uses a properly issued Let's Encrypt certificate "
                            f"with automated renewal (valid through {expiry})."
                        ),
                    })

    # Add aggregated positives
    if pq_hosts:
        unique_pq = list(dict.fromkeys(pq_hosts))  # dedupe preserving order
        positives.append({
            "title": "Post-Quantum SSH Algorithms",
            "description": (
                f"{len(unique_pq)} device(s) support ML-KEM (Kyber) and/or sntrup761 "
                f"post-quantum key exchange, providing forward security against "
                f"quantum computing threats."
            ),
        })

    unique_tls_a = set(grade_a_tls_hosts)
    if len(unique_tls_a) >= 3:
        positives.append({
            "title": "Grade-A TLS Across Infrastructure",
            "description": (
                f"{len(unique_tls_a)} hosts achieve TLS Grade A on their primary "
                f"HTTPS ports with strong cipher suites."
            ),
        })

    if tls13_only:
        unique_13 = list(dict.fromkeys(tls13_only))
        positives.append({
            "title": "TLS 1.3-Only Configuration",
            "description": (
                f"{len(unique_13)} device(s) only accept TLS 1.3 connections with "
                f"no legacy protocol fallback — the gold standard for transport encryption."
            ),
        })

    unique_redirects = set(redirect_hosts)
    if len(unique_redirects) >= 3:
        positives.append({
            "title": "HTTP-to-HTTPS Redirects Enforced",
            "description": (
                f"{len(unique_redirects)} hosts properly redirect HTTP to HTTPS, "
                f"preventing accidental plaintext browsing of management interfaces."
            ),
        })

    return positives


def _build_actions(findings: list) -> list:
    """Build a prioritized action plan from findings."""
    actions = []
    sev_to_urgency = {
        "critical": "immediate",
        "high": "short-term",
        "medium": "medium-term",
        "low": "long-term",
        "info": "long-term",
    }

    for i, f in enumerate(findings):
        sev = f.get("severity", "info").lower()
        if sev == "info":
            continue  # Skip info-level from action plan

        affected = f.get("affected_device", "")
        actions.append({
            "priority": len(actions) + 1,
            "urgency": sev_to_urgency.get(sev, "medium-term"),
            "action": f.get("remediation", f.get("title", "")),
            "effort": _estimate_effort(f),
            "impact": "high" if sev in ("critical", "high") else "medium",
            "affected": affected,
        })

    return actions


def _estimate_effort(finding: dict) -> str:
    """Estimate remediation effort from finding details."""
    remediation = finding.get("remediation", "").lower()
    if any(w in remediation for w in ["firmware update", "update firmware"]):
        return "medium"
    if any(w in remediation for w in ["isolate", "vlan", "firewall"]):
        return "low"
    if any(w in remediation for w in ["disable", "enable auth"]):
        return "low"
    return "medium"


def _compute_grade(findings: list, positives: list,
                   hosts_count: int, ports_count: int) -> tuple[str, str]:
    """Compute a letter grade based on findings and positives."""
    # Start at A+, deduct for findings (post-merge, so each finding
    # represents a unique issue, not a per-host duplicate)
    score = 100

    for f in findings:
        sev = f.get("severity", "info").lower()
        if sev == "critical":
            score -= 15
        elif sev == "high":
            score -= 7
        elif sev == "medium":
            score -= 3
        elif sev == "low":
            score -= 1

    # Bonus for positives (max +12)
    score += min(len(positives) * 2, 12)

    # Clamp
    score = max(0, min(100, score))

    # Map to grade
    if score >= 97:
        grade = "A+"
    elif score >= 93:
        grade = "A"
    elif score >= 90:
        grade = "A-"
    elif score >= 87:
        grade = "B+"
    elif score >= 83:
        grade = "B"
    elif score >= 80:
        grade = "B-"
    elif score >= 77:
        grade = "C+"
    elif score >= 73:
        grade = "C"
    elif score >= 70:
        grade = "C-"
    elif score >= 67:
        grade = "D+"
    elif score >= 63:
        grade = "D"
    elif score >= 60:
        grade = "D-"
    else:
        grade = "F"

    # Build explanation
    sev_counts = Counter(f.get("severity", "info").lower() for f in findings)
    parts = []
    for sev in ["critical", "high", "medium", "low"]:
        c = sev_counts.get(sev, 0)
        if c:
            parts.append(f"{c} {sev}")

    finding_summary = ", ".join(parts) if parts else "no significant"
    explanation = (
        f"The network earns a {grade} grade with {finding_summary} findings "
        f"and {len(positives)} positive security practices identified across "
        f"{hosts_count} hosts with {ports_count} open ports."
    )

    return grade, explanation


def _build_summary(client: str, subnet: str, date_str: str,
                   all_hosts: list, hosts_with_ports: list,
                   total_ports: int, findings: list, positives: list,
                   grade: str) -> str:
    """Build an executive summary from analysis results."""
    sev_counts = Counter(f.get("severity", "info").lower() for f in findings)

    # Format date
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_display = date_obj.strftime("%B %-d, %Y")
    except (ValueError, Exception):
        date_display = date_str

    # Identify top risks
    high_findings = [f for f in findings if f.get("severity", "").lower() in ("critical", "high")]
    risk_descriptions = []
    for f in high_findings[:3]:
        risk_descriptions.append(f.get("title", ""))

    risks_text = ""
    if risk_descriptions:
        risk_list = ", ".join(risk_descriptions[:-1])
        if len(risk_descriptions) > 1:
            risk_list += f", and {risk_descriptions[-1]}"
        else:
            risk_list = risk_descriptions[0]
        risks_text = (
            f"The most significant risks include {risk_list}. "
            f"These should be addressed as a priority."
        )

    # Positive summary
    positive_highlights = [p.get("title", "") for p in positives[:3]]
    positive_text = ""
    if positive_highlights:
        positive_text = (
            f"On the positive side, the network demonstrates strong security fundamentals "
            f"including {', '.join(positive_highlights[:2])}"
            + (f", and {positive_highlights[2]}" if len(positive_highlights) > 2 else "")
            + "."
        )

    summary = (
        f"A comprehensive security audit of the {client} network ({subnet}) was conducted "
        f"on {date_display}. Of {len(all_hosts)} live hosts discovered, "
        f"{len(hosts_with_ports)} presented open ports totaling {total_ports} services. "
        f"The overall security grade is {grade}.\n"
        f"{risks_text}\n"
        f"{positive_text}"
    )

    return summary.strip()
