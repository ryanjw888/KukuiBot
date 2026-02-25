"""
server_helpers.py — Pure helper functions and constants extracted from server.py.

All functions in this module are stateless (no global mutable state access).
They depend only on their arguments and on config.py constants.

Phase 1 of the server.py module extraction (P0 maintainability).
"""

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    CODEX_CONTEXT_WINDOW,
    CODEX_COMPACTION_THRESHOLD,
    KUKUIBOT_HOME,
    MODEL,
    SPARK_CONTEXT_WINDOW,
    SPARK_COMPACTION_THRESHOLD,
    SPARK_MODEL,
    WORKSPACE,
)
from claude_bridge import CONTEXT_WINDOW as CLAUDE_CONTEXT_WINDOW, COMPACTION_THRESHOLD as CLAUDE_COMPACTION_THRESHOLD

logger = logging.getLogger("kukuibot.server_helpers")


# ---------------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------------

def human_bytes(n: int) -> str:
    n = max(0, int(n or 0))
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


def clamp_int(v: Any, *, default: int, min_value: int, max_value: int) -> int:
    try:
        x = int(v)
    except Exception:
        x = int(default)
    if x < min_value:
        x = min_value
    if x > max_value:
        x = max_value
    return x


# ---------------------------------------------------------------------------
# Model profiles & session resolution
# ---------------------------------------------------------------------------

MODEL_PROFILES = {
    "codex": {
        "api_model": MODEL,
        "ui_model": MODEL,
        "context_window": CODEX_CONTEXT_WINDOW,
        "compaction_threshold": CODEX_COMPACTION_THRESHOLD,
    },
    # NOTE: ChatGPT Codex responses endpoint currently rejects model='spark'.
    # Keep Spark tab limits distinct, while routing through Codex API model.
    "spark": {
        "api_model": MODEL,
        "ui_model": SPARK_MODEL,
        "context_window": SPARK_CONTEXT_WINDOW,
        "compaction_threshold": SPARK_COMPACTION_THRESHOLD,
    },
    "openrouter": {
        "api_model": "openrouter",
        "ui_model": "openrouter",
        "context_window": 1_000_000,
        "compaction_threshold": 800_000,
    },
    "claude": {
        "api_model": "claude-opus-4-6",
        "ui_model": "claude-code (opus)",
        "context_window": CLAUDE_CONTEXT_WINDOW,
        "compaction_threshold": CLAUDE_COMPACTION_THRESHOLD,
    },
    "claude_sonnet": {
        "api_model": "claude-sonnet-4-6",
        "ui_model": "claude-code (sonnet)",
        "context_window": CLAUDE_CONTEXT_WINDOW,
        "compaction_threshold": CLAUDE_COMPACTION_THRESHOLD,
    },
    "anthropic": {
        "api_model": "claude-sonnet-4-6",
        "ui_model": "anthropic (sonnet 4.6)",
        "context_window": 1_000_000,
        "compaction_threshold": 800_000,
    },
    "anthropic_sonnet45": {
        "api_model": "claude-sonnet-4-5-20250929",
        "ui_model": "anthropic (sonnet 4.5)",
        "context_window": 200_000,
        "compaction_threshold": 160_000,
    },
    "anthropic_haiku": {
        "api_model": "claude-haiku-4-5-20251001",
        "ui_model": "anthropic (haiku)",
        "context_window": 200_000,
        "compaction_threshold": 160_000,
    },
    "anthropic_opus": {
        "api_model": "claude-opus-4-6",
        "ui_model": "anthropic (opus)",
        "context_window": 200_000,
        "compaction_threshold": 160_000,
    },
}


def resolve_profile(session_id: str | None) -> str:
    sid = (session_id or "").lower()
    if sid.startswith("tab-spark") or sid.startswith("spark"):
        return "spark"
    if sid.startswith("tab-openrouter") or sid.startswith("openrouter"):
        return "openrouter"
    if sid.startswith("tab-anthropic_haiku") or sid.startswith("anthropic_haiku"):
        return "anthropic_haiku"
    if sid.startswith("tab-anthropic_opus") or sid.startswith("anthropic_opus"):
        return "anthropic_opus"
    if sid.startswith("tab-anthropic_sonnet45") or sid.startswith("anthropic_sonnet45"):
        return "anthropic_sonnet45"
    if sid.startswith("tab-anthropic") or sid.startswith("anthropic"):
        return "anthropic"
    if sid.startswith("tab-claude_sonnet") or sid.startswith("claude_sonnet"):
        return "claude_sonnet"
    if sid.startswith("tab-claude") or sid.startswith("claude"):
        return "claude"
    return "codex"


