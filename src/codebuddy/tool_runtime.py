from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .command_broker import CommandBroker
from .edit_broker import EditBroker
from .errors import ConfirmationRequired, DeniedByPolicy
from .events import AgentEvent
from .hashutil import sha256_bytes
from .search import Searcher
from .session import SessionLedger
from .textfile import is_probably_binary_file
from .tool_calls import MALFORMED_TOOL_CALL_NAME, ParsedToolCall
from .tool_result import ToolResult
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
        record_changed_file: Callable[[str], None] | None = None,
        current_changed_files: Callable[[], list[str]] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.ledger = ledger
        self.edit_broker = edit_broker
        self.command_broker = command_broker
        self.searcher = searcher
        self.validation = validation
        self.enabled_tools = enabled_tools or {}
        self.mark_plan = mark_plan or (lambda _step, _status: None)
        self.record_changed_file = record_changed_file or (lambda _path: None)
        self.current_changed_files = current_changed_files

    def run(self, calls: list[ParsedToolCall], events: list[AgentEvent]) -> list[str]:
        return [result.to_prompt() for result in self.run_structured(calls, events)]

    def run_structured(self, calls: list[ParsedToolCall], events: list[AgentEvent]) -> list[ToolResult]:
        results: list[ToolResult] = []
        for call in calls:
            self._ensure_tool_enabled(call.name)
            validation_error = _validate_tool_arguments(call)
            if validation_error:
                events.append(AgentEvent("tool", "Tool", f"{call.name}: {validation_error}", "failed"))
                results.append(
                    ToolResult(
                        call.name,
                        False,
                        f"{call.name} invalid arguments: {validation_error}",
                        error_type="invalid_arguments",
                        retryable=False,
                        next_action="repair_tool_arguments",
                    )
                )
                continue
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
                results.append(
                    ToolResult(
                        call.name,
                        False,
                        f"unknown tool: {call.name}",
                        error_type="unknown_tool",
                        retryable=False,
                        next_action="stop_and_report_unknown_tool",
                    )
                )
        return results

    def _read_text(self, call: ParsedToolCall, events: list[AgentEvent]) -> ToolResult:
        if not self.searcher:
            return ToolResult("read_text", False, "read_text failed: searcher unavailable", error_type="tool_unavailable")
        path = str(call.arguments["path"])
        try:
            content = self.searcher.read_text(path)
        except Exception as exc:
            recovered = self._recover_missing_read(path, exc, events)
            if recovered:
                return recovered
            events.append(AgentEvent("read", "Read", f"{path}: {exc}", "failed"))
            return ToolResult(
                "read_text",
                False,
                f"read_text {path} failed: {exc}",
                error_type=_classify_read_error(exc),
                retryable=True,
                next_action="search_for_correct_path",
                metadata={"path": path},
            )
        try:
            file_hash = sha256_bytes(self.edit_broker.read_text(path).raw)
        except Exception:
            file_hash = "unavailable"
        if path not in self.ledger.files_inspected:
            self.ledger.files_inspected.append(path)
        self.mark_plan("Gather context", "completed")
        events.append(AgentEvent("read", "Read", f"{path} ({len(content.splitlines())} lines)"))
        return ToolResult(
            "read_text",
            True,
            f"read_text {path}:\nsha256: {file_hash}\ncontent:\n{content}",
            metadata={"path": path, "sha256": file_hash},
        )

    def _recover_missing_read(self, requested_path: str, exc: Exception, events: list[AgentEvent]) -> ToolResult | None:
        if _classify_read_error(exc) != "file_not_found" or not self.searcher:
            return None
        requested_name = Path(requested_path).name
        if not requested_name:
            return None
        matches: list[Path] = []
        for path in self.project_root.rglob(requested_name):
            if not path.is_file():
                continue
            try:
                if self.edit_broker.policy.is_sensitive(path) or is_probably_binary_file(path):
                    continue
                self.edit_broker.policy.ensure_read_allowed(path)
            except Exception:
                continue
            matches.append(path)
            if len(matches) > 1:
                return None
        if len(matches) != 1:
            return None
        recovered_path = matches[0].relative_to(self.project_root).as_posix()
        content = self.searcher.read_text(recovered_path)
        file_hash = sha256_bytes(self.edit_broker.read_text(recovered_path).raw)
        if recovered_path not in self.ledger.files_inspected:
            self.ledger.files_inspected.append(recovered_path)
        self.mark_plan("Gather context", "completed")
        events.append(AgentEvent("read", "Read", f"{requested_path} -> {recovered_path} ({len(content.splitlines())} lines)"))
        return ToolResult(
            "read_text",
            True,
            f"read_text {recovered_path} recovered_from={requested_path}:\nsha256: {file_hash}\ncontent:\n{content}",
            metadata={
                "path": recovered_path,
                "requested_path": requested_path,
                "sha256": file_hash,
                "recovered": True,
            },
        )

    def _search(self, call: ParsedToolCall, events: list[AgentEvent]) -> ToolResult:
        if not self.searcher:
            return ToolResult("search", False, "search failed: searcher unavailable", error_type="tool_unavailable")
        pattern = str(call.arguments["pattern"])
        try:
            matches = self.searcher.search(pattern)
        except Exception as exc:
            events.append(AgentEvent("search", "Search", f"{pattern!r}: {exc}", "failed"))
            return ToolResult(
                "search",
                False,
                f"search {pattern!r} failed: {exc}",
                error_type="search_failed",
                retryable=True,
                next_action="try_different_search_pattern",
                metadata={"pattern": pattern},
            )
        self.mark_plan("Gather context", "completed")
        events.append(AgentEvent("search", "Search", f"{pattern!r} ({len(matches)} matches)"))
        content = "search results:\n" + "\n".join(f"{m.path}:{m.line}:{m.text}" for m in matches)
        return ToolResult(
            "search",
            True,
            content,
            metadata={
                "pattern": pattern,
                "matches": len(matches),
                "paths": sorted({match.path for match in matches}),
            },
        )

    def _edit_exact_replace(self, call: ParsedToolCall, events: list[AgentEvent]) -> ToolResult:
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
            return _edit_failure("edit_exact_replace", path, exc, self.edit_broker)
        return self._record_edit_result("edit_exact_replace", result, events, "Edit", "edited")

    def _create_file(self, call: ParsedToolCall, events: list[AgentEvent]) -> ToolResult:
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
            return _edit_failure("create_file", path, exc, self.edit_broker)
        return self._record_edit_result("create_file", result, events, "Create", "created")

    def _rewrite_file(self, call: ParsedToolCall, events: list[AgentEvent]) -> ToolResult:
        path = str(call.arguments["path"])
        try:
            result = self.edit_broker.rewrite_file(
                path,
                str(call.arguments["content"]),
                call.arguments.get("expected_hash"),
            )
        except Exception as exc:
            events.append(AgentEvent("edit", "Rewrite", f"{path}: {exc}", "failed"))
            return _edit_failure("rewrite_file", path, exc, self.edit_broker)
        return self._record_edit_result("rewrite_file", result, events, "Rewrite", "rewrote")

    def _apply_unified_diff(self, call: ParsedToolCall, events: list[AgentEvent]) -> ToolResult:
        path = str(call.arguments["path"])
        try:
            result = self.edit_broker.apply_unified_diff(
                path,
                str(call.arguments["patch"]),
                call.arguments.get("expected_hash"),
            )
        except Exception as exc:
            events.append(AgentEvent("edit", "Patch", f"{path}: {exc}", "failed"))
            return _edit_failure("apply_unified_diff", path, exc, self.edit_broker)
        return self._record_edit_result("apply_unified_diff", result, events, "Patch", "patched")

    def _record_edit_result(self, tool: str, result, events: list[AgentEvent], event_title: str, verb: str) -> ToolResult:
        rel = result.path.relative_to(self.project_root).as_posix()
        if rel not in self.ledger.files_edited:
            self.ledger.files_edited.append(rel)
        self.record_changed_file(rel)
        self.ledger.completed_actions.append(f"{verb} {rel}")
        diff_stat = _diff_stat(result.diff)
        events.append(AgentEvent("edit", event_title, f"{rel} ({diff_stat})", body=result.diff))
        return ToolResult(
            tool,
            True,
            f"{verb} {rel}:\n{result.diff}",
            changed_files=[rel],
            metadata={"path": rel, "before_hash": result.before_hash, "after_hash": result.after_hash, "diff_stat": diff_stat},
        )

    def _run_command(self, call: ParsedToolCall, events: list[AgentEvent]) -> ToolResult:
        command = str(call.arguments["command"])
        try:
            result = self.command_broker.run(command)
        except ConfirmationRequired as exc:
            self.ledger.pending_next_step = "approve command before execution"
            self.ledger.approvals["pending_command"] = command
            self.ledger.approvals["pending_command_cwd"] = str(self.project_root)
            events.append(AgentEvent("shell", "Shell", f"{command}: {exc}", "failed"))
            return ToolResult(
                "run_command",
                False,
                f"command needs approval: {command}: {exc}",
                error_type="confirmation_required",
                retryable=True,
                next_action="request_user_approval",
                metadata={"command": command},
            )
        self.ledger.commands_run.append(command)
        status = "done" if result.exit_code == 0 else "failed"
        events.append(AgentEvent("shell", "Shell", f"{command} (exit {result.exit_code}, {result.duration_seconds:.1f}s)", status))
        return ToolResult(
            "run_command",
            result.exit_code == 0,
            f"command {command} exit={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            error_type=None if result.exit_code == 0 else "command_failed",
            retryable=result.exit_code != 0,
            next_action="inspect_command_output" if result.exit_code != 0 else None,
            metadata={
                "command": command,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
            },
        )

    def _validate(self, events: list[AgentEvent]) -> ToolResult:
        if not self.validation:
            return ToolResult("validate", False, "validate failed: validation harness unavailable", error_type="tool_unavailable")
        try:
            touched = self._validation_targets()
            validation = self.validation.validate(touched, expected_files=touched)
        except Exception as exc:
            events.append(AgentEvent("validate", "Validate", str(exc), "failed"))
            return ToolResult("validate", False, f"validation failed: {exc}", error_type="validation_error", retryable=True, next_action="inspect_validation_error")
        self.ledger.validation_state = {"passed": validation.passed, "failures": validation.failures}
        self.mark_plan("Validate outcome", "completed" if validation.passed else "pending")
        detail = "passed" if validation.passed else f"failed ({len(validation.failures)} failures)"
        events.append(AgentEvent("validate", "Validate", detail, "done" if validation.passed else "failed"))
        return ToolResult(
            "validate",
            validation.passed,
            f"validation passed={validation.passed} failures={validation.failures}",
            error_type=None if validation.passed else "validation_failed",
            retryable=not validation.passed,
            next_action="fix_validation_failures" if not validation.passed else None,
            metadata={
                "failures": validation.failures,
                "checks": validation.checks,
                "tiers": validation.tiers,
                "unexpected_files": validation.unexpected_files,
            },
        )

    def _validation_targets(self) -> list[Path]:
        rel_paths = self.current_changed_files() if self.current_changed_files else list(self.ledger.files_edited)
        if not rel_paths:
            rel_paths = list(self.ledger.files_edited)
        return [self.project_root / path for path in rel_paths]

    @staticmethod
    def _malformed_tool_call(call: ParsedToolCall, events: list[AgentEvent]) -> ToolResult:
        attempted = str(call.arguments.get("name", "unknown"))
        error = str(call.arguments.get("error", "malformed arguments"))
        events.append(AgentEvent("tool", "Tool", f"{attempted}: {error}", "failed"))
        content = (
            f"malformed tool call arguments for {attempted}: {error}\n"
            "Retry the same tool call with valid JSON. Escape multiline strings as \\n "
            "and escape quotes inside string values as \\\". Do not change files until the tool call parses."
        )
        return ToolResult(
            MALFORMED_TOOL_CALL_NAME,
            False,
            content,
            error_type="malformed_tool_call",
            retryable=True,
            next_action="repair_tool_arguments",
            metadata={"attempted_tool": attempted, "error": error},
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


TOOL_ARGUMENTS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "read_text": {"path": str},
    "search": {"pattern": str},
    "edit_exact_replace": {"path": str, "old": str, "new": str},
    "create_file": {"path": str, "content": str},
    "rewrite_file": {"path": str, "content": str, "expected_hash": str},
    "apply_unified_diff": {"path": str, "patch": str},
    "run_command": {"command": str},
    "validate": {},
    MALFORMED_TOOL_CALL_NAME: {},
}


