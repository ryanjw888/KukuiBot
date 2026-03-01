"""Phase 5 — Vulnerability Assessment: Nuclei CVE templates + version checks."""

import time
from datetime import datetime

from ..audit_log import AuditLog
from ..config import AuditConfig
from ..executor import run_command
from ..parsers.nuclei_parser import parse_nuclei_jsonl


async def run_vulns(config: AuditConfig, audit_log: AuditLog) -> None:
    """Run vulnerability assessment with Nuclei CVE templates."""
    start = time.monotonic()
    start_time = datetime.now().isoformat()
    errors = []
    commands_run = []

    live_hosts = audit_log.get_live_hosts()
    has_nuclei = config.tools.get("nuclei") and config.tools["nuclei"].available

    if not live_hosts:
        print("[Phase 5] No hosts for vulnerability assessment")
        audit_log.log_phase(
            phase=5, name="Vulnerability Assessment",
            start_time=start_time, end_time=datetime.now().isoformat(),
            duration=0, status="skipped",
        )
        audit_log.save()
        return

    # Build target list — only hosts with open HTTP-ish ports
    http_ports = {80, 443, 8080, 8443, 8000, 8888, 3000, 5000}
    targets_urls = []

    for host in live_hosts:
        ip = host["ip"]
        open_ports = {p["port"] for p in host.get("ports", []) if p.get("state") == "open"}
        host_http = open_ports & http_ports

        if host_http:
            for port in sorted(host_http):
                scheme = "https" if port in (443, 8443) else "http"
                targets_urls.append(f"{scheme}://{ip}:{port}")
        else:
            # Still add the IP for non-HTTP nuclei templates
            targets_urls.append(ip)

    if has_nuclei and targets_urls:
        print(f"[Phase 5] Running Nuclei CVE scan on {len(targets_urls)} targets...")

        targets_file = config.output_dir / "nuclei_cve_targets.txt"
        targets_file.write_text("\n".join(targets_urls))

        output_file = config.output_dir / "nuclei_cve_results.jsonl"

        nuclei_cmd = [
            "nuclei",
            "-l", str(targets_file),
            "-t", "cves",
            "-severity", "critical,high",
            "-jsonl",
            "-o", str(output_file),
            "-silent",
            "-timeout", "10",
            "-retries", "1",
            "-rate-limit", "50",
        ]

        result = await run_command(nuclei_cmd, timeout=600)
        commands_run.append({
            "cmd": result.command,
            "exit_code": result.exit_code,
            "duration": round(result.duration, 2),
        })

        if result.exit_code != 0 and not result.timed_out:
            errors.append(f"Nuclei CVE scan error: {result.stderr[:300]}")

        if output_file.exists():
            findings = parse_nuclei_jsonl(output_file)
            for f in findings:
                audit_log.add_nuclei_finding(f)
                host_ip = f.get("host", "")
                if host_ip:
                    audit_log.add_host({
                        "ip": host_ip,
                        "vulnerabilities": [{
                            "id": f.get("template_id", ""),
                            "source": "nuclei",
                            "severity": f.get("severity", "info"),
                            "name": f.get("name", ""),
                            "description": f.get("description", ""),
                            "evidence": f.get("matched_at", ""),
                            "template": f.get("template_id", ""),
                            "matched_at": f.get("matched_at", ""),
                        }],
                    })
            print(f"  Found {len(findings)} CVE findings")
    elif not has_nuclei:
        print("[Phase 5] Nuclei not available — skipping CVE scan")
        errors.append("Nuclei not available for CVE scanning")

    # Version-based checks (supplement to Nuclei)
    version_findings = _check_known_vulnerable_versions(live_hosts)
    for vf in version_findings:
        host_ip = vf.pop("ip", "")
        if host_ip:
            audit_log.add_host({"ip": host_ip, "vulnerabilities": [vf]})

    if version_findings:
        print(f"  Found {len(version_findings)} version-based findings")

    elapsed = time.monotonic() - start
    audit_log.log_phase(
        phase=5,
        name="Vulnerability Assessment",
        start_time=start_time,
        end_time=datetime.now().isoformat(),
        duration=elapsed,
        commands_run=commands_run,
        errors=errors,
    )
    audit_log.save()

    print(f"[Phase 5] Vulnerability assessment complete in {elapsed:.1f}s")


def _check_known_vulnerable_versions(hosts: list[dict]) -> list[dict]:
    """Basic version-based vulnerability checks."""
    findings = []

    # Known vulnerable version patterns
    checks = [
        {
            "service": "ssh",
            "pattern": "OpenSSH",
            "vulnerable_versions": ["6.", "7.0", "7.1", "7.2", "7.3", "7.4"],
            "severity": "medium",
            "name": "Outdated OpenSSH Version",
            "description": "OpenSSH version is outdated and may contain known vulnerabilities.",
        },
        {
            "service": "http",
            "pattern": "Apache",
            "vulnerable_versions": ["2.4.49", "2.4.50"],
            "severity": "critical",
            "name": "Apache Path Traversal (CVE-2021-41773/42013)",
            "description": "Apache 2.4.49/2.4.50 are vulnerable to path traversal attacks.",
        },
        {
            "service": "ssl",
            "pattern": "TLSv1.0",
            "vulnerable_versions": ["TLSv1.0"],
            "severity": "medium",
            "name": "TLS 1.0 Enabled",
            "description": "TLS 1.0 is deprecated and vulnerable to BEAST and POODLE attacks.",
        },
    ]

    for host in hosts:
        ip = host.get("ip", "")
        for port in host.get("ports", []):
            version = port.get("version", "").lower()
            service = port.get("service", "").lower()

            for check in checks:
                if check["pattern"].lower() in version or check["pattern"].lower() in service:
                    for vuln_ver in check["vulnerable_versions"]:
                        if vuln_ver.lower() in version:
                            findings.append({
                                "ip": ip,
                                "id": f"version-check-{check['name'].lower().replace(' ', '-')}",
                                "source": "version-check",
                                "severity": check["severity"],
                                "name": check["name"],
                                "description": check["description"],
                                "evidence": f"Port {port['port']}: {port.get('version', '')}",
                                "template": "",
                                "matched_at": f"{ip}:{port['port']}",
                            })
                            break

        # Check TLS probes
        tls_probes = host.get("security_probes", {}).get("tls", {})
        for cipher_info in tls_probes.get("ciphers", []):
            if "TLSv1.0" in str(cipher_info) or "SSLv3" in str(cipher_info):
                findings.append({
                    "ip": ip,
                    "id": "version-check-weak-tls",
                    "source": "version-check",
                    "severity": "medium",
                    "name": "Weak TLS/SSL Protocol",
                    "description": "Host supports deprecated TLS/SSL protocols.",
                    "evidence": str(cipher_info)[:200],
                    "template": "",
                    "matched_at": ip,
                })
                break

    return findings
