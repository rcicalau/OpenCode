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
import time
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
from .researcher import Researcher
from .search import Searcher
from .session import PlanItem, SessionLedger
from .steering import SteeringInbox
from .tool_calls import (
    MALFORMED_TOOL_CALL_NAME,
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


READ_ONLY_TOOL_NAMES = {"explore_project", "read_text", "search", "git_status", "git_diff", "git_log", "git_remote_info", "git_merge_ready"}


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
        researcher: Researcher | None = None,
        max_tool_iterations: int | None = 200,
        max_work_items_per_prompt: int = 200,
        max_item_attempts: int = 3,
        no_progress_repeat_limit: int = 8,
        model_timeout_seconds: float = 300,
        model_timeout_grace_seconds: float = 30,
        rate_limit_retries: int = 4,
        rate_limit_backoff_seconds: float = 2,
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
        self.researcher = researcher
        if max_tool_iterations is not None and max_tool_iterations < 0:
            raise ValueError("max_tool_iterations must be non-negative or None")
        self.max_tool_iterations = max_tool_iterations or None
        if max_work_items_per_prompt <= 0:
            raise ValueError("max_work_items_per_prompt must be positive")
        self.max_work_items_per_prompt = max_work_items_per_prompt
        if max_item_attempts <= 0:
            raise ValueError("max_item_attempts must be positive")
        self.max_item_attempts = max_item_attempts
        if no_progress_repeat_limit <= 0:
            raise ValueError("no_progress_repeat_limit must be positive")
        self.no_progress_repeat_limit = no_progress_repeat_limit
        if model_timeout_seconds <= 0:
            raise ValueError("model_timeout_seconds must be positive")
        self.model_timeout_seconds = float(model_timeout_seconds)
        if model_timeout_grace_seconds < 0:
            raise ValueError("model_timeout_grace_seconds must be non-negative")
        self.model_timeout_grace_seconds = float(model_timeout_grace_seconds)
        if rate_limit_retries < 0:
            raise ValueError("rate_limit_retries must be non-negative")
        if rate_limit_backoff_seconds < 0:
            raise ValueError("rate_limit_backoff_seconds must be non-negative")
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_backoff_seconds = float(rate_limit_backoff_seconds)
        self._current_changed_files: list[str] = []
        self.tool_runtime = ToolRuntime(
            self.project_root,
            self.ledger,
            self.edit_broker,
            self.command_broker,
            self.searcher,
            self.validation,
            self.enabled_tools,
            self._mark_plan,
            self._record_changed_file,
            self._current_changed_files_snapshot,
            git_manager=self.git_manager,
        )

    def handle(self, prompt: str, event_sink=None) -> AgentResult:
        self._current_changed_files = []
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
        model_context = project_context.text
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
            model_context = self._context_with_research(prompt, mode, model_context, events)
            workplans = WorkPlanManager(self.project_root, self.ledger.session_id, self.edit_broker.policy)
            work_plan = workplans.active_or_new(prompt)
            if work_plan:
                self.ledger.objective = work_plan.objective
                events.append(AgentEvent("plan", "Plan", workplans.summary(work_plan)))
                return self._handle_work_plan(workplans, work_plan, model_context, events)
        elif mode == "scope":
            model_context = self._context_with_research(prompt, mode, model_context, events)
        validate_events_before = _validation_event_count(events)
        message = self._run_model_loop(prompt, model_context, events)
        validated_during_prompt = _validation_event_count(events) > validate_events_before
        validation_passed = self._auto_validate_after_edits(events, validated_during_prompt) if mode == "execute" else None
        if validation_passed is False:
            self.ledger.objective_state = BLOCKED
            message = (message.strip() + "\n\n" if message.strip() else "") + self._validation_failed_message()
        elif mode == "execute" and self.ledger.objective_state not in {APPROVAL_WAIT, BLOCKED}:
            self.ledger.objective_state = COMPLETE
        return AgentResult(mode=mode, message=message, changed_files=list(self.ledger.files_edited), events=events)

    def _run_model_loop(self, prompt: str, project_context: str, events: list[AgentEvent]) -> str:
        messages = [
            Message(
                "system",
                "You are Code Buddy. Be concise and grounded in the current project. "
                "Use the project context before answering questions about the repository. "
                "If context is insufficient, call explore_project first, then inspect files with read/search tools instead of guessing. "
                "In chat or scope mode, stay read-only: explore, search, and read are allowed; edits and shell commands are not. "
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
                response = self._complete_model(messages, tool_schemas, events)
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
            changed_count_before = len(self._current_changed_files)
            tool_results = self._run_tool_calls(calls, events)
            changed_count_after = len(self._current_changed_files)
            if signature == repeated_signature and changed_count_after == changed_count_before:
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

    def _complete_model(self, messages: list[Message], tool_schemas: list[dict], events: list[AgentEvent] | None = None):
        attempts = self.rate_limit_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return self._complete_model_once(messages, tool_schemas)
            except CodeBuddyError as exc:
                if attempt >= attempts or not _is_rate_limit_error(exc):
                    raise
                delay = self.rate_limit_backoff_seconds * attempt
                if events is not None:
                    events.append(AgentEvent("model", "Model", f"rate limited; retry {attempt}/{self.rate_limit_retries} in {delay:g}s", "running"))
                if delay:
                    time.sleep(delay)
        raise CodeBuddyError("model request failed after retries")

    def _complete_model_once(self, messages: list[Message], tool_schemas: list[dict]):
        result_queue: Queue = Queue(maxsize=1)

        def run_request() -> None:
            try:
                result_queue.put(("ok", self.llm.complete(messages, tools=tool_schemas)), block=False)
            except Exception as exc:
                result_queue.put(("error", exc), block=False)

        Thread(target=run_request, name="codebuddy-llm", daemon=True).start()
        try:
            status, payload = result_queue.get(timeout=self.model_timeout_seconds + self.model_timeout_grace_seconds)
        except Empty as exc:
            grace = f" plus {self.model_timeout_grace_seconds:g}s grace" if self.model_timeout_grace_seconds else ""
            raise CodeBuddyError(f"model request timed out after {self.model_timeout_seconds:g}s{grace}") from exc
        if status == "ok":
            return payload
        if isinstance(payload, CodeBuddyError):
            raise payload
        raise CodeBuddyError(f"model request failed: {payload}") from payload

    def _has_live_event_sink(self, events: list[AgentEvent]) -> bool:
        return bool(getattr(events, "sink", None))

    def _handle_work_plan(self, manager: WorkPlanManager, plan: WorkPlan, project_context: str, events: list[AgentEvent]) -> AgentResult:
        messages: list[str] = []
        processed = 0
        while processed < self.max_work_items_per_prompt:
            item = plan.next_item()
            if not item:
                break
            message, finished = self._run_work_item(manager, plan, item, project_context, events)
            if message.strip():
                messages.append(message.strip())
            manager.save(plan)
            self._sync_ledger_plan(plan)
            if self.ledger.objective_state in {APPROVAL_WAIT, BLOCKED} or not finished:
                break
            processed += 1
        if plan.next_item() and processed >= self.max_work_items_per_prompt:
            self.ledger.objective_state = WORKING
            events.append(AgentEvent("work", "Work", f"paused after {processed} work items; continue to resume"))
        return self._work_plan_result(manager, plan, messages, events)

    def _run_work_item(
        self,
        manager: WorkPlanManager,
        plan: WorkPlan,
        item: WorkItem,
        project_context: str,
        events: list[AgentEvent],
    ) -> tuple[str, bool]:
        last_message = ""
        while item.attempts < self.max_item_attempts:
            self.ledger.objective_state = WORKING
            item.status = "in_progress"
            item.attempts += 1
            manager.save(plan)
            self._sync_ledger_plan(plan)
            self.ledger.pending_next_step = item.label
            events.append(AgentEvent("work", "Work", f"{item.label} attempt {item.attempts}/{self.max_item_attempts}"))

            changed_count_before = len(self._current_changed_files)
            prompt = self._with_active_steering(manager.item_prompt(plan, item))
            last_message = self._run_model_loop(prompt, project_context, events)
            if self.ledger.objective_state == APPROVAL_WAIT:
                manager.save(plan)
                return last_message, False

            changed_now = self._current_changed_files[changed_count_before:]
            if not changed_now:
                validation_passed = True
            else:
                validation_passed = self._validate_work_slice(events, changed_now)
            item.validation_passed = validation_passed
            required_change = self._item_required_change_done(item, changed_now)
            if validation_passed and required_change:
                item.status = "completed"
                item.summary = last_message
                item.last_error = None
                self.ledger.completed_actions.append(f"completed work item {item.label}")
                self._checkpoint_work_slice(item, changed_now, events)
                return last_message, True

            item.last_error = "validation failed" if not validation_passed else "no expected file change detected"
            if not validation_passed and item.attempts < self.max_item_attempts:
                item.status = "pending"
                self.ledger.objective_state = WORKING
                events.append(AgentEvent("work", "Retry", f"{item.label}: {item.last_error}"))
                manager.save(plan)
                continue
            item.status = "blocked"
            self.ledger.objective_state = BLOCKED
            if f"{item.label}: {item.last_error}" not in self.ledger.blockers:
                self.ledger.blockers.append(f"{item.label}: {item.last_error}")
            events.append(AgentEvent("work", "Work", item.last_error, "failed"))
            return last_message, False

        item.status = "blocked"
        item.last_error = item.last_error or "attempt budget exhausted"
        self.ledger.objective_state = BLOCKED
        events.append(AgentEvent("work", "Work", item.last_error, "failed"))
        return last_message, False

    def _work_plan_result(self, manager: WorkPlanManager, plan: WorkPlan, messages: list[str], events: list[AgentEvent]) -> AgentResult:
        self._sync_ledger_plan(plan)
        next_item = plan.next_item()
        approval_waiting = self.ledger.objective_state == APPROVAL_WAIT
        if not approval_waiting:
            self.ledger.pending_next_step = next_item.label if next_item else None
        if approval_waiting:
            summary = f"Work plan paused for approval: {manager.summary(plan)}"
        elif plan.blocked_items():
            self.ledger.objective_state = BLOCKED
            blocked = ", ".join(f"{blocked.label}: {blocked.last_error or 'blocked'}" for blocked in plan.blocked_items()[:5])
            self.ledger.pending_next_step = f"resolve blocked work items: {blocked}"
            summary = f"Work plan blocked: {manager.summary(plan)}\nBlocked: {blocked}\nUse 'retry blocked' after resolving the issue."
        elif next_item:
            self.ledger.objective_state = WORKING
            summary = f"Work plan: paused {manager.summary(plan)}\nUse `continue` to resume."
        else:
            self.ledger.objective_state = COMPLETE
            summary = f"Work plan complete: {manager.summary(plan)}"
        body = "\n\n".join(messages[-3:])
        return AgentResult(
            mode="execute",
            message=(body + "\n\n" if body else "") + summary,
            changed_files=list(self.ledger.files_edited),
            events=events,
        )

    def _with_active_steering(self, prompt: str) -> str:
        steering = SteeringInbox(self.project_root).read()
        if not steering:
            return prompt
        return f"{prompt}\n\nUser steering:\n{steering}\n"

    def _context_with_research(self, prompt: str, mode: str, project_context: str, events: list[AgentEvent]) -> str:
        if not self.researcher:
            return project_context
        brief = self.researcher.research(prompt, project_context, mode)
        if not brief:
            if getattr(self.researcher, "last_error", None):
                events.append(AgentEvent("research", "Research", str(self.researcher.last_error), "failed"))
            return project_context
        events.append(AgentEvent("research", "Research", f"{len(brief.relevant_files)} relevant files"))
        return project_context + "\n\n" + brief.to_context()

    def _run_text_tool_calls(self, text: str, events: list[AgentEvent]) -> list[str]:
        return [result.to_prompt() for result in self._run_tool_calls(parse_text_tool_calls(text), events)]

    def _collect_tool_calls(self, response) -> list[ParsedToolCall]:
        calls = list(response.tool_calls or [])
        calls.extend(parse_text_edit_blocks(response.content))
        calls.extend(parse_text_tool_calls(response.content))
        return calls

    def _run_tool_calls(self, calls: list[ParsedToolCall], events: list[AgentEvent]) -> list[ToolResult]:
        if self.ledger.mode not in {"chat", "scope"}:
            return self.tool_runtime.run_structured(calls, events)
        allowed: list[ParsedToolCall] = []
        results: list[ToolResult] = []
        for call in calls:
            if call.name in READ_ONLY_TOOL_NAMES or call.name == MALFORMED_TOOL_CALL_NAME:
                allowed.append(call)
                continue
            detail = f"{call.name}: denied in {self.ledger.mode} mode; use /do or an execute prompt for mutations"
            events.append(AgentEvent("tool", "Tool", detail, "failed"))
            results.append(
                ToolResult(
                    call.name,
                    False,
                    f"{call.name} denied: {self.ledger.mode} mode is read-only. Continue with explore_project, search, or read_text.",
                    error_type="read_only_mode",
                    retryable=False,
                    next_action="continue_read_only_or_ask_for_execute_mode",
                )
            )
        if allowed:
            results.extend(self.tool_runtime.run_structured(allowed, events))
        return results

    def _validate_work_slice(self, events: list[AgentEvent], rel_paths: list[str] | None = None) -> bool:
        if not self.validation:
            return True
        touched_rel_paths = list(dict.fromkeys(rel_paths if rel_paths is not None else self._current_changed_files))
        if not touched_rel_paths:
            return True
        touched = [self.project_root / path for path in touched_rel_paths]
        validation = self.validation.validate(touched, expected_files=touched)
        self.ledger.validation_state = {
            "passed": validation.passed,
            "failures": validation.failures,
            "failure_code": validation.failure_code,
            "recovery_actions": validation.recovery_actions,
            "touched_files": validation.touched_files,
            "unexpected_files": validation.unexpected_files,
            "worktree_report": validation.worktree_report,
        }
        self._mark_plan("Validate outcome", "completed" if validation.passed else "pending")
        detail = "passed" if validation.passed else f"failed ({len(validation.failures)} failures)"
        events.append(AgentEvent("validate", "Validate", detail, "done" if validation.passed else "failed"))
        return validation.passed

    def _auto_validate_after_edits(self, events: list[AgentEvent], already_validated: bool = False) -> bool | None:
        if already_validated or not self._current_changed_files:
            return None
        return self._validate_work_slice(events)

    def _item_required_change_done(self, item: WorkItem, changed_now: list[str]) -> bool:
        if item.action == "document_file":
            return item.target_path in changed_now
        if item.action == "create_tests_for_class":
            return any(path.startswith("tests/") or Path(path).name.startswith("test_") for path in changed_now)
        if item.action == "create_tests_for_file":
            return any(path.startswith("tests/") or Path(path).name.startswith("test_") for path in changed_now)
        return bool(changed_now)

    def _record_changed_file(self, rel_path: str) -> None:
        self._current_changed_files.append(rel_path)

    def _current_changed_files_snapshot(self) -> list[str]:
        return list(self._current_changed_files)

    def _validation_failed_message(self) -> str:
        failures = self.ledger.validation_state.get("failures") if isinstance(self.ledger.validation_state, dict) else None
        failure_code = self.ledger.validation_state.get("failure_code") if isinstance(self.ledger.validation_state, dict) else None
        if failure_code == "unexpected_worktree_changes":
            unexpected = self.ledger.validation_state.get("unexpected_files") or []
            files = ", ".join(str(path) for path in unexpected[:5]) if unexpected else "unknown files"
            return (
                "Validation found unexpected worktree changes outside the current Buddy edit set: "
                f"{files}. I will preserve those changes. Next recovery step: inspect git status, keep user changes separate, "
                "and retry validation or commit only the expected agent-owned files."
            )
        if not failures:
            return "Validation failed after edits. Review validation output before continuing."
        detail = "; ".join(str(failure) for failure in failures[:3])
        return f"Validation failed after edits: {detail}"

    def _checkpoint_work_slice(self, item: WorkItem, changed_now: list[str], events: list[AgentEvent]) -> None:
        if not changed_now:
            return
        try:
            if self.git_manager.checkpoint_commit(f"Code Buddy slice: {item.label}", changed_now):
                events.append(AgentEvent("git", "Git", f"checkpoint committed {item.label}"))
                if self.git_manager.push_current_branch("origin"):
                    events.append(AgentEvent("git", "Git", "pushed checkpoint to origin"))
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
            "explore": {
                "type": "function",
                "function": {
                    "name": "explore_project",
                    "description": (
                        "Build a read-only project exploration map: key files, stack signals, modules, "
                        "entrypoints, tests, symbols, relevant files, and recommended next reads."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "focus": {"type": "string"},
                            "max_files": {"type": "integer"},
                            "max_symbols": {"type": "integer"},
                        },
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
            "git_status": {
                "type": "function",
                "function": {
                    "name": "git_status",
                    "description": "Inspect the current project's git branch and porcelain status.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            "git_diff": {
                "type": "function",
                "function": {
                    "name": "git_diff",
                    "description": "Show a review-grade git diff summary for staged, unstaged, and untracked changes.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            "git_log": {
                "type": "function",
                "function": {
                    "name": "git_log",
                    "description": "Read recent git history for the current project.",
                    "parameters": {
                        "type": "object",
                        "properties": {"max_count": {"type": "integer"}},
                    },
                },
            },
            "git_remote_info": {
                "type": "function",
                "function": {
                    "name": "git_remote_info",
                    "description": "Inspect the origin remote provider, host, owner, and repository.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            "git_merge_ready": {
                "type": "function",
                "function": {
                    "name": "git_merge_ready",
                    "description": "Check whether the agent branch is clean, validated, and ready to merge.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            "git_commit": {
                "type": "function",
                "function": {
                    "name": "git_commit",
                    "description": "Commit only agent-owned changed files on the agent branch.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "paths": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["message"],
                    },
                },
            },
            "git_push": {
                "type": "function",
                "function": {
                    "name": "git_push",
                    "description": "Push the current agent branch to a configured remote.",
                    "parameters": {
                        "type": "object",
                        "properties": {"remote": {"type": "string"}},
                    },
                },
            },
        }
        enabled = []
        allowed = set(schemas)
        if self.ledger.mode in {"chat", "scope"}:
            allowed = {"explore", "read", "search", "git_status", "git_diff", "git_log", "git_remote_info", "git_merge_ready"}
        for key, schema in schemas.items():
            if key in allowed and self.enabled_tools.get(key, True):
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


def _validation_event_count(events: list[AgentEvent]) -> int:
    return sum(1 for event in events if event.kind == "validate")


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


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ["429", "rate limit", "rate_limit", "too many requests", "quota exceeded", "request limit"])
