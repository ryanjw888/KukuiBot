"""
tools.py — Tool execution engine.
Handles bash, read_file, write_file, edit_file, spawn_agent, bash_background, bash_check,
memory_search, memory_read, web tools, and browser automation tools.
"""

import ast
import html
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import certifi
import requests as http_requests

from config import (
    BASH_TIMEOUT,
    MAX_OUTPUT_CHARS,
    TOOL_ENV,
    WORKSPACE,
)
from security import (
    check_bash_command,
    check_path_access,
    consume_elevation,
    is_session_elevated,
    request_elevation,
)
from injection_guard import scan_and_filter, scan_text
from log_store import log_write

logger = logging.getLogger("kukuibot.tools")


# --- Tool Definitions (OpenAI Responses API format) ---
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "bash",
        "description": "Run a shell command. Returns stdout+stderr. Use for git, file ops, service management, testing, etc. Timeout: 30min.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to execute"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read file contents. Supports offset/limit for large files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspace)"},
                "offset": {"type": "integer", "description": "Line number to start from (1-indexed)"},
                "limit": {"type": "integer", "description": "Max lines to read"},
            },
            "required": ["path"],
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write content to a file. Creates parent dirs if needed. Overwrites existing.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "edit_file",
        "description": "Replace exact text in a file. old_text must match exactly (including whitespace).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "old_text": {"type": "string", "description": "Exact text to find"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "spawn_agent",
        "description": (
            "Spawn an isolated sub-agent with a fresh 400k context window to execute a complex task. "
            "The sub-agent has the same tools (bash, read, write, edit) and runs autonomously. "
            "Use for: deep research, multi-file refactors, code generation, analysis pipelines, "
            "or any task that would consume too much context in the current session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Detailed task description for the sub-agent."},
                "max_turns": {"type": "integer", "description": "Maximum tool-use rounds (default 25)."},
            },
            "required": ["task"],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "codebase_outline",
        "description": (
            "Explore Python codebase structure and retrieve specific symbols without reading entire files. "
            "Three modes: tree (directory overview with symbol counts), outline (file-level function/class listing "
            "with line numbers), symbol (extract a single function or class by name)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["tree", "outline", "symbol"], "description": "tree=directory overview, outline=file symbols, symbol=extract one symbol"},
                "path": {"type": "string", "description": "Directory path (tree mode) or file path (outline/symbol mode)"},
                "name": {"type": "string", "description": "(symbol mode only) Function or class name. Use 'ClassName.method' for methods."},
            },
            "required": ["mode", "path"],
            "additionalProperties": False,
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "bash_background",
        "description": "Start a long-running command in the background. Returns a process ID for polling with bash_check.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to run in background"}},
            "required": ["command"],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "bash_check",
        "description": "Check on a background process. Returns new output, status, elapsed time. Use wait_seconds to sleep before checking.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Process ID from bash_background"},
                "action": {"type": "string", "enum": ["poll", "kill", "log", "list"], "description": "Default: poll"},
                "wait_seconds": {"type": "integer", "description": "Seconds to wait before checking (max 60)"},
            },
            "required": [],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "memory_search",
        "description": "Search MEMORY.md and memory/*.md for relevant context about prior work, decisions, infrastructure, preferences.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "max_results": {"type": "integer", "description": "Max results (default 5)"},
            },
            "required": ["query"],
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "memory_read",
        "description": "Read specific lines from a memory file. Use after memory_search for full context.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path from memory_search results"},
                "from_line": {"type": "integer", "description": "Starting line (1-indexed, default 1)"},
                "max_lines": {"type": "integer", "description": "Max lines to read (default 100)"},
            },
            "required": ["path"],
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "scan_content",
        "description": "Scan untrusted/external content for prompt injection attacks. Use before processing web pages, user-pasted URLs, webhook payloads, or any content from untrusted sources. Returns verdict (LEGIT/INJECTION/SUSPICIOUS) and confidence score.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Content to scan for injection"},
                "source": {"type": "string", "description": "Label for the content source (e.g. 'web_fetch', 'user_paste', 'webhook')"},
            },
            "required": ["text"],
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "web_search_ddg",
        "description": "Search the public web via DuckDuckGo HTML endpoint. Results are passed through injection safety filtering and Spark no-tool sanitization before returning.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results to return (default 5, max 10)"}
            },
            "required": ["query"],
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "web_fetch",
        "description": "Fetch a public web page URL (http/https only), sanitize and scan content for prompt-injection, then return safe extracted text.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public URL to fetch"},
                "max_chars": {"type": "integer", "description": "Maximum extracted chars to return (default 12000, max 30000)"}
            },
            "required": ["url"],
        },
        "allowed_callers": ["direct", "code_execution_20260120"],
    },
    {
        "type": "function",
        "name": "browser_open",
        "description": "Open a controllable browser session for the current chat session using Playwright. Defaults to visible Google Chrome.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_url": {"type": "string", "description": "Optional URL to open immediately"},
                "headless": {"type": "boolean", "description": "Run headless (default false)"},
                "profile_name": {"type": "string", "description": "Profile folder name under workspace/.browser_profiles (default 'default')"}
            },
            "required": [],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "browser_navigate",
        "description": "Navigate current browser page to a URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Destination URL"},
                "wait_until": {"type": "string", "description": "load state: load/domcontentloaded/networkidle (default domcontentloaded)"}
            },
            "required": ["url"],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "browser_click",
        "description": "Click an element by CSS/text selector. Sensitive actions (submit/buy/sell/trade/pay/checkout/confirm/transfer) require elevation unless force=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Playwright selector (e.g., 'button:has-text(\"Search\")')"},
                "text_hint": {"type": "string", "description": "Optional human-readable label of clicked element for safety checks"},
                "force": {"type": "boolean", "description": "Bypass sensitivity gate when already elevated"}
            },
            "required": ["selector"],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "browser_type",
        "description": "Type text into an input/textarea using selector.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Playwright selector"},
                "text": {"type": "string", "description": "Text to type"},
                "press_enter": {"type": "boolean", "description": "Press Enter after typing"}
            },
            "required": ["selector", "text"],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "browser_extract",
        "description": "Extract text or HTML from page/element for research summarization.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Optional selector to extract from (default body)"},
                "format": {"type": "string", "description": "text or html (default text)"},
                "max_chars": {"type": "integer", "description": "Max chars to return (default 12000, max 60000)"}
            },
            "required": [],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "browser_snapshot",
        "description": "Save a screenshot of current page to workspace and return file path + page metadata.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional output path (relative/absolute)"},
                "full_page": {"type": "boolean", "description": "Capture full page (default true)"}
            },
            "required": [],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "browser_close",
        "description": "Close browser session for current chat session.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "delegate_task",
        "description": (
            "Delegate a task to another KukuiBot worker (e.g. developer, it-admin). "
            "Sends the prompt to the target worker's active session via the local API. "
            "The task runs asynchronously — use check_task to monitor progress. "
            "Use list_tasks to see all delegated tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Target worker identity (e.g. 'developer', 'it-admin')"},
                "prompt": {"type": "string", "description": "Detailed task prompt for the target worker"},
                "model": {"type": "string", "description": "Optional: target model_key to pick a specific tab (e.g. 'codex', 'anthropic')"},
                "force": {"type": "boolean", "description": "Optional manual override to bypass collision guard for stale delegated sessions."},
            },
            "required": ["worker", "prompt"],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "check_task",
        "description": "Check the status of a delegated task. Returns current status, elapsed time, and latest response from the target worker.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID returned by delegate_task"},
            },
            "required": ["task_id"],
        },
    
        "allowed_callers": ["direct"],
    },
    {
        "type": "function",
        "name": "list_tasks",
        "description": "List all tasks delegated from the current session. Shows status, target worker, and progress for each.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    
        "allowed_callers": ["direct"],
    },
]

