from __future__ import annotations

import sys
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.agent import CodeBuddyAgent
from codebuddy.command_broker import CommandBroker, CommandPolicy
from codebuddy.edit_broker import EditBroker
from codebuddy.git_manager import GitManager
from codebuddy.hashutil import sha256_bytes
from codebuddy.journal import Journal
from codebuddy.llm import FakeLLMClient, LLMResponse
from codebuddy.objective_state import APPROVAL_WAIT, BLOCKED, COMPLETE
from codebuddy.tool_calls import MALFORMED_TOOL_CALL_NAME, ParsedToolCall
from codebuddy.paths import PathPolicy
from codebuddy.search import Searcher
from codebuddy.session import SessionManager
from codebuddy.tool_runtime import ToolRuntime
from codebuddy.tool_calls import parse_text_edit_blocks, parse_text_tool_calls, strip_tool_calls
from codebuddy.validation import ValidationHarness, ValidationResult
from codebuddy.errors import CodeBuddyError, DeniedByPolicy


class AgentToolsSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        manager = SessionManager(self.root)
        self.ledger = manager.load_or_create()
        self.journal = Journal(manager.session_dir(self.ledger.session_id) / "journal.jsonl")
        self.policy = PathPolicy(self.root)
        self.edit = EditBroker(self.policy, self.journal, self.ledger.session_id)
        self.command = CommandBroker(self.root, CommandPolicy(default_timeout_seconds=10), self.journal, self.ledger.session_id)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_agent(
        self,
        responses: list[str | LLMResponse],
        enabled_tools: dict[str, bool] | None = None,
        **agent_kwargs,
    ) -> CodeBuddyAgent:
        return CodeBuddyAgent(
            self.root,
            self.ledger,
            FakeLLMClient(responses),
            self.edit,
            self.command,
            GitManager(self.root),
            Searcher(self.policy),
            ValidationHarness(self.root, self.command),
            enabled_tools,
            **agent_kwargs,
        )

    def test_agent_retries_transient_rate_limit_errors(self) -> None:
        class FlakyRateLimitLLM:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    raise CodeBuddyError("provider HTTP error 429: rate limit exceeded")
                return LLMResponse("Recovered after retry.")

        llm = FlakyRateLimitLLM()
        agent = CodeBuddyAgent(
            self.root,
            self.ledger,
            llm,
            self.edit,
            self.command,
            GitManager(self.root),
            Searcher(self.policy),
            ValidationHarness(self.root, self.command),
            rate_limit_retries=2,
            rate_limit_backoff_seconds=0,
        )

        result = agent.handle("What is here?")

        self.assertEqual(result.message, "Recovered after retry.")
        self.assertEqual(llm.calls, 2)
        self.assertTrue(any(event.title == "Model" and "rate limited" in event.detail for event in result.events))

    def test_text_tool_call_parser(self) -> None:
        text = 'hello <tool_call>{"name":"read_text","arguments":{"path":"a.py"}}</tool_call> bye'

        calls = parse_text_tool_calls(text)

        self.assertEqual(calls[0].name, "read_text")
        self.assertEqual(calls[0].arguments["path"], "a.py")
        self.assertEqual(strip_tool_calls(text), "hello  bye")

    def test_text_tool_call_parser_accepts_unquoted_keys(self) -> None:
        text = "<tool_call>{name:\"read_text\", arguments:{path:\"a.py\"}}</tool_call>"

        calls = parse_text_tool_calls(text)

        self.assertEqual(calls[0].name, "read_text")
        self.assertEqual(calls[0].arguments["path"], "a.py")

    def test_text_tool_call_parser_accepts_python_style_quotes(self) -> None:
        text = "<tool_call>{'name':'read_text', 'arguments':{'path':'a.py'}}</tool_call>"

        calls = parse_text_tool_calls(text)

        self.assertEqual(calls[0].name, "read_text")
        self.assertEqual(calls[0].arguments["path"], "a.py")

    def test_raw_replace_edit_block_parser_avoids_json_escaping(self) -> None:
        text = '''<codebuddy_replace path="agent.py">
<old>
def handle():
    return 'ok'
</old>
<new>
def handle():
    """Return ok."""
    return 'ok'
</new>
</codebuddy_replace>'''

        calls = parse_text_edit_blocks(text)

        self.assertEqual(calls[0].name, "edit_exact_replace")
        self.assertEqual(calls[0].arguments["path"], "agent.py")
        self.assertIn('"""Return ok."""', calls[0].arguments["new"])
        self.assertEqual(strip_tool_calls(text), "")

    def test_raw_rewrite_edit_block_avoids_json_escaping_for_whole_file_edits(self) -> None:
        target = self.root / "agent.py"
        target.write_text("def handle():\n    return 'old'\n", encoding="utf-8")
        before_hash = sha256_bytes(target.read_bytes())
        agent = self.make_agent(
            [
                f'''<codebuddy_rewrite path="agent.py" expected_hash="{before_hash}">
def handle():
    """Return the updated response."""
    return 'new'
</codebuddy_rewrite>''',
                "Rewrote agent.py.",
            ]
        )

        result = agent.handle("Rewrite agent.py with documentation")

        self.assertIn("Rewrote agent.py", result.message)
        self.assertIn('"""Return the updated response."""', target.read_text(encoding="utf-8"))

    def test_malformed_tool_call_raises_structured_error(self) -> None:
        with self.assertRaises(CodeBuddyError):
            parse_text_tool_calls("<tool_call>{bad json}</tool_call>")

    def test_agent_executes_edit_tool_and_validation_through_brokers(self) -> None:
        target = self.root / "app.py"
        target.write_text("def f():\n    return 1\n", encoding="utf-8")
        tool_call = """<tool_call>{"name":"edit_exact_replace","arguments":{"path":"app.py","old":"return 1","new":"return 2"}}</tool_call>
<tool_call>{"name":"validate","arguments":{}}</tool_call>"""
        agent = self.make_agent([tool_call, "Edited app.py and validation passed."])

        result = agent.handle("Change f to return 2")

        self.assertIn("validation passed", result.message)
        self.assertEqual(target.read_text(encoding="utf-8"), "def f():\n    return 2\n")
        self.assertEqual(self.ledger.files_edited, ["app.py"])
        self.assertTrue(self.ledger.validation_state["passed"])
        self.assertEqual([event.title for event in result.events], ["Context", "Edit", "Validate"])
        self.assertEqual([entry.action for entry in self.journal.entries()], ["edit_intent", "exact_replace"])

    def test_execute_auto_validates_after_edit_when_model_forgets(self) -> None:
        target = self.root / "app.py"
        target.write_text("def f():\n    return 1\n", encoding="utf-8")
        tool_call = '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"app.py","old":"return 1","new":"return 2"}}</tool_call>'
        agent = self.make_agent([tool_call, "Edited app.py."])

        result = agent.handle("Change f to return 2")

        self.assertTrue(self.ledger.validation_state["passed"])
        self.assertEqual([event.title for event in result.events], ["Context", "Edit", "Validate"])
        self.assertEqual(self.ledger.objective_state, COMPLETE)

    def test_execute_does_not_auto_validate_stale_session_edits_without_current_edit(self) -> None:
        bad = self.root / "bad.py"
        bad.write_text("def broken(:\n", encoding="utf-8")
        self.ledger.files_edited.append("bad.py")
        self.ledger.validation_state.clear()
        agent = self.make_agent(["No file changes were needed."])

        result = agent.handle("Do nothing for this objective")

        self.assertNotIn("Validation failed after edits", result.message)
        self.assertFalse(any(event.kind == "validate" for event in result.events))
        self.assertEqual(self.ledger.validation_state, {})
        self.assertEqual(self.ledger.objective_state, COMPLETE)

    def test_execute_auto_validates_current_edit_even_with_previous_validation_state(self) -> None:
        target = self.root / "app.py"
        target.write_text("def f():\n    return 1\n", encoding="utf-8")
        self.ledger.files_edited.append("app.py")
        self.ledger.validation_state = {"passed": False, "failures": ["old failure"]}
        tool_call = '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"app.py","old":"return 1","new":"return 2"}}</tool_call>'
        agent = self.make_agent([tool_call, "Edited app.py."])

        result = agent.handle("Change f to return 2 again")

        self.assertIn("Edited app.py", result.message)
        self.assertTrue(any(event.kind == "validate" for event in result.events))
        self.assertTrue(self.ledger.validation_state["passed"])
        self.assertEqual(self.ledger.validation_state["touched_files"], ["app.py"])

    def test_execute_auto_validation_allows_python_launcher_version_flag(self) -> None:
        target = self.root / "app.py"
        target.write_text("def f():\n    return 1\n", encoding="utf-8")
        tests_dir = self.root / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_smoke.py").write_text(
            "import unittest\n\nclass SmokeTests(unittest.TestCase):\n    def test_smoke(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        tool_call = '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"app.py","old":"return 1","new":"return 2"}}</tool_call>'
        agent = CodeBuddyAgent(
            self.root,
            self.ledger,
            FakeLLMClient([tool_call, "Edited app.py."]),
            self.edit,
            self.command,
            GitManager(self.root),
            Searcher(self.policy),
            ValidationHarness(self.root, self.command, ["python -m unittest discover -s tests"]),
        )

        result = agent.handle("Change f to return 2")

        self.assertIn("Edited app.py", result.message)
        self.assertTrue(self.ledger.validation_state["passed"])

    def test_execute_reports_validation_confirmation_without_crashing(self) -> None:
        target = self.root / "app.py"
        target.write_text("def f():\n    return 1\n", encoding="utf-8")
        tool_call = '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"app.py","old":"return 1","new":"return 2"}}</tool_call>'
        agent = CodeBuddyAgent(
            self.root,
            self.ledger,
            FakeLLMClient([tool_call, "Edited app.py."]),
            self.edit,
            self.command,
            GitManager(self.root),
            Searcher(self.policy),
            ValidationHarness(self.root, self.command, ["git add ."]),
        )

        result = agent.handle("Change f to return 2")

        self.assertIn("Validation failed after edits", result.message)
        self.assertFalse(self.ledger.validation_state["passed"])
        self.assertIn("requires confirmation", self.ledger.validation_state["failures"][0])

    def test_run_command_confirmation_pauses_and_can_be_approved(self) -> None:
        command = "Write-Output hi > approved.txt"
        first = self.make_agent(
            [
                f'<tool_call>{{"name":"run_command","arguments":{{"command":"{command}"}}}}</tool_call>',
            ]
        )

        paused = first.handle("Run the writer")

        self.assertIn("needs approval", paused.message)
        self.assertEqual(self.ledger.pending_next_step, "approve command before execution")
        self.assertEqual(self.ledger.approvals["pending_command"], command)
        self.assertEqual(self.ledger.objective_state, APPROVAL_WAIT)
        self.assertFalse((self.root / "approved.txt").exists())

    def test_workplan_preserves_command_approval_wait_state(self) -> None:
        command = "Write-Output hi > approved.txt"
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        agent = self.make_agent(
            [
                f'<tool_call>{{"name":"run_command","arguments":{{"command":"{command}"}}}}</tool_call>',
            ]
        )

        result = agent.handle("Document each file in the codebase")

        self.assertIn("Command needs approval", result.message)
        self.assertEqual(self.ledger.pending_next_step, "approve command before execution")
        self.assertEqual(self.ledger.objective_state, APPROVAL_WAIT)

    def test_agent_loops_over_multiple_tool_rounds_before_answering(self) -> None:
        (self.root / "README.md").write_text("needle lives here\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"search","arguments":{"pattern":"needle"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                "The project mentions that the needle lives here.",
            ]
        )

        result = agent.handle("What does this project say about needle?")

        self.assertIn("needle lives here", result.message)
        self.assertEqual(len(agent.llm.calls), 3)
        self.assertEqual([event.title for event in result.events], ["Context", "Search", "Read"])

    def test_missing_read_text_file_is_tool_failure_not_crash(self) -> None:
        (self.root / "README.md").write_text("real file\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"read_text","arguments":{"path":"tests/test_agent.py"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                "Recovered after missing file.",
            ]
        )

        result = agent.handle("/ask inspect files")

        self.assertIn("Recovered", result.message)
        self.assertTrue(any(event.title == "Read" and event.status == "failed" for event in result.events))
        self.assertIn("README.md", self.ledger.files_inspected)
        replayed_prompt = "\n".join(message.content for message in agent.llm.calls[-1])
        self.assertIn("Recovery playbook", replayed_prompt)
        self.assertIn("Search for the correct path", replayed_prompt)

    def test_replays_bad_provider_tool_output_without_traceback(self) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures" / "perplexity_bad_tool_outputs.json"
        cases = json.loads(fixture.read_text(encoding="utf-8"))
        agent = self.make_agent(cases[0]["responses"])

        result = agent.handle("/ask inspect likely test file")

        self.assertIn("Recovered", result.message)
        self.assertTrue(any(event.status == "failed" for event in result.events))

    def test_tool_runtime_contains_missing_file_errors(self) -> None:
        runtime = ToolRuntime(
            self.root,
            self.ledger,
            self.edit,
            self.command,
            Searcher(self.policy),
            ValidationHarness(self.root, self.command),
        )
        events = []

        results = runtime.run([ParsedToolCall("read_text", {"path": "missing.py"})], events)

        self.assertIn("read_text missing.py failed", results[0])
        self.assertEqual(events[0].title, "Read")
        self.assertEqual(events[0].status, "failed")

    def test_edit_conflict_is_tool_failure_not_crash(self) -> None:
        (self.root / "agent.py").write_text("def handle():\n    return 'ok'\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"agent.py","old":"missing","new":"replacement"}}</tool_call>',
                "Could not apply exact edit.",
            ]
        )

        result = agent.handle("Document agent.py")

        self.assertIn("Could not apply", result.message)
        self.assertTrue(any(event.title == "Edit" and event.status == "failed" for event in result.events))
        self.assertEqual(self.ledger.objective_state, BLOCKED)
        self.assertEqual((self.root / "agent.py").read_text(encoding="utf-8"), "def handle():\n    return 'ok'\n")

    def test_agent_executes_native_tool_calls(self) -> None:
        (self.root / "README.md").write_text("native tool context\n", encoding="utf-8")
        agent = self.make_agent(
            [
                LLMResponse(
                    "",
                    tool_calls=[
                        ParsedToolCall(
                            "read_text",
                            {"path": "README.md"},
                            call_id="call_1",
                        )
                    ],
                ),
                "The README says native tool context.",
            ]
        )

        result = agent.handle("/ask Read the README")

        self.assertIn("native tool context", "\n".join(message.content for message in agent.llm.calls[1]))
        self.assertIn("native tool context", result.message)
        self.assertEqual([event.title for event in result.events], ["Context", "Read"])
        self.assertTrue(agent.llm.tool_requests[0])

    def test_agent_recovers_from_malformed_native_tool_arguments(self) -> None:
        (self.root / "agent.py").write_text("def handle():\n    return 'ok'\n", encoding="utf-8")
        agent = self.make_agent(
            [
                LLMResponse(
                    "",
                    tool_calls=[
                        ParsedToolCall(
                            MALFORMED_TOOL_CALL_NAME,
                            {
                                "name": "edit_exact_replace",
                                "error": "Unterminated string starting at: line 1 column 91",
                                "raw_arguments": '{"path":"agent.py","old":"def handle():\n    return \'ok\'\n","new":"def handle():\n    """Return ok."""\n    return \'ok\'\n"}',
                            },
                            call_id="call_bad",
                        )
                    ],
                ),
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"agent.py","old":"def handle():\\n    return \'ok\'\\n","new":"def handle():\\n    \\"\\"\\"Return ok.\\"\\"\\"\\n    return \'ok\'\\n"}}</tool_call>',
                "Documented agent.py.",
            ]
        )

        result = agent.handle("Add google style documentation to agent.py")

        self.assertIn("Documented agent.py", result.message)
        self.assertIn('"""Return ok."""', (self.root / "agent.py").read_text(encoding="utf-8"))
        self.assertTrue(any(event.title == "Tool" and event.status == "failed" for event in result.events))
        self.assertIn("Retry the same tool call with valid JSON", "\n".join(message.content for message in agent.llm.calls[1]))

    def test_agent_recovers_from_malformed_text_tool_call_json(self) -> None:
        (self.root / "agent.py").write_text("def handle():\n    return 'ok'\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"agent.py","old":"def handle():\n    return \'ok\'\n","new":"unterminated}}</tool_call>',
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"agent.py","old":"def handle():\\n    return \'ok\'\\n","new":"def handle():\\n    \\"\\"\\"Return ok.\\"\\"\\"\\n    return \'ok\'\\n"}}</tool_call>',
                "Documented agent.py.",
            ]
        )

        result = agent.handle("Add google style documentation to agent.py")

        self.assertIn("Documented agent.py", result.message)
        self.assertIn('"""Return ok."""', (self.root / "agent.py").read_text(encoding="utf-8"))
        self.assertTrue(any(event.title == "Tool" and event.status == "failed" for event in result.events))

    def test_stale_hash_edit_failure_adds_recovery_playbook_to_next_prompt(self) -> None:
        (self.root / "agent.py").write_text("def handle():\n    return 'ok'\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '''<codebuddy_rewrite path="agent.py" expected_hash="stale">
def handle():
    return 'new'
</codebuddy_rewrite>''',
                "I will recover.",
            ]
        )

        agent.handle("Rewrite agent.py")
        recovery_prompt = agent.llm.calls[1][-1].content

        self.assertIn("Recovery playbook", recovery_prompt)
        self.assertIn("reread_file_then_retry", recovery_prompt)
        self.assertIn("expected_hash", recovery_prompt)

    def test_agent_executes_raw_replace_edit_block_for_multiline_documentation(self) -> None:
        (self.root / "agent.py").write_text("def handle():\n    return 'ok'\n", encoding="utf-8")
        raw_edit = '''<codebuddy_replace path="agent.py">
<old>
def handle():
    return 'ok'
</old>
<new>
def handle():
    """Return ok."""
    # Keep the explicit success value easy for junior developers to see.
    return 'ok'
</new>
</codebuddy_replace>'''
        agent = self.make_agent([raw_edit, "Documented agent.py."])

        result = agent.handle("Add google style documentation to agent.py")

        content = (self.root / "agent.py").read_text(encoding="utf-8")
        self.assertIn('"""Return ok."""', content)
        self.assertIn("junior developers", content)
        self.assertIn("1/1 completed", result.message)

    def test_native_tool_schema_does_not_expose_fragile_edit_tools(self) -> None:
        agent = self.make_agent(["done"])

        tool_names = {schema["function"]["name"] for schema in agent._tool_schemas()}

        self.assertNotIn("edit_exact_replace", tool_names)
        self.assertNotIn("create_file", tool_names)
        self.assertIn("read_text", tool_names)

    def test_agent_event_sink_receives_events_before_result(self) -> None:
        (self.root / "README.md").write_text("live events\n", encoding="utf-8")
        seen: list[str] = []
        agent = self.make_agent(
            [
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                "Read it.",
            ]
        )

        result = agent.handle("/ask Read README", event_sink=lambda event: seen.append(event.title))

        self.assertIn("Read", seen)
        self.assertEqual(seen, [event.title for event in result.events])

    def test_model_request_after_tool_result_times_out_with_live_event(self) -> None:
        release_provider = threading.Event()

        class SlowAfterToolLLM:
            def __init__(self) -> None:
                self.calls = 0
                self.tool_requests: list[list[dict]] = []

            def complete(self, _messages, tools=None):
                self.calls += 1
                self.tool_requests.append(list(tools or []))
                if self.calls == 1:
                    return LLMResponse(
                        '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>'
                    )
                release_provider.wait(2.0)
                return LLMResponse("late response")

        (self.root / "README.md").write_text("slow model repro\n", encoding="utf-8")
        seen = []
        agent = CodeBuddyAgent(
            self.root,
            self.ledger,
            SlowAfterToolLLM(),
            self.edit,
            self.command,
            GitManager(self.root),
            Searcher(self.policy),
            ValidationHarness(self.root, self.command),
            max_tool_iterations=3,
            model_timeout_seconds=0.05,
            model_timeout_grace_seconds=0,
        )

        try:
            started = time.monotonic()
            result = agent.handle("/ask Read README", event_sink=lambda event: seen.append(event))
        finally:
            release_provider.set()

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("model request timed out", result.message.lower())
        self.assertTrue(any(event.title == "Model" for event in seen))
        self.assertTrue(any(event.title == "Model" and event.status == "failed" for event in seen))
        self.assertEqual(self.ledger.objective_state, BLOCKED)

    def test_agent_executes_lenient_text_tool_call_without_retry_noise(self) -> None:
        (self.root / "README.md").write_text("lenient read\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{name:"read_text", arguments:{path:"README.md"}}</tool_call>',
                "Read it.",
            ]
        )

        result = agent.handle("/ask Read README")

        self.assertIn("Read it", result.message)
        self.assertFalse(any(event.title == "Tool" and event.status == "failed" for event in result.events))
        self.assertEqual([event.title for event in result.events], ["Context", "Read"])

    def test_read_text_result_includes_hash_for_guarded_rewrites(self) -> None:
        target = self.root / "README.md"
        target.write_text("hash me\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                "Read it.",
            ]
        )

        agent.handle("/ask Read README")
        tool_result_prompt = agent.llm.calls[1][-1].content

        self.assertIn("sha256:", tool_result_prompt)
        self.assertIn(sha256_bytes(target.read_bytes()), tool_result_prompt)

    def test_default_agent_loop_can_finish_after_more_than_six_tool_rounds(self) -> None:
        (self.root / "README.md").write_text("long loop\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                "Finished after a longer inspection loop.",
            ]
        )

        result = agent.handle("/ask Keep inspecting until done")

        self.assertIn("Finished after a longer inspection loop.", result.message)
        self.assertEqual([event.title for event in result.events].count("Read"), 7)
        self.assertNotEqual(self.ledger.objective_state, BLOCKED)

    def test_agent_blocks_repeated_identical_tool_loop_without_waiting_for_model_budget(self) -> None:
        (self.root / "README.md").write_text("stuck loop\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
                '<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>',
            ]
        )

        result = agent.handle("/ask Read README until you know what to do")

        self.assertIn("no progress", result.message.lower())
        self.assertEqual(self.ledger.objective_state, BLOCKED)
        self.assertTrue(any(event.title == "Loop" and event.status == "failed" for event in result.events))

    def test_dirty_worktree_blocks_execution_with_approval_instruction(self) -> None:
        subprocess.run(["git", "init"], cwd=self.root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "branch", "-M", "main"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("dirty\n", encoding="utf-8")
        agent = self.make_agent(["should not run"])

        result = agent.handle("Update docs")

        self.assertIn("/approve-branch", result.message)
        self.assertEqual(agent.llm.calls, [])
        self.assertEqual(result.events[-1].status, "failed")

    def test_dirty_worktree_approval_allows_next_execute_prompt_once(self) -> None:
        subprocess.run(["git", "init"], cwd=self.root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "branch", "-M", "main"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("dirty\n", encoding="utf-8")
        self.ledger.approvals["dirty_branch"] = True
        agent = self.make_agent(["Approved branch created."])

        result = agent.handle("Update docs")
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.root, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()

        self.assertTrue(branch.startswith("codebuddy/"))
        self.assertIn("Approved branch created", result.message)
        self.assertFalse(self.ledger.approvals.get("dirty_branch", False))

    def test_document_codebase_workplan_processes_one_file_and_persists_remaining(self) -> None:
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"a.py","old":"def a():\\n    return 1\\n","new":"def a():\\n    \\"\\"\\"Return one.\\"\\"\\"\\n    return 1\\n"}}</tool_call>',
                "Documented a.py.",
            ],
            max_work_items_per_prompt=1,
        )

        result = agent.handle("Document each file in the codebase")

        self.assertIn("Work plan:", result.message)
        self.assertIn("1/2 completed", result.message)
        self.assertIn("b.py", self.ledger.pending_next_step)
        self.assertIn('"""Return one."""', (self.root / "a.py").read_text(encoding="utf-8"))
        self.assertNotIn('"""', (self.root / "b.py").read_text(encoding="utf-8"))
        saved = (self.root / ".buddy" / "workplans" / "current.json").read_text(encoding="utf-8")
        self.assertIn('"status": "completed"', saved)
        self.assertIn('"status": "pending"', saved)

    def test_document_codebase_workplan_can_complete_multiple_files_in_one_prompt(self) -> None:
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"a.py","old":"def a():\\n    return 1\\n","new":"def a():\\n    \\"\\"\\"Return one.\\"\\"\\"\\n    return 1\\n"}}</tool_call>',
                "Documented a.py.",
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"b.py","old":"def b():\\n    return 2\\n","new":"def b():\\n    \\"\\"\\"Return two.\\"\\"\\"\\n    return 2\\n"}}</tool_call>',
                "Documented b.py.",
            ]
        )

        result = agent.handle("Document each file in the codebase using Google style")

        self.assertIn("2/2 completed", result.message)
        self.assertEqual(self.ledger.pending_next_step, None)
        self.assertIn('"""Return one."""', (self.root / "a.py").read_text(encoding="utf-8"))
        self.assertIn('"""Return two."""', (self.root / "b.py").read_text(encoding="utf-8"))
        self.assertIn("Google style", "\n".join(message.content for message in agent.llm.calls[0]))

    def test_workplan_prompts_include_active_user_steering(self) -> None:
        (self.root / ".buddy" / "steering").mkdir(parents=True)
        (self.root / ".buddy" / "steering" / "active.md").write_text(
            "Prefer short module docstrings and do not rename symbols.\n",
            encoding="utf-8",
        )
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        agent = self.make_agent(
            [
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"a.py","old":"def a():\\n    return 1\\n","new":"def a():\\n    \\"\\"\\"Return one.\\"\\"\\"\\n    return 1\\n"}}</tool_call>',
                "Documented a.py.",
            ]
        )

        agent.handle("Document each file in the codebase")

        first_prompt = "\n".join(message.content for message in agent.llm.calls[0])
        self.assertIn("User steering", first_prompt)
        self.assertIn("do not rename symbols", first_prompt)

    def test_workplan_retries_validation_failure_before_blocking(self) -> None:
        class FailOnceValidation(ValidationHarness):
            def __init__(self, root: Path, command: CommandBroker) -> None:
                super().__init__(root, command)
                self.calls = 0

            def validate(self, touched_files=None, expected_files=None):
                self.calls += 1
                if self.calls == 1:
                    return ValidationResult(False, failures=["temporary validation failure"])
                return super().validate(touched_files, expected_files)

        target = self.root / "a.py"
        target.write_text("def a():\n    return 1\n", encoding="utf-8")
        validation = FailOnceValidation(self.root, self.command)
        agent = CodeBuddyAgent(
            self.root,
            self.ledger,
            FakeLLMClient(
                [
                    '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"a.py","old":"def a():\\n    return 1\\n","new":"def a():\\n    \\"\\"\\"Return one, draft.\\n\\n    Returns:\\n        int: The number one.\\n    \\"\\"\\"\\n    return 1\\n"}}</tool_call>',
                    "Draft documentation added.",
                    '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"a.py","old":"\\"\\"\\"Return one, draft.\\n\\n    Returns:\\n        int: The number one.\\n    \\"\\"\\"","new":"\\"\\"\\"Return the number one.\\n\\n    Returns:\\n        int: Always returns 1.\\n    \\"\\"\\""}}</tool_call>',
                    "Fixed validation failure.",
                ]
            ),
            self.edit,
            self.command,
            GitManager(self.root),
            Searcher(self.policy),
            validation,
            max_item_attempts=2,
        )

        result = agent.handle("Document each file in the codebase")

        self.assertIn("1/1 completed", result.message)
        self.assertEqual(validation.calls, 2)
        self.assertIn("Always returns 1", target.read_text(encoding="utf-8"))

    def test_single_file_documentation_task_uses_document_workplan(self) -> None:
        (self.root / "agent.py").write_text("def handle():\n    return 'ok'\n", encoding="utf-8")
        agent = self.make_agent(
            [
                """<tool_call>{"name":"edit_exact_replace","arguments":{"path":"agent.py","old":"def handle():\\n    return 'ok'\\n","new":"def handle():\\n    \\"\\"\\"Return the fixed success response.\\"\\"\\"\\n    return 'ok'\\n"}}</tool_call>""",
                "Documented agent.py.",
            ]
        )

        result = agent.handle("Add google style documentation to agent.py")

        self.assertIn("1/1 completed", result.message)
        self.assertIn('"""Return the fixed success response."""', (self.root / "agent.py").read_text(encoding="utf-8"))
        self.assertFalse(self.ledger.commands_run)

    def test_workplan_resume_processes_next_pending_item(self) -> None:
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        first = self.make_agent(
            [
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"a.py","old":"def a():\\n    return 1\\n","new":"def a():\\n    \\"\\"\\"Return one.\\"\\"\\"\\n    return 1\\n"}}</tool_call>',
                "Documented a.py.",
            ],
            max_work_items_per_prompt=1,
        )
        first.handle("Document each file in the codebase")
        second = self.make_agent(
            [
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"b.py","old":"def b():\\n    return 2\\n","new":"def b():\\n    \\"\\"\\"Return two.\\"\\"\\"\\n    return 2\\n"}}</tool_call>',
                "Documented b.py.",
            ]
        )

        result = second.handle("continue")

        self.assertIn("2/2 completed", result.message)
        self.assertEqual(self.ledger.pending_next_step, None)
        self.assertIn('"""Return two."""', (self.root / "b.py").read_text(encoding="utf-8"))

    def test_blocked_workplan_does_not_report_complete_on_continue(self) -> None:
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        first = self.make_agent(["I could not find a safe edit."])

        blocked = first.handle("Document each file in the codebase")

        self.assertIn("blocked", blocked.message.lower())
        self.assertIn("0/1 completed", blocked.message)
        second = self.make_agent(["unused"])
        result = second.handle("continue")
        self.assertIn("Work plan blocked", result.message)
        self.assertNotIn("Work plan complete", result.message)

    def test_class_test_workplan_can_create_test_file(self) -> None:
        (self.root / "widget.py").write_text("class WidgetRunner:\n    def run(self):\n        return 'ok'\n", encoding="utf-8")
        agent = self.make_agent(
            [
                """<tool_call>{"name":"create_file","arguments":{"path":"tests/test_widget.py","content":"from widget import WidgetRunner\\n\\ndef test_widget_runner_run():\\n    assert WidgetRunner().run() == 'ok'\\n"}}</tool_call>""",
                "Created focused tests for WidgetRunner.",
            ]
        )

        result = agent.handle("Create a test suite for class WidgetRunner")

        self.assertIn("1/1 completed", result.message)
        self.assertTrue((self.root / "tests" / "test_widget.py").exists())
        self.assertTrue(self.ledger.validation_state["passed"])

    def test_full_test_suite_workplan_expands_to_python_source_files(self) -> None:
        src = self.root / "src"
        src.mkdir()
        (src / "app.py").write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")
        agent = self.make_agent(
            [
                """<tool_call>{"name":"create_file","arguments":{"path":"tests/test_app.py","content":"from src.app import add\\n\\ndef test_add_returns_sum():\\n    assert add(2, 3) == 5\\n"}}</tool_call>""",
                "Created tests for src/app.py.",
            ]
        )

        result = agent.handle("Create a full suite of tests for app")

        self.assertIn("1/1 completed", result.message)
        self.assertTrue((self.root / "tests" / "test_app.py").exists())
        self.assertIn("create or improve tests for source file src/app.py", "\n".join(message.content for message in agent.llm.calls[0]))

    def test_chat_questions_are_grounded_in_project_context(self) -> None:
        (self.root / "README.md").write_text("# Widget Service\n\nProcesses widget invoices.\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text("[project]\nname = \"widget-service\"\n", encoding="utf-8")
        src = self.root / "src"
        src.mkdir()
        (src / "app.py").write_text("class WidgetRunner:\n    pass\n", encoding="utf-8")
        fake = FakeLLMClient(["This project processes widget invoices."])
        agent = CodeBuddyAgent(
            self.root,
            self.ledger,
            fake,
            self.edit,
            self.command,
            GitManager(self.root),
            Searcher(self.policy),
            ValidationHarness(self.root, self.command),
        )

        result = agent.handle("What does this project do?")
        first_call = "\n".join(message.content for message in fake.calls[0])

        self.assertEqual(result.mode, "chat")
        self.assertIn("Project context", first_call)
        self.assertIn("README.md", first_call)
        self.assertIn("Processes widget invoices.", first_call)
        self.assertIn("pyproject.toml", first_call)
        self.assertIn("src/app.py", first_call)
        self.assertIn("WidgetRunner", first_call)
        self.assertEqual(result.events[0].title, "Context")

    def test_llm_cannot_self_approve_dangerous_command(self) -> None:
        tool_call = '<tool_call>{"name":"run_command","arguments":{"command":"git reset --hard","approve":true}}</tool_call>'
        agent = self.make_agent([tool_call])

        with self.assertRaises(DeniedByPolicy):
            agent.handle("do dangerous thing")

        self.assertEqual(self.journal.entries(), [])

    def test_disabled_shell_tool_is_denied(self) -> None:
        tool_call = '<tool_call>{"name":"run_command","arguments":{"command":"git status --short"}}</tool_call>'
        agent = self.make_agent([tool_call], {"shell": False})

        with self.assertRaises(DeniedByPolicy):
            agent.handle("run status")

    def test_searcher_reads_and_searches_without_sensitive_files(self) -> None:
        (self.root / "app.py").write_text("needle = 1\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=needle\n", encoding="utf-8")
        searcher = Searcher(self.policy)

        self.assertIn("needle", searcher.read_text("app.py"))
        matches = searcher.search("needle")

        self.assertEqual(matches[0].path, "app.py")


if __name__ == "__main__":
    unittest.main()
