"""
content_guard.py — Two-stage content security for KukuiBot.

Thin orchestration layer over injection_guard.py (Stage 1) and spark_guard.py (Stage 2).
Import this module for convenience functions; the server endpoints wire things directly.

INGRESS (inbound content):
  Stage 1: injection_guard.scan_text() → regex + DeBERTa → LEGIT / SUSPICIOUS / INJECTION
  Stage 2: spark_guard.assess_inbound() → sandboxed Codex (no tools) → ALLOW / BLOCK / REDACT

EGRESS (outbound content):
  Stage 1: Regex rules for IPs, keys, paths, ports → findings list
  Stage 2: spark_guard.assess_outbound() → sandboxed Codex → ALLOW / BLOCK / REDACT
"""

import logging
import re

logger = logging.getLogger("kukuibot.content-guard")

# Egress rules (also used by server endpoints directly)
EGRESS_RULES = [
    ("IPv4 address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("localhost/private host", re.compile(r"\b(?:localhost|127\.0\.0\.1|\.local\b)\b", re.IGNORECASE)),
    ("explicit port", re.compile(r"\bport\s*[:#]?\s*\d{2,5}\b", re.IGNORECASE)),
    ("URL with port", re.compile(r"https?://[^\s/:]+:\d{2,5}\b", re.IGNORECASE)),
    ("email address", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
    ("filesystem path", re.compile(r"(?:/Users/|/home/|~\/|[A-Za-z]:\\\\)[^\s]*")),
    ("API key/token label", re.compile(r"\b(?:api[_-]?key|app[_-]?password|access[_-]?token|auth[_-]?token|secret|private[_-]?key)\b", re.IGNORECASE)),
    ("secret prefix", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")),
    ("SSH key marker", re.compile(r"BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY")),
    ("device/account ID", re.compile(r"\b(?:device\s*id|account\s*id|user\s*id|uuid)\b", re.IGNORECASE)),
]


def scan_and_filter(text: str, source: str = "unknown") -> str:
    """
    Full two-stage ingress scan. Returns filtered text.
    
    LEGIT → original text
    INJECTION → blocked message
    SUSPICIOUS (Stage 2 unavailable) → text with warning note
    """
    if not text or len(text.strip()) < 20:
        return text

    from injection_guard import scan_text

    # Stage 1
    first_pass = scan_text(text, source=source)

    if first_pass["verdict"] == "LEGIT":
        return text

    # High-confidence injection with patterns — auto-block, skip Stage 2
    if (first_pass["verdict"] == "INJECTION"
            and first_pass.get("confidence", 0) >= 0.99
            and len(first_pass.get("pattern_matches", [])) >= 2):
        conf = first_pass["confidence"]
        return f"[CONTENT BLOCKED: Prompt injection detected in {source} (confidence: {conf:.0%}). Filtered for safety.]"

    # Stage 2: Spark tiebreaker
    try:
        from spark_guard import assess_inbound
        assessment = assess_inbound(text, first_pass)
        action = assessment.get("action", "").upper()
        reason = assessment.get("reason", "")

        if action == "ALLOW":
            return text
        elif action == "BLOCK":
            return f"[CONTENT BLOCKED: {reason or 'Prompt injection detected'} (source: {source}). Filtered for safety.]"
        elif action == "REDACT":
            return f"[NOTE: Content partially flagged — {reason}]\n{text}"
    except Exception as e:
        logger.warning(f"[content-guard] Spark Stage 2 failed: {e}")

    # Fallback: Stage 2 failed, use Stage 1 verdict
    if first_pass["verdict"] == "INJECTION":
        conf = first_pass.get("confidence", 0)
        return f"[CONTENT BLOCKED: Prompt injection detected in {source} (confidence: {conf:.0%}). Filtered for safety.]"

    return f"[NOTE: Content flagged as suspicious but could not be confirmed.]\n{text}"


def scan_egress(text: str) -> tuple[bool, list[dict]]:
    """Stage 1 egress: regex scan for sensitive data. Returns (is_clean, findings)."""
    findings = []
    for rule_name, pattern in EGRESS_RULES:
        for m in pattern.finditer(text):
            findings.append({
                "rule": rule_name,
                "match": m.group(),
                "position": m.start(),
                "preview": text[max(0, m.start() - 20):m.end() + 20].replace("\n", " "),
            })
    return (len(findings) == 0, findings)


def preflight_email(subject: str, body: str) -> tuple[bool, list[dict]]:
    """Quick email preflight (Stage 1 regex only)."""
    combined = f"Subject:\n{subject}\n\nBody:\n{body}"
    return scan_egress(combined)
