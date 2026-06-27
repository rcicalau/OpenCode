from __future__ import annotations

from pathlib import Path


BUDDY_TEMPLATE = """# BUDDY.md

Project instructions for Code Buddy.

## Project Rules
- Keep changes scoped to this project.
- Prefer small, validated edits.
- Record assumptions before making broad changes.

## Validation
- Add project-specific validation commands here.
- Note commands that are unsafe, slow, or require approval.

## Skills
- Add reusable guidance under `.buddy/skills/`.
- Keep each skill focused on one workflow.
"""


SKILL_README = """# Project Skills

Add project-specific Code Buddy skills in this folder.

Suggested files:
- `docs.md`
- `testing.md`
- `debugging.md`
- `architecture.md`
"""


def ensure_buddy_scaffold(project_root: Path) -> None:
    root = project_root.resolve()
    buddy_path = root / "BUDDY.md"
    if not buddy_path.exists():
        buddy_path.write_text(BUDDY_TEMPLATE, encoding="utf-8")
    for relative in [
        ".buddy/skills",
        ".buddy/templates",
        ".buddy/validators",
        ".buddy/tools",
    ]:
        (root / relative).mkdir(parents=True, exist_ok=True)
    skills_readme = root / ".buddy" / "skills" / "README.md"
    if not skills_readme.exists():
        skills_readme.write_text(SKILL_README, encoding="utf-8")
