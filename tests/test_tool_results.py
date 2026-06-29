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

    def test_missing_read_auto_recovers_when_filename_has_single_project_match(self) -> None:
        (self.root / "README.md").write_text("auto recovered\n", encoding="utf-8")

        results = self.runtime.run_structured([ParsedToolCall("read_text", {"path": "docs/README.md"})], [])

        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].metadata["path"], "README.md")
        self.assertEqual(results[0].metadata["requested_path"], "docs/README.md")
        self.assertTrue(results[0].metadata["recovered"])
        self.assertIn("auto recovered", results[0].content)

    def test_explore_project_returns_repo_overview_without_sensitive_files(self) -> None:
        (self.root / "README.md").write_text("# Widget Service\n\nProcesses widget invoices.\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text("[project]\nname = \"widget-service\"\n", encoding="utf-8")
        src = self.root / "src"
        src.mkdir()
        (src / "app.py").write_text(
            "from fastapi import FastAPI\n\napp = FastAPI()\n\nclass WidgetRunner:\n    pass\n",
            encoding="utf-8",
        )
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_app.py").write_text("def test_widget():\n    assert True\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=widget-invoices\n", encoding="utf-8")

        results = self.runtime.run_structured([ParsedToolCall("explore_project", {"focus": "widget invoices"})], [])

        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].tool, "explore_project")
        self.assertIn("Project exploration", results[0].content)
        self.assertIn("README.md", results[0].content)
        self.assertIn("pyproject.toml", results[0].content)
        self.assertIn("src/app.py", results[0].content)
        self.assertIn("tests/test_app.py", results[0].content)
        self.assertIn("WidgetRunner", results[0].content)
        self.assertIn("FastAPI", results[0].content)
        self.assertNotIn(".env", results[0].content)
        self.assertNotIn("SECRET", results[0].content)
        self.assertGreaterEqual(results[0].metadata["files_scanned"], 4)

    def test_invalid_tool_arguments_return_structured_schema_error(self) -> None:
        results = self.runtime.run_structured([ParsedToolCall("read_text", {})], [])

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].tool, "read_text")
        self.assertEqual(results[0].error_type, "invalid_arguments")
        self.assertFalse(results[0].retryable)
        self.assertEqual(results[0].next_action, "repair_tool_arguments")
        self.assertIn("missing required argument: path", results[0].content)

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

    def test_stale_hash_edit_auto_returns_current_file_snapshot_without_writing(self) -> None:
        target = self.root / "agent.py"
        target.write_text("def handle():\n    return 'current'\n", encoding="utf-8")

        results = self.runtime.run_structured(
            [
                ParsedToolCall(
                    "rewrite_file",
                    {
                        "path": "agent.py",
                        "content": "def handle():\n    return 'new'\n",
                        "expected_hash": "stale",
                    },
                )
            ],
            [],
        )

        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].error_type, "stale_hash")
        self.assertEqual(results[0].metadata["current_sha256"], sha256_bytes(target.read_bytes()))
        self.assertIn("Current file snapshot", results[0].content)
        self.assertIn("return 'current'", results[0].content)
        self.assertEqual(target.read_text(encoding="utf-8"), "def handle():\n    return 'current'\n")

    def test_search_shell_validate_and_malformed_calls_are_native_structured_results(self) -> None:
        (self.root / "README.md").write_text("needle\n", encoding="utf-8")
        bad = self.root / "bad.py"
        bad.write_text("def broken(:\n", encoding="utf-8")
        self.ledger.files_edited.append("bad.py")

        search, shell, validation, malformed = self.runtime.run_structured(
            [
                ParsedToolCall("search", {"pattern": "needle"}),
                ParsedToolCall("run_command", {"command": "Get-ChildItem"}),
                ParsedToolCall("validate", {}),
                ParsedToolCall("__malformed_tool_call__", {"name": "edit_exact_replace", "error": "bad json"}),
            ],
            [],
        )

        self.assertTrue(search.ok)
        self.assertEqual(search.tool, "search")
        self.assertEqual(search.metadata["matches"], 1)
        self.assertTrue(shell.ok)
        self.assertEqual(shell.metadata["exit_code"], 0)
        self.assertFalse(validation.ok)
        self.assertEqual(validation.error_type, "validation_failed")
        self.assertFalse(malformed.ok)
        self.assertEqual(malformed.error_type, "malformed_tool_call")


if __name__ == "__main__":
    unittest.main()
