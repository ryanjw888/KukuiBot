"""
routes/projects.py — Project context registry CRUD + auto-scan.

Manages project definitions used by the Project Context Switcher.
Each project maps a root_path to context files (CLAUDE.md, README.md, key files)
that get injected into model context when a tab selects that project.
"""

import json
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth import db_connection
from routes.tabs import _ensure_tab_meta_schema

logger = logging.getLogger("kukuibot.projects")

router = APIRouter()

# --- Security ---

ALLOWED_ROOTS = [Path("/Users/jarvis")]

VALID_STATUSES = {"active", "archived", "paused"}

EXCLUDED_PATTERNS = re.compile(
    r"(\.env$|\.credentials|secrets?|password|key\.json|id_rsa|\.p12$|\.pfx$|\.pem$)",
    re.IGNORECASE,
)


def validate_project_path(path_str: str) -> Path:
    """Validate a project root path is safe and exists."""
    if not path_str or not path_str.strip():
        raise ValueError("Path is empty")
    p = Path(path_str).resolve()
    if not any(p.is_relative_to(root) for root in ALLOWED_ROOTS):
        raise ValueError(f"Path {p} is outside allowed roots")
    if ".." in str(p):
        raise ValueError(f"Path traversal detected in {p}")
    if not p.is_dir():
        raise ValueError(f"Path {p} is not a directory")
    return p


# --- Default Projects ---

DEFAULT_PROJECTS = [
    {
        "id": "kukuibot",
        "name": "KukuiBot",
        "root_path": "/Users/jarvis/.kukuibot/src",
        "description": "Multi-model AI agent platform",
        "key_files": ["README.md", "docs/"],
        "context_budget": 8000,
    },
    {
        "id": "jarvis",
        "name": "Jarvis",
        "root_path": "/Users/jarvis/.jarvis",
        "description": "Home automation backend",
        "key_files": ["CLAUDE.md", "src/backend/main.py"],
        "context_budget": 8000,
    },
]


