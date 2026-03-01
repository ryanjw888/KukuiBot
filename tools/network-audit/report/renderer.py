"""renderer.py — Merge scan data + AI analysis into branded HTML report."""

import html
import json
from datetime import datetime
from pathlib import Path
from string import Template


TEMPLATE_PATH = Path(__file__).parent / "template.html"

SEVERITY_COLORS = {
    "critical": "#dc3545",
    "high": "#fd7e14",
    "medium": "#ffc107",
    "low": "#17a2b8",
    "info": "#6c757d",
}

SEVERITY_EMOJI = {
    "critical": "\U0001f534",  # red circle
    "high": "\U0001f7e0",      # orange circle
    "medium": "\U0001f7e1",    # yellow circle
    "low": "\U0001f535",       # blue circle
    "info": "\u2139\ufe0f",    # info
}

URGENCY_COLORS = {
    "immediate": "#dc3545",
    "short-term": "#fd7e14",
    "medium-term": "#ffc107",
    "long-term": "#17a2b8",
}

GRADE_COLORS = {
    "A+": "#28a745", "A": "#28a745", "A-": "#5aad1a",
    "B+": "#5aad1a", "B": "#7cb342", "B-": "#9e9d24",
    "C+": "#ffc107", "C": "#ff9800", "C-": "#ff7043",
    "D+": "#ff5722", "D": "#f44336", "D-": "#e53935",
    "F": "#dc3545",
}


