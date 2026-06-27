from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from .command_broker import CommandBroker, CommandResult
from .errors import ConfirmationRequired


@dataclass(slots=True)
class ValidationResult:
    passed: bool
    command_results: list[CommandResult] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


class ValidationHarness:
    def __init__(self, project_root: Path, command_broker: CommandBroker, commands: list[str] | None = None) -> None:
        self.project_root = project_root.resolve()
        self.command_broker = command_broker
        self.commands = commands or []

    def validate(self, touched_files: list[Path] | None = None) -> ValidationResult:
        result = ValidationResult(passed=True)
        for path in touched_files or []:
            if path.suffix == ".py" and path.exists():
                try:
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                    result.checks.append(f"python syntax ok: {path}")
                except SyntaxError as exc:
                    result.passed = False
                    result.failures.append(f"python syntax failed: {path}: {exc}")
        for command in self.commands:
            try:
                command_result = self.command_broker.run(command, cwd=self.project_root)
            except ConfirmationRequired as exc:
                result.passed = False
                result.failures.append(f"validation command requires confirmation: {command}: {exc}")
                continue
            result.command_results.append(command_result)
            if command_result.exit_code != 0:
                result.passed = False
                result.failures.append(f"command failed ({command_result.exit_code}): {command}")
        return result