# Sub-agent tools: same but without spawn_agent and delegation tools (prevent recursion)
_DELEGATION_TOOLS = {"delegate_task", "check_task", "list_tasks", "spawn_agent"}
SUB_AGENT_TOOLS = [t for t in TOOL_DEFINITIONS if t["name"] not in _DELEGATION_TOOLS]


# --- Codebase Outline Tool ---
_OUTLINE_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".tox", ".mypy_cache", ".pytest_cache"}
_OUTLINE_MAX_FILES = 200


def _codebase_outline(mode: str, path: str, name: str = None) -> str:
    """Explore Python codebase structure using stdlib ast."""
    if mode == "tree":
        return _outline_tree(path)
    elif mode == "outline":
        return _outline_file(path)
    elif mode == "symbol":
        if not name:
            return "ERROR: 'name' parameter is required for symbol mode."
        return _outline_symbol(path, name)
    else:
        return f"ERROR: Unknown mode '{mode}'. Use tree, outline, or symbol."


def _outline_tree(dir_path: str) -> str:
    """Walk directory, list files with Python symbol counts."""
    dir_p = Path(dir_path)
    if not dir_p.is_dir():
        return f"ERROR: '{dir_path}' is not a directory."

    entries = []  # (relative_path, is_python, info_str)
    py_count = 0
    total_count = 0

    for root, dirs, files in os.walk(dir_p):
        # Skip hidden/cache dirs in-place
        dirs[:] = [d for d in sorted(dirs) if d not in _OUTLINE_SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(files):
            if total_count >= _OUTLINE_MAX_FILES:
                break
            fpath = Path(root) / fname
            rel = fpath.relative_to(dir_p)
            total_count += 1

            if fname.endswith(".py"):
                py_count += 1
                try:
                    source = fpath.read_text(errors="replace")
                    tree = ast.parse(source, filename=str(fpath))
                    funcs = sum(1 for n in ast.iter_child_nodes(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
                    classes = sum(1 for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef))
                    lines = source.count("\n") + 1
                    info = f"{funcs} function{'s' if funcs != 1 else ''}, {classes} class{'es' if classes != 1 else ''} ({lines} lines)"
                except SyntaxError:
                    info = "(parse error)"
                except Exception as e:
                    info = f"(error: {type(e).__name__})"
                entries.append((str(rel), True, info))
            else:
                try:
                    size = fpath.stat().st_size
                    if size >= 1024:
                        size_str = f"{size / 1024:.1f} KB"
                    else:
                        size_str = f"{size} bytes"
                except OSError:
                    size_str = "? bytes"
                entries.append((str(rel), False, f"(non-python, {size_str})"))
        if total_count >= _OUTLINE_MAX_FILES:
            break

    if not entries:
        return f"No files found in {dir_path}"

    # Calculate remaining
    remaining = 0
    if total_count >= _OUTLINE_MAX_FILES:
        # Count remaining files
        for root, dirs, files in os.walk(dir_p):
            dirs[:] = [d for d in dirs if d not in _OUTLINE_SKIP_DIRS and not d.startswith(".")]
            remaining += len(files)
        remaining = max(0, remaining - _OUTLINE_MAX_FILES)

    # Format output
    max_path_len = max(len(e[0]) for e in entries)
    lines = [f"{dir_p.name}/ ({py_count} Python files, {total_count} total files)\n"]
    for rel_path, is_py, info in entries:
        if is_py:
            lines.append(f"  {rel_path:<{max_path_len}}  — {info}")
        else:
            lines.append(f"  {rel_path:<{max_path_len}}  — {info}")

    if remaining > 0:
        lines.append(f"\n[{remaining} more files omitted]")

    return "\n".join(lines)


def _outline_file(file_path: str) -> str:
    """Parse a Python file and list all top-level symbols with line numbers."""
    fp = Path(file_path)
    if not fp.is_file():
        return f"ERROR: '{file_path}' is not a file."
    if not fp.name.endswith(".py"):
        return f"ERROR: '{file_path}' is not a Python file."

    try:
        source = fp.read_text(errors="replace")
    except Exception as e:
        return f"ERROR reading {file_path}: {e}"

    try:
        tree = ast.parse(source, filename=str(fp))
    except SyntaxError as e:
        return f"ERROR: Syntax error in {file_path}: {e}"

    total_lines = source.count("\n") + 1
    top_funcs = sum(1 for n in ast.iter_child_nodes(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    top_classes = sum(1 for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef))

    lines = [f"{fp.name} ({top_funcs} functions, {top_classes} classes, {total_lines} lines)\n"]

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            sig = _format_signature(node)
            decorators = _format_decorators(node)
            for dec in decorators:
                lines.append(f"  {node.decorator_list[0].lineno:>5}  {dec}")
            lines.append(f"  {node.lineno:>5}  {prefix} {node.name}({sig})")
            doc = _get_docstring_first_line(node)
            if doc:
                lines.append(f"         — {doc}")

        elif isinstance(node, ast.ClassDef):
            decorators = _format_decorators(node)
            bases = ", ".join(_format_expr(b) for b in node.bases) if node.bases else ""
            for dec in decorators:
                lines.append(f"  {node.decorator_list[0].lineno:>5}  {dec}")
            lines.append(f"  {node.lineno:>5}  class {node.name}" + (f"({bases})" if bases else ""))
            doc = _get_docstring_first_line(node)
            if doc:
                lines.append(f"         — {doc}")
            # List methods
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mprefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                    msig = _format_signature(child)
                    lines.append(f"  {child.lineno:>5}    {mprefix} {child.name}({msig})")
                    mdoc = _get_docstring_first_line(child)
                    if mdoc:
                        lines.append(f"           — {mdoc}")

    return "\n".join(lines)


def _outline_symbol(file_path: str, name: str) -> str:
    """Extract a single symbol's source code from a Python file."""
    fp = Path(file_path)
    if not fp.is_file():
        return f"ERROR: '{file_path}' is not a file."

    try:
        source = fp.read_text(errors="replace")
    except Exception as e:
        return f"ERROR reading {file_path}: {e}"

    try:
        tree = ast.parse(source, filename=str(fp))
    except SyntaxError as e:
        return f"ERROR: Syntax error in {file_path}: {e}"

    source_lines = source.splitlines()

    # Support dotted names like ClassName.method_name
    parts = name.split(".", 1)

    target_node = None
    if len(parts) == 2:
        class_name, method_name = parts
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == method_name:
                        target_node = child
                        break
                break
    else:
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                target_node = node
                break

    if target_node is None:
        # List available symbols
        available = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                available.append(node.name)
            elif isinstance(node, ast.ClassDef):
                available.append(node.name)
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        available.append(f"{node.name}.{child.name}")
        suggestion = ", ".join(available[:20])
        return f"Symbol '{name}' not found in {file_path}. Available symbols: [{suggestion}]"

    # Get line range — include decorators
    start_line = target_node.lineno
    if hasattr(target_node, "decorator_list") and target_node.decorator_list:
        start_line = target_node.decorator_list[0].lineno

    end_line = getattr(target_node, "end_lineno", None)
    if end_line is None:
        # Fallback: scan until next top-level node or EOF
        end_line = len(source_lines)
        parent = tree if len(parts) == 1 else None
        if parent:
            found = False
            for node in ast.iter_child_nodes(parent):
                if found and hasattr(node, "lineno"):
                    end_line = node.lineno - 1
                    break
                if node is target_node:
                    found = True

    extracted = source_lines[start_line - 1:end_line]
    header = f"# {fp.name}:{start_line}-{end_line} ({name})"
    return header + "\n" + "\n".join(extracted)


def _format_signature(node) -> str:
    """Format function argument signature."""
    args = node.args
    parts = []
    # Positional args
    defaults_offset = len(args.args) - len(args.defaults)
    for i, arg in enumerate(args.args):
        s = arg.arg
        if arg.annotation:
            s += f": {_format_expr(arg.annotation)}"
        di = i - defaults_offset
        if di >= 0 and di < len(args.defaults):
            s += f" = ..."
        parts.append(s)
    if args.vararg:
        s = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            s += f": {_format_expr(args.vararg.annotation)}"
        parts.append(s)
    for i, arg in enumerate(args.kwonlyargs):
        s = arg.arg
        if arg.annotation:
            s += f": {_format_expr(arg.annotation)}"
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            s += f" = ..."
        parts.append(s)
    if args.kwarg:
        s = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            s += f": {_format_expr(args.kwarg.annotation)}"
        parts.append(s)
    return ", ".join(parts)


def _format_expr(node) -> str:
    """Best-effort formatting of an AST expression node."""
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Constant):
        return repr(node.value)
    elif isinstance(node, ast.Attribute):
        return f"{_format_expr(node.value)}.{node.attr}"
    elif isinstance(node, ast.Subscript):
        return f"{_format_expr(node.value)}[{_format_expr(node.slice)}]"
    elif isinstance(node, ast.Tuple):
        return ", ".join(_format_expr(e) for e in node.elts)
    elif isinstance(node, ast.List):
        return "[" + ", ".join(_format_expr(e) for e in node.elts) + "]"
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_format_expr(node.left)} | {_format_expr(node.right)}"
    return ast.dump(node)


def _format_decorators(node) -> list[str]:
    """Return formatted decorator lines like @decorator."""
    results = []
    for dec in getattr(node, "decorator_list", []):
        results.append(f"@{_format_expr(dec)}")
    return results


def _get_docstring_first_line(node) -> str:
    """Get the first line of a docstring, truncated to 80 chars."""
    try:
        doc = ast.get_docstring(node)
        if doc:
            first = doc.split("\n")[0].strip()
            if len(first) > 80:
                first = first[:77] + "..."
            return f'"{first}"'
    except Exception:
        pass
    return ""


# --- Background Process Management ---
_bg_procs: dict[str, dict] = {}
_bg_lock = threading.Lock()


def _start_background(command: str) -> str:
    proc_id = uuid.uuid4().hex[:8]
    proc = subprocess.Popen(
        command, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        cwd=str(WORKSPACE), env=TOOL_ENV,
    )
    entry = {"proc": proc, "output": [], "read_offset": 0, "started": time.time(), "command": command, "done": False, "exit_code": None}
    _bg_procs[proc_id] = entry

    def reader():
        for line in proc.stdout:
            with _bg_lock:
                entry["output"].append(line)
        proc.wait()
        with _bg_lock:
            entry["done"] = True
            entry["exit_code"] = proc.returncode

    threading.Thread(target=reader, daemon=True).start()
    return proc_id


def _check_background(proc_id: str = None, action: str = "poll", wait_seconds: int = 0) -> str:
    if action == "list":
        with _bg_lock:
            entries = [f"{pid}: {'done' if e['done'] else 'running'} ({time.time() - e['started']:.0f}s) — {e['command'][:80]}" for pid, e in _bg_procs.items()]
            return "\n".join(entries) if entries else "No background processes."

    if not proc_id or proc_id not in _bg_procs:
        return f"ERROR: Unknown process ID '{proc_id}'. Use action='list' to see active processes."

    entry = _bg_procs[proc_id]
    if action == "kill":
        entry["proc"].kill()
        entry["done"] = True
        entry["exit_code"] = -9
        return f"Killed process {proc_id}."

    if wait_seconds > 0:
        time.sleep(min(wait_seconds, 60))

    with _bg_lock:
        if action == "log":
            all_output = "".join(entry["output"])
            text = all_output[-10000:] if len(all_output) > 10000 else all_output
        else:
            new_lines = entry["output"][entry["read_offset"]:]
            entry["read_offset"] = len(entry["output"])
            text = "".join(new_lines)
            if len(text) > 10000:
                text = text[-10000:]
        elapsed = time.time() - entry["started"]
        status = "done" if entry["done"] else "running"
        exit_code = entry["exit_code"]

    header = f"[{status}] elapsed={elapsed:.1f}s"
    if exit_code is not None:
        header += f" exit_code={exit_code}"
    if not text.strip():
        header += " (no new output)"
    return f"{header}\n{text}" if text.strip() else header


def _resolve_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(str(WORKSPACE), path)


# --- Network command detection (for injection scanning on output) ---
_NETWORK_CMD_PATTERNS = re.compile(
    r"(^|\s|;|&&|\|\|)(curl|wget|fetch|http|nc|ncat|netcat|ssh|scp|sftp|rsync|ftp)\s",
    re.IGNORECASE,
)


def _is_network_command(cmd: str) -> bool:
    """Check if a bash command involves network access."""
    return bool(_NETWORK_CMD_PATTERNS.search(f" {cmd}"))


# --- Web search safety helpers ---
_ALLOWED_DDG_HOSTS = {"duckduckgo.com", "www.duckduckgo.com", "html.duckduckgo.com"}


def _host_is_private_or_local(hostname: str) -> bool:
    if not hostname:
        return True
    h = hostname.strip().lower().rstrip(".")
    if h in {"localhost", "127.0.0.1", "::1"} or h.endswith(".local"):
        return True
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception:
        return True
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
                return True
        except ValueError:
            return True
    return False


def _safe_public_url(url: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        if not p.netloc:
            return False
        host = p.hostname or ""
        return not _host_is_private_or_local(host)
    except Exception:
        return False


def _extract_real_ddg_url(href: str) -> str:
    if not href:
        return ""
    href = html.unescape(href.strip())
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if (p.hostname or "").lower() in _ALLOWED_DDG_HOSTS and p.path.startswith("/l/"):
        q = parse_qs(p.query)
        uddg = (q.get("uddg") or [""])[0]
        if uddg:
            return unquote(uddg)
    return href


def _normalize_web_search_scan(first_pass: dict, results: list[dict]) -> dict:
    """Reduce known false positives from first-pass scan on benign search listings."""
    fp = dict(first_pass or {})
    verdict = str(fp.get("verdict", "")).upper()
    pattern_count = len(fp.get("pattern_matches", []) or [])
    confidence = float(fp.get("confidence", 0) or 0)

    # If model-only flag with zero pattern hits on standard web results, downgrade.
    if verdict == "INJECTION" and pattern_count == 0 and confidence >= 0.98 and (results or []):
        benign_signals = 0
        for r in (results or [])[:10]:
            u = str(r.get("url", ""))
            t = str(r.get("title", "")).lower()
            if u.startswith("http"):
                benign_signals += 1
            if any(x in t for x in ["wikipedia", "reddit", "official", "news", "docs", "guide", "privacy"]):
                benign_signals += 1
        if benign_signals >= 3:
            fp["verdict"] = "SUSPICIOUS"
            fp["confidence"] = 0.55
            fp["note"] = "downgraded_from_injection_false_positive"
    return fp


def _format_search_results_markdown(results: list[dict]) -> str:
    """Render search results in a Google/DuckDuckGo-like markdown list."""
    blocks = []
    for r in (results or []):
        title = (r.get("title") or r.get("url") or "Untitled result").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        if not url:
            continue
        block = [f"### [{title}]({url})", f"{url}"]
        if snippet:
            block.append(snippet)
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def _format_search_results_html(results: list[dict]) -> str:
    """Render results as Google-like cards (no markdown syntax visible)."""
    cards = []
    for r in (results or []):
        raw_url = (r.get("url") or "").strip()
        if not raw_url:
            continue

        p = urlparse(raw_url)
        host = (p.netloc or p.hostname or "").strip().lower()
        path = (p.path or "").strip()
        display_url = host + (path if path and path != "/" else "")
        if len(display_url) > 90:
            display_url = display_url[:87].rstrip("/") + "…"

        title = html.escape((r.get("title") or raw_url or "Untitled result").strip())
        url = html.escape(raw_url)
        snippet = html.escape((r.get("snippet") or "").strip())
        safe_host = html.escape(host or raw_url)
        safe_display_url = html.escape(display_url or raw_url)
        favicon_domain = quote(host or raw_url, safe="")
        favicon = f"https://www.google.com/s2/favicons?domain={favicon_domain}&sz=32"

        snippet_html = f"<div class=\"search-snippet\">{snippet}</div>" if snippet else ""
        cards.append(
            "<article class=\"search-result\">"
            "<div class=\"search-meta\">"
            f"<img class=\"search-favicon\" src=\"{favicon}\" alt=\"\" loading=\"lazy\" referrerpolicy=\"no-referrer\" />"
            "<div class=\"search-site-wrap\">"
            f"<div class=\"search-site\">{safe_host}</div>"
            f"<div class=\"search-url\">{safe_display_url}</div>"
            "</div>"
            "</div>"
            f"<a class=\"search-title\" href=\"{url}\" target=\"_blank\" rel=\"noopener noreferrer\">{title}</a>"
            f"{snippet_html}"
            "</article>"
        )
    if not cards:
        return ""
    return "<section class=\"search-results\">" + "".join(cards) + "</section>"


def _ddg_search_raw(query: str, max_results: int = 5) -> list[dict]:
    max_results = max(1, min(int(max_results or 5), 10))
    q = (query or "").strip()
    if not q:
        return []
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    resp = http_requests.post(url, data={"q": q}, headers=headers, timeout=10, allow_redirects=False, verify=certifi.where())
    if resp.status_code != 200:
        raise RuntimeError(f"DuckDuckGo returned HTTP {resp.status_code}")
    html_doc = resp.text[:700_000]

    # Parse each result block
    blocks = re.findall(r'<div class="result results_links[^>]*>.*?<\/div>\s*<\/div>', html_doc, flags=re.S)
    out = []
    for blk in blocks:
        a = re.search(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)<\/a>', blk, flags=re.S)
        if not a:
            continue
        href = _extract_real_ddg_url(a.group(1))
        title = re.sub(r"<.*?>", "", a.group(2) or "")
        title = html.unescape(re.sub(r"\s+", " ", title).strip())

        sn = re.search(r'<a[^>]*class="result__snippet"[^>]*>(.*?)<\/a>|<div[^>]*class="result__snippet"[^>]*>(.*?)<\/div>', blk, flags=re.S)
        snippet_raw = (sn.group(1) or sn.group(2) or "") if sn else ""
        snippet = re.sub(r"<.*?>", "", snippet_raw)
        snippet = html.unescape(re.sub(r"\s+", " ", snippet).strip())

        if not href or not _safe_public_url(href):
            continue
        out.append({"title": title[:180], "url": href[:500], "snippet": snippet[:800]})
        if len(out) >= max_results:
            break
    return out


def _strip_html_to_text(html_doc: str) -> str:
    body = re.sub(r"<script\b[^>]*>.*?</script>", " ", html_doc, flags=re.I | re.S)
    body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.I | re.S)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"\s+", " ", body).strip()
    return body


