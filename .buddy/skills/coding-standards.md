# Coding Standards Skill

Use this skill for implementation and refactoring work.

- Follow Google Python style: readable names, small functions, clear module boundaries, and explicit error handling.
- Prefer simple standard-library constructs over custom abstractions until duplication or complexity makes an abstraction pay rent.
- Keep formatting stable and compatible with common formatters such as black and ruff.
- Avoid broad exception swallowing; when catching exceptions, preserve useful error context.
- Keep edits reversible: small slices, validation after each slice, checkpoint commits when configured.
- Update tests and documentation when behavior changes.
