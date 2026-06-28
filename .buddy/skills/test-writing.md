# Test Writing Skill

Use this skill when creating or expanding tests.

- Prefer the project's existing test framework, fixtures, naming, and folder layout.
- Use pytest style when the project has no clear test convention; use unittest only when the codebase already does.
- Build tests from visible behavior: arrange, act, assert, with clear test names and focused assertions.
- Cover success paths, edge cases, error handling, path handling, approval/permission branches, and regression cases for reported bugs.
- Keep production edits out of test-writing slices unless the tests reveal a real bug and the objective includes fixing it.
- Run the narrowest useful test command first, then broaden validation when the slice is stable.
