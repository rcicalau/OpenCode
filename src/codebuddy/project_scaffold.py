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
- `documentation.md`
- `test-writing.md`
- `coding-standards.md`
- `debugging.md`
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
    "documentation.md": """# Documentation Skill

Use this skill when writing or improving code documentation.

- Follow Google Python docstring style for modules, classes, functions, methods, and complex properties.
- Write for junior developers: explain intent, important inputs, return values, side effects, and failure modes.
- Prefer tutorial-like clarity for unfamiliar flows, but keep comments attached to genuinely non-obvious code.
- Do not add noisy comments that simply repeat the next line of code.
- Preserve public behavior while documenting; documentation-only tasks should not refactor code.
- Use consistent formatting: short summary line, blank line, Args, Returns, Raises, Yields, and Examples sections when relevant.
""",
    "test-writing.md": """# Test Writing Skill

Use this skill when creating or expanding tests.

- Prefer the project's existing test framework, fixtures, naming, and folder layout.
- Use pytest style when the project has no clear test convention; use unittest only when the codebase already does.
- Build tests from visible behavior: arrange, act, assert, with clear test names and focused assertions.
- Cover success paths, edge cases, error handling, path handling, approval/permission branches, and regression cases for reported bugs.
- Keep production edits out of test-writing slices unless the tests reveal a real bug and the objective includes fixing it.
- Run the narrowest useful test command first, then broaden validation when the slice is stable.
""",
    "coding-standards.md": """# Coding Standards Skill

Use this skill for implementation and refactoring work.

- Follow Google Python style: readable names, small functions, clear module boundaries, and explicit error handling.
- Prefer simple standard-library constructs over custom abstractions until duplication or complexity makes an abstraction pay rent.
- Keep formatting stable and compatible with common formatters such as black and ruff.
- Avoid broad exception swallowing; when catching exceptions, preserve useful error context.
- Keep edits reversible: small slices, validation after each slice, checkpoint commits when configured.
- Update tests and documentation when behavior changes.
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
        ".buddy/steering",
    ]:
        (root / relative).mkdir(parents=True, exist_ok=True)
    skills_readme = root / ".buddy" / "skills" / "README.md"
    if not skills_readme.exists():
        skills_readme.write_text(SKILL_README, encoding="utf-8")
    for name, content in BASE_SKILLS.items():
        path = root / ".buddy" / "skills" / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
