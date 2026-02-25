#!/usr/bin/env python3
"""Generate ~/.kukuibot/PROJECT-REPORT.md from deterministic project signals.

Data sources (rule-based only):
- ROADMAP.md outstanding priorities + architecture summary
- git log (last 48h)
- docs/LESSONS-LEARNED.md critical anti-pattern headings
- SQLite chat logs via log_store.log_query() for decision signals

Usage:
  python3 src/generate_project_report.py
  python3 src/generate_project_report.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import KUKUIBOT_HOME, WORKSPACE
from log_store import log_query

MAX_REPORT_CHARS = 5500
TARGET_MIN_CHARS = 3000
MAX_PRIORITIES = 5
MAX_COMMITS = 8
MAX_ANTI_PATTERNS = 5
MAX_DECISIONS = 3
MAX_ARCH_NOTES = 10

PST = ZoneInfo("America/Los_Angeles")


@dataclass
class PriorityItem:
    priority: str  # P0/P1
    title: str
    status: str
    notes: str


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _extract_section(text: str, heading: str) -> str:
    """Extract markdown section body for an exact heading line.

    Rules:
    - For `##` headings, stop at the next `##` heading (allow nested `###` inside).
    - For `###` headings, stop at the next `###` or `##` heading.
    """
    marker = heading.strip()
    if marker.startswith("###"):
        stop = r"(?=^\s*##\s+|^\s*###\s+|\Z)"
    else:
        stop = r"(?=^\s*##\s+|\Z)"

    pattern = re.compile(rf"(?ms)^\s*{re.escape(marker)}\s*$\n(.*?){stop}")
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _parse_markdown_table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw in section.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        if re.match(r"^\|\s*-+", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0] == "#" and cells[1].lower() == "item":
            continue
        rows.append(cells)
    return rows


def _clean_md(text: str) -> str:
    t = re.sub(r"`+", "", text or "")
    t = re.sub(r"\*\*|__", "", t)
    t = re.sub(r"~~", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _collect_priorities(roadmap_text: str) -> list[PriorityItem]:
    items: list[PriorityItem] = []
    section_map = [
        ("### High Priority", "P0"),
        ("### Medium Priority", "P1"),
    ]

    for heading, p in section_map:
        section = _extract_section(roadmap_text, heading)
        for row in _parse_markdown_table_rows(section):
            title = _clean_md(row[1])
            status = _clean_md(row[2])
            notes = _clean_md(row[3])
            row_blob = " | ".join(row)
            if "~~" in row_blob:
                continue
            if not title:
                continue
            if status.lower() in {"done", "obsolete"}:
                continue
            items.append(PriorityItem(priority=p, title=title, status=status, notes=notes))

    return items[:MAX_PRIORITIES]


def _run_git_log_since_48h(repo_dir: Path) -> list[tuple[str, str]]:
    try:
        out = subprocess.check_output(
            [
                "git",
                "log",
                "--since=48 hours ago",
                "--pretty=format:%h\t%s",
                f"--max-count={MAX_COMMITS}",
            ],
            cwd=str(repo_dir),
            text=True,
        ).strip()
    except Exception:
        return []

    commits: list[tuple[str, str]] = []
    if not out:
        return commits

    for line in out.splitlines():
        if "\t" not in line:
            continue
        sha, subj = line.split("\t", 1)
        subj = re.sub(
            r"^(feat|fix|chore|docs|refactor|perf|test|ci|build)(\([^)]+\))?:\s*",
            "",
            subj,
            flags=re.IGNORECASE,
        )
        commits.append((sha.strip(), _clean_md(subj)))
    return commits[:MAX_COMMITS]


def _collect_anti_patterns(lessons_text: str) -> list[str]:
    section = _extract_section(lessons_text, "## Critical Anti-Patterns (DO NOT REPEAT)")
    if not section:
        # fallback if heading text varies slightly
        section = _extract_section(lessons_text, "## Critical Anti-Patterns")

    headings = re.findall(r"(?m)^###\s+\d+\.\s+(.+)$", section)
    cleaned = [_clean_md(h) for h in headings if h.strip()]
    return cleaned[:MAX_ANTI_PATTERNS]


def _first_signal_line(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            line = re.sub(r"\s+", " ", line)
            return line[:220] + ("..." if len(line) > 220 else "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:220] + ("..." if len(text) > 220 else "")


def _collect_key_decisions() -> list[str]:
    patterns = [
        re.compile(r"\bdecided to\b", re.IGNORECASE),
        re.compile(r"\bgoing with\b", re.IGNORECASE),
        re.compile(r"\bapproach:\b", re.IGNORECASE),
    ]

    since = time.time() - (7 * 24 * 3600)
    rows = log_query(category="chat", role="assistant", since_unix=since, limit=4000, order="DESC")

    decisions: list[str] = []
    seen: set[str] = set()

    for row in rows:
        msg = row.get("message", "")
        if not msg:
            continue
        if not any(p.search(msg) for p in patterns):
            continue

        snippet = _clean_md(_first_signal_line(msg))
        if len(snippet) < 20:
            continue

        key = snippet.lower()
        if key in seen:
            continue
        seen.add(key)
        decisions.append(snippet)
        if len(decisions) >= MAX_DECISIONS:
            break

    # Present in chronological order for readability
    return list(reversed(decisions))


def _collect_architecture_notes(roadmap_text: str) -> list[str]:
    section = _extract_section(roadmap_text, "## Architecture Summary")
    if not section:
        return []

    notes: list[str] = []

    server_py = WORKSPACE / "src" / "server.py"
    try:
        line_count = len(server_py.read_text(encoding="utf-8").splitlines())
        notes.append(
            f"server.py currently has {line_count:,} lines; roadmap still flags module-splitting as a maintainability priority."
        )
    except Exception:
        pass

    line_map = {
        "FastAPI (server.py)": "Runtime is a single FastAPI/uvicorn process on HTTPS port 7000 with UI + API + routing in one service.",
        "Log Store (log_store.py)": "Log storage is SQLite-backed (logs/kukuibot-logs.db) and used as durable chat/system history.",
        "Delegation (delegation.py)": "Cross-worker delegation is a first-class subsystem and a current reliability focus area.",
        "Compaction (compaction.py)": "Compaction is programmatic (no LLM summarizer) with context re-injection for continuity.",
        "Claude Bridge (claude_bridge.py)": "Claude sessions run through a persistent subprocess bridge with per-tab context behavior.",
        "OpenRouter Bridge (openrouter_bridge.py)": "OpenRouter models are integrated through a dedicated streaming bridge.",
        "Storage: ~/.kukuibot/": "Primary state is under ~/.kukuibot, including identity files, DBs, logs, memory, and worker profiles.",
        "kukuibot.db": "kukuibot.db remains the core relational store for auth, sessions, history, and runtime config.",
    }

    for raw in section.splitlines():
        line = raw.strip()
        for key, value in line_map.items():
            if key in line and value not in notes:
                notes.append(value)

    # Provider table summary inside Architecture Summary
    provider_rows = re.findall(r"(?m)^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$", section)
    provider_bits = []
    for name, model, status in provider_rows:
        n = _clean_md(name)
        if n.lower() in {"provider", "----------"}:
            continue
        provider_bits.append(f"{n}: {_clean_md(model)} ({_clean_md(status)})")
    if provider_bits:
        notes.append("Providers snapshot: " + "; ".join(provider_bits[:4]) + ".")

    # Keep deterministic max
    return notes[:MAX_ARCH_NOTES]


def _build_report(
    *,
    generated_at: datetime,
    priorities: list[PriorityItem],
    commits: list[tuple[str, str]],
    anti_patterns: list[str],
    decisions: list[str],
    architecture_notes: list[str],
) -> str:
    ts = generated_at.astimezone(PST).strftime("%Y-%m-%d %H:%M %Z")

    priority_lines = [
        f"{idx}. [{item.priority}] {item.title} — Status: {item.status}. {item.notes}".strip()
        for idx, item in enumerate(priorities, start=1)
    ] or ["1. [P0] No active high/medium priorities parsed from ROADMAP."]

    completion_lines = [
        f"- commit {sha}: {subject}" for sha, subject in commits
    ] or ["- No commits found in the last 48 hours."]

    anti_pattern_lines = [f"- {ap}" for ap in anti_patterns] or ["- No anti-pattern headings parsed."]
    decision_lines = [f"- {d}" for d in decisions] or ["- No explicit decision signals found in recent assistant logs."]
    arch_lines = [f"- {n}" for n in architecture_notes] or ["- Architecture Summary section not found in ROADMAP."]

    report = (
        "# Project Report\n"
        f"_Auto-generated: {ts}_\n\n"
        "## Current Priorities\n"
        + "\n".join(priority_lines)
        + "\n\n## Recent Completions (Last 48h)\n"
        + "\n".join(completion_lines)
        + "\n\n## Active Anti-Patterns\n"
        + "\n".join(anti_pattern_lines)
        + "\n\n## Key Decisions\n"
        + "\n".join(decision_lines)
        + "\n\n## Architecture Notes\n"
        + "\n".join(arch_lines)
        + "\n"
    )

    # Keep under hard max by trimming lower-priority sections first.
    while len(report) > MAX_REPORT_CHARS and len(architecture_notes) > 2:
        architecture_notes = architecture_notes[:-1]
        arch_lines = [f"- {n}" for n in architecture_notes]
        report = report.rsplit("## Architecture Notes\n", 1)[0] + "## Architecture Notes\n" + "\n".join(arch_lines) + "\n"

    if len(report) > MAX_REPORT_CHARS and len(decisions) > 1:
        decisions = decisions[:1]
        decision_lines = [f"- {d}" for d in decisions]
        report = (
            "# Project Report\n"
            f"_Auto-generated: {ts}_\n\n"
            "## Current Priorities\n"
            + "\n".join(priority_lines)
            + "\n\n## Recent Completions (Last 48h)\n"
            + "\n".join(completion_lines)
            + "\n\n## Active Anti-Patterns\n"
            + "\n".join(anti_pattern_lines)
            + "\n\n## Key Decisions\n"
            + "\n".join(decision_lines)
            + "\n\n## Architecture Notes\n"
            + "\n".join([f"- {n}" for n in architecture_notes])
            + "\n"
        )

    if len(report) > MAX_REPORT_CHARS:
        report = report[: MAX_REPORT_CHARS - 17].rstrip() + "\n... (truncated)\n"

    # If too short, pad architecture notes with stable source context snippets.
    if len(report) < TARGET_MIN_CHARS:
        pad = (
            "\n"
            "- Reliability focus remains delegation notification delivery, wake preemption behavior, and state consistency between SQLite and runtime caches.\n"
            "- Context continuity depends on shared identity files (SOUL/USER/TOOLS), per-model/worker profiles, and chat-tail recovery through SQLite logs.\n"
            "- Current roadmap cadence indicates rapid iteration; anti-pattern discipline is required to avoid repeat regressions in high-churn areas.\n"
        )
        if len(report) + len(pad) <= MAX_REPORT_CHARS:
            report += pad

    return report


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def generate_project_report(now: datetime | None = None) -> str:
    now = now or datetime.now(PST)

    roadmap_text = _read_text(KUKUIBOT_HOME / "ROADMAP.md")
    lessons_text = _read_text(WORKSPACE / "docs" / "LESSONS-LEARNED.md")

    priorities = _collect_priorities(roadmap_text)
    commits = _run_git_log_since_48h(WORKSPACE)
    anti_patterns = _collect_anti_patterns(lessons_text)
    decisions = _collect_key_decisions()
    architecture_notes = _collect_architecture_notes(roadmap_text)

    return _build_report(
        generated_at=now,
        priorities=priorities,
        commits=commits,
        anti_patterns=anti_patterns,
        decisions=decisions,
        architecture_notes=architecture_notes,
    )


def write_project_report(report: str, now: datetime | None = None) -> tuple[Path, Path]:
    now = now or datetime.now(PST)
    project_report_path = KUKUIBOT_HOME / "PROJECT-REPORT.md"
    archive_path = KUKUIBOT_HOME / "daily_reports" / f"{now.date().isoformat()}-project-report.md"

    _atomic_write(project_report_path, report)
    _atomic_write(archive_path, report)
    return project_report_path, archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate ~/.kukuibot/PROJECT-REPORT.md")
    parser.add_argument("--dry-run", action="store_true", help="Print report to stdout without writing files")
    args = parser.parse_args(argv)

    report = generate_project_report()

    if args.dry_run:
        print(report)
        print(f"\n[chars={len(report)}]", flush=True)
        return 0

    out_path, archive_path = write_project_report(report)
    print(f"Wrote {out_path} ({len(report)} chars)")
    print(f"Archive: {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
