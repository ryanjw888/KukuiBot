"""
email_sanitize.py — Outbound email content sanitization.

Scans email subject/body for actual secrets that must never be sent externally.
Blocks real credentials and keys only — not general technical content like
IP addresses, port numbers, file paths, or email addresses.

Rules (high-confidence secrets only):
  - API keys with known prefixes (sk-, ghp_, xox*, etc.)
  - SSH/PGP private key blocks
  - Passwords or secrets appearing as key=value assignments
"""

import re
from dataclasses import dataclass
from typing import Pattern


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: Pattern[str]
    severity: str = "high"


RULES: list[Rule] = [
    # Actual API keys / tokens with known prefixes
    Rule("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    Rule("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    Rule("GitHub token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    Rule("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    Rule("AWS key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    Rule("Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    # SSH / PGP private key blocks
    Rule("SSH/PGP private key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
    # Inline password/secret assignments (e.g. password=xyz, secret: abc)
    Rule("inline secret assignment", re.compile(
        r"""(?:password|passwd|app_password|secret_key|api_key|auth_token|access_token)\s*[=:]\s*["']?[A-Za-z0-9_/+.@!#$%^&*-]{8,}""",
        re.IGNORECASE,
    )),
]

# HTML/XSS-specific rules for outbound HTML email content
HTML_RULES: list[Rule] = [
    Rule("script tag", re.compile(r"<script\b", re.IGNORECASE), severity="critical"),
    Rule("iframe tag", re.compile(r"<iframe\b", re.IGNORECASE), severity="critical"),
    Rule("object tag", re.compile(r"<object\b", re.IGNORECASE), severity="high"),
    Rule("embed tag", re.compile(r"<embed\b", re.IGNORECASE), severity="high"),
    Rule("javascript URL", re.compile(r"javascript\s*:", re.IGNORECASE), severity="critical"),
    Rule("event handler", re.compile(r"\bon\w+\s*=", re.IGNORECASE), severity="critical"),
    Rule("data URL (non-image)", re.compile(r"data\s*:(?!image/)", re.IGNORECASE), severity="high"),
    Rule("form tag", re.compile(r"<form\b", re.IGNORECASE), severity="high"),
    Rule("meta refresh", re.compile(r'<meta\b[^>]*http-equiv\s*=\s*["\']?refresh', re.IGNORECASE), severity="high"),
]


def redact_preview(text: str, start: int, end: int, width: int = 28) -> str:
    """Show context around a finding."""
    lo = max(0, start - width)
    hi = min(len(text), end + width)
    return text[lo:hi].replace("\n", " ")


def scan(text: str) -> list[dict]:
    """Scan text for all matching rules. Returns list of findings."""
    findings = []
    for rule in RULES:
        for m in rule.pattern.finditer(text):
            findings.append({
                "rule": rule.name,
                "severity": rule.severity,
                "match": m.group(0),
                "start": m.start(),
                "end": m.end(),
                "preview": redact_preview(text, m.start(), m.end()),
            })
    return findings


def scan_html(html: str) -> list[dict]:
    """Scan HTML content for XSS/injection patterns. Returns list of findings."""
    findings = []
    for rule in HTML_RULES:
        for m in rule.pattern.finditer(html):
            findings.append({
                "rule": rule.name,
                "severity": rule.severity,
                "match": m.group(0),
                "start": m.start(),
                "end": m.end(),
                "preview": redact_preview(html, m.start(), m.end()),
            })
    return findings


def preflight_html_email(subject: str, body_text: str, body_html: str) -> tuple[bool, list[dict]]:
    """
    Preflight check for outbound HTML email.
    Runs secret scanning on text content and XSS scanning on raw HTML.
    Returns (passed: bool, findings: list).
    """
    # Secret scanning on the plain text version
    text = f"Subject:\n{subject}\n\nBody:\n{body_text}"
    findings = scan(text)
    # HTML/XSS scanning on raw HTML
    findings.extend(scan_html(body_html))
    return (len(findings) == 0, findings)


def preflight_email(subject: str, body: str, body_html: str | None = None) -> tuple[bool, list[dict]]:
    """
    Preflight check for outbound email.
    Returns (passed: bool, findings: list).
    If passed is False, email must NOT be sent.
    If body_html is provided, delegates to preflight_html_email().
    """
    if body_html:
        return preflight_html_email(subject, body, body_html)
    text = f"Subject:\n{subject}\n\nBody:\n{body}"
    findings = scan(text)
    return (len(findings) == 0, findings)


# --- CLI ---

if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Email sanitization preflight check")
    parser.add_argument("--subject", default="", help="Email subject")
    parser.add_argument("--body", default="", help="Email body (inline)")
    parser.add_argument("--body-file", help="Read body from file")
    parser.add_argument("--stdin", action="store_true", help="Read body from stdin")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    args = parser.parse_args()

    if args.stdin:
        body = sys.stdin.read()
    elif args.body_file:
        with open(args.body_file) as f:
            body = f.read()
    else:
        body = args.body

    passed, findings = preflight_email(args.subject, body)

    if args.json:
        print(json.dumps({"passed": passed, "findings": findings}, indent=2))
    else:
        if passed:
            print("✅ PASS — no sensitive content detected")
        else:
            print(f"❌ FAIL — {len(findings)} finding(s):")
            for f in findings:
                print(f"  [{f['severity']}] {f['rule']}: \"{f['match']}\"")
                print(f"    context: ...{f['preview']}...")

    sys.exit(0 if passed else 1)
