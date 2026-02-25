#!/usr/bin/env python3
"""Generate a nightly KukuiBot activity report from chat.log + git history.

Default window is "yesterday" (local calendar day), suitable for a 3am cron.
The report is always saved to ~/.kukuibot/daily_reports as HTML.
Optional email delivery is available via --send-email.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path

KUKUIBOT_HOME = Path(os.path.expanduser("~/.kukuibot"))
REPO_DIR = KUKUIBOT_HOME

# Add src/ to path for log_store import
_src_dir = str(KUKUIBOT_HOME / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


DAILY_REPORTS_DIR = KUKUIBOT_HOME / "daily_reports"
_PORT = os.environ.get("KUKUIBOT_PORT", "7000")
_API_BASE = os.environ.get("KUKUIBOT_URL", f"https://localhost:{_PORT}")
GMAIL_SEND_URL = f"{_API_BASE}/api/gmail/send"
DB_PATH = KUKUIBOT_HOME / "kukuibot.db"


def _db_config(key: str, default: str = "") -> str:
    """Read a config value from kukuibot.db (no dependency on server code)."""
    import sqlite3
    if not DB_PATH.exists():
        return default
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        row = con.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        con.close()
        return row[0] if row else default
    except Exception:
        return default

TODO_PATTERNS = [
    r"\bwant me\b",
    r"\bwe could\b",
    r"\bnext steps\b",
    r"\bneeds? (?:a )?restart\b",
    r"\bcan add\b",
    r"\bcould add\b",
    r"\bpending\b",
    r"\bwould you like me\b",
]

PROJECT_PATTERNS = [
    r"\bcompleted\b",
    r"\bshipped\b",
    r"\bimplemented\b",
    r"\bfixed\b",
    r"\badded\b",
    r"\bresolved\b",
]


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, cwd=str(REPO_DIR), text=True).strip()


def _parse_chat_window(since: dt.datetime, until: dt.datetime) -> tuple[list[str], list[str]]:
    """Returns (assistant_msgs, user_msgs) from SQLite log."""
    try:
        from log_store import log_query
        rows = log_query(
            category="chat",
            since_unix=since.timestamp(),
            until_unix=until.timestamp(),
            limit=100000,
            order="ASC",
        )
        assistant_msgs = []
        user_msgs = []
        for r in rows:
            role = (r.get("role") or "").lower()
            msg = r.get("message", "").strip()
            if not msg:
                continue
            if role == "assistant":
                assistant_msgs.append(msg)
            elif role == "user":
                user_msgs.append(msg)
        return assistant_msgs, user_msgs
    except Exception:
        return [], []


def _clean_line(text: str, limit: int = 170) -> str:
    line = text.strip().splitlines()[0].strip()
    line = re.sub(r"\s+", " ", line)
    if len(line) > limit:
        line = line[: limit - 3] + "..."
    return line


def _dedupe_keep_order(items: list[str], *, limit: int) -> list[str]:
    seen = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _collect_todos(assistant_msgs: list[str]) -> list[str]:
    todos: list[str] = []
    combined_re = re.compile("|".join(TODO_PATTERNS), re.IGNORECASE)

    for msg in assistant_msgs:
        if not combined_re.search(msg):
            continue
        todos.append(_clean_line(msg))

    return _dedupe_keep_order(todos, limit=8)


def _normalize_commit_subject(subject: str) -> str:
    # Remove conventional-commit style prefixes.
    s = re.sub(r"^(feat|fix|chore|docs|refactor|perf|test|ci|build)(\([^)]+\))?:\s*", "", subject, flags=re.IGNORECASE)
    return _clean_line(s, limit=150)


def _collect_project_highlights(assistant_msgs: list[str], commits: list[tuple[str, str]]) -> list[str]:
    projects: list[str] = []

    # Start from commit subjects (strong signal of completed work).
    for _h, subj in commits[:10]:
        normalized = _normalize_commit_subject(subj)
        if normalized and normalized != "—":
            projects.append(normalized)

    # Add assistant-completion statements from chat when available.
    patt = re.compile("|".join(PROJECT_PATTERNS), re.IGNORECASE)
    for msg in assistant_msgs:
        if patt.search(msg):
            projects.append(_clean_line(msg, limit=150))

    return _dedupe_keep_order(projects, limit=10)


def _git_commits(since: dt.datetime, until: dt.datetime) -> list[tuple[str, str]]:
    out = _run([
        "git",
        "log",
        f"--since={since.strftime('%Y-%m-%d %H:%M:%S')}",
        f"--until={until.strftime('%Y-%m-%d %H:%M:%S')}",
        "--pretty=format:%h\t%s",
    ])
    commits: list[tuple[str, str]] = []
    if not out:
        return commits
    for line in out.splitlines():
        if "\t" in line:
            h, s = line.split("\t", 1)
            commits.append((h.strip(), s.strip()))
    return commits


def _git_top_files(since: dt.datetime, until: dt.datetime, limit: int = 8) -> list[tuple[str, int]]:
    out = _run([
        "bash",
        "-lc",
        (
            "git log "
            f"--since='{since.strftime('%Y-%m-%d %H:%M:%S')}' "
            f"--until='{until.strftime('%Y-%m-%d %H:%M:%S')}' "
            "--name-only --pretty=format: | sed '/^$/d'"
        ),
    ])
    if not out:
        return []
    counts = Counter(out.splitlines())
    return counts.most_common(limit)


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def build_html_report(
    *,
    since: dt.datetime,
    until: dt.datetime,
    commits: list[tuple[str, str]],
    top_files: list[tuple[str, int]],
    carryover_todos: list[str],
    project_highlights: list[str],
) -> str:
    date_label = f"{since.strftime('%Y-%m-%d %H:%M')} → {until.strftime('%Y-%m-%d %H:%M')}"
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")

    todos_html = "".join(
        f"<li style='margin:6px 0;'>{_esc(t)}</li>"
        for t in (carryover_todos or ["No explicit follow-up items detected in the previous 24 hours."])
    )

    project_html = "".join(
        f"<li style='margin:6px 0;'>{_esc(p)}</li>"
        for p in (project_highlights or ["No explicit project-completion statements found."])
    )

    files_html = "".join(
        f"<li style='margin:6px 0;'><code style='background:#0d1117;padding:2px 6px;border-radius:6px;color:#dbeafe'>{_esc(path)}</code> — {_esc(str(count))} touch(es)</li>"
        for path, count in top_files
    ) or "<li style='margin:6px 0;'>No file changes detected in this window.</li>"

    commits_html = "".join(
        f"<li style='margin:6px 0;'><code style='background:#0d1117;padding:2px 6px;border-radius:6px;color:#dbeafe'>{_esc(h)}</code> — {_esc(s)}</li>"
        for h, s in (commits[:8] if commits else [("—", "No commits in the selected window")])
    )

    return f"""<!doctype html>