OPTIONAL_TOOL_ARGUMENTS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "edit_exact_replace": {"expected_hash": str},
    "create_file": {"overwrite": bool, "expected_hash": str},
    "apply_unified_diff": {"expected_hash": str},
    "run_command": {"approve": bool},
    MALFORMED_TOOL_CALL_NAME: {"name": str, "error": str, "raw_arguments": str},
}


def _validate_tool_arguments(call: ParsedToolCall) -> str | None:
    required = TOOL_ARGUMENTS.get(call.name)
    if required is None:
        return None
    optional = OPTIONAL_TOOL_ARGUMENTS.get(call.name, {})
    allowed = set(required) | set(optional)
    for key, expected_type in required.items():
        if key not in call.arguments:
            return f"missing required argument: {key}"
        if not isinstance(call.arguments[key], expected_type):
            return f"argument {key} must be {_type_name(expected_type)}"
    for key, value in call.arguments.items():
        expected_type = required.get(key, optional.get(key))
        if key not in allowed:
            return f"unknown argument: {key}"
        if expected_type and not isinstance(value, expected_type):
            return f"argument {key} must be {_type_name(expected_type)}"
    return None


def _type_name(expected_type: type | tuple[type, ...]) -> str:
    if isinstance(expected_type, tuple):
        return " or ".join(item.__name__ for item in expected_type)
    return expected_type.__name__


