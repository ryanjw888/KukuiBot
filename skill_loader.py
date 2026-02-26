"""
skill_loader.py — Load composable skills for worker roles.

Skills live in ~/.kukuibot/skills/ as standalone markdown files.
A _meta.json registry maps skills to workers and defines load order.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("skill_loader")

# Maximum total characters for all skills combined per worker
MAX_SKILLS_CHARS = 20000


def load_skills_for_worker(worker_identity: str, skills_dir: Path) -> list[str]:
    """Load skills applicable to the given worker role.

    Reads _meta.json, filters by worker, loads SKILL.md files in priority order.
    Returns a list of formatted skill section strings.
    """
    meta_file = skills_dir / "_meta.json"
    if not meta_file.is_file():
        return []

    try:
        meta = json.loads(meta_file.read_text())
    except Exception as e:
        logger.warning(f"Failed to parse skills _meta.json: {e}")
        return []

    skills = meta.get("skills", [])
    if not skills:
        return []

    # Filter skills applicable to this worker (or wildcard "*")
    applicable = []
    for skill in skills:
        workers = skill.get("workers", [])
        if worker_identity in workers or "*" in workers:
            applicable.append(skill)

    if not applicable:
        return []

    # Sort by priority (lower number = higher priority)
    applicable.sort(key=lambda s: s.get("priority", 99))

    sections = []
    total_chars = 0

    for skill in applicable:
        skill_file = skills_dir / skill.get("file", "")
        if not skill_file.is_file():
            logger.warning(f"Skill file not found: {skill_file}")
            continue

        try:
            content = skill_file.read_text()
        except Exception as e:
            logger.warning(f"Failed to read skill {skill.get('id', '?')}: {e}")
            continue

        # Enforce per-skill max
        max_chars = skill.get("max_chars", 4000)
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...(truncated)"

        # Enforce total budget
        if total_chars + len(content) > MAX_SKILLS_CHARS:
            logger.info(
                f"Skills budget exhausted ({total_chars}/{MAX_SKILLS_CHARS} chars), "
                f"skipping remaining skills for worker={worker_identity}"
            )
            break

        skill_id = skill.get("id", skill_file.stem)
        sections.append(f"## Skill: {skill_id}\n{content}")
        total_chars += len(content)

    return sections
