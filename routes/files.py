"""
routes/files.py — File browser & editor API for the KukuiBot file editor.

Endpoints:
  GET  /api/files/tree   — list directory contents
  GET  /api/files/read   — read file contents
  POST /api/files/write  — save file contents
"""

import logging
import mimetypes
import os
import stat

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth import is_localhost, get_request_user
from config import WORKSPACE
from security import check_path_access

logger = logging.getLogger("kukuibot.files")

router = APIRouter()

# Max file size we'll serve to the editor (2 MB)
MAX_READ_BYTES = 2 * 1024 * 1024

# Directories hidden from the file tree by default
_HIDDEN_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", ".eggs", "*.egg-info",
}

# File extension → Ace editor mode name
_EXT_TO_ACE_MODE = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".json": "json",
    ".md": "markdown", ".markdown": "markdown",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml", ".svg": "svg",
    ".sql": "sql",
    ".sh": "sh", ".bash": "sh", ".zsh": "sh",
    ".rb": "ruby",
    ".go": "golang",
    ".rs": "rust",
    ".java": "java",
    ".c": "c_cpp", ".cpp": "c_cpp", ".h": "c_cpp", ".hpp": "c_cpp",
    ".swift": "swift",
    ".r": "r", ".R": "r",
    ".lua": "lua",
    ".pl": "perl", ".pm": "perl",
    ".php": "php",
    ".ini": "ini", ".cfg": "ini",
    ".dockerfile": "dockerfile",
    ".env": "text",
    ".txt": "text", ".log": "text", ".csv": "text",
    ".conf": "text",
}

# Filenames (no extension) that map to specific modes
_NAME_TO_ACE_MODE = {
    "Makefile": "makefile",
    "Dockerfile": "dockerfile",
    "Gemfile": "ruby",
    "Rakefile": "ruby",
    "Vagrantfile": "ruby",
    ".gitignore": "text",
    ".dockerignore": "text",
    ".editorconfig": "ini",
}


def _ace_mode(filename: str) -> str:
    """Determine the Ace editor mode from a filename."""
    if filename in _NAME_TO_ACE_MODE:
        return _NAME_TO_ACE_MODE[filename]
    _, ext = os.path.splitext(filename)
    return _EXT_TO_ACE_MODE.get(ext.lower(), "text")


def _is_binary(path: str, chunk_size: int = 8192) -> bool:
    """Quick heuristic: read first chunk and look for null bytes."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(chunk_size)
        return b"\x00" in chunk
    except Exception:
        return True


def _resolve_and_guard(raw_path: str, *, for_write: bool = False) -> tuple[str | None, str | None]:
    """Resolve a user-supplied path and run security checks.

    Returns (resolved_path, error_message). If error_message is set, the request
    should be rejected with that message.
    """
    if not raw_path:
        return None, "path parameter is required"

    # Expand ~ and resolve
    expanded = os.path.expanduser(raw_path)
    resolved = os.path.realpath(expanded)

    # Security: use the same path guard as tool calls
    blocked = check_path_access(resolved, for_write=for_write, elevated=False)
    if blocked:
        return None, blocked

    return resolved, None


@router.get("/api/files/tree")
async def api_files_tree(request: Request, path: str = "", show_hidden: bool = False):
    """List directory contents for the file tree sidebar."""

    # Default to workspace root
    if not path:
        path = str(WORKSPACE)

    resolved, err = _resolve_and_guard(path)
    if err:
        return JSONResponse({"error": err}, status_code=403)

    if not os.path.isdir(resolved):
        return JSONResponse({"error": "Not a directory"}, status_code=400)

    entries = []
    try:
        with os.scandir(resolved) as it:
            for entry in it:
                name = entry.name

                # Skip hidden dotfiles unless requested
                if not show_hidden and name.startswith("."):
                    continue

                # Skip blacklisted directories
                if entry.is_dir(follow_symlinks=False) and name in _HIDDEN_DIRS:
                    continue

                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                is_dir = stat.S_ISDIR(st.st_mode)
                entries.append({
                    "name": name,
                    "path": os.path.join(resolved, name),
                    "type": "dir" if is_dir else "file",
                    "size": st.st_size if not is_dir else None,
                    "modified": st.st_mtime,
                })
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)

    # Sort: directories first, then alphabetically (case-insensitive)
    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))

    return {"path": resolved, "entries": entries}


@router.get("/api/files/read")
async def api_files_read(request: Request, path: str = ""):
    """Read a file's contents for the editor."""

    resolved, err = _resolve_and_guard(path)
    if err:
        return JSONResponse({"error": err}, status_code=403)

    if not os.path.isfile(resolved):
        return JSONResponse({"error": "Not a file"}, status_code=400)

    # Size check
    try:
        size = os.path.getsize(resolved)
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    if size > MAX_READ_BYTES:
        return JSONResponse({
            "error": f"File is too large ({size:,} bytes). Max is {MAX_READ_BYTES:,} bytes.",
            "size": size,
            "max": MAX_READ_BYTES,
        }, status_code=413)

    # Binary check
    if _is_binary(resolved):
        return JSONResponse({"error": "Binary file — cannot edit"}, status_code=415)

    # Read content
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    filename = os.path.basename(resolved)
    return {
        "path": resolved,
        "content": content,
        "size": size,
        "language": _ace_mode(filename),
        "readonly": not os.access(resolved, os.W_OK),
    }


@router.post("/api/files/write")
async def api_files_write(request: Request):
    """Save file contents from the editor."""

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    path = body.get("path", "")
    content = body.get("content")

    if content is None:
        return JSONResponse({"error": "content field is required"}, status_code=400)

    resolved, err = _resolve_and_guard(path, for_write=True)
    if err:
        return JSONResponse({"error": err}, status_code=403)

    # Create parent directories if needed
    parent = os.path.dirname(resolved)
    if not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return JSONResponse({"error": f"Cannot create directory: {e}"}, status_code=500)

    # Write
    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    size = os.path.getsize(resolved)
    logger.info("File saved: %s (%d bytes)", resolved, size)

    return {"ok": True, "path": resolved, "size": size}
