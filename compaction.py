"""
compaction.py — Smart compaction for KukuiBot.

Re-injects the same context as a fresh tab (SOUL, USER, TOOLS, Model Identity,
Worker Identity) plus the last 20KB of the per-worker chat log for continuity.
No LLM call — purely programmatic.

The append-only chat log preserves everything permanently.
"""

import logging
from datetime import datetime
from pathlib import Path

from config import (
    COMPACTION_LOG_FILE,
    COMPACTION_LOG_MAX_LINES,
    KUKUIBOT_HOME,
    RECENT_MESSAGES_TO_KEEP,
    WORKSPACE,
)
from log_store import log_query

logger = logging.getLogger("kukuibot.compact")


def _load_chat_log_tail(worker_identity: str = "", model_key: str = "", session_id: str = "", max_chars: int = 20_000) -> str:
    """Read recent chat history from SQLite.

    - Queries by worker and/or session_id for per-tab continuity.
    - Returns up to max_chars with per-line truncation.
    """
    try:
        row_estimate = max(50, (max_chars // 150) * (5 if session_id else 1))
        rows = log_query(
            category="chat",
            session_id=session_id or None,
            worker=worker_identity or None,
            limit=row_estimate,
            order="DESC",
        )
        if not rows and worker_identity:
            rows = log_query(
                category="chat",
                session_id=session_id or None,
                limit=row_estimate,
                order="DESC",
            )
        if not rows:
            return ""

        result = []
        total = 0
        for r in rows:
            role = (r["role"] or "system").upper()
            ts = r["ts"][:19].replace("T", " ") if r["ts"] else ""
            sid = r["session_id"] or ""
            msg = r["message"]
            line = f"[{role} {ts} {sid}]: {msg}"
            if len(line) > 10_000:
                line = line[:10_000] + "... (truncated)"
            if total + len(line) + 1 > max_chars:
                break
            result.append(line)
            total += len(line) + 1
            
        result.reverse()
        return "\n".join(result)
    except Exception as e:
        logger.warning(f"Failed to load chat log tail: {e}")
        return ""


def _load_context_file(path: Path) -> str:
    """Load a context file, return empty string on failure."""
    try:
        if path.exists():
            text = path.read_text().strip()
            return text if text else ""
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
    return ""


def _load_project_report(path: Path, max_chars: int = 6000) -> str:
    """Load PROJECT-REPORT.md and prepend staleness warning if older than 48h."""
    content = _load_context_file(path)
    if not content:
        return ""

    if len(content) > max_chars:
        content = content[:max_chars] + "\n... (truncated)"

    try:
        age_seconds = max(0.0, datetime.now().timestamp() - path.stat().st_mtime)
        if age_seconds > 48 * 3600:
            updated = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            warning = (
                "> ⚠️ Staleness warning: PROJECT-REPORT.md is older than 48 hours "
                f"(last updated {updated} local time).\n\n"
            )
            return warning + content
    except Exception:
        pass

    return content


def _resolve_model_file(model_key: str) -> Path | None:
    """Find the per-model identity file in ~/.kukuibot/models/."""
    models_dir = KUKUIBOT_HOME / "models"
    if not model_key or not models_dir.is_dir():
        return None
    direct = models_dir / f"{model_key}.md"
    if direct.is_file():
        return direct
    for part in [model_key.split("_")[0], model_key.split("-")[0]]:
        candidate = models_dir / f"{part}.md"
        if candidate.is_file():
            return candidate
    return None


def compact_messages(
    items: list,
    get_token_fn=None,
    get_account_id_fn=None,
    self_compact: bool = True,
    active_docs: set | None = None,
    session_id: str = "",
    model_key: str = "",
    worker_identity: str = "",
) -> list:
    """Smart compact: re-inject New Tab context + 20KB chat log.

    Injects: SOUL, USER, TOOLS, Model Identity, Worker Identity, Project Report + chat log tail.
    Then keeps last N recent messages verbatim after the summary.

    Args:
        items: Full conversation history (list of message dicts)
        get_token_fn: Unused (kept for API compat)
        get_account_id_fn: Unused (kept for API compat)
        self_compact: Unused (kept for API compat)
        active_docs: Unused (kept for API compat)
        session_id: Session ID (for picking the right chat log)
        model_key: Model key (for model identity file)
        worker_identity: Worker role key (for worker identity file)

    Returns:
        Compacted items list with context + recent messages
    """
    if len(items) <= RECENT_MESSAGES_TO_KEEP:
        return items

    split_point = len(items) - RECENT_MESSAGES_TO_KEEP
    # Don't split between function_call and function_call_output
    while split_point > 0:
        item = items[split_point]
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            split_point -= 1
        else:
            break

    recent_items = items[split_point:]

    # Build context: SOUL, USER, TOOLS, Model Identity, Worker Identity, Project Report
    sections = []

    soul = _load_context_file(WORKSPACE / "SOUL.md")
    if soul:
        sections.append(f"# Identity\n{soul}")

    user_md = _load_context_file(WORKSPACE / "USER.md")
    if user_md:
        sections.append(f"# About the User\n{user_md}")

    tools_md = _load_context_file(WORKSPACE / "TOOLS.md")
    if tools_md:
        sections.append(f"# Tools & Infrastructure Reference\n{tools_md}")

    model_file = _resolve_model_file(model_key)
    if model_file:
        content = _load_context_file(model_file)
        if content:
            sections.append(f"# Model Profile\n{content}")

    if worker_identity:
        worker_file = WORKSPACE / "workers" / f"{worker_identity}.md"
        content = _load_context_file(worker_file)
        if content:
            sections.append(f"# Worker Role\n{content}")

    project_report = _load_project_report(WORKSPACE / "PROJECT-REPORT.md")
    if project_report:
        sections.append(f"# Project Report\n{project_report}")

    # Chat log tail (20KB)
    chat_tail = _load_chat_log_tail(
        worker_identity=worker_identity,
        model_key=model_key,
        session_id=session_id,
    )
    if chat_tail:
        sections.append(f"# Recent Chat History\n{chat_tail}")

    summary = "\n\n---\n\n".join(sections)

    # Flush to compaction log
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _flush_to_compaction_log(timestamp, f"Compacted {len(items)} items, context={len(summary)} chars")

    # Build compacted items
    compacted = [
        {
            "role": "user",
            "content": (
                f"[SMART COMPACT — {timestamp}]\n"
                f"Context re-injected: SOUL + USER + TOOLS + Model Identity + Worker Identity + Project Report + 20KB chat log. "
                f"Continue from where we left off.\n\n"
                f"{summary}\n\n"
                f"[END CONTEXT — Recent messages follow]"
            ),
        },
        {
            "role": "assistant",
            "content": "Ready.",
        },
    ]
    compacted.extend(recent_items)

    logger.info(
        f"Smart compact complete: {len(items)} items → {len(compacted)} items, "
        f"context={len(summary)} chars"
    )
    return compacted


def _flush_to_compaction_log(timestamp: str, summary_text: str):
    """Append to rolling compaction log (last N lines)."""
    try:
        entry = f"\n\n## Compaction ({timestamp})\n{summary_text}\n"
        with open(COMPACTION_LOG_FILE, "a") as f:
            f.write(entry)
        # Trim to max lines
        with open(COMPACTION_LOG_FILE, "r") as f:
            lines = f.readlines()
        if len(lines) > COMPACTION_LOG_MAX_LINES:
            with open(COMPACTION_LOG_FILE, "w") as f:
                f.writelines(lines[-COMPACTION_LOG_MAX_LINES:])
        logger.info(f"Flushed compaction summary to {COMPACTION_LOG_FILE}")
    except Exception as e:
        logger.warning(f"Failed to flush compaction log: {e}")