def render_report(
    scan_data: dict,
    analysis: dict,
    output_path: Path,
) -> Path:
    """Generate the final HTML report from scan data and AI analysis."""
    template_text = TEMPLATE_PATH.read_text()
    template = Template(template_text)

    meta = scan_data.get("audit_meta", {})
    stats = scan_data.get("summary_stats", {})
    hosts = scan_data.get("hosts", [])
    grade = analysis.get("overall_grade", "N/A")

    vuln_counts = stats.get("vulnerabilities_by_severity", {})
    total_vulns = sum(vuln_counts.values())

    values = {
        "audit_date": meta.get("date", datetime.now().strftime("%Y-%m-%d")),
        "client_name": meta.get("client_name", "Network Security Audit"),
        "total_hosts": str(stats.get("total_hosts", len(hosts))),
        "total_ports": str(stats.get("total_open_ports", 0)),
        "overall_grade": grade,
        "grade_color": GRADE_COLORS.get(grade, "#333"),
        "total_vulns": str(total_vulns),
        "executive_summary": _render_executive_summary(analysis),
        "vulnerabilities_html": _render_vulnerabilities(analysis),
        "positive_practices_html": _render_positive_practices(analysis),
        "device_inventory_html": _render_device_inventory(hosts),
        "priority_actions_html": _render_priority_actions(analysis),
        "technical_appendix_html": _render_technical_appendix(scan_data),
        "generation_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    rendered = template.safe_substitute(values)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered)
    return output_path


def _esc(text: str) -> str:
    return html.escape(str(text or ""))


def _render_executive_summary(analysis: dict) -> str:
    summary = analysis.get("executive_summary", "No executive summary provided.")
    explanation = analysis.get("overall_grade_explanation", "")
    parts = [f"<p style=\"margin: 0 0 12px 0;\">{_esc(summary)}</p>"]
    if explanation:
        parts.append(
            f"<p style=\"margin: 12px 0 0 0; font-style: italic; color: #777;\">"
            f"Grade rationale: {_esc(explanation)}</p>"
        )
    return "\n".join(parts)


def _render_vulnerabilities(analysis: dict) -> str:
    findings = analysis.get("key_findings", [])
    if not findings:
        return "<p style=\"color: #28a745;\">&#10004; No significant vulnerabilities identified.</p>"

    rows = []
    # Sort by severity
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings.sort(key=lambda f: severity_order.get(f.get("severity", "info"), 5))

    for f in findings:
        sev = f.get("severity", "info").lower()
        color = SEVERITY_COLORS.get(sev, "#6c757d")
        emoji = SEVERITY_EMOJI.get(sev, "")

        rows.append(f"""
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
       style="margin-bottom: 15px; border: 1px solid #e9ecef; border-left: 4px solid {color}; border-radius: 4px;">
<tr>
<td style="padding: 15px;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
<tr>
<td>
<span style="font-size: 15px; font-weight: 600; color: #333;">{emoji} {_esc(f.get('title', 'Finding'))}</span>
<span style="display: inline-block; padding: 2px 8px; font-size: 11px; font-weight: 600;
       color: #fff; background-color: {color}; border-radius: 3px; margin-left: 8px;
       text-transform: uppercase;">{_esc(sev)}</span>
</td>
</tr>
<tr>
<td style="padding-top: 8px; font-size: 13px; color: #555;">
{_esc(f.get('description', ''))}
</td>
</tr>
<tr>
<td style="padding-top: 8px;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
<tr>
<td style="width: 50%; font-size: 12px; color: #777;">
<strong>Affected:</strong> {_esc(f.get('affected_device', 'N/A'))}
</td>
<td style="width: 50%; font-size: 12px; color: #777;">
<strong>Evidence:</strong> {_esc(f.get('evidence', 'N/A'))}
</td>
</tr>
</table>
</td>
</tr>
<tr>
<td style="padding-top: 6px; font-size: 12px; color: #777;">
<strong>Business Impact:</strong> {_esc(f.get('business_impact', 'N/A'))}
</td>
</tr>
<tr>
<td style="padding-top: 6px; font-size: 12px; color: #28a745;">
<strong>Remediation:</strong> {_esc(f.get('remediation', 'N/A'))}
</td>
</tr>
</table>
</td>
</tr>
</table>""")

    return "\n".join(rows)


def _render_positive_practices(analysis: dict) -> str:
    practices = analysis.get("positive_practices", [])
    if not practices:
        return "<p style=\"color: #666; font-style: italic;\">No specific positive practices noted.</p>"

    rows = []
    for p in practices:
        rows.append(f"""
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
       style="margin-bottom: 10px; border: 1px solid #d4edda; border-left: 4px solid #28a745; border-radius: 4px;">
<tr>
<td style="padding: 12px 15px;">
<span style="font-size: 14px; font-weight: 600; color: #155724;">&#10004;&#65039; {_esc(p.get('title', ''))}</span><br>
<span style="font-size: 13px; color: #555;">{_esc(p.get('description', ''))}</span>
</td>
</tr>
</table>""")

    return "\n".join(rows)


def _render_device_inventory(hosts: list[dict]) -> str:
    if not hosts:
        return "<p style=\"color: #666;\">No devices discovered.</p>"

    header = """
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
       style="border-collapse: collapse; font-size: 13px;">
<tr style="background-color: #5aad1a; color: #fff;">
<td style="padding: 8px 12px; font-weight: 600; border: 1px solid #4a9615;">IP Address</td>
<td style="padding: 8px 12px; font-weight: 600; border: 1px solid #4a9615;">Hostname</td>
<td style="padding: 8px 12px; font-weight: 600; border: 1px solid #4a9615;">Vendor</td>
<td style="padding: 8px 12px; font-weight: 600; border: 1px solid #4a9615;">Category</td>
<td style="padding: 8px 12px; font-weight: 600; border: 1px solid #4a9615;">Open Ports</td>
</tr>"""

    rows = []
    for i, h in enumerate(sorted(hosts, key=lambda x: _ip_sort_key(x.get("ip", "")))):
        bg = "#ffffff" if i % 2 == 0 else "#f8f9fa"
        ports = sorted(p["port"] for p in h.get("ports", []) if p.get("state") == "open")
        ports_str = ", ".join(str(p) for p in ports[:15])
        if len(ports) > 15:
            ports_str += f" (+{len(ports) - 15} more)"

        rows.append(f"""
<tr style="background-color: {bg};">
<td style="padding: 6px 12px; border: 1px solid #e9ecef; font-family: monospace;">{_esc(h.get('ip', ''))}</td>
<td style="padding: 6px 12px; border: 1px solid #e9ecef;">{_esc(h.get('hostname', '') or '-')}</td>
<td style="padding: 6px 12px; border: 1px solid #e9ecef;">{_esc(h.get('vendor', '') or 'Unknown')}</td>
<td style="padding: 6px 12px; border: 1px solid #e9ecef;">{_esc(h.get('category', '') or 'Unknown')}</td>
<td style="padding: 6px 12px; border: 1px solid #e9ecef; font-family: monospace; font-size: 12px;">{_esc(ports_str) or '-'}</td>
</tr>""")

    return header + "\n".join(rows) + "\n</table>"


def _render_priority_actions(analysis: dict) -> str:
    actions = analysis.get("priority_actions", [])
    if not actions:
        return "<p style=\"color: #666; font-style: italic;\">No priority actions identified.</p>"

    rows = []
    for a in actions:
        urgency = a.get("urgency", "medium-term").lower()
        color = URGENCY_COLORS.get(urgency, "#ffc107")
        priority = a.get("priority", "")

        rows.append(f"""
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
       style="margin-bottom: 10px; border: 1px solid #e9ecef; border-radius: 4px;">
<tr>
<td style="padding: 12px 15px;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
<tr>
<td style="width: 40px; vertical-align: top;">
<span style="display: inline-block; width: 28px; height: 28px; line-height: 28px; text-align: center;
       background-color: {color}; color: #fff; border-radius: 50%; font-size: 13px; font-weight: 700;">
{_esc(str(priority))}</span>
</td>
<td style="vertical-align: top;">
<span style="font-size: 14px; font-weight: 600; color: #333;">{_esc(a.get('action', ''))}</span>
<span style="display: inline-block; padding: 1px 6px; font-size: 10px; font-weight: 600;
       color: {color}; border: 1px solid {color}; border-radius: 3px; margin-left: 8px;
       text-transform: uppercase;">{_esc(urgency)}</span>
<br>
<span style="font-size: 12px; color: #777;">
Effort: {_esc(a.get('effort', 'N/A'))} &bull; Impact: {_esc(a.get('impact', 'N/A'))}
</span>
</td>
</tr>
</table>
</td>
</tr>
</table>""")

    return "\n".join(rows)


def _render_technical_appendix(scan_data: dict) -> str:
    parts = []

    # Phase timing
    phase_logs = scan_data.get("phase_logs", [])
    if phase_logs:
        parts.append("<strong>Phase Timing</strong>")
        parts.append("""<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
            style="border-collapse: collapse; margin: 8px 0 15px 0;">""")
        for pl in phase_logs:
            parts.append(f"""<tr>
<td style="padding: 4px 8px; border: 1px solid #e9ecef;">Phase {pl.get('phase', '?')}: {_esc(pl.get('name', ''))}</td>
<td style="padding: 4px 8px; border: 1px solid #e9ecef; text-align: right;">{pl.get('duration_seconds', 0):.1f}s</td>
<td style="padding: 4px 8px; border: 1px solid #e9ecef;">{_esc(pl.get('status', ''))}</td>
</tr>""")
        parts.append("</table>")

    # Vulnerability summary counts
    stats = scan_data.get("summary_stats", {})
    vuln_counts = stats.get("vulnerabilities_by_severity", {})
    if any(vuln_counts.values()):
        parts.append("<strong>Vulnerability Summary</strong>")
        parts.append("<table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\" "
                     "style=\"border-collapse: collapse; margin: 8px 0 15px 0;\">")
        for sev in ("critical", "high", "medium", "low", "info"):
            count = vuln_counts.get(sev, 0)
            if count:
                color = SEVERITY_COLORS.get(sev, "#666")
                parts.append(f"""<tr>
<td style="padding: 3px 8px;"><span style="color: {color}; font-weight: 600; text-transform: capitalize;">{sev}</span></td>
<td style="padding: 3px 8px; text-align: right; font-weight: 600;">{count}</td>
</tr>""")
        parts.append("</table>")

    # Scan metadata
    meta = scan_data.get("audit_meta", {})
    parts.append("<strong>Scan Parameters</strong>")
    parts.append(f"<p style=\"margin: 4px 0;\">Subnet: {_esc(meta.get('subnet', 'N/A'))}</p>")
    parts.append(f"<p style=\"margin: 4px 0;\">Interface: {_esc(meta.get('interface', 'N/A'))}</p>")
    parts.append(f"<p style=\"margin: 4px 0;\">Duration: {meta.get('duration_seconds', 0):.0f}s</p>")
    tools = meta.get("tools_versions", {})
    if tools:
        tool_str = ", ".join(f"{k} v{v}" for k, v in tools.items())
        parts.append(f"<p style=\"margin: 4px 0;\">Tools: {_esc(tool_str)}</p>")

    return "\n".join(parts) if parts else "<p>No technical data available.</p>"


def _ip_sort_key(ip: str) -> tuple:
    try:
        return tuple(int(x) for x in ip.split("."))
    except (ValueError, AttributeError):
        return (999, 999, 999, 999)
