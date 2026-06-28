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

BASE_SKILLS = {
    "reasoning.md": """# Reasoning Skill

Use this skill for broad or ambiguous work.

- Restate the objective in one sentence before acting.
- Separate known facts from assumptions.
- Ask a clarifying question when a missing decision could change the implementation.
- Prefer a small plan with validation checkpoints over a large opaque rewrite.
- Stop and report a real blocker instead of looping on the same failed action.
""",
    "development.md": """# Development Skill

Use this skill when editing code.

- Inspect the local pattern before changing code.
- Make the smallest coherent change that satisfies the objective.
- Route all file writes through the edit broker.
- Preserve user changes outside the requested scope.
- Validate the changed behavior before calling the work complete.
""",
    "testing.md": """# Testing Skill

Use this skill when adding or repairing tests.

- Reproduce the bug or expected behavior with a focused test first when practical.
- Prefer project-native test tools and existing fixtures.
- Add edge cases for path handling, missing files, approvals, and retries.
- Run targeted tests before broad suites.
""",
    "debugging.md": """# Debugging Skill

Use this skill for broken behavior.

- Reproduce the failure with the smallest command or test.
- Inspect the exact error path and the state that led there.
- Patch the root cause, then add a regression test.
- Avoid masking failures with broad exception handling unless the user-facing contract requires it.
""",
}


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
    for name, content in BASE_SKILLS.items():
        path = root / ".buddy" / "skills" / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
