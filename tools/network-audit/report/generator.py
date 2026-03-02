"""generator.py — Branded HTML report generator for network security audits.

Generates a Gmail-compatible, table-based, inline-styled HTML report from
scan_results.json + analysis.json. Follows Kukui IT branding guidelines.

The AI's job is to write analysis.json with expert findings. This module
handles all HTML/CSS rendering so the model doesn't have to generate
50KB of markup in-context.

Usage:
    from report.generator import generate_report
    html_path = generate_report(scan_data, analysis, output_dir)
"""

import html
import re
from datetime import datetime
from pathlib import Path

# ── Branding constants ───────────────────────────────────────────────
LOGO_URL = "https://kukuiit.com/wp-content/uploads/2024/07/logo.png"
BRAND_GREEN = "#5aad1a"
BRAND_GREEN_LIGHT = "#e8f5d9"
BRAND_GREEN_DARK = "#3d7a0e"
BRAND_RED = "#ef4444"
BRAND_ORANGE = "#f59e0b"
BRAND_BLUE = "#3b82f6"
BRAND_GRAY = "#64748b"
BRAND_BG = "#f0f4f8"
BRAND_TEXT = "#1e293b"
BRAND_MUTED = "#64748b"

# ── Severity config ──────────────────────────────────────────────────
SEVERITY_CONFIG = {
    "critical": {
        "color": "#dc2626", "bg": "#fef2f2", "border": "#fecaca",
        "gradient": "linear-gradient(180deg,#dc2626,#b91c1c)",
        "badge_bg": "#ef4444", "label": "CRITICAL", "emoji": "&#x1F534;",
    },
    "high": {
        "color": "#ef4444", "bg": "#fef2f2", "border": "#fecaca",
        "gradient": "linear-gradient(180deg,#ef4444,#dc2626)",
        "badge_bg": "#ef4444", "label": "HIGH", "emoji": "&#x26A0;",
    },
    "medium": {
        "color": "#f59e0b", "bg": "#fffbeb", "border": "#fed7aa",
        "gradient": "linear-gradient(180deg,#f59e0b,#d97706)",
        "badge_bg": "#f59e0b", "label": "MEDIUM", "emoji": "&#x26A0;",
    },
    "low": {
        "color": "#3b82f6", "bg": "#eff6ff", "border": "#bfdbfe",
        "gradient": "linear-gradient(180deg,#3b82f6,#2563eb)",
        "badge_bg": "#3b82f6", "label": "LOW", "emoji": "&#x2139;",
    },
    "info": {
        "color": "#64748b", "bg": "#f8fafc", "border": "#e2e8f0",
        "gradient": "linear-gradient(180deg,#64748b,#475569)",
        "badge_bg": "#64748b", "label": "INFO", "emoji": "&#x2139;",
    },
}

URGENCY_CONFIG = {
    "immediate": {"bg": "#fef2f2", "color": "#dc2626", "label": "Immediate"},
    "this-week": {"bg": "#fef2f2", "color": "#dc2626", "label": "This Week"},
    "this-month": {"bg": "#fffbeb", "color": "#d97706", "label": "This Month"},
    "short-term": {"bg": "#fffbeb", "color": "#d97706", "label": "Short Term"},
    "medium-term": {"bg": "#eff6ff", "color": "#3b82f6", "label": "This Quarter"},
    "quarterly": {"bg": "#f0f4f8", "color": "#64748b", "label": "Quarterly"},
    "long-term": {"bg": "#f0f4f8", "color": "#64748b", "label": "Long Term"},
}

GRADE_OFFSETS = {
    "A+": 0, "A": 20, "A-": 41,
    "B+": 61, "B": 82, "B-": 102,
    "C+": 122, "C": 143, "C-": 163,
    "D+": 184, "D": 204, "D-": 224,
    "F": 306,
}


def _esc(text) -> str:
    """HTML-escape a value, handling None."""
    return html.escape(str(text or ""))



def _mask_mac(mac: str) -> str:
    """Mask a MAC address for privacy (keep last 3 octets)."""
    if not mac or len(mac) < 8:
        return mac
    parts = re.split(r"[:\-]", mac)
    if len(parts) == 6:
        return f"XX:XX:XX:{parts[3]}:{parts[4]}:{parts[5]}"
    return mac


# ── Main entry point ─────────────────────────────────────────────────

