"""
skill_loader.py — Load composable skills for worker roles.

Skills live in ~/.kukuibot/skills/{worker-identity}/ as standalone markdown files.
Each worker has its own folder. Whatever .md files are in the folder get loaded.
Files are sorted alphabetically (use numeric prefixes like 00-, 01- for ordering).
"""

import logging
from pathlib import Path

logger = logging.getLogger("skill_loader")

# Maximum total characters for all skills combined per worker
MAX_SKILLS_CHARS = 20000
# Maximum characters per individual skill file
MAX_SKILL_CHARS = 5500


def load_skills_for_worker(worker_identity: str, skills_dir: Path) -> list[str]:
    """Load skills for a worker from its dedicated folder.

    Scans skills_dir/{worker_identity}/*.md, sorted alphabetically.
    Returns a list of formatted skill section strings.
    """
    worker_dir = skills_dir / worker_identity
    if not worker_dir.is_dir():
        return []

    skill_files = sorted(worker_dir.glob("*.md"))
    if not skill_files:
        return []

    sections = []
    total_chars = 0

    for skill_file in skill_files:
        try:
            content = skill_file.read_text()
        except Exception as e:
            logger.warning(f"Failed to read skill {skill_file.name}: {e}")
            continue

        # Enforce per-skill max
        if len(content) > MAX_SKILL_CHARS:
            content = content[:MAX_SKILL_CHARS] + "\n...(truncated)"

        # Enforce total budget
        if total_chars + len(content) > MAX_SKILLS_CHARS:
            logger.info(
                f"Skills budget exhausted ({total_chars}/{MAX_SKILLS_CHARS} chars), "
                f"skipping remaining skills for worker={worker_identity}"
            )
            break

        # Derive skill ID from filename (strip numeric prefix like "00-")
        stem = skill_file.stem
        skill_id = stem.lstrip("0123456789-") or stem
        sections.append(f"## Skill: {skill_id}\n{content}")
        total_chars += len(content)

    return sections


def list_skills_for_worker(worker_identity: str, skills_dir: Path) -> list[dict]:
    """List skill metadata for a worker (used by the API/frontend).

    Returns list of {id, description, file} dicts.
    """
    worker_dir = skills_dir / worker_identity
    if not worker_dir.is_dir():
        return []

    skill_files = sorted(worker_dir.glob("*.md"))
    skills = []

    for skill_file in skill_files:
        stem = skill_file.stem
        skill_id = stem.lstrip("0123456789-") or stem
        # Extract first non-empty, non-heading line as description
        description = ""
        try:
            for line in skill_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:120]
                    break
        except Exception:
            pass

        skills.append({
            "id": skill_id,
            "description": description,
            "file": f"{worker_identity}/{skill_file.name}",
        })

    return skills
