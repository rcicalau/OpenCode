from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.command_broker import CommandBroker, CommandPolicy
from codebuddy.edit_broker import EditBroker
from codebuddy.hashutil import sha256_bytes
from codebuddy.journal import Journal
from codebuddy.paths import PathPolicy
from codebuddy.search import Searcher
from codebuddy.session import SessionManager
from codebuddy.tool_calls import ParsedToolCall
from codebuddy.tool_runtime import ToolRuntime
from codebuddy.validation import ValidationHarness


class ToolResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        manager = SessionManager(self.root)
        self.ledger = manager.load_or_create()
        self.journal = Journal(manager.session_dir(self.ledger.session_id) / "journal.jsonl")
        self.policy = PathPolicy(self.root)
        self.command = CommandBroker(self.root, CommandPolicy(default_timeout_seconds=10), self.journal, self.ledger.session_id)
        self.runtime = ToolRuntime(
            self.root,
            self.ledger,
            EditBroker(self.policy, self.journal, self.ledger.session_id),
            self.command,
            Searcher(self.policy),
            ValidationHarness(self.root, self.command),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_read_returns_structured_retryable_result(self) -> None:
        results = self.runtime.run_structured([ParsedToolCall("read_text", {"path": "missing.py"})], [])

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].tool, "read_text")
        self.assertEqual(results[0].error_type, "file_not_found")
        self.assertTrue(results[0].retryable)
        self.assertEqual(results[0].next_action, "search_for_correct_path")
        self.assertIn('"ok": false', results[0].to_prompt())

    def test_rewrite_result_reports_changed_files_for_validation(self) -> None:
        target = self.root / "agent.py"
        target.write_text("def handle():\n    return 'old'\n", encoding="utf-8")

        results = self.runtime.run_structured(
            [
                ParsedToolCall(
                    "rewrite_file",
                    {
                        "path": "agent.py",
                        "content": "def handle():\n    return 'new'\n",
                        "expected_hash": sha256_bytes(target.read_bytes()),
                    },
                )
            ],
            [],
        )

        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].changed_files, ["agent.py"])
        self.assertEqual(results[0].metadata["diff_stat"], "+1/-1")
        self.assertIn('"changed_files": ["agent.py"]', results[0].to_prompt())


if __name__ == "__main__":
    unittest.main()