def seed_default_projects():
    """Insert default projects if the projects table is empty."""
    try:
        with db_connection() as db:
            _ensure_tab_meta_schema(db)  # ensures projects table exists too
            count = db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            if count > 0:
                return
            now = int(time.time())
            for proj in DEFAULT_PROJECTS:
                db.execute(
                    """INSERT OR IGNORE INTO projects (id, name, root_path, description, key_files, context_budget, auto_scan, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (
                        proj["id"],
                        proj["name"],
                        proj["root_path"],
                        proj.get("description", ""),
                        json.dumps(proj.get("key_files", [])),
                        proj.get("context_budget", 8000),
                        proj.get("status", "active"),
                        now,
                        now,
                    ),
                )
            db.commit()
            logger.info(f"Seeded {len(DEFAULT_PROJECTS)} default projects")
    except Exception as e:
        logger.warning(f"Failed to seed default projects: {e}")


# --- Endpoints ---

@router.get("/api/projects")
async def api_list_projects(request: Request):
    """List all registered projects. Optional ?status=active|archived|paused filter."""
    try:
        status_filter = request.query_params.get("status")
        with db_connection() as db:
            _ensure_tab_meta_schema(db)
            if status_filter and status_filter in VALID_STATUSES:
                rows = db.execute(
                    "SELECT id, name, root_path, description, key_files, context_budget, auto_scan, status, created_at, updated_at FROM projects WHERE status = ? ORDER BY name",
                    (status_filter,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id, name, root_path, description, key_files, context_budget, auto_scan, status, created_at, updated_at FROM projects ORDER BY name"
                ).fetchall()
        projects = []
        for row in rows:
            projects.append({
                "id": row[0],
                "name": row[1],
                "root_path": row[2],
                "description": row[3] or "",
                "key_files": json.loads(row[4]) if row[4] else [],
                "context_budget": row[5] or 8000,
                "auto_scan": bool(row[6]),
                "status": row[7] or "active",
                "created_at": row[8] or 0,
                "updated_at": row[9] or 0,
            })
        return {"projects": projects}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/projects")
async def api_create_project(request: Request):
    """Create a new project."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    project_id = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    root_path = str(body.get("root_path") or "").strip()

    if not project_id or not name or not root_path:
        return JSONResponse({"error": "id, name, and root_path are required"}, status_code=400)

    # Validate path security
    try:
        validated_path = validate_project_path(root_path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    description = str(body.get("description") or "").strip()
    key_files = body.get("key_files", [])
    if not isinstance(key_files, list):
        key_files = []
    context_budget = int(body.get("context_budget", 8000) or 8000)
    status = str(body.get("status", "active")).strip()
    if status not in VALID_STATUSES:
        return JSONResponse({"error": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}"}, status_code=400)

    now = int(time.time())
    try:
        with db_connection() as db:
            _ensure_tab_meta_schema(db)
            existing = db.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if existing:
                return JSONResponse({"error": f"Project '{project_id}' already exists"}, status_code=409)
            db.execute(
                """INSERT INTO projects (id, name, root_path, description, key_files, context_budget, auto_scan, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (project_id, name, str(validated_path), description, json.dumps(key_files), context_budget, status, now, now),
            )
            db.commit()
        return {"ok": True, "id": project_id}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/projects/{project_id}")
async def api_update_project(project_id: str, request: Request):
    """Update an existing project."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    try:
        with db_connection() as db:
            _ensure_tab_meta_schema(db)
            existing = db.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not existing:
                return JSONResponse({"error": f"Project '{project_id}' not found"}, status_code=404)

            updates = []
            params = []

            if "name" in body:
                updates.append("name = ?")
                params.append(str(body["name"]).strip())
            if "root_path" in body:
                try:
                    validated = validate_project_path(str(body["root_path"]))
                except ValueError as e:
                    return JSONResponse({"error": str(e)}, status_code=400)
                updates.append("root_path = ?")
                params.append(str(validated))
            if "description" in body:
                updates.append("description = ?")
                params.append(str(body["description"]).strip())
            if "key_files" in body:
                kf = body["key_files"]
                if not isinstance(kf, list):
                    kf = []
                updates.append("key_files = ?")
                params.append(json.dumps(kf))
            if "context_budget" in body:
                updates.append("context_budget = ?")
                params.append(int(body["context_budget"] or 8000))
            if "auto_scan" in body:
                updates.append("auto_scan = ?")
                params.append(1 if body["auto_scan"] else 0)
            if "status" in body:
                s = str(body["status"]).strip()
                if s not in VALID_STATUSES:
                    return JSONResponse({"error": f"Invalid status '{s}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}"}, status_code=400)
                updates.append("status = ?")
                params.append(s)

            if not updates:
                return {"ok": True, "id": project_id, "changed": 0}

            updates.append("updated_at = ?")
            params.append(int(time.time()))
            params.append(project_id)

            db.execute(f"UPDATE projects SET {', '.join(updates)} WHERE id = ?", params)
            db.commit()
        return {"ok": True, "id": project_id}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str):
    """Delete a project."""
    try:
        with db_connection() as db:
            _ensure_tab_meta_schema(db)
            existing = db.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not existing:
                return JSONResponse({"error": f"Project '{project_id}' not found"}, status_code=404)
            db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            db.commit()
        return {"ok": True, "deleted": project_id}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/projects/scan")
async def api_scan_project(request: Request):
    """Auto-detect context files for a given root_path."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    root_path = str(body.get("root_path") or "").strip()
    if not root_path:
        return JSONResponse({"error": "root_path is required"}, status_code=400)

    try:
        root = validate_project_path(root_path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Detect priority files
    priority_files = ["CLAUDE.md", "README.md", "pyproject.toml", "package.json"]
    detected_files = []
    suggested_key_files = []
    for fname in priority_files:
        fpath = root / fname
        if fpath.is_file():
            try:
                size = fpath.stat().st_size
                detected_files.append({"name": fname, "size": size, "type": "file"})
                suggested_key_files.append(fname)
            except Exception:
                pass

    # Generate shallow file tree (depth 2, max 50 entries)
    tree_lines = []
    entry_count = 0
    try:
        for entry in sorted(root.iterdir()):
            if entry_count >= 50:
                tree_lines.append("... (more)")
                break
            name = entry.name
            # Skip hidden dirs (except .github)
            if name.startswith(".") and name not in [".github"]:
                continue
            # Skip sensitive files
            if EXCLUDED_PATTERNS.search(name):
                continue
            if entry.is_dir():
                sub_count = 0
                sub_lines = []
                try:
                    for sub in sorted(entry.iterdir()):
                        if sub.name.startswith("."):
                            continue
                        if EXCLUDED_PATTERNS.search(sub.name):
                            continue
                        sub_count += 1
                        if len(sub_lines) < 10:
                            prefix = "f" if sub.is_file() else "d"
                            sub_lines.append(f"    {sub.name}{'/' if sub.is_dir() else ''}")
                except PermissionError:
                    pass
                tree_lines.append(f"  {name}/ ({sub_count} entries)")
                tree_lines.extend(sub_lines[:5])
                if sub_count > 5:
                    tree_lines.append(f"    ... ({sub_count - 5} more)")
            else:
                tree_lines.append(f"  {name}")
            entry_count += 1
    except PermissionError:
        pass

    tree_preview = "\n".join(tree_lines) if tree_lines else "(empty)"

    return {
        "root_path": str(root),
        "detected_files": detected_files,
        "tree_preview": tree_preview,
        "suggested_key_files": suggested_key_files,
    }