<html>
  <body style='margin:0;padding:0;background:#1a1a2e;color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'>
    <div style='max-width:680px;margin:24px auto;padding:0 12px;'>
      <div style='background:#1e1e38;border:1px solid #2b2b52;border-radius:14px;padding:20px 22px;'>
        <h1 style='margin:0 0 6px 0;font-size:22px;'>🌙 Nightly KukuiBot Activity Report</h1>
        <p style='margin:0;color:#a8b2d1;font-size:14px;'>Summary window: <b>{_esc(date_label)}</b> (yesterday)</p>
      </div>

      <div style='display:flex;gap:10px;flex-wrap:wrap;margin-top:12px;'>
        <div style='flex:1;min-width:140px;background:#1e1e38;border:1px solid #2b2b52;border-radius:10px;padding:10px 12px;'><div style='color:#8ab4ff;font-size:12px;'>Carryover To-Dos (last 24h)</div><div style='font-size:20px;font-weight:700;'>{len(carryover_todos)}</div></div>
        <div style='flex:1;min-width:140px;background:#1e1e38;border:1px solid #2b2b52;border-radius:10px;padding:10px 12px;'><div style='color:#8ab4ff;font-size:12px;'>Projects Highlighted</div><div style='font-size:20px;font-weight:700;'>{len(project_highlights)}</div></div>
        <div style='flex:1;min-width:140px;background:#1e1e38;border:1px solid #2b2b52;border-radius:10px;padding:10px 12px;'><div style='color:#8ab4ff;font-size:12px;'>Key Files</div><div style='font-size:20px;font-weight:700;'>{len(top_files)}</div></div>
      </div>

      <div style='margin-top:12px;background:#1e1e38;border-left:4px solid #f59e0b;border-radius:10px;padding:14px 16px;'>
        <div style='font-weight:700;color:#fbbf24;margin-bottom:8px;'>🟡 Carryover To-Do Items (from prior 24h)</div>
        <ul style='margin:0 0 0 18px;padding:0;line-height:1.6;color:#e2e8f0;'>
          {todos_html}
        </ul>
      </div>

      <div style='margin-top:12px;background:#1e1e38;border-left:4px solid #22c55e;border-radius:10px;padding:14px 16px;'>
        <div style='font-weight:700;color:#86efac;margin-bottom:8px;'>✅ Key Projects Completed</div>
        <ul style='margin:0 0 0 18px;padding:0;line-height:1.6;color:#e2e8f0;'>
          {project_html}
        </ul>
      </div>

      <div style='margin-top:12px;background:#1e1e38;border:1px solid #2b2b52;border-radius:10px;padding:14px 16px;'>
        <div style='font-weight:700;margin-bottom:8px;'>📁 Key Files Worked On</div>
        <ul style='margin:0 0 0 18px;padding:0;line-height:1.65;color:#e2e8f0;'>
          {files_html}
        </ul>
      </div>

      <div style='margin-top:12px;background:#1e1e38;border:1px solid #2b2b52;border-radius:10px;padding:14px 16px;'>
        <div style='font-weight:700;margin-bottom:8px;'>🧷 Notable Commits</div>
        <ul style='margin:0 0 0 18px;padding:0;line-height:1.65;color:#e2e8f0;'>
          {commits_html}
        </ul>
      </div>

      <div style='margin-top:14px;color:#8b93b8;font-size:12px;text-align:center;'>Generated by <b>KukuiBot</b> • {now}</div>
    </div>
  </body>