def _fetch_public_url(url: str, max_chars: int = 12000) -> dict:
    if not _safe_public_url(url):
        raise ValueError("URL is not allowed (must be public http/https)")

    max_chars = max(2000, min(int(max_chars or 12000), 30000))
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
    }
    resp = http_requests.get(url, headers=headers, timeout=12, allow_redirects=True, verify=certifi.where())
    final_url = resp.url
    if not _safe_public_url(final_url):
        raise ValueError("Redirected to disallowed/private URL")

    ctype = (resp.headers.get("content-type") or "").lower()
    raw = resp.text[:800_000]
    if "html" in ctype or "xml" in ctype:
        extracted = _strip_html_to_text(raw)
    elif ctype.startswith("text/"):
        extracted = raw
    else:
        raise ValueError(f"Unsupported content-type: {ctype or 'unknown'}")

    extracted = extracted[:max_chars]
    return {
        "url": url,
        "final_url": final_url,
        "status": resp.status_code,
        "content_type": ctype,
        "text": extracted,
    }


# --- Browser automation (Playwright) ---
_browser_lock = threading.Lock()
_browser_sessions: dict[str, dict] = {}

_SENSITIVE_CLICK_RE = re.compile(r"\b(submit|place order|buy|sell|trade|checkout|pay|confirm|transfer|wire|send)\b", re.I)