def profile_limits(session_id: str | None) -> tuple[str, int, int]:
    profile = resolve_profile(session_id)
    cfg = MODEL_PROFILES[profile]
    return profile, int(cfg["context_window"]), int(cfg["compaction_threshold"])


def model_key_from_session(session_id: str) -> str:
    """Extract the frontend model key from a session ID.

    Session IDs look like: tab-codex-abc123, tab-claude_opus-xyz, tab-openrouter_gemini31-foo
    Returns: 'codex', 'claude_opus', 'openrouter_gemini31', etc.
    """
    sid = str(session_id or "").strip()
    if sid.startswith("tab-"):
        sid = sid[4:]
    # Delegated sessions: "deleg-codex-developer" → extract model base between first and second hyphen
    if sid.startswith("deleg-"):
        parts = sid.split("-", 2)  # ["deleg", "codex", "developer"]
        return parts[1] if len(parts) >= 2 else sid
    # Split on last hyphen-group (the random suffix) — model key is everything before it
    # Model keys use underscores, suffixes use hyphens: "openrouter_gemini31-ts4rand"
    parts = sid.rsplit("-", 1)
    if len(parts) == 2:
        return parts[0]
    return sid


def resolve_model_file(model_key: str) -> Path | None:
    """Find the per-model identity file in ~/.kukuibot/models/."""
    models_dir = KUKUIBOT_HOME / "models"
    if not model_key or not models_dir.is_dir():
        return None
    # Direct match: models/codex.md, models/claude.md, models/openrouter.md
    direct = models_dir / f"{model_key}.md"
    if direct.is_file():
        return direct
    # Prefix match: "openrouter_gemini31" → "openrouter"
    for part in [model_key.split("_")[0], model_key.split("-")[0]]:
        candidate = models_dir / f"{part}.md"
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Text / link helpers
# ---------------------------------------------------------------------------

def response_has_links(text: str) -> bool:
    if not text:
        return False
    if re.search(r"\[[^\]]+\]\(https?://[^)]+\)", text):
        return True
    if re.search(r"https?://\S+", text):
        return True
    return False