</html>"""


def _save_report(*, html_report: str, report_date: dt.date) -> Path:
    DAILY_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DAILY_REPORTS_DIR / f"{report_date.isoformat()}-nightly-report.html"
    out_path.write_text(html_report, encoding="utf-8")
    return out_path


def send_email(*, to: str, subject: str, body_html: str, dry_run: bool) -> None:
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
        print(text)


def _resolve_window(window: str, hours: int) -> tuple[dt.datetime, dt.datetime, dt.date]:
    now = dt.datetime.now()

    if window == "yesterday":
        yesterday = now.date() - dt.timedelta(days=1)
        since = dt.datetime.combine(yesterday, dt.time.min)
        until = since + dt.timedelta(days=1)
        return since, until, yesterday

    since = now - dt.timedelta(hours=hours)
    until = now
    return since, until, since.date()


def main() -> int:
    p = argparse.ArgumentParser(description="Generate a nightly KukuiBot activity report")
    p.add_argument("--window", choices=["yesterday", "hours"], default="yesterday")
    p.add_argument("--hours", type=int, default=24, help="Used when --window hours")
    p.add_argument("--to", default="", help="Recipient email (overrides DB config)")
    p.add_argument("--send-email", action="store_true", help="Also email the HTML report")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--ignore-enabled", action="store_true", help="Run even if disabled in settings")
    args = p.parse_args()

    # Check DB config — respect enabled toggle unless overridden
    if not args.ignore_enabled and _db_config("nightly_report.enabled", "1") != "1":
        print("disabled=true (nightly report disabled in settings)")
        return 0

    # Use DB config for email if not provided via CLI
    if not args.to:
        args.to = _db_config("nightly_report.to_email", "user@example.com") or "user@example.com"
    if args.send_email and _db_config("nightly_report.send_email", "1") != "1":
        args.send_email = False

    since, until, report_date = _resolve_window(args.window, args.hours)
    now = dt.datetime.now()

    assistant_window_msgs, _user_msgs = _parse_chat_window(since, until)
    assistant_last24_msgs, _ = _parse_chat_window(now - dt.timedelta(hours=24), now)

    commits = _git_commits(since, until)

    # Skip entirely if there's no activity to report
    if not assistant_window_msgs and not _user_msgs and not commits:
        print(f"no_activity=true window={since.isoformat()}..{until.isoformat()}")
        return 0

    carryover_todos = _collect_todos(assistant_last24_msgs)
    top_files = _git_top_files(since, until)
    project_highlights = _collect_project_highlights(assistant_window_msgs, commits)

    html_report = build_html_report(
        since=since,
        until=until,
        commits=commits,
        top_files=top_files,
        carryover_todos=carryover_todos,
        project_highlights=project_highlights,
    )

    out_path = _save_report(html_report=html_report, report_date=report_date)
    print(f"saved_report={out_path}")

    if args.send_email:
        subject = f"Nightly KukuiBot Report — {report_date.isoformat()}"
        send_email(to=args.to, subject=subject, body_html=html_report, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
