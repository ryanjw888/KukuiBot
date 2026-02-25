#!/usr/bin/env python3
"""Nightly AI-powered project analysis — produces PROJECT-REPORT.md via Opus analyst.

Dispatches a prompt to a dedicated Claude Opus delegation session
(deleg-claude_opus-nightly-analyst-1) with full project context, streams the
response, extracts the generated report, and writes it to ~/.kukuibot/PROJECT-REPORT.md.

Falls back to the rule-based generate_project_report.py if the AI session fails
or times out.

Exit codes:
  0 — AI analysis succeeded
  1 — Fell back to rule-based report
  2 — Total failure (both AI and rule-based failed)

Usage:
  python3 src/nightly_project_analysis.py             # Full run
  python3 src/nightly_project_analysis.py --dry-run   # Print prompt, don't dispatch
  python3 src/nightly_project_analysis.py --timeout 180  # Custom timeout (seconds)
  python3 src/nightly_project_analysis.py --send-email --to user@example.com  # Email report
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KUKUIBOT_HOME = Path(os.path.expanduser("~/.kukuibot"))
WORKSPACE = KUKUIBOT_HOME
SRC_DIR = KUKUIBOT_HOME / "src"
PROJECT_REPORT_PATH = KUKUIBOT_HOME / "PROJECT-REPORT.md"
ARCHIVE_DIR = KUKUIBOT_HOME / "daily_reports"
MEMORY_PATH = KUKUIBOT_HOME / "nightly-analysis-memory.md"
LOG_PATH = KUKUIBOT_HOME / "logs" / "nightly-analysis.log"

API_BASE = "https://localhost:7000"
GMAIL_SEND_URL = "https://localhost:7000/api/gmail/send"
SESSION_ID = "deleg-claude_opus-nightly-analyst-1"
TASK_ID_PREFIX = "nightly"
DEFAULT_TIMEOUT = 300  # 5 minutes

PST = ZoneInfo("America/Los_Angeles")

# Add src/ to path for fallback import
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _log(msg: str) -> None:
    ts = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_text(path: Path, max_chars: int = 0) -> str:
    try:
        text = path.read_text(encoding="utf-8")
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return text
    except Exception:
        return ""


def _git_log_48h() -> str:
    """Get git log for the last 48 hours."""
    try:
        out = subprocess.check_output(
            [
                "git", "log",
                "--since=48 hours ago",
                "--pretty=format:%h %s",
                "--max-count=20",
            ],
            cwd=str(WORKSPACE),
            text=True,
            timeout=10,
        ).strip()
        return out or "(no commits in the last 48 hours)"
    except Exception as e:
        return f"(git log failed: {e})"


def _git_diff_stat_48h() -> str:
    """Get diffstat summary for context on what areas were touched."""
    try:
        out = subprocess.check_output(
            [
                "git", "diff",
                "--stat",
                "HEAD~20..HEAD",
                "--",
            ],
            cwd=str(WORKSPACE),
            text=True,
            timeout=10,
        ).strip()
        # Keep just the summary line
        lines = out.splitlines()
        if lines:
            return lines[-1].strip()
        return ""
    except Exception:
        return ""


def _build_prompt(task_id: str) -> str:
    """Build the analyst prompt with all context injected."""
    now = datetime.now(PST)
    ts = now.strftime("%Y-%m-%d %H:%M %Z")

    git_log = _git_log_48h()
    diff_stat = _git_diff_stat_48h()
    roadmap = _read_text(KUKUIBOT_HOME / "ROADMAP.md", max_chars=6000)
    lessons = _read_text(WORKSPACE / "docs" / "LESSONS-LEARNED.md", max_chars=4000)
    prev_report = _read_text(PROJECT_REPORT_PATH, max_chars=5000)
    memory = _read_text(MEMORY_PATH, max_chars=2000)

    # Count server.py lines for architecture notes
    server_lines = ""
    try:
        count = len((SRC_DIR / "server.py").read_text(encoding="utf-8").splitlines())
        server_lines = f"\nserver.py line count: {count:,}"
    except Exception:
        pass

    prompt = f"""You are the Nightly Analyst. Generate a fresh PROJECT-REPORT.md based on the context below.

Current timestamp: {ts}
Task ID: {task_id}
{server_lines}

---
## Git Log (Last 48h)
{git_log}

{f"Diff summary: {diff_stat}" if diff_stat else ""}

---
## ROADMAP.md
{roadmap if roadmap else "(not found)"}

---
## LESSONS-LEARNED.md (Anti-Patterns)
{lessons if lessons else "(not found)"}

---
## Previous PROJECT-REPORT.md
{prev_report if prev_report else "(no previous report)"}

