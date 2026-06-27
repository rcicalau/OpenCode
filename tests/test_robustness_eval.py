from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.agent import CodeBuddyAgent
from codebuddy.command_broker import CommandBroker, CommandPolicy
from codebuddy.edit_broker import EditBroker
from codebuddy.git_manager import GitManager
from codebuddy.journal import Journal
from codebuddy.llm import FakeLLMClient
from codebuddy.objective_state import BLOCKED
from codebuddy.paths import PathPolicy
from codebuddy.robustness_eval import run_agent_replay
from codebuddy.search import Searcher
from codebuddy.session import SessionManager
from codebuddy.validation import ValidationHarness


class RobustnessEvalTests(unittest.TestCase):
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

    def make_agent(self, responses: list[str]) -> CodeBuddyAgent:
        return CodeBuddyAgent(
            self.root,
            self.ledger,
            FakeLLMClient(responses),
            self.edit,
            self.command,
            GitManager(self.root),
            Searcher(self.policy),
            ValidationHarness(self.root, self.command),
        )

    def test_replay_harness_reports_no_crash_no_hang_for_bad_tool_output(self) -> None:
        agent = self.make_agent(
            [
                '<tool_call>{name:"read_text", arguments:{path:"missing.py"}}</tool_call>',
                "Recovered after missing file.",
            ]
        )

        outcome = run_agent_replay(agent, "/ask inspect likely test file", timeout_seconds=5)

        self.assertFalse(outcome.timed_out)
        self.assertFalse(outcome.crashed)
        self.assertIn("Recovered", outcome.message)
        self.assertTrue(outcome.clean_exit)

    def test_replay_harness_reports_clean_block_for_no_progress_loop(self) -> None:
        (self.root / "README.md").write_text("loop\n", encoding="utf-8")
        agent = self.make_agent(
            ['<tool_call>{"name":"read_text","arguments":{"path":"README.md"}}</tool_call>'] * 10
        )

        outcome = run_agent_replay(agent, "/ask read forever", timeout_seconds=5)

        self.assertFalse(outcome.timed_out)
        self.assertFalse(outcome.crashed)
        self.assertEqual(outcome.objective_state, BLOCKED)
        self.assertIn("no progress", outcome.message.lower())
        self.assertTrue(outcome.clean_exit)


if __name__ == "__main__":
    unittest.main()
