from __future__ import annotations

import ast
import inspect
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .command_broker import CommandBroker, CommandResult
from .errors import ConfirmationRequired
from .hashutil import sha256_bytes


@dataclass(slots=True)
class ValidationResult:
    passed: bool
    command_results: list[CommandResult] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    failure_code: str | None = None
    recovery_actions: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    unexpected_files: list[str] = field(default_factory=list)
    worktree_report: str = ""
    tiers: list[str] = field(default_factory=list)


class ValidationHarness:
    def __init__(self, project_root: Path, command_broker: CommandBroker, commands: list[str] | None = None) -> None:
        self.project_root = project_root.resolve()
        self.command_broker = command_broker
        self.commands = commands or []

    def validate(
        self,
        touched_files: list[Path] | None = None,
        expected_files: list[Path] | None = None,
        allowed_existing_changes: dict[str, str] | None = None,
    ) -> ValidationResult:
        result = ValidationResult(passed=True)
        result.tiers.append("syntax")
        for path in touched_files or []:
            result.touched_files.append(self._relative(path))
            if path.suffix == ".py" and path.exists():
                try:
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                    result.checks.append(f"python syntax ok: {path}")
                except SyntaxError as exc:
                    result.passed = False
                    result.failures.append(f"python syntax failed: {path}: {exc}")
        if expected_files is not None:
            result.tiers.append("worktree")
            unexpected = self._unexpected_changed_files(expected_files, allowed_existing_changes or {})
            if unexpected:
                result.passed = False
                result.failure_code = "unexpected_worktree_changes"
                result.unexpected_files = unexpected
                result.worktree_report = self._git_status_report()
                result.recovery_actions = [
                    "git_status",
                    "inspect_unexpected_files",
                    "commit_only_expected_files",
                    "ask_user_before_touching_unexpected_files",
                ]
                result.failures.append(
                    "unexpected files changed: "
                    + ", ".join(unexpected)
                    + ". Recovery: inspect git status, preserve user changes, and commit only expected files."
                )
        if self.commands:
            result.tiers.append("commands")
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
                result.failure_code = result.failure_code or "validation_command_failed"
                result.failures.append(f"command failed ({command_result.exit_code}): {command}")
        return result

    def _unexpected_changed_files(self, expected_files: list[Path], allowed_existing_changes: dict[str, str] | None = None) -> list[str]:
        if not (self.project_root / ".git").exists():
            return []
        try:
            completed = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=self.project_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0:
            return []
        expected = {self._relative(path) for path in expected_files}
        allowed_existing_changes = {
            str(path).replace("\\", "/"): str(signature)
            for path, signature in (allowed_existing_changes or {}).items()
        }
        changed: set[str] = set()
        for line in completed.stdout.splitlines():
            if len(line) < 4:
                continue
            rel = line[3:].strip()
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1].strip()
            rel = rel.replace("\\", "/")
            if rel.startswith((".git/", ".buddy/")):
                continue
            if rel in allowed_existing_changes and _file_signature(self.project_root, rel) == allowed_existing_changes[rel]:
                continue
            changed.add(rel)
        return sorted(path for path in changed if path not in expected)

    def _git_status_report(self) -> str:
        if not (self.project_root / ".git").exists():
            return ""
        try:
            completed = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=self.project_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if completed.returncode != 0:
            return completed.stderr.strip()
        return completed.stdout.strip()

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError:
            return resolved.as_posix()


def validate_with_worktree_context(
    validation: Any,
    touched_files: list[Path],
    *,
    expected_files: list[Path],
    allowed_existing_changes: dict[str, str] | None = None,
) -> ValidationResult:
    try:
        parameters = inspect.signature(validation.validate).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "allowed_existing_changes" in parameters:
        return validation.validate(
            touched_files,
            expected_files=expected_files,
            allowed_existing_changes=allowed_existing_changes or {},
        )
    return validation.validate(touched_files, expected_files=expected_files)


def _file_signature(project_root: Path, rel_path: str) -> str:
    path = project_root / rel_path
    try:
        if path.is_file():
            return sha256_bytes(path.read_bytes())
        if path.exists():
            return "<non-file>"
    except OSError:
        return "<unreadable>"
    return "<missing>"