def generate_report(
    scan_data: dict,
    analysis: dict,
    output_dir: str | Path,
    filename: str = "network_security_report.html",
) -> Path:
    """Generate a branded HTML report from scan data + analysis.

    Args:
        scan_data: Parsed scan_results.json dict
        analysis: Parsed analysis.json dict (AI-generated)
        output_dir: Directory to write the report to
        filename: Output filename (default: network_security_report.html)

    Returns:
        Path to the generated HTML file
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename

    meta = scan_data.get("audit_meta", {})
    hosts = scan_data.get("hosts", [])
    findings = analysis.get("key_findings", [])
    positives = analysis.get("positive_practices", [])
    actions = analysis.get("priority_actions", [])
    grade = analysis.get("overall_grade", "N/A")

    # Compute stats
    hosts_with_ports = [h for h in hosts if any(
        p.get("state") == "open" for p in h.get("ports", [])
    )]
    total_ports = sum(
        len([p for p in h.get("ports", []) if p.get("state") == "open"])
        for h in hosts
    )

    # Count findings by severity
    sev_counts = {}
    for f in findings:
        s = f.get("severity", "info").lower()
        sev_counts[s] = sev_counts.get(s, 0) + 1

    # Sort findings by severity
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings_sorted = sorted(findings, key=lambda f: sev_order.get(f.get("severity", "info").lower(), 5))

    # Format date
    audit_date = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
    try:
        date_obj = datetime.strptime(audit_date, "%Y-%m-%d")
        date_display = date_obj.strftime("%B %-d, %Y")
    except (ValueError, Exception):
        date_display = audit_date

    client_name = meta.get("client_name", "Network Security Audit")
    subnet = meta.get("subnet", "")
    report_id = f"{client_name[:2].upper()}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # Build HTML
    parts = []
    parts.append(_render_doctype())
    parts.append(_render_hero(client_name, date_display, subnet, sev_counts, len(positives)))
    parts.append(_render_container_open())
    parts.append(_render_score_ring(grade, analysis.get("overall_grade_explanation", "")))
    parts.append(_render_stats_row(len(hosts_with_ports), total_ports, len(findings), len(positives)))
    parts.append(_render_executive_summary(analysis.get("executive_summary", "")))
    parts.append(_render_findings_section(findings_sorted, sev_counts))
    parts.append(_render_positives_section(positives))
    parts.append(_render_action_plan(actions))
    parts.append(_render_footer(client_name, date_display, report_id))
    parts.append(_render_container_close())

    html_content = "\n".join(parts)
    output_path.write_text(html_content)
    return output_path


# ── Section renderers ────────────────────────────────────────────────

def _render_doctype() -> str:
    return """<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>Network Security Audit Report — Kukui IT</title>