# Optional hostname allowlist for browser navigation/open.
# Comma-separated hosts or suffixes via env, e.g. "schwab.com,finance.yahoo.com"
_BROWSER_ALLOWLIST = [h.strip().lower() for h in (os.getenv("KUKUIBOT_BROWSER_ALLOWLIST", "") or "").split(",") if h.strip()]
# Optional localhost loopback allowance for browser tools.
# Off by default to preserve SSRF guardrails.
# Enable with: KUKUIBOT_BROWSER_ALLOW_LOCALHOST=1
_BROWSER_ALLOW_LOCALHOST = str(os.getenv("KUKUIBOT_BROWSER_ALLOW_LOCALHOST", "")).strip().lower() in {"1", "true", "yes", "on"}


def _load_playwright_sync():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        return None


def _safe_session_key(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", (session_id or "default"))[:64]


def _url_allowed_for_browser(url: str) -> bool:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        is_loopback = host in {"localhost", "127.0.0.1", "::1"}
    except Exception:
        return False

    if is_loopback and _BROWSER_ALLOW_LOCALHOST:
        # Optional dev-mode exception for local KukuiBot UI automation.
        if p.scheme not in ("http", "https"):
            return False
        if _BROWSER_ALLOWLIST:
            for rule in _BROWSER_ALLOWLIST:
                rule = rule.lstrip(".")
                if host == rule or host.endswith("." + rule):
                    return True
            return False
        return True

    if not _safe_public_url(url):
        return False
    if not _BROWSER_ALLOWLIST:
        return True
    if not host:
        return False
    for rule in _BROWSER_ALLOWLIST:
        rule = rule.lstrip(".")
        if host == rule or host.endswith("." + rule):
            return True
    return False


def _browser_profile_dir(profile_name: str, session_id: str) -> Path:
    pname = re.sub(r"[^a-zA-Z0-9_.-]", "_", (profile_name or "default"))[:32]
    skey = _safe_session_key(session_id)
    d = WORKSPACE / ".browser_profiles" / f"{pname}-{skey}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _browser_meta(page) -> dict:
    meta = {"url": "", "title": ""}
    try:
        meta["url"] = page.url
    except Exception:
        pass
    try:
        meta["title"] = page.title()
    except Exception:
        pass
    return meta


def _browser_get(session_id: str) -> dict | None:
    with _browser_lock:
        return _browser_sessions.get(session_id)


def _browser_close(session_id: str) -> dict:
    with _browser_lock:
        entry = _browser_sessions.pop(session_id, None)
    if not entry:
        return {"ok": True, "closed": False, "message": "No active browser session."}

    err = None
    try:
        entry["context"].close()
    except Exception as e:
        err = str(e)
    try:
        entry["playwright"].stop()
    except Exception:
        pass

    out = {
        "ok": err is None,
        "closed": True,
        "opened_at": entry.get("opened_at"),
        "profile_dir": str(entry.get("profile_dir", "")),
    }
    if err:
        out["error"] = err
    return out


def _browser_open(session_id: str, start_url: str = "", headless: bool = False, profile_name: str = "default") -> dict:
    # Ensure single active context per session.
    _browser_close(session_id)

    sync_playwright = _load_playwright_sync()
    if not sync_playwright:
        return {
            "ok": False,
            "error": "Playwright is not installed. Run: pip install playwright && python3 -m playwright install chromium",
        }

    try:
        p = sync_playwright().start()
        profile_dir = _browser_profile_dir(profile_name, session_id)
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=bool(headless),
            viewport={"width": 1440, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        if start_url:
            page.goto(start_url, wait_until="domcontentloaded", timeout=45000)

        entry = {
            "playwright": p,
            "context": context,
            "page": page,
            "opened_at": datetime.utcnow().isoformat() + "Z",
            "profile_dir": profile_dir,
            "headless": bool(headless),
        }
        with _browser_lock:
            _browser_sessions[session_id] = entry

        return {
            "ok": True,
            "session_id": session_id,
            "headless": bool(headless),
            "profile_dir": str(profile_dir),
            **_browser_meta(page),
        }
    except Exception as e:
        return {"ok": False, "error": f"browser_open failed: {type(e).__name__}: {e}"}


def _browser_require_open(session_id: str):
    entry = _browser_get(session_id)
    if not entry:
        raise RuntimeError("No active browser session. Call browser_open first.")
    return entry


# --- Main Tool Executor ---

_DELEG_BLOCKED_RESTART_PATTERNS = [
    re.compile(r"api/restart"),
    re.compile(r"os\._exit"),
    re.compile(r"sys\.exit"),
    re.compile(r"kill\b.*(?:uvicorn|hypercorn|kukuibot|kukuibot|server)"),
    re.compile(r"launchctl\s+(?:stop|remove|unload).*(?:kukuibot|kukuibot)"),
    re.compile(r"pkill\b.*(?:uvicorn|hypercorn|kukuibot|kukuibot|server)"),
]

def _is_deleg_restart_command(cmd: str) -> str | None:
    """If cmd would restart/kill the server and session is delegated, return reason."""
    for pat in _DELEG_BLOCKED_RESTART_PATTERNS:
        if pat.search(cmd):
            return f"Server restart commands are not allowed in delegated sessions. The Dev Manager will coordinate restarts."
    return None


def execute_tool(name: str, input_data: dict, elevation_id: str = None, session_id: str = "default") -> str:
    """Execute a tool and return the result string."""
    start = time.time()
    try:
        log_write("tool_call", f"CALL {name} | {json.dumps(input_data)[:500]}", source="kukuibot.tools", session_id=session_id)
    except Exception:
        pass
    result = _execute_inner(name, input_data, elevation_id, session_id)
    elapsed = time.time() - start
    try:
        log_write("tool_call", f"RESULT {name} | {elapsed:.1f}s | {result[:300]}", source="kukuibot.tools", session_id=session_id)
    except Exception:
        pass
    return result


def _execute_inner(name: str, input_data: dict, elevation_id: str = None, session_id: str = "default") -> str:
    elevated = is_session_elevated(session_id)
    if elevation_id:
        consumed = consume_elevation(elevation_id)
        if consumed:
            elevated = True
        else:
            return "BLOCKED: Elevation request was not approved or has expired."

    is_deleg = str(session_id or "").startswith("deleg-")

    try:
        if name == "bash":
            cmd = input_data.get("command", "")
            if is_deleg:
                restart_reason = _is_deleg_restart_command(cmd)
                if restart_reason:
                    return f"BLOCKED: {restart_reason}"
            if not elevated:
                blocked = check_bash_command(cmd, session_id=session_id)
                if blocked:
                    rid = request_elevation("bash", input_data, blocked, session_id)
                    return f"ELEVATION_REQUIRED:{rid}:{blocked}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=BASH_TIMEOUT, cwd=str(WORKSPACE), env=TOOL_ENV)
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            output = output[:MAX_OUTPUT_CHARS] or "(no output)"
            # Scan output from network commands for prompt injection
            if _is_network_command(cmd):
                output = scan_and_filter(output, source="bash_network")
            return output

        elif name == "read_file":
            fpath = _resolve_path(input_data["path"])
            blocked = check_path_access(fpath, for_write=False, elevated=elevated, session_id=session_id)
            if blocked:
                rid = request_elevation("read_file", input_data, blocked, session_id)
                return f"ELEVATION_REQUIRED:{rid}:{blocked}"
            offset = input_data.get("offset", 1)
            limit = input_data.get("limit", 2000)
            lines = Path(fpath).read_text().splitlines()
            total = len(lines)
            start_idx = max(0, offset - 1)
            selected = lines[start_idx:start_idx + limit]
            result = "\n".join(selected)
            if start_idx + limit < total:
                result += f"\n\n[{total - start_idx - limit} more lines. Use offset={start_idx + limit + 1} to continue.]"
            return result[:MAX_OUTPUT_CHARS]

        elif name == "write_file":
            fpath = _resolve_path(input_data["path"])
            blocked = check_path_access(fpath, for_write=True, elevated=elevated, session_id=session_id)
            if blocked:
                if blocked.startswith("BLOCKED:"):
                    return blocked
                rid = request_elevation("write_file", input_data, blocked, session_id)
                return f"ELEVATION_REQUIRED:{rid}:{blocked}"
            Path(fpath).parent.mkdir(parents=True, exist_ok=True)
            Path(fpath).write_text(input_data["content"])
            return f"Written {len(input_data['content'])} bytes to {fpath}"

        elif name == "edit_file":
            fpath = _resolve_path(input_data["path"])
            blocked = check_path_access(fpath, for_write=True, elevated=elevated, session_id=session_id)
            if blocked:
                if blocked.startswith("BLOCKED:"):
                    return blocked
                rid = request_elevation("edit_file", input_data, blocked, session_id)
                return f"ELEVATION_REQUIRED:{rid}:{blocked}"
            content = Path(fpath).read_text()
            old = input_data["old_text"]
            new = input_data["new_text"]
            if old not in content:
                return f"ERROR: old_text not found in {fpath}"
            count = content.count(old)
            content = content.replace(old, new, 1)
            Path(fpath).write_text(content)
            return f"Replaced {count} occurrence(s) in {fpath}"

        elif name == "spawn_agent":
            task = input_data.get("task", "")
            max_turns = input_data.get("max_turns", 25)
            if not task:
                return "ERROR: No task provided."
            # Import here to avoid circular
            from subagent import run_subagent
            return run_subagent(task, max_turns, session_id)

        elif name == "bash_background":
            command = input_data.get("command", "")
            if not command:
                return "ERROR: No command provided."
            if is_deleg:
                restart_reason = _is_deleg_restart_command(command)
                if restart_reason:
                    return f"BLOCKED: {restart_reason}"
            if not elevated:
                blocked = check_bash_command(command, session_id=session_id)
                if blocked:
                    rid = request_elevation("bash_background", input_data, blocked, session_id)
                    return f"ELEVATION_REQUIRED:{rid}:{blocked}"
            proc_id = _start_background(command)
            return f"Started background process: {proc_id}\nCommand: {command}\nUse bash_check with id='{proc_id}' to poll."

        elif name == "bash_check":
            return _check_background(
                input_data.get("id", ""),
                input_data.get("action", "poll"),
                int(input_data.get("wait_seconds", 0)),
            )

        elif name == "memory_search":
            from memory import search
            query = input_data.get("query", "")
            max_results = input_data.get("max_results", 5)
            return json.dumps(search(query, max_results=max_results), indent=2)

        elif name == "memory_read":
            from memory import read_memory
            return read_memory(
                input_data.get("path", ""),
                from_line=input_data.get("from_line", 1),
                max_lines=input_data.get("max_lines", 100),
            )

        elif name == "scan_content":
            text = input_data.get("text", "")
            source = input_data.get("source", "unknown")
            if not text:
                return "ERROR: No text provided."
            result = scan_text(text, source=source)
            return json.dumps(result, indent=2)

        elif name == "web_search_ddg":
            query = (input_data.get("query") or "").strip()
            max_results = int(input_data.get("max_results", 5) or 5)
            if not query:
                return "ERROR: query is required"

            raw = _ddg_search_raw(query, max_results=max_results)
            corpus = "\n\n".join(f"TITLE: {r.get('title','')}\nURL: {r.get('url','')}\nSNIPPET: {r.get('snippet','')}" for r in raw)

            # First-pass ingress scan
            first_pass = scan_text(corpus, source="web_search") if corpus else {"verdict": "LEGIT", "confidence": 1.0, "pattern_matches": []}
            first_pass = _normalize_web_search_scan(first_pass, raw)

            # Spark no-tool second pass: sanitize + summarize
            from spark_guard import assess_search_results
            assessed = assess_search_results(query=query, results=raw, first_pass=first_pass)

            action = str(assessed.get("action", "ALLOW")).upper()
            if action == "BLOCK":
                payload = {
                    "ok": False,
                    "query": query,
                    "blocked": True,
                    "reason": assessed.get("reason", "Blocked by security policy"),
                    "safe_summary": assessed.get("safe_summary", ""),
                    "results": [],
                    "security": {
                        "first_pass": {
                            "verdict": first_pass.get("verdict"),
                            "confidence": first_pass.get("confidence"),
                            "pattern_count": len(first_pass.get("pattern_matches", []) or []),
                        },
                        "second_pass": {"action": action, "reason": assessed.get("reason", "")},
                    },
                }
                return json.dumps(payload, indent=2)

            safe_results = assessed.get("results") or raw
            safe_results = safe_results[: max(1, min(max_results, 10))]

            markdown_links = [
                f"- [{(r.get('title') or r.get('url') or 'Source').strip()}]({(r.get('url') or '').strip()})"
                for r in safe_results
                if (r.get('url') or '').strip()
            ]
            results_markdown = _format_search_results_markdown(safe_results)
            results_html = _format_search_results_html(safe_results)

            payload = {
                "ok": True,
                "query": query,
                "safe_summary": assessed.get("safe_summary", ""),
                "results": safe_results,
                "citation_urls": [r.get("url") for r in safe_results if r.get("url")],
                "links_markdown": "\n".join(markdown_links),
                "results_markdown": results_markdown,
                "results_html": results_html,
                "security": {
                    "first_pass": {
                        "verdict": first_pass.get("verdict"),
                        "confidence": first_pass.get("confidence"),
                        "pattern_count": len(first_pass.get("pattern_matches", []) or []),
                    },
                    "second_pass": {"action": action, "reason": assessed.get("reason", "")},
                },
            }
            return json.dumps(payload, indent=2)

        elif name == "web_fetch":
            url = (input_data.get("url") or "").strip()
            max_chars = int(input_data.get("max_chars", 12000) or 12000)
            if not url:
                return "ERROR: url is required"

            fetched = _fetch_public_url(url, max_chars=max_chars)
            scanned = scan_and_filter(fetched.get("text", ""), source="web_fetch")
            blocked = scanned.startswith("[CONTENT BLOCKED:") if scanned else False

            payload = {
                "ok": not blocked,
                "url": fetched.get("url"),
                "final_url": fetched.get("final_url"),
                "status": fetched.get("status"),
                "content_type": fetched.get("content_type"),
                "blocked": blocked,
                "text": scanned,
            }
            return json.dumps(payload, indent=2)

        elif name == "browser_open":
            start_url = (input_data.get("start_url") or "").strip()
            headless = bool(input_data.get("headless", False))
            profile_name = (input_data.get("profile_name") or "default").strip() or "default"
            if start_url and not _url_allowed_for_browser(start_url):
                return "ERROR: start_url is not allowed (must be public http/https and match KUKUIBOT_BROWSER_ALLOWLIST if configured)"
            payload = _browser_open(session_id=session_id, start_url=start_url, headless=headless, profile_name=profile_name)
            return json.dumps(payload, indent=2)

        elif name == "browser_navigate":
            url = (input_data.get("url") or "").strip()
            if not url:
                return "ERROR: url is required"
            if not _url_allowed_for_browser(url):
                return "ERROR: url is not allowed (must be public http/https and match KUKUIBOT_BROWSER_ALLOWLIST if configured)"
            wait_until = (input_data.get("wait_until") or "domcontentloaded").strip().lower()
            if wait_until not in {"load", "domcontentloaded", "networkidle"}:
                wait_until = "domcontentloaded"
            entry = _browser_require_open(session_id)
            page = entry["page"]
            page.goto(url, wait_until=wait_until, timeout=45000)
            return json.dumps({"ok": True, **_browser_meta(page)}, indent=2)

        elif name == "browser_click":
            selector = (input_data.get("selector") or "").strip()
            if not selector:
                return "ERROR: selector is required"
            text_hint = (input_data.get("text_hint") or "").strip()
            force = bool(input_data.get("force", False))

            if not elevated and not force and _SENSITIVE_CLICK_RE.search(f"{selector} {text_hint}"):
                reason = "Sensitive browser action requires approval (submit/buy/sell/trade/pay/checkout/confirm/transfer)."
                rid = request_elevation("browser_click", input_data, reason, session_id)
                return f"ELEVATION_REQUIRED:{rid}:{reason}"

            entry = _browser_require_open(session_id)
            page = entry["page"]
            page.locator(selector).first.click(timeout=15000)
            return json.dumps({"ok": True, **_browser_meta(page), "clicked": selector}, indent=2)

        elif name == "browser_type":
            selector = (input_data.get("selector") or "").strip()
            text = input_data.get("text", "")
            press_enter = bool(input_data.get("press_enter", False))
            if not selector:
                return "ERROR: selector is required"
            entry = _browser_require_open(session_id)
            page = entry["page"]
            locator = page.locator(selector).first
            locator.click(timeout=15000)
            locator.fill(str(text))
            if press_enter:
                locator.press("Enter")
            return json.dumps({"ok": True, **_browser_meta(page), "typed_into": selector}, indent=2)

        elif name == "browser_extract":
            selector = (input_data.get("selector") or "body").strip() or "body"
            fmt = (input_data.get("format") or "text").strip().lower()
            max_chars = max(1000, min(int(input_data.get("max_chars", 12000) or 12000), 60000))
            if fmt not in {"text", "html"}:
                fmt = "text"
            entry = _browser_require_open(session_id)
            page = entry["page"]
            loc = page.locator(selector).first
            if fmt == "html":
                content = loc.inner_html(timeout=15000)
            else:
                content = loc.inner_text(timeout=15000)
            content = (content or "")[:max_chars]
            scanned = scan_and_filter(content, source="browser_extract")
            return json.dumps({"ok": True, **_browser_meta(page), "selector": selector, "format": fmt, "content": scanned}, indent=2)

        elif name == "browser_snapshot":
            rel = (input_data.get("path") or "").strip()
            full_page = bool(input_data.get("full_page", True))
            entry = _browser_require_open(session_id)
            page = entry["page"]

            shots_dir = WORKSPACE / "artifacts" / "browser"
            shots_dir.mkdir(parents=True, exist_ok=True)
            if rel:
                out_path = Path(_resolve_path(rel))
            else:
                stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                out_path = shots_dir / f"{_safe_session_key(session_id)}-{stamp}.png"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out_path), full_page=full_page)
            return json.dumps({"ok": True, **_browser_meta(page), "path": str(out_path), "full_page": full_page}, indent=2)

        elif name == "browser_close":
            payload = _browser_close(session_id)
            return json.dumps(payload, indent=2)

        elif name == "codebase_outline":
            mode = input_data.get("mode", "")
            outline_path = _resolve_path(input_data.get("path", ""))
            blocked = check_path_access(outline_path, for_write=False, elevated=elevated, session_id=session_id)
            if blocked:
                rid = request_elevation("codebase_outline", input_data, blocked, session_id)
                return f"ELEVATION_REQUIRED:{rid}:{blocked}"
            sym_name = input_data.get("name")
            result = _codebase_outline(mode, outline_path, sym_name)
            return result[:MAX_OUTPUT_CHARS]

        elif name == "delegate_task":
            from delegation import delegate_task
            return delegate_task(
                parent_session_id=session_id,
                worker=input_data.get("worker", ""),
                prompt=input_data.get("prompt", ""),
                model=input_data.get("model", ""),
                force=bool(input_data.get("force", False)),
            )

        elif name == "check_task":
            from delegation import check_task
            return check_task(
                task_id=input_data.get("task_id", ""),
                parent_session_id=session_id,
            )

        elif name == "list_tasks":
            from delegation import list_tasks
            return list_tasks(parent_session_id=session_id)

        else:
            return f"Unknown tool: {name}"

    except subprocess.TimeoutExpired:
        return "ERROR: Command timed out (30min limit)"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