---
## Analysis Memory (Rolling Notes)
{memory if memory else "(no previous memory)"}

---

Now produce the complete PROJECT-REPORT.md content. Follow your worker identity instructions exactly.
Output ONLY the markdown — no preamble, no commentary.
After the report, end with: TASK_DONE {task_id}"""

    return prompt


def _dispatch_and_stream(prompt: str, task_id: str, timeout: int) -> str | None:
    """POST to /api/chat and stream the response. Returns full text or None on failure."""
    payload = json.dumps({
        "session_id": SESSION_ID,
        "message": prompt,
    }).encode("utf-8")

    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        f"{API_BASE}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    _log(f"Dispatching to {SESSION_ID} (timeout={timeout}s)")

    try:
        resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
    except Exception as e:
        _log(f"HTTP request failed: {e}")
        return None

    # Stream SSE events and collect text
    full_text = []
    done_text = None
    deadline = time.time() + timeout

    try:
        while time.time() < deadline:
            line = resp.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()

            if not decoded.startswith("data: "):
                continue

            try:
                evt = json.loads(decoded[6:])
            except (json.JSONDecodeError, ValueError):
                continue

            evt_type = evt.get("type", "")

            if evt_type == "text":
                chunk = evt.get("text", "")
                if chunk:
                    full_text.append(chunk)

            elif evt_type == "done":
                done_text = evt.get("text", "")
                break

            elif evt_type == "error":
                _log(f"Error event: {evt.get('message', 'unknown')}")
                return None

    except Exception as e:
        _log(f"Stream error: {e}")
        # Fall through — we may have partial text
    finally:
        try:
            resp.close()
        except Exception:
            pass

    # Prefer the done event's full text, fall back to accumulated chunks
    result = done_text if done_text else "".join(full_text)

    if not result or len(result.strip()) < 100:
        _log(f"Response too short ({len(result.strip()) if result else 0} chars)")
        return None

    # Verify TASK_DONE marker
    if f"TASK_DONE {task_id}" not in result and f"TASK_DONE {TASK_ID_PREFIX}" not in result:
        _log("Warning: TASK_DONE marker not found in response (using response anyway)")

    return result


def _extract_report(raw: str, task_id: str) -> str:
    """Extract the PROJECT-REPORT.md content from the AI response."""
    # Strip the TASK_DONE line
    text = re.sub(rf"TASK_DONE\s+{re.escape(task_id)}\s*$", "", raw, flags=re.MULTILINE).strip()

    # Find the report start
    match = re.search(r"^# Project Report", text, re.MULTILINE)
    if match:
        text = text[match.start():]

    # Strip any trailing commentary after the report
    # The report should end after Architecture Notes section content
    lines = text.splitlines()
    report_lines = []
    in_report = False
    for line in lines:
        if line.startswith("# Project Report"):
            in_report = True
        if in_report:
            report_lines.append(line)

    result = "\n".join(report_lines).strip() + "\n"

    # Sanity check
    if len(result) < 200:
        _log(f"Extracted report suspiciously short ({len(result)} chars)")
        return ""

    if len(result) > 6000:
        _log(f"Extracted report too long ({len(result)} chars), truncating")
        result = result[:5500].rstrip() + "\n"

    return result


def _update_memory(report: str, now: datetime) -> None:
    """Maintain rolling memory file with last 3 report summaries."""
    date_str = now.strftime("%Y-%m-%d")

    # Extract just the priorities and completions sections for memory
    priorities = ""
    completions = ""
    for section_name, target in [("Current Priorities", "priorities"), ("Recent Completions", "completions")]:
        match = re.search(rf"## {section_name}\n(.*?)(?=\n## |\Z)", report, re.DOTALL)
        if match:
            if target == "priorities":
                priorities = match.group(1).strip()
            else:
                completions = match.group(1).strip()

    entry = f"### {date_str}\nPriorities: {priorities[:300]}\nCompletions: {completions[:300]}\n"

    existing = _read_text(MEMORY_PATH)
    entries = re.split(r"(?=^### \d{4}-\d{2}-\d{2})", existing, flags=re.MULTILINE)
    entries = [e.strip() for e in entries if e.strip() and e.strip().startswith("### ")]

    # Prepend new entry, keep last 3
    entries.insert(0, entry.strip())
    entries = entries[:3]

    content = "# Nightly Analysis Memory\n\n" + "\n\n".join(entries) + "\n"

    try:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = MEMORY_PATH.with_name(f".{MEMORY_PATH.name}.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(MEMORY_PATH)
    except Exception as e:
        _log(f"Failed to update memory: {e}")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _wrap_markdown_as_html(markdown: str, date_str: str) -> str:
    """Wrap markdown report in a dark-themed HTML email template."""
    import html as html_mod
    escaped = html_mod.escape(markdown)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#1a1a2e;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:680px;margin:0 auto;padding:24px;">
<h1 style="color:#f8fafc;font-size:20px;margin-bottom:4px;">KukuiBot Project Report</h1>
<p style="color:#a8b2d1;font-size:13px;margin-top:0;">{date_str}</p>
<div style="background:#1e1e38;border-radius:8px;padding:20px;margin-top:16px;">
<pre style="color:#f8fafc;font-family:'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;font-size:13px;line-height:1.6;white-space:pre-wrap;word-wrap:break-word;margin:0;">{escaped}</pre>
</div>
<p style="color:#555;font-size:11px;text-align:center;margin-top:24px;">Generated by KukuiBot &middot; Nightly Project Analysis</p>
</div>
</body>
</html>"""


