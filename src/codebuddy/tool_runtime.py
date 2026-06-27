from __future__ import annotations

from pathlib import Path
from typing import Any

from .command_broker import CommandBroker
from .edit_broker import EditBroker
from .errors import ConfirmationRequired, DeniedByPolicy
from .events import AgentEvent
from .hashutil import sha256_bytes
from .search import Searcher
from .session import SessionLedger
from .tool_calls import MALFORMED_TOOL_CALL_NAME, ParsedToolCall
from .validation import ValidationHarness


class ToolRuntime:
    def __init__(
        self,
        project_root: Path,
        ledger: SessionLedger,
        edit_broker: EditBroker,
        command_broker: CommandBroker,
        searcher: Searcher | None = None,
        validation: ValidationHarness | None = None,
        enabled_tools: dict[str, bool] | None = None,
        mark_plan=None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.ledger = ledger
        self.edit_broker = edit_broker
        self.command_broker = command_broker
        self.searcher = searcher
        self.validation = validation
        self.enabled_tools = enabled_tools or {}
        self.mark_plan = mark_plan or (lambda _step, _status: None)

    def run(self, calls: list[ParsedToolCall], events: list[AgentEvent]) -> list[str]:
        results: list[str] = []
        for call in calls:
            self._ensure_tool_enabled(call.name)
            if call.name == "read_text":
                results.append(self._read_text(call, events))
            elif call.name == "search":
                results.append(self._search(call, events))
            elif call.name == "edit_exact_replace":
                results.append(self._edit_exact_replace(call, events))
            elif call.name == "create_file":
                results.append(self._create_file(call, events))
            elif call.name == "rewrite_file":
                results.append(self._rewrite_file(call, events))
            elif call.name == "apply_unified_diff":
                results.append(self._apply_unified_diff(call, events))
            elif call.name == "run_command":
                results.append(self._run_command(call, events))
            elif call.name == "validate":
                results.append(self._validate(events))
            elif call.name == MALFORMED_TOOL_CALL_NAME:
                results.append(self._malformed_tool_call(call, events))
            else:
                events.append(AgentEvent("tool", "Tool", call.name, "failed"))
                results.append(f"unknown tool: {call.name}")
        return results

    def _read_text(self, call: ParsedToolCall, events: list[AgentEvent]) -> str:
        if not self.searcher:
            return "read_text failed: searcher unavailable"
        path = str(call.arguments["path"])
        try:
            content = self.searcher.read_text(path)
        except Exception as exc:
            events.append(AgentEvent("read", "Read", f"{path}: {exc}", "failed"))
            return f"read_text {path} failed: {exc}"
        try:
            file_hash = sha256_bytes(self.edit_broker.read_text(path).raw)
        except Exception:
            file_hash = "unavailable"
        if path not in self.ledger.files_inspected:
            self.ledger.files_inspected.append(path)
        self.mark_plan("Gather context", "completed")
        events.append(AgentEvent("read", "Read", f"{path} ({len(content.splitlines())} lines)"))
        return f"read_text {path}:\nsha256: {file_hash}\ncontent:\n{content}"

    def _search(self, call: ParsedToolCall, events: list[AgentEvent]) -> str:
        if not self.searcher:
            return "search failed: searcher unavailable"
        pattern = str(call.arguments["pattern"])
        try:
            matches = self.searcher.search(pattern)
        except Exception as exc:
            events.append(AgentEvent("search", "Search", f"{pattern!r}: {exc}", "failed"))
            return f"search {pattern!r} failed: {exc}"
        self.mark_plan("Gather context", "completed")
        events.append(AgentEvent("search", "Search", f"{pattern!r} ({len(matches)} matches)"))
        return "search results:\n" + "\n".join(f"{m.path}:{m.line}:{m.text}" for m in matches)

    def _edit_exact_replace(self, call: ParsedToolCall, events: list[AgentEvent]) -> str:
        path = str(call.arguments["path"])
        try:
            result = self.edit_broker.exact_replace(
                path,
                str(call.arguments["old"]),
                str(call.arguments["new"]),
                call.arguments.get("expected_hash"),
            )
        except Exception as exc:
            events.append(AgentEvent("edit", "Edit", f"{path}: {exc}", "failed"))
            return f"edit_exact_replace {path} failed: {exc}"
        rel = result.path.relative_to(self.project_root).as_posix()
        if rel not in self.ledger.files_edited:
            self.ledger.files_edited.append(rel)
        self.ledger.completed_actions.append(f"edited {rel}")
        events.append(AgentEvent("edit", "Edit", f"{rel} ({_diff_stat(result.diff)})"))
        return f"edited {rel}:\n{result.diff}"

    def _create_file(self, call: ParsedToolCall, events: list[AgentEvent]) -> str:
        path = str(call.arguments["path"])
        try:
            result = self.edit_broker.create_file(
                path,
                str(call.arguments["content"]),
                bool(call.arguments.get("overwrite", False)),
                call.arguments.get("expected_hash"),
            )
        except Exception as exc:
            events.append(AgentEvent("edit", "Create", f"{path}: {exc}", "failed"))
            return f"create_file {path} failed: {exc}"
        rel = result.path.relative_to(self.project_root).as_posix()
        if rel not in self.ledger.files_edited:
            self.ledger.files_edited.append(rel)
        self.ledger.completed_actions.append(f"created {rel}")
        events.append(AgentEvent("edit", "Create", f"{rel} ({_diff_stat(result.diff)})"))
        return f"created {rel}:\n{result.diff}"

    def _rewrite_file(self, call: ParsedToolCall, events: list[AgentEvent]) -> str:
        path = str(call.arguments["path"])
        try:
            result = self.edit_broker.rewrite_file(
                path,
                str(call.arguments["content"]),
                call.arguments.get("expected_hash"),
            )
        except Exception as exc:
            events.append(AgentEvent("edit", "Rewrite", f"{path}: {exc}", "failed"))
            return f"rewrite_file {path} failed: {exc}"
        rel = result.path.relative_to(self.project_root).as_posix()
        if rel not in self.ledger.files_edited:
            self.ledger.files_edited.append(rel)
        self.ledger.completed_actions.append(f"rewrote {rel}")
        events.append(AgentEvent("edit", "Rewrite", f"{rel} ({_diff_stat(result.diff)})"))
        return f"rewrote {rel}:\n{result.diff}"

    def _apply_unified_diff(self, call: ParsedToolCall, events: list[AgentEvent]) -> str:
        path = str(call.arguments["path"])
        try:
            result = self.edit_broker.apply_unified_diff(
                path,
                str(call.arguments["patch"]),
                call.arguments.get("expected_hash"),
            )
        except Exception as exc:
            events.append(AgentEvent("edit", "Patch", f"{path}: {exc}", "failed"))
            return f"apply_unified_diff {path} failed: {exc}"
        rel = result.path.relative_to(self.project_root).as_posix()
        if rel not in self.ledger.files_edited:
            self.ledger.files_edited.append(rel)
        self.ledger.completed_actions.append(f"patched {rel}")
        events.append(AgentEvent("edit", "Patch", f"{rel} ({_diff_stat(result.diff)})"))
        return f"patched {rel}:\n{result.diff}"

    def _run_command(self, call: ParsedToolCall, events: list[AgentEvent]) -> str:
        command = str(call.arguments["command"])
        try:
            result = self.command_broker.run(command)
        except ConfirmationRequired as exc:
            self.ledger.pending_next_step = "approve command before execution"
            self.ledger.approvals["pending_command"] = command
            self.ledger.approvals["pending_command_cwd"] = str(self.project_root)
            events.append(AgentEvent("shell", "Shell", f"{command}: {exc}", "failed"))
            return f"command needs approval: {command}: {exc}"
        self.ledger.commands_run.append(command)
        status = "done" if result.exit_code == 0 else "failed"
        events.append(AgentEvent("shell", "Shell", f"{command} (exit {result.exit_code}, {result.duration_seconds:.1f}s)", status))
        return f"command {command} exit={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    def _validate(self, events: list[AgentEvent]) -> str:
        if not self.validation:
            return "validate failed: validation harness unavailable"
        try:
            touched = [self.project_root / path for path in self.ledger.files_edited]
            validation = self.validation.validate(touched, expected_files=touched)
        except Exception as exc:
            events.append(AgentEvent("validate", "Validate", str(exc), "failed"))
            return f"validation failed: {exc}"
        self.ledger.validation_state = {"passed": validation.passed, "failures": validation.failures}
        self.mark_plan("Validate outcome", "completed" if validation.passed else "pending")
        detail = "passed" if validation.passed else f"failed ({len(validation.failures)} failures)"
        events.append(AgentEvent("validate", "Validate", detail, "done" if validation.passed else "failed"))
        return f"validation passed={validation.passed} failures={validation.failures}"

    @staticmethod
    def _malformed_tool_call(call: ParsedToolCall, events: list[AgentEvent]) -> str:
        attempted = str(call.arguments.get("name", "unknown"))
        error = str(call.arguments.get("error", "malformed arguments"))
        events.append(AgentEvent("tool", "Tool", f"{attempted}: {error}", "failed"))
        return (
            "malformed tool call arguments for "
            f"{attempted}: {error}\n"
            "Retry the same tool call with valid JSON. Escape multiline strings as \\n "
            "and escape quotes inside string values as \\\". Do not change files until the tool call parses."
        )

    def _ensure_tool_enabled(self, name: str) -> None:
        tool_map = {
            "read_text": "read",
            "search": "search",
            "edit_exact_replace": "edit",
            "create_file": "edit",
            "rewrite_file": "edit",
            "apply_unified_diff": "edit",
            "run_command": "shell",
            "validate": "validate",
        }
        config_name = tool_map.get(name)
        if config_name and self.enabled_tools.get(config_name, True) is False:
            raise DeniedByPolicy(f"tool disabled by config: {config_name}")


def _diff_stat(diff: str) -> str:
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return f"+{added}/-{removed}"