<style>
body{margin:0;padding:0;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:#f0f4f8;color:#1e293b;line-height:1.65;-webkit-font-smoothing:antialiased}
table{border-spacing:0;border-collapse:collapse}
img{border:0;outline:none;text-decoration:none}
.preheader{display:none!important;mso-hide:all;visibility:hidden;opacity:0;height:0;width:0;max-height:0;max-width:0;overflow:hidden}
@media only screen and (max-width:600px){
  .outer-table{width:100%!important}
  .hero-title{font-size:22px!important}
  .hero-subtitle{font-size:11px!important}
  .stat-cell{display:block!important;width:100%!important;text-align:center!important;padding:6px 0!important}
  .finding-grid td{display:block!important;width:100%!important;padding:4px 0!important}
  .section-title{font-size:18px!important}
  .positive-cell{display:block!important;width:100%!important}
}
@media print{
  body{background:#fff!important}
  .hero-bg{background:#1a2332!important}
  .card{box-shadow:none!important;border:1px solid #e2e8f0!important;break-inside:avoid}
  .finding-card{break-inside:avoid}
}
</style>
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1e293b;line-height:1.65">"""


def _render_hero(client_name: str, date_display: str, subnet: str,
                 sev_counts: dict, positive_count: int) -> str:
    # Build severity badges
    badges = []
    for sev, label_cfg in [
        ("critical", ("&#x1F534;", "rgba(220,38,38,0.25)")),
        ("high", ("&#x26A0;", "rgba(239,68,68,0.2)")),
        ("medium", ("&#x26A0;", "rgba(245,158,11,0.18)")),
        ("low", ("&#x2139;", "rgba(59,130,246,0.18)")),
    ]:
        count = sev_counts.get(sev, 0)
        if count > 0:
            emoji, bg = label_cfg
            badges.append(
                f'<td style="padding:5px 12px;border-radius:8px;font-size:12px;font-weight:600;'
                f'color:rgba(255,255,255,0.9);background:{bg};border:1px solid rgba(255,255,255,0.08)">'
                f'{emoji} {count} {sev.capitalize()}</td>'
            )
    if positive_count > 0:
        badges.append(
            f'<td style="padding:5px 12px;border-radius:8px;font-size:12px;font-weight:600;'
            f'color:rgba(255,255,255,0.9);background:rgba(90,173,26,0.18);border:1px solid rgba(255,255,255,0.08)">'
            f'&#x2705; {positive_count} Positive</td>'
        )
    badges_html = "\n    ".join(badges)

    return f"""
<!-- Preheader -->
<span class="preheader" style="display:none!important;mso-hide:all;visibility:hidden;opacity:0;height:0;width:0;max-height:0;max-width:0;overflow:hidden">Network Security Audit — {_esc(client_name)} — {_esc(subnet)}</span>

<!-- HERO HEADER -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td class="hero-bg" style="background:linear-gradient(160deg,#080e14 0%,#0d1f12 35%,#0a1a1a 65%,#0f1923 100%);padding:50px 20px 40px;text-align:center">
  <img src="{LOGO_URL}" alt="Kukui IT" width="56" height="56" referrerpolicy="no-referrer" style="display:inline-block;max-height:56px;margin-bottom:18px">
  <h1 class="hero-title" style="font-size:28px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;margin:0 0 6px 0">&#x1F512; Network Security Audit Report</h1>
  <p class="hero-subtitle" style="color:rgba(255,255,255,0.5);font-size:12px;font-weight:400;letter-spacing:1.5px;text-transform:uppercase;margin:0 0 16px 0">{_esc(client_name)} &mdash; Comprehensive Vulnerability Assessment</p>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center">
  <tr><td style="padding:7px 22px;border-radius:100px;background:rgba(90,173,26,0.12);border:1px solid rgba(90,173,26,0.25);color:rgba(255,255,255,0.85);font-size:13px;font-weight:500">
    &#x1F334; Performed by Kukui IT &mdash; {_esc(date_display)} &bull; {_esc(subnet)}
  </td></tr>
  </table>
  <table role="presentation" cellpadding="0" cellspacing="4" border="0" align="center" style="margin-top:18px">
  <tr>
    {badges_html}
  </tr>
  </table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:20px">
  <tr><td style="height:3px;background:linear-gradient(90deg,transparent,#5aad1a,rgba(59,130,246,0.6),#5aad1a,transparent)">&nbsp;</td></tr>
  </table>
</td></tr>
</table>"""


def _render_container_open() -> str:
    return """
<!-- MAIN CONTAINER -->
<table class="outer-table" role="presentation" width="920" cellpadding="0" cellspacing="0" border="0" align="center" style="max-width:920px;margin:0 auto">
<tr><td style="padding:0 20px 40px">"""


def _render_container_close() -> str:
    return """
</td></tr>
</table>
</body>
</html>"""


def _render_score_ring(grade: str, explanation: str) -> str:
    offset = GRADE_OFFSETS.get(grade, 204)
    color = BRAND_GREEN if offset < 130 else (BRAND_ORANGE if offset < 200 else BRAND_RED)
    return f"""
<!-- SECURITY SCORE -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td style="text-align:center;padding:40px 20px 30px">
  <div style="display:inline-block;position:relative;width:140px;height:140px">
    <svg width="140" height="140" viewBox="0 0 140 140" style="transform:rotate(-90deg)">
      <circle cx="70" cy="70" r="65" fill="none" stroke="#e2e8f0" stroke-width="10"/>
      <circle cx="70" cy="70" r="65" fill="none" stroke="{color}" stroke-width="10" stroke-linecap="round" stroke-dasharray="408" stroke-dashoffset="{offset}"/>
    </svg>
    <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:48px;font-weight:800;color:{color};line-height:1">{_esc(grade)}<br><span style="display:block;font-size:12px;font-weight:500;color:#64748b;margin-top:2px;letter-spacing:1px">SCORE</span></div>
  </div>
  <p style="margin:16px auto 0;max-width:500px;font-size:14px;color:#64748b">{_esc(explanation)}</p>
</td></tr>
</table>"""


def _render_stats_row(hosts: int, ports: int, findings: int, positives: int) -> str:
    stats = [
        (hosts, "&#x1F4BB; Hosts Scanned", BRAND_TEXT),
        (ports, "&#x1F6AA; Open Ports", BRAND_TEXT),
        (findings, "&#x1F50D; Findings", BRAND_ORANGE),
        (positives, "&#x2705; Positive", BRAND_GREEN),
    ]
    cells = []
    for value, label, color in stats:
        cells.append(
            f'<td class="stat-cell" width="25%" style="background:#ffffff;border-radius:12px;'
            f'padding:18px 12px;text-align:center;border:1px solid #e8ecf1;'
            f'box-shadow:0 2px 8px rgba(0,0,0,0.04)">'
            f'<div style="font-size:28px;font-weight:800;color:{color}">{value}</div>'
            f'<div style="font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;'
            f'letter-spacing:1px;margin-top:2px">{label}</div></td>'
        )
    return f"""
<!-- STATS ROW -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="8" border="0" style="margin-bottom:24px">
<tr>
  {chr(10).join(cells)}
</tr>
</table>"""


def _render_executive_summary(summary: str) -> str:
    # Split summary into paragraphs (by \n\n or single \n)
    paragraphs = [p.strip() for p in summary.split("\n") if p.strip()]
    body = "\n".join(
        f'  <p style="margin:{("16px" if i == 0 else "14px")} 0 0;font-size:14px;color:#334155">{_esc(p)}</p>'
        for i, p in enumerate(paragraphs)
    )
    return f"""
<!-- EXECUTIVE SUMMARY -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px">
<tr><td class="card" style="background:#ffffff;border-radius:16px;padding:28px 32px;border:1px solid #e8ecf1;box-shadow:0 4px 24px rgba(0,0,0,0.06)">
  <h2 class="section-title" style="font-size:20px;font-weight:700;color:#1e293b;margin:0 0 4px 0;border-bottom:3px solid #5aad1a;padding-bottom:10px">&#x1F4CB; Executive Summary</h2>
{body}
</td></tr>
</table>"""


def _render_finding_card(finding: dict) -> str:
    sev = finding.get("severity", "info").lower()
    cfg = SEVERITY_CONFIG.get(sev, SEVERITY_CONFIG["info"])
    fid = finding.get("id", "")
    title = finding.get("title", "Finding")
    description = finding.get("description", "")
    affected = finding.get("affected_device", "")
    evidence = finding.get("evidence", "")
    risk = finding.get("business_impact", "")
    remediation = finding.get("remediation", "")
    category = finding.get("category", "")
    port = finding.get("port", "")

    # Detail grid rows
    grid_rows = []
    if affected:
        grid_rows.append(f'<td style="padding:6px 12px;font-size:12px"><strong style="color:#64748b">Host:</strong> {_esc(affected)}</td>')
    if port:
        grid_rows.append(f'<td style="padding:6px 12px;font-size:12px"><strong style="color:#64748b">Port:</strong> {_esc(port)}</td>')

    grid_html = ""
    if grid_rows:
        # Pair up grid cells into rows of 2
        paired = []
        for i in range(0, len(grid_rows), 2):
            row_cells = grid_rows[i:i+2]
            if len(row_cells) == 1:
                row_cells.append('<td></td>')
            paired.append(f'<tr>{"".join(row_cells)}</tr>')
        grid_html = f"""
      <table role="presentation" class="finding-grid" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:12px 0;background:rgba(255,255,255,0.6);border-radius:8px;padding:2px">
      {"".join(paired)}
      </table>"""

    evidence_html = ""
    if evidence:
        # Replace newlines in evidence with <br>
        ev_lines = _esc(evidence).replace("\n", "<br>")
        evidence_html = f"""
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0">
      <tr><td style="background:#1e293b;border-radius:8px;padding:12px 16px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:12px;color:#86efac;line-height:1.5">
        {ev_lines}
      </td></tr>
      </table>"""

    risk_html = ""
    if risk:
        risk_html = f"""
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0">
      <tr><td style="background:#f8fafc;border-left:3px solid #94a3b8;border-radius:0 6px 6px 0;padding:10px 14px;font-size:12px;color:#475569">
        <strong>Risk:</strong> {_esc(risk)}
      </td></tr>
      </table>"""

    remediation_html = ""
    if remediation:
        remediation_html = f"""
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0">
      <tr><td style="background:#f0fdf4;border-left:4px solid #5aad1a;border-radius:0 6px 6px 0;padding:10px 14px;font-size:12px;color:#166534">
        &#x1F527; <strong>Remediation:</strong> {_esc(remediation)}
      </td></tr>
      </table>"""

    fid_prefix = f"{fid}: " if fid else ""

    return f"""
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="finding-card" style="margin:16px 0;border-radius:14px;border:1px solid {cfg['border']};overflow:hidden">
  <tr>
    <td width="5" style="background:{cfg['gradient']}">&nbsp;</td>
    <td style="padding:20px 22px;background:{cfg['bg']}">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
        <td style="padding:3px 10px;border-radius:6px;font-size:11px;font-weight:700;color:#fff;background:{cfg['badge_bg']};vertical-align:middle">{cfg['emoji']} {cfg['label']}</td>
        <td style="padding-left:10px;font-size:16px;font-weight:700;color:#1e293b;vertical-align:middle">{_esc(fid_prefix)}{_esc(title)}</td>
      </tr></table>
      <p style="margin:12px 0 0;font-size:13px;color:#334155">{_esc(description)}</p>
{grid_html}{evidence_html}{risk_html}{remediation_html}
    </td>
  </tr>
  </table>"""


def _render_findings_section(findings: list, sev_counts: dict) -> str:
    if not findings:
        return """
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px">
<tr><td class="card" style="background:#ffffff;border-radius:16px;padding:28px 32px;border:1px solid #e8ecf1;box-shadow:0 4px 24px rgba(0,0,0,0.06)">
  <h2 class="section-title" style="font-size:20px;font-weight:700;color:#1e293b;margin:0;border-bottom:3px solid #5aad1a;padding-bottom:10px">&#x1F6A8; Security Findings</h2>
  <p style="margin:16px 0 0;color:#5aad1a;font-size:14px">&#x2705; No significant security findings identified.</p>
</td></tr>
</table>"""

    # Severity count badges
    badge_configs = [
        ("critical", "linear-gradient(135deg,#dc2626,#b91c1c)"),
        ("high", "linear-gradient(135deg,#ef4444,#dc2626)"),
        ("medium", "linear-gradient(135deg,#f59e0b,#d97706)"),
        ("low", "linear-gradient(135deg,#3b82f6,#2563eb)"),
        ("info", "linear-gradient(135deg,#64748b,#475569)"),
    ]
    count_badges = []
    for sev, bg in badge_configs:
        count = sev_counts.get(sev, 0)
        if count > 0:
            cfg = SEVERITY_CONFIG[sev]
            count_badges.append(
                f'<td style="padding:4px 14px;border-radius:20px;font-size:12px;font-weight:700;'
                f'color:#fff;background:{bg}">{cfg["emoji"]} {sev.capitalize()} &nbsp;{count}</td>'
            )

    badges_row = "\n    ".join(count_badges)
    finding_cards = "\n".join(_render_finding_card(f) for f in findings)

    return f"""
<!-- SECURITY FINDINGS -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px">
<tr><td class="card" style="background:#ffffff;border-radius:16px;padding:28px 32px;border:1px solid #e8ecf1;box-shadow:0 4px 24px rgba(0,0,0,0.06)">
  <h2 class="section-title" style="font-size:20px;font-weight:700;color:#1e293b;margin:0 0 4px 0;border-bottom:3px solid #5aad1a;padding-bottom:10px">&#x1F6A8; Security Findings</h2>
  <table role="presentation" cellpadding="0" cellspacing="4" border="0" style="margin:16px 0">
  <tr>
    {badges_row}
  </tr>
  </table>
{finding_cards}
</td></tr>
</table>"""


def _render_positives_section(positives: list) -> str:
    if not positives:
        return ""

    # Render as 2-column grid
    cells = []
    for p in positives:
        title = p.get("title", "")
        desc = p.get("description", "")
        cells.append(
            f'<td class="positive-cell" width="50%" valign="top" style="background:#f0fdf4;border-radius:12px;'
            f'padding:16px 18px;border:1px solid #bbf7d0">'
            f'<div style="font-size:14px;font-weight:700;color:#166534;margin-bottom:6px">&#x2705; {_esc(title)}</div>'
            f'<div style="font-size:12px;color:#334155">{_esc(desc)}</div></td>'
        )

    # Pair into rows of 2
    rows = []
    for i in range(0, len(cells), 2):
        pair = cells[i:i+2]
        if len(pair) == 1:
            pair.append('<td class="positive-cell" width="50%"></td>')
        rows.append(f'<tr>{"".join(pair)}</tr>')

    return f"""
<!-- POSITIVE FINDINGS -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px">
<tr><td class="card" style="background:#ffffff;border-radius:16px;padding:28px 32px;border:1px solid #e8ecf1;box-shadow:0 4px 24px rgba(0,0,0,0.06)">
  <h2 class="section-title" style="font-size:20px;font-weight:700;color:#1e293b;margin:0 0 16px 0;border-bottom:3px solid #5aad1a;padding-bottom:10px">&#x1F6E1; Positive Findings</h2>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="8" border="0">
  {chr(10).join(rows)}
  </table>
</td></tr>
</table>"""


def _render_action_plan(actions: list) -> str:
    if not actions:
        return ""

    rows = []
    for i, a in enumerate(actions):
        urgency = a.get("urgency", "medium-term").lower()
        u_cfg = URGENCY_CONFIG.get(urgency, URGENCY_CONFIG["medium-term"])
        priority = a.get("priority", i + 1)
        action_text = a.get("action", "")
        effort = a.get("effort", "").capitalize()
        bg = "#fafbfc" if i % 2 else "#ffffff"

        effort_color = BRAND_GREEN if effort.lower() == "low" else (
            BRAND_ORANGE if effort.lower() == "medium" else BRAND_RED
        )

        rows.append(
            f'<tr style="border-bottom:1px solid #f1f5f9;background:{bg}">'
            f'<td style="padding:10px 8px;font-weight:700">{priority}</td>'
            f'<td style="padding:10px 8px"><span style="display:inline-block;padding:3px 10px;'
            f'border-radius:10px;background:{u_cfg["bg"]};color:{u_cfg["color"]};'
            f'font-size:11px;font-weight:700">{u_cfg["label"]}</span></td>'
            f'<td style="padding:10px 8px">{_esc(action_text)}</td>'
            f'<td style="padding:10px 8px"><span style="color:{effort_color};font-weight:600">{_esc(effort)}</span></td>'
            f'</tr>'
        )

    return f"""
<!-- ACTION PLAN -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px">
<tr><td class="card" style="background:#ffffff;border-radius:16px;padding:28px 32px;border:1px solid #e8ecf1;box-shadow:0 4px 24px rgba(0,0,0,0.06)">
  <h2 class="section-title" style="font-size:20px;font-weight:700;color:#1e293b;margin:0 0 16px 0;border-bottom:3px solid #5aad1a;padding-bottom:10px">&#x1F3AF; Prioritized Action Plan</h2>
  <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="font-size:13px;min-width:500px">
  <thead>
  <tr style="background:#f8fafc">
    <th style="padding:10px 8px;text-align:left;font-weight:700;color:#64748b;border-bottom:2px solid #e2e8f0;font-size:11px;text-transform:uppercase">#</th>
    <th style="padding:10px 8px;text-align:left;font-weight:700;color:#64748b;border-bottom:2px solid #e2e8f0;font-size:11px;text-transform:uppercase">Urgency</th>
    <th style="padding:10px 8px;text-align:left;font-weight:700;color:#64748b;border-bottom:2px solid #e2e8f0;font-size:11px;text-transform:uppercase">Action</th>
    <th style="padding:10px 8px;text-align:left;font-weight:700;color:#64748b;border-bottom:2px solid #e2e8f0;font-size:11px;text-transform:uppercase">Effort</th>
  </tr>
  </thead>
  <tbody>
  {chr(10).join(rows)}
  </tbody>
  </table>
  </div>
</td></tr>
</table>"""


def _render_footer(client_name: str, date_display: str, report_id: str) -> str:
    return f"""
<!-- FOOTER -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td style="text-align:center;padding:30px 20px 40px">
  <img src="{LOGO_URL}" alt="Kukui IT" width="36" height="36" referrerpolicy="no-referrer" style="display:inline-block;max-height:36px;margin-bottom:10px">
  <p style="font-size:13px;font-weight:600;color:#64748b;margin:0 0 4px">Protecting Hawaii&rsquo;s Networks Since 1996</p>
  <p style="margin:0 0 16px"><a href="https://kukuiit.com" style="color:#5aad1a;font-size:13px;text-decoration:none;font-weight:500">kukuiit.com</a></p>
  <p style="font-size:11px;color:#94a3b8;max-width:500px;margin:0 auto;line-height:1.5">
    <strong>CONFIDENTIAL</strong> &mdash; This report was prepared for {_esc(client_name)} by Kukui IT on {_esc(date_display)}. Report ID: {_esc(report_id)}. Distribution is restricted to authorized recipients only.
  </p>
</td></tr>
</table>"""