def send_email(*, to: str, subject: str, body_html: str, dry_run: bool) -> None:
    """Send email via KukuiBot Gmail API endpoint."""
    payload = json.dumps({"to": to, "subject": subject, "body": body_html}).encode("utf-8")
    if dry_run:
        print("[dry-run] would send email:")
        print(f"to={to}")
        print(f"subject={subject}")
        return

    req = urllib.request.Request(
        GMAIL_SEND_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
        text = resp.read().decode("utf-8", "ignore")
        _log(f"Email sent: {text}")


def _fallback_rule_based() -> int:
    """Run the rule-based report generator as fallback. Returns exit code."""
    _log("Falling back to rule-based report generator")
    try:
        from generate_project_report import generate_project_report, write_project_report
        report = generate_project_report()
        out_path, archive_path = write_project_report(report)
        _log(f"Rule-based fallback wrote {out_path} ({len(report)} chars)")
        return 1  # Exit code 1 = fell back to rule-based
    except Exception as e:
        _log(f"Rule-based fallback also failed: {e}")
        return 2  # Exit code 2 = total failure


def _clear_session_history() -> None:
    """Clear the analyst session history before dispatch to start fresh."""
    try:
        from auth import clear_history
        clear_history(SESSION_ID)
        _log(f"Cleared session history for {SESSION_ID}")
    except Exception as e:
        _log(f"Failed to clear session history (non-fatal): {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly AI-powered project analysis")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt without dispatching")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="AI timeout in seconds")
    parser.add_argument("--skip-fallback", action="store_true", help="Don't fall back to rule-based on AI failure")
    parser.add_argument("--send-email", action="store_true", help="Email the report after generation")
    parser.add_argument("--to", default="user@example.com", help="Email recipient (default: user@example.com)")
    args = parser.parse_args(argv)

    now = datetime.now(PST)
    task_id = f"{TASK_ID_PREFIX}-{now.strftime('%Y%m%d-%H%M%S')}"

    _log(f"Starting nightly project analysis (task_id={task_id})")

    prompt = _build_prompt(task_id)

    if args.dry_run:
        print("=" * 60)
        print("PROMPT (dry-run):")
        print("=" * 60)
        print(prompt)
        print("=" * 60)
        print(f"\n[prompt_chars={len(prompt)}, session={SESSION_ID}, task_id={task_id}]")
        return 0

    # Clear previous session history for a clean context
    _clear_session_history()

    # Dispatch to AI analyst
    raw_response = _dispatch_and_stream(prompt, task_id, args.timeout)

    if raw_response:
        report = _extract_report(raw_response, task_id)
        if report:
            # Write the report
            _atomic_write(PROJECT_REPORT_PATH, report)
            archive_path = ARCHIVE_DIR / f"{now.date().isoformat()}-project-report.md"
            _atomic_write(archive_path, report)

            # Update rolling memory
            _update_memory(report, now)

            _log(f"AI analysis complete: {PROJECT_REPORT_PATH} ({len(report)} chars)")

            # Send email if requested
            if args.send_email:
                date_str = now.strftime("%Y-%m-%d")
                subject = f"KukuiBot Project Report — {date_str}"
                body_html = _wrap_markdown_as_html(report, date_str)
                try:
                    send_email(to=args.to, subject=subject, body_html=body_html, dry_run=args.dry_run)
                except Exception as e:
                    _log(f"Failed to send email: {e}")

            return 0
        else:
            _log("Failed to extract valid report from AI response")
    else:
        _log("AI analysis returned no usable response")

    # Fallback
    if args.skip_fallback:
        _log("Skipping fallback (--skip-fallback)")
        return 2

    return _fallback_rule_based()


if __name__ == "__main__":
    raise SystemExit(main())
