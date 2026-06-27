from __future__ import annotations

"""Core agent orchestration for Code Buddy.

This module wires together the large language model (LLM), project context,
search, editing, git, and validation into a single high-level "agent"
interface.

At a very high level, the flow for a single user prompt is:

1. Decide what the user is trying to do (chat, scope, or execute).
2. Build a summary of the current project (files, key files, symbols).
3. If we're executing, ensure we are on a safe agent branch and create
   or resume a structured work plan.
4. Run a loop where the LLM can:
   - Ask to read files.
   - Search the project.
   - Edit files safely through the edit broker.
   - Run commands through the command broker.
   - Run validation after edits.
5. Collect results, update the session ledger, and emit human-readable
   events that the terminal UI can render.

The main entry point is :class:`CodeBuddyAgent`. As a junior developer,
you can think of this class as the "brain" of Code Buddy. It does not
contain project-specific logic; instead, it coordinates other services:

- ``LLMClient``: talks to the language model.
- ``EditBroker``: applies safe file edits, diffs, and new files.
- ``CommandBroker``: runs PowerShell commands under a policy.
- ``GitManager``: manages branches and checkpoint commits.
- ``Searcher``: reads and searches text files.
- ``ValidationHarness``: runs tests and other validation commands.
- ``SessionLedger``: tracks what has happened in this session so far.

If you are new to this codebase, start by reading ``CodeBuddyAgent.handle``
and ``CodeBuddyAgent._run_model_loop``. They show the high-level control
flow for each user prompt.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

from .command_broker import CommandBroker
from .edit_broker import EditBroker
from .events import AgentEvent
from .git_manager import GitManager
from .llm import LLMClient, Message
from .objective_state import APPROVAL_WAIT, BLOCKED, COMPLETE, IDLE, PLANNING, WORKING
from .project_context import build_project_context
from .search import Searcher
from .session import PlanItem, SessionLedger
from .tool_calls import (
    ParsedToolCall,
    parse_text_edit_blocks,
    parse_text_tool_calls,
    strip_tool_calls,
)
from .tool_result import ToolResult
from .tool_runtime import ToolRuntime
from .validation import ValidationHarness
from .workplan import WorkItem, WorkPlan, WorkPlanManager
from .errors import CodeBuddyError, ConfirmationRequired


@dataclass(slots=True)
class AgentResult:
    """Final outcome for handling a single user prompt.

    An :class:`AgentResult` is what the agent returns to the caller
    (typically the terminal UI) after all tool calls, edits, and validation
    are complete.

    Attributes:
        mode: The high-level mode for this prompt. One of:
            - ``"chat"``: conversational answer only, no edits or commands.
            - ``"scope"``: explore or summarize the project without editing.
            - ``"execute"``: run tools, edit files, and validate changes.
        message: The assistant's final answer to the user. This may include
            explanations, status messages, or validation notes.
        changed_files: Relative paths (from ``project_root``) of files that
            were edited or created during this prompt.
        events: A list of :class:`AgentEvent` instances describing actions
            taken during this prompt, such as reads, edits, commands, or
            validation runs.

    Example:
        After an execution prompt that edits a file and runs tests, you might
        see::

            AgentResult(
                mode="execute",
                message="Documentation added and tests passed.",
                changed_files=["src/codebuddy/agent.py"],
                events=[...],
            )
    """

    mode: str
    message: str
    changed_files: list[str]
    events: list["AgentEvent"]


def route_intent(prompt: str) -> str:
    """Classify the user's prompt as chat, scope, or execute.

    This helper is intentionally simple. It uses a few heuristics on the
    raw text of the prompt to decide which high-level mode the agent
    should use.

    The decision influences how :meth:`CodeBuddyAgent.handle` sets up the
    session:

    - ``"chat"``: answer using the LLM only, with no edits or commands.
    - ``"scope"``: explore the project (read/search) but do not modify it.
    - ``"execute"``: treat the prompt as an objective and allow edits,
      commands, and validation.

    Args:
        prompt: The raw user input string from the terminal.

    Returns:
        A string mode value: ``"chat"``, ``"scope"``, or ``"execute"``.

    Rules:
        - If the prompt starts with ``/ask``, it is treated as ``"chat"``.
        - If the prompt starts with ``/scope`` or explicitly says things
          like "do not write code" or "explore", it is treated as
          ``"scope"``.
        - If the prompt starts with ``/do``, it is treated as ``"execute"``.
        - If the prompt looks like a question (ends in ``?`` or starts
          with "what", "why", "how", "explain", "summarize"), it is treated
          as ``"chat"``.
        - Otherwise, we assume ``"execute"`` so the agent can perform
          useful work (edits, commands, validation) by default.

    As a junior developer:
        - You can safely add more heuristics here if you want to support
          additional slash commands or intent patterns, as long as you
          keep the returns to ``"chat"``, ``"scope"``, or ``"execute"``.
    """
    stripped = prompt.strip()
    lowered = stripped.lower()
    # Explicit chat command: "/ask" means "just answer my question".
    if stripped.startswith("/ask"):
        return "chat"
    # Explicit scoping command or phrasing that asks not to write code.
    if stripped.startswith("/scope") or "do not write code" in lowered or "explore" in lowered:
        return "scope"
    # Explicit execution command: "/do" means "perform actions".
    if stripped.startswith("/do"):
        return "execute"
    # Simple question detection: questions are usually chat, not execution.
    if lowered.endswith("?") or lowered.startswith(("what ", "why ", "how ", "explain", "summarize")):
        return "chat"
    # Default: treat as an execution objective so the agent can use tools.
    return "execute"


class CodeBuddyAgent:
    def __init__(
        self,
        project_root: Path,
        ledger: SessionLedger,
        llm: LLMClient,
        edit_broker: EditBroker,
        command_broker: CommandBroker,
        git_manager: GitManager,
        searcher: Searcher | None = None,
        validation: ValidationHarness | None = None,
        enabled_tools: dict[str, bool] | None = None,
        max_tool_iterations: int | None = None,
        no_progress_repeat_limit: int = 8,
        model_timeout_seconds: float = 75,
    ) -> None:
        self.project_root = project_root
        self.ledger = ledger
        self.llm = llm
        self.edit_broker = edit_broker
        self.command_broker = command_broker
        self.git_manager = git_manager
        self.searcher = searcher
        self.validation = validation
        self.enabled_tools = enabled_tools or {}
        if max_tool_iterations is not None and max_tool_iterations < 0:
            raise ValueError("max_tool_iterations must be non-negative or None")
        self.max_tool_iterations = max_tool_iterations or None
        if no_progress_repeat_limit <= 0:
            raise ValueError("no_progress_repeat_limit must be positive")
        self.no_progress_repeat_limit = no_progress_repeat_limit
        if model_timeout_seconds <= 0:
            raise ValueError("model_timeout_seconds must be positive")
        self.model_timeout_seconds = float(model_timeout_seconds)
        self.tool_runtime = ToolRuntime(
            self.project_root,
            self.ledger,
            self.edit_broker,
            self.command_broker,
            self.searcher,
            self.validation,
            self.enabled_tools,
            self._mark_plan,
        )

    def handle(self, prompt: str, event_sink=None) -> AgentResult:
        mode = route_intent(prompt)
        self.ledger.mode = mode
        self.ledger.objective_state = IDLE if mode != "execute" else PLANNING
        events: list[AgentEvent] = _EventStream(event_sink)
        project_context = build_project_context(self.project_root, self.edit_broker.policy, self.ledger)
        if project_context.text:
            events.append(
                AgentEvent(
                    "context",
                    "Context",
                    f"{project_context.files_count} files, {len(project_context.key_files)} key files, {project_context.symbols_count} symbols",
                )
            )
        if mode == "execute":
            self.ledger.objective = prompt
            self.ledger.plan = [PlanItem("Understand objective", "completed"), PlanItem("Gather context", "pending"), PlanItem("Validate outcome", "pending")]
            approve_dirty_branch = bool(self.ledger.approvals.pop("dirty_branch", False))
            try:
                branch = self.git_manager.ensure_agent_branch(prompt, approve_protected=approve_dirty_branch)
            except ConfirmationRequired as exc:
                self.ledger.objective_state = APPROVAL_WAIT
                self.ledger.pending_next_step = "approve dirty branch before execution"
                self.ledger.blockers.append(str(exc))
                events.append(AgentEvent("git", "Git", str(exc), "failed"))
                return AgentResult(
                    mode=mode,
                    message=(
                        f"{exc}\n\n"
                        "Choose: `y` approve and continue, `/diff` inspect changes, `/clear` cancel active objective. "
                        "You can also type `/a`, `/approve`, or `/approve-branch`."
                    ),
                    changed_files=list(self.ledger.files_edited),
                    events=events,
                )
            if branch:
                events.append(AgentEvent("git", "Git", f"agent branch {branch}"))
            workplans = WorkPlanManager(self.project_root, self.ledger.session_id, self.edit_broker.policy)
            work_plan = workplans.active_or_new(prompt)
            if work_plan:
                self.ledger.objective = work_plan.objective
                events.append(AgentEvent("plan", "Plan", workplans.summary(work_plan)))
                return self._handle_work_plan(workplans, work_plan, project_context.text, events)
        message = self._run_model_loop(prompt, project_context.text, events)
        validation_passed = self._auto_validate_after_edits(events) if mode == "execute" else None
        if validation_passed is False:
            self.ledger.objective_state = BLOCKED
            message = (message.strip() + "\n\n" if message.strip() else "") + "Validation failed after edits. Review validation output before continuing."
        elif mode == "execute" and self.ledger.objective_state not in {APPROVAL_WAIT, BLOCKED}:
            self.ledger.objective_state = COMPLETE
        return AgentResult(mode=mode, message=message, changed_files=list(self.ledger.files_edited), events=events)

    def _run_model_loop(self, prompt: str, project_context: str, events: list[AgentEvent]) -> str:
        messages = [
            Message(
                "system",
                "You are Code Buddy. Be concise and grounded in the current project. "
                "Use the project context before answering questions about the repository. "
                "If context is insufficient, inspect files with tools instead of guessing. "
                "For execution tasks, keep calling tools until the objective is complete or blocked. "
                "For edits, do not use native JSON tool calls. Use raw edit blocks so multiline code cannot break JSON:\n"
                "<codebuddy_replace path=\"relative/path.py\">\n<old>\nexact old text\n</old>\n<new>\nexact new text\n</new>\n</codebuddy_replace>\n"
                "For guarded whole-file rewrites after reading a file, use:\n"
                "<codebuddy_rewrite path=\"relative/path.py\" expected_hash=\"sha256-from-read\">\nfull replacement text\n</codebuddy_rewrite>\n"
                "For non-edit tools, use native tools when available; otherwise use <tool_call>{\"name\":\"...\",\"arguments\":{}}</tool_call>.",
            ),
            Message("system", project_context),
            Message("user", prompt),
        ]
        message = ""
        tool_schemas = self._tool_schemas()
        iteration = 0
        repeated_signature: str | None = None
        repeated_count = 0
        while self.max_tool_iterations is None or iteration < self.max_tool_iterations:
            if self._has_live_event_sink(events):
                label = f"request {iteration + 1}"
                if self.max_tool_iterations is not None:
                    label = f"{label}/{self.max_tool_iterations}"
                events.append(AgentEvent("model", "Model", label, "running"))
            iteration += 1
            try:
                response = self._complete_model(messages, tool_schemas)
            except CodeBuddyError as exc:
                self.ledger.objective_state = BLOCKED
                events.append(AgentEvent("model", "Model", str(exc), "failed"))
                return f"Model request failed: {exc}"
            try:
                calls = self._collect_tool_calls(response)
            except CodeBuddyError as exc:
                events.append(AgentEvent("tool", "Tool", str(exc), "failed"))
                messages.append(
                    Message(
                        "user",
                        "Tool call parsing failed. Retry the tool call with valid JSON arguments. "
                        "Escape multiline strings with \\n and escape quotes inside strings with \\\". "
                        f"Parser error: {exc}",
                    )
                )
                continue
            visible_content = strip_tool_calls(response.content)
            if not calls:
                message = visible_content
                break
            signature = _tool_call_signature(calls)
            edited_before = set(self.ledger.files_edited)
            tool_results = self._run_tool_calls(calls, events)
            edited_after = set(self.ledger.files_edited)
            if signature == repeated_signature and edited_after == edited_before:
                repeated_count += 1
            else:
                repeated_signature = signature
                repeated_count = 1
            if repeated_count > self.no_progress_repeat_limit:
                self.ledger.objective_state = BLOCKED
                message = (
                    "Stopped for no progress because the model repeated the same tool call. "
                    "Try a narrower prompt or inspect the latest tool result."
                )
                self.ledger.blockers.append("no progress: repeated identical tool call")
                events.append(AgentEvent("tool", "Loop", "no progress: repeated identical tool call", "failed"))
                break
            if self.ledger.pending_next_step == "approve command before execution":
                self.ledger.objective_state = APPROVAL_WAIT
                command = self.ledger.approvals.get("pending_command", "")
                return (
                    f"Command needs approval before execution: `{command}`\n\n"
                    "Choose: `y` approve and run, `/diff` inspect changes, `/clear` cancel active objective. "
                    "You can also type `/a` or `/approve`."
                )
            if visible_content:
                messages.append(Message("assistant", visible_content))
            messages.append(
                Message(
                    "user",
                    "Tool results:\n"
                    + "\n\n".join(result.to_prompt() for result in tool_results)
                    + _recovery_playbook(tool_results)
                    + "\n\nContinue from these results. If the objective is complete, give the final answer. "
                    "If more work is needed, call the next tool.",
                )
            )
        else:
            self.ledger.objective_state = BLOCKED
            message = "Stopped after reaching the tool-iteration budget. Review the visible tool results and continue with a narrower prompt."
            events.append(AgentEvent("tool", "Loop", f"stopped after {self.max_tool_iterations} tool iterations", "failed"))
        return message

    def _complete_model(self, messages: list[Message], tool_schemas: list[dict]):
        result_queue: Queue = Queue(maxsize=1)

        def run_request() -> None:
            try:
                result_queue.put(("ok", self.llm.complete(messages, tools=tool_schemas)), block=False)
            except Exception as exc:
                result_queue.put(("error", exc), block=False)

        Thread(target=run_request, name="codebuddy-llm", daemon=True).start()
        try:
            status, payload = result_queue.get(timeout=self.model_timeout_seconds)
        except Empty as exc:
            raise CodeBuddyError(f"model request timed out after {self.model_timeout_seconds:g}s") from exc
        if status == "ok":
            return payload
        if isinstance(payload, CodeBuddyError):
            raise payload
        raise CodeBuddyError(f"model request failed: {payload}") from payload

    def _has_live_event_sink(self, events: list[AgentEvent]) -> bool:
        return bool(getattr(events, "sink", None))

    def _handle_work_plan(self, manager: WorkPlanManager, plan: WorkPlan, project_context: str, events: list[AgentEvent]) -> AgentResult:
        item = plan.next_item()
        if not item:
            self._sync_ledger_plan(plan)
            if plan.blocked_items():
                self.ledger.objective_state = BLOCKED
                blocked = ", ".join(f"{blocked.label}: {blocked.last_error or 'blocked'}" for blocked in plan.blocked_items()[:5])
                self.ledger.pending_next_step = f"resolve blocked work items: {blocked}"
                return AgentResult(
                    mode="execute",
                    message=f"Work plan blocked: {manager.summary(plan)}\nBlocked: {blocked}\nUse 'retry blocked' after resolving the issue.",
                    changed_files=list(self.ledger.files_edited),
                    events=events,
                )
            self.ledger.objective_state = COMPLETE
            return AgentResult(
                mode="execute",
                message=f"Work plan complete: {manager.summary(plan)}",
                changed_files=list(self.ledger.files_edited),
                events=events,
            )
        before_edited = set(self.ledger.files_edited)
        self.ledger.objective_state = WORKING
        item.status = "in_progress"
        item.attempts += 1
        manager.save(plan)
        self._sync_ledger_plan(plan)
        self.ledger.pending_next_step = item.label
        events.append(AgentEvent("work", "Work", f"{item.label}"))

        message = self._run_model_loop(manager.item_prompt(plan, item), project_context, events)
        changed_now = [path for path in self.ledger.files_edited if path not in before_edited]
        validation_passed = self._validate_work_slice(events)
        item.validation_passed = validation_passed
        required_change = self._item_required_change_done(item, changed_now)
        if validation_passed and required_change:
            item.status = "completed"
            item.summary = message
            item.last_error = None
            self.ledger.completed_actions.append(f"completed work item {item.label}")
            self._checkpoint_work_slice(item, changed_now, events)
        else:
            self.ledger.objective_state = BLOCKED
            item.status = "blocked"
            item.last_error = "validation failed" if not validation_passed else "no expected file change detected"
            self.ledger.blockers.append(f"{item.label}: {item.last_error}")
            events.append(AgentEvent("work", "Work", item.last_error, "failed"))
        manager.save(plan)
        self._sync_ledger_plan(plan)
        next_item = plan.next_item()
        self.ledger.pending_next_step = next_item.label if next_item else None
        if self.ledger.objective_state != BLOCKED:
            self.ledger.objective_state = WORKING if next_item else COMPLETE
        return AgentResult(
            mode="execute",
            message=(message.strip() + "\n\n" if message.strip() else "") + f"Work plan: {manager.summary(plan)}",
            changed_files=list(self.ledger.files_edited),
            events=events,
        )

    def _run_text_tool_calls(self, text: str, events: list[AgentEvent]) -> list[str]:
        return [result.to_prompt() for result in self._run_tool_calls(parse_text_tool_calls(text), events)]

    def _collect_tool_calls(self, response) -> list[ParsedToolCall]:
        calls = list(response.tool_calls or [])
        calls.extend(parse_text_edit_blocks(response.content))
        calls.extend(parse_text_tool_calls(response.content))
        return calls

    def _run_tool_calls(self, calls: list[ParsedToolCall], events: list[AgentEvent]) -> list[ToolResult]:
        return self.tool_runtime.run_structured(calls, events)

    def _validate_work_slice(self, events: list[AgentEvent]) -> bool:
        if not self.validation:
            return True
        touched = [self.project_root / path for path in self.ledger.files_edited]
        validation = self.validation.validate(touched, expected_files=touched)
        self.ledger.validation_state = {"passed": validation.passed, "failures": validation.failures}
        self._mark_plan("Validate outcome", "completed" if validation.passed else "pending")
        detail = "passed" if validation.passed else f"failed ({len(validation.failures)} failures)"
        events.append(AgentEvent("validate", "Validate", detail, "done" if validation.passed else "failed"))
        return validation.passed

    def _auto_validate_after_edits(self, events: list[AgentEvent]) -> bool | None:
        if not self.ledger.files_edited or self.ledger.validation_state:
            return None
        return self._validate_work_slice(events)

    def _item_required_change_done(self, item: WorkItem, changed_now: list[str]) -> bool:
        if item.action == "document_file":
            return item.target_path in self.ledger.files_edited
        if item.action == "create_tests_for_class":
            return any(path.startswith("tests/") or Path(path).name.startswith("test_") for path in changed_now)
        return bool(changed_now)

    def _checkpoint_work_slice(self, item: WorkItem, changed_now: list[str], events: list[AgentEvent]) -> None:
        if not changed_now:
            return
        try:
            if self.git_manager.checkpoint_commit(f"Code Buddy slice: {item.label}", changed_now):
                events.append(AgentEvent("git", "Git", f"checkpoint committed {item.label}"))
        except Exception as exc:
            events.append(AgentEvent("git", "Git", f"checkpoint skipped: {exc}", "failed"))

    def _sync_ledger_plan(self, plan: WorkPlan) -> None:
        self.ledger.plan = [
            PlanItem(f"{item.action}: {item.target_path}" + (f"::{item.symbol}" if item.symbol else ""), item.status)
            for item in plan.items
        ]

    def _mark_plan(self, step: str, status: str) -> None:
        for item in self.ledger.plan:
            if item.step == step:
                item.status = status
                return

    def _tool_schemas(self) -> list[dict]:
        schemas = {
            "read": {
                "type": "function",
                "function": {
                    "name": "read_text",
                    "description": "Read a non-sensitive text file inside the project.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            },
            "search": {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search the project for a text pattern.",
                    "parameters": {
                        "type": "object",
                        "properties": {"pattern": {"type": "string"}},
                        "required": ["pattern"],
                    },
                },
            },
            "shell": {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run a policy-controlled PowerShell command in the project.",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
            },
            "validate": {
                "type": "function",
                "function": {
                    "name": "validate",
                    "description": "Run configured validation and syntax checks.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        }
        enabled = []
        for key, schema in schemas.items():
            if self.enabled_tools.get(key, True):
                enabled.append(schema)
        return enabled


class _EventStream(list):
    def __init__(self, sink=None) -> None:
        super().__init__()
        self.sink = sink

    def append(self, event) -> None:
        super().append(event)
        if self.sink:
            self.sink(event)


def _tool_call_signature(calls: list[ParsedToolCall]) -> str:
    payload = [
        {
            "name": call.name,
            "arguments": call.arguments,
        }
        for call in calls
    ]
    return json.dumps(payload, sort_keys=True, default=str)


def _recovery_playbook(results: list[ToolResult]) -> str:
    lines: list[str] = []
    for result in results:
        if result.ok or not result.retryable:
            continue
        path = result.metadata.get("path") if isinstance(result.metadata, dict) else None
        if result.error_type == "stale_hash":
            lines.append(
                f"- {result.tool} failed with stale_hash for {path}. "
                "Reread the file, copy the returned sha256, and retry with that value as expected_hash."
            )
        elif result.error_type == "file_not_found":
            lines.append(
                f"- {result.tool} could not find {path}. Search for the correct path before retrying."
            )
        elif result.error_type == "exact_block_not_found":
            lines.append(
                f"- {result.tool} could not find the expected block in {path}. "
                "Reread the file and use a guarded rewrite or a larger exact block."
            )
        elif result.next_action:
            lines.append(f"- {result.tool} failed with {result.error_type}. Next action: {result.next_action}.")
    if not lines:
        return ""
    return "\n\nRecovery playbook:\n" + "\n".join(lines)