def extract_web_links_from_tool_output(output: str) -> list[str]:
    try:
        data = json.loads(output or "{}")
    except Exception:
        return []

    links: list[str] = []
    links_md = data.get("links_markdown") or ""
    if isinstance(links_md, str) and links_md.strip():
        links.extend(re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", links_md))

    for u in (data.get("citation_urls") or []):
        if isinstance(u, str) and u.startswith("http"):
            links.append(u)

    # dedupe, preserve order
    seen = set()
    deduped = []
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


def sanitize_bearer_token(tok: str) -> str:
    """Normalize tokens pasted with newlines/spaces/quotes or accidental 'Bearer ' prefix.

    Claude Code / fetch() will reject header values containing newlines.
    """
    tok = (tok or "").strip()
    # Strip accidental wrapping quotes
    tok = tok.strip("\"'")
    # Strip accidental 'Bearer ' prefix
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    # Remove all whitespace (line wraps, spaces)
    tok = re.sub(r"\s+", "", tok)
    return tok


# ---------------------------------------------------------------------------
# History / tool-item repair
# ---------------------------------------------------------------------------

def repair_tool_items(items: list[dict]) -> list[dict]:
    """Ensure function_call/function_call_output pairs are valid for Responses API.

    - Remove orphan function_call_output entries (no prior matching function_call)
    - Insert synthetic function_call_output for any unpaired function_call
    """
    if not isinstance(items, list):
        return items

    repaired = list(items)

    # Pass 1: drop orphan outputs that reference unknown call_ids
    seen_calls = set()
    i = 0
    while i < len(repaired):
        it = repaired[i]
        if isinstance(it, dict) and it.get("type") == "function_call":
            cid = it.get("call_id")
            if cid:
                seen_calls.add(cid)
        elif isinstance(it, dict) and it.get("type") == "function_call_output":
            cid = it.get("call_id")
            if not cid or cid not in seen_calls:
                repaired.pop(i)
                continue
        i += 1

    # Pass 2: ensure every function_call has an immediate output after it
    i = 0
    while i < len(repaired):
        if isinstance(repaired[i], dict) and repaired[i].get("type") == "function_call":
            cid = repaired[i].get("call_id")
            if i + 1 >= len(repaired) or repaired[i + 1].get("type") != "function_call_output" or repaired[i + 1].get("call_id") != cid:
                repaired.insert(i + 1, {"type": "function_call_output", "call_id": cid, "output": "ERROR: Tool execution was interrupted."})
                i += 2
                continue
        i += 1

    return repaired


# ---------------------------------------------------------------------------
# Attachment helpers (paste / drag-drop file support)
# ---------------------------------------------------------------------------

ATTACHMENT_TMP_DIR = Path(os.path.expanduser("~/.kukuibot/tmp/attachments"))


def validate_attachments(raw: list) -> list[dict]:
    """Validate and normalize attachment objects from the frontend.

    Returns a clean list; silently drops invalid entries.
    """
    if not isinstance(raw, list):
        return []
    valid = []
    for att in raw[:10]:  # hard cap
        if not isinstance(att, dict):
            continue
        name = str(att.get("name") or "").strip()
        atype = str(att.get("type") or "").strip()
        if not name or not atype:
            continue
        has_data = bool(att.get("dataUrl"))
        has_text = att.get("textContent") is not None
        if not has_data and not has_text:
            continue
        valid.append({
            "name": name,
            "type": atype,
            "isImage": bool(att.get("isImage")),
            "dataUrl": att.get("dataUrl") if has_data else None,
            "textContent": att.get("textContent") if has_text else None,
        })
    return valid


def save_attachment_image(att: dict) -> str | None:
    """Save a base64 data-URL image to a temp file. Returns the path or None."""
    import base64 as b64mod
    try:
        ATTACHMENT_TMP_DIR.mkdir(parents=True, exist_ok=True)
        data_url = att.get("dataUrl", "")
        if "," not in data_url:
            return None
        header, b64data = data_url.split(",", 1)
        raw_bytes = b64mod.b64decode(b64data)
        # Derive extension from MIME
        ext = "png"
        if "jpeg" in header or "jpg" in header:
            ext = "jpg"
        elif "gif" in header:
            ext = "gif"
        elif "webp" in header:
            ext = "webp"
        elif "svg" in header:
            ext = "svg"
        fname = f"{uuid.uuid4().hex[:12]}_{att['name']}"
        if not fname.lower().endswith(f".{ext}"):
            fname = f"{fname}.{ext}"
        fpath = ATTACHMENT_TMP_DIR / fname
        fpath.write_bytes(raw_bytes)
        return str(fpath)
    except Exception as e:
        logger.warning(f"Failed to save attachment image: {e}")
        return None


def format_attachments_as_text(attachments: list[dict]) -> str:
    """Format attachments as plain-text blocks for providers that only accept text (Claude Code CLI)."""
    parts = []
    for att in attachments:
        if att.get("textContent") is not None:
            ext = Path(att["name"]).suffix.lstrip(".") or "txt"
            parts.append(f"\U0001f4ce **{att['name']}**\n```{ext}\n{att['textContent']}\n```")
        elif att.get("isImage") and att.get("dataUrl"):
            # Save image to temp file for Claude CLI to read
            saved = save_attachment_image(att)
            if saved:
                parts.append(f"\U0001f4ce [Attached image: {att['name']} \u2014 saved to {saved}]")
            else:
                parts.append(f"\U0001f4ce [Attached image: {att['name']} \u2014 could not save]")
    return "\n\n".join(parts)


def cleanup_old_attachments():
    """Remove attachment temp files older than 1 hour."""
    try:
        if not ATTACHMENT_TMP_DIR.is_dir():
            return
        cutoff = time.time() - 3600
        for f in ATTACHMENT_TMP_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except Exception:
        pass


def build_anthropic_attachment_blocks(attachments: list[dict]) -> list[dict]:
    """Convert attachments to Anthropic Messages API content blocks."""
    import base64 as b64mod
    blocks = []
    for att in attachments:
        if att.get("isImage") and att.get("dataUrl"):
            data_url = att["dataUrl"]
            if "," in data_url:
                header, b64data = data_url.split(",", 1)
                # Extract media type from data:image/png;base64,...
                media_type = "image/png"
                if ":" in header and ";" in header:
                    media_type = header.split(":")[1].split(";")[0]
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64data},
                })
        elif att.get("textContent") is not None:
            ext = Path(att["name"]).suffix.lstrip(".") or "txt"
            blocks.append({
                "type": "text",
                "text": f"\U0001f4ce **{att['name']}**\n```{ext}\n{att['textContent']}\n```",
            })
    return blocks


