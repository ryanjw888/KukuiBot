"""nuclei_parser.py — Parse Nuclei JSONL output into structured findings."""

import json
from pathlib import Path


def parse_nuclei_jsonl(path: str | Path) -> list[dict]:
    """Parse Nuclei JSONL output file, return list of finding dicts."""
    findings = []
    try:
        text = Path(path).read_text()
    except (FileNotFoundError, OSError):
        return []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue

        finding = _normalize_finding(raw)
        if finding:
            findings.append(finding)

    return findings


def parse_nuclei_jsonl_string(text: str) -> list[dict]:
    """Parse Nuclei JSONL from a string."""
    findings = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        finding = _normalize_finding(raw)
        if finding:
            findings.append(finding)
    return findings


def _normalize_finding(raw: dict) -> dict | None:
    """Normalize a single Nuclei JSON finding to our schema."""
    template_id = raw.get("template-id", raw.get("templateID", ""))
    if not template_id:
        return None

    info = raw.get("info", {})
    severity = info.get("severity", raw.get("severity", "info")).lower()
    if severity not in ("critical", "high", "medium", "low", "info"):
        severity = "info"

    # Extract host and port from matched-at
    matched_at = raw.get("matched-at", raw.get("matched", ""))
    host = raw.get("host", raw.get("ip", ""))
    port = raw.get("port", 0)

    if not host and matched_at:
        # Try to extract host from URL
        if "://" in matched_at:
            host_part = matched_at.split("://", 1)[1].split("/")[0].split(":")[0]
            host = host_part
        else:
            host = matched_at.split(":")[0] if ":" in matched_at else matched_at

    if not port and matched_at and ":" in matched_at:
        try:
            # Try URL port
            if "://" in matched_at:
                host_port = matched_at.split("://", 1)[1].split("/")[0]
                if ":" in host_port:
                    port = int(host_port.split(":")[-1])
            else:
                port = int(matched_at.split(":")[-1])
        except (ValueError, IndexError):
            pass

    # Classification
    tags = info.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]

    finding_type = "misc"
    for tag in tags:
        tag_lower = tag.lower()
        if "cve" in tag_lower:
            finding_type = "cve"
            break
        elif "default-login" in tag_lower or "default-credential" in tag_lower:
            finding_type = "default-login"
            break
        elif "exposure" in tag_lower or "exposed" in tag_lower:
            finding_type = "exposure"
            break
        elif "misconfig" in tag_lower:
            finding_type = "misconfig"
            break

    if finding_type == "misc" and template_id.startswith("CVE-"):
        finding_type = "cve"

    return {
        "template_id": template_id,
        "name": info.get("name", raw.get("name", template_id)),
        "severity": severity,
        "type": finding_type,
        "host": host,
        "port": port,
        "matched_at": matched_at,
        "extracted_results": raw.get("extracted-results", raw.get("extractedResults", [])),
        "description": info.get("description", raw.get("description", "")),
        "reference": info.get("reference", raw.get("reference", [])),
    }
