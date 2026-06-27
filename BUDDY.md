# Code Buddy Project Instructions

Code Buddy is a Windows-first Python coding agent. Runtime behavior must stay project-bound: reads, writes, commands, git, journals, plans, skills, and history belong to the selected target project.

## Engineering Rules
- Use Python only for the agent runtime.
- Keep edits brokered, journaled, validated, and reversible where practical.
- Prefer project-local state under `.pyagent/`.
- Prefer reusable project skills under `.buddy/skills/`.
- Do not add Node.js dependencies.

## Validation
- Run `py -3.12 -m unittest discover -s tests` before claiming production readiness.
- Add focused regression tests for every fixed bug.

## Safety
- Never let the implementation repo become the implicit target project.
- Commands must run through policy and in the selected project root.
- Approval prompts must be stateful and execute approved actions once.