def _classify_read_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "no such file" in text or "cannot find" in text or "not found" in text:
        return "file_not_found"
    if "sensitive" in text:
        return "sensitive_path"
    if "outside" in text or "not under project root" in text:
        return "outside_project"
    if "binary" in text:
        return "binary_file"
    return "read_failed"


def _edit_failure(tool: str, path: str, exc: Exception, edit_broker: EditBroker | None = None) -> ToolResult:
    error_type, next_action = _classify_edit_error(exc)
    content = f"{tool} {path} failed: {exc}"
    metadata: dict[str, Any] = {"path": path}
    if error_type == "stale_hash" and edit_broker is not None:
        snapshot = _current_file_snapshot(edit_broker, path)
        if snapshot:
            current_hash, current_text = snapshot
            metadata["current_sha256"] = current_hash
            content += f"\n\nCurrent file snapshot after stale hash:\nsha256: {current_hash}\ncontent:\n{current_text}"
    return ToolResult(
        tool,
        False,
        content,
        error_type=error_type,
        retryable=error_type in {"stale_hash", "exact_block_not_found", "not_unique", "no_change"},
        next_action=next_action,
        metadata=metadata,
    )


def _current_file_snapshot(edit_broker: EditBroker, path: str) -> tuple[str, str] | None:
    try:
        snapshot = edit_broker.read_text(path)
    except Exception:
        return None
    return sha256_bytes(snapshot.raw), snapshot.text


def _classify_edit_error(exc: Exception) -> tuple[str, str]:
    text = str(exc).lower()
    if "changed since it was read" in text:
        return "stale_hash", "reread_file_then_retry"
    if "exact block not found" in text or "patch context not found" in text:
        return "exact_block_not_found", "reread_file_then_use_rewrite"
    if "not unique" in text:
        return "not_unique", "reread_file_then_use_larger_context"
    if "produced no change" in text:
        return "no_change", "verify_objective_or_stop"
    if "binary file" in text:
        return "binary_file", "stop_and_report_binary_file"
    if "sensitive" in text:
        return "sensitive_path", "stop_and_report_sensitive_path"
    if "outside" in text or "not under project root" in text:
        return "outside_project", "stop_and_report_outside_project"
    return "edit_failed", "inspect_tool_error"