def build_openai_attachment_parts(attachments: list[dict]) -> list[dict]:
    """Convert attachments to OpenAI Chat Completions vision-style content parts."""
    parts = []
    for att in attachments:
        if att.get("isImage") and att.get("dataUrl"):
            parts.append({"type": "image_url", "image_url": {"url": att["dataUrl"]}})
        elif att.get("textContent") is not None:
            ext = Path(att["name"]).suffix.lstrip(".") or "txt"
            parts.append({"type": "text", "text": f"\U0001f4ce **{att['name']}**\n```{ext}\n{att['textContent']}\n```"})
    return parts


def build_codex_attachment_items(attachments: list[dict]) -> list[dict]:
    """Convert attachments to OpenAI Responses API input content parts."""
    parts = []
    for att in attachments:
        if att.get("isImage") and att.get("dataUrl"):
            parts.append({"type": "input_image", "image_url": att["dataUrl"]})
        elif att.get("textContent") is not None:
            ext = Path(att["name"]).suffix.lstrip(".") or "txt"
            parts.append({"type": "input_text", "text": f"\U0001f4ce **{att['name']}**\n```{ext}\n{att['textContent']}\n```"})
    return parts


def attachment_summary(attachments: list[dict]) -> str:
    """Short description of attachments for chat log (no base64 data)."""
    names = [att["name"] for att in attachments]
    return f"[Attachments: {', '.join(names)}]"


# ---------------------------------------------------------------------------
# Session-type checkers (stateless — depend only on session ID string)
# ---------------------------------------------------------------------------

def is_claude_session(session_id: str) -> bool:
    sid = str(session_id or "").strip().lower()
    return sid.startswith("claude") or sid.startswith("tab-claude") or sid.startswith("deleg-claude")


def claude_model_for_session(session_id: str) -> str:
    """Return the Claude CLI --model value for a session ID."""
    sid = str(session_id or "").strip().lower()
    if "claude_sonnet" in sid:
        return "sonnet"
    return "opus"


def is_openrouter_session(session_id: str) -> bool:
    sid = str(session_id or "").strip().lower()
    return sid.startswith("openrouter") or sid.startswith("tab-openrouter") or sid.startswith("deleg-openrouter")


def is_anthropic_session(session_id: str) -> bool:
    sid = str(session_id or "").strip().lower()
    return sid.startswith("anthropic") or sid.startswith("tab-anthropic") or sid.startswith("deleg-anthropic")


def worker_identity_for_session(session_id: str) -> str:
    """Look up the worker_identity for a session from tab_meta."""
    try:
        from auth import db_connection
        with db_connection() as db:
            row = db.execute(
                "SELECT worker_identity FROM tab_meta WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row and row[0]:
                return str(row[0])
        # Delegated sessions: "deleg-codex-developer-1" or legacy "deleg-codex-developer"
        sid = str(session_id or "")
        if sid.startswith("deleg-"):
            parts = sid.split("-", 2)  # ["deleg", "codex", "developer-1"] or ["deleg", "codex", "developer"]
            if len(parts) >= 3:
                worker_part = parts[2]
                # Strip trailing slot number (e.g. "developer-1" → "developer")
                return re.sub(r"-\d+$", "", worker_part)
        return ""
    except Exception:
        return ""
