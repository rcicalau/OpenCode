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
        return [result.to_prompt() for result in self.run_structured(calls, events)]

    def run_structured(self, calls: list[ParsedToolCall], events: list[AgentEvent]) -> list[ToolResult]:
        results: list[ToolResult] = []
        for call in calls:
            self._ensure_tool_enabled(call.name)
            if call.name == "read_text":
                results.append(self._read_text(call, events))
            elif call.name == "search":
                results.append(_coerce_tool_result("search", self._search(call, events)))
            elif call.name == "edit_exact_replace":
                results.append(self._edit_exact_replace(call, events))
            elif call.name == "create_file":
                results.append(self._create_file(call, events))
            elif call.name == "rewrite_file":
                results.append(self._rewrite_file(call, events))
            elif call.name == "apply_unified_diff":
                results.append(self._apply_unified_diff(call, events))
            elif call.name == "run_command":
                results.append(_coerce_tool_result("run_command", self._run_command(call, events)))
            elif call.name == "validate":
                results.append(_coerce_tool_result("validate", self._validate(events)))
            elif call.name == MALFORMED_TOOL_CALL_NAME:
                results.append(_coerce_tool_result(MALFORMED_TOOL_CALL_NAME, self._malformed_tool_call(call, events)))
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
            return _edit_failure("edit_exact_replace", path, exc)
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
            return _edit_failure("create_file", path, exc)
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
            return _edit_failure("rewrite_file", path, exc)
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
            return _edit_failure("apply_unified_diff", path, exc)
        return self._record_edit_result("apply_unified_diff", result, events, "Patch", "patched")

    def _record_edit_result(self, tool: str, result, events: list[AgentEvent], event_title: str, verb: str) -> ToolResult:
        rel = result.path.relative_to(self.project_root).as_posix()
        if rel not in self.ledger.files_edited:
            self.ledger.files_edited.append(rel)
        self.ledger.completed_actions.append(f"{verb} {rel}")
        diff_stat = _diff_stat(result.diff)
        events.append(AgentEvent("edit", event_title, f"{rel} ({diff_stat})"))
        return ToolResult(
            tool,
            True,
            f"{verb} {rel}:\n{result.diff}",
            changed_files=[rel],
            metadata={"path": rel, "before_hash": result.before_hash, "after_hash": result.after_hash, "diff_stat": diff_stat},
        )

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


def _edit_failure(tool: str, path: str, exc: Exception) -> ToolResult:
    error_type, next_action = _classify_edit_error(exc)
    return ToolResult(
        tool,
        False,
        f"{tool} {path} failed: {exc}",
        error_type=error_type,
        retryable=error_type in {"stale_hash", "exact_block_not_found", "not_unique", "no_change"},
        next_action=next_action,
        metadata={"path": path},
    )


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


def _coerce_tool_result(tool: str, value) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    text = str(value)
    failed = " failed:" in text or text.startswith(("unknown tool:", "malformed tool call arguments"))
    return ToolResult(
        tool,
        not failed,
        text,
        error_type="tool_failed" if failed else None,
        retryable=failed,
        next_action="inspect_tool_error" if failed else None,
    )
