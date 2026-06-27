from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.command_broker import CommandBroker, CommandPolicy
from codebuddy.errors import ConfirmationRequired, DeniedByPolicy
from codebuddy.validation import ValidationHarness


class ValidationHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.broker = CommandBroker(self.root, CommandPolicy(default_timeout_seconds=10))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_python_syntax_check_passes_and_fails(self) -> None:
        good = self.root / "good.py"
        bad = self.root / "bad.py"
        good.write_text("x = 1\n", encoding="utf-8")
        bad.write_text("def broken(:\n", encoding="utf-8")

        passed = ValidationHarness(self.root, self.broker).validate([good])
        failed = ValidationHarness(self.root, self.broker).validate([bad])

        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)
        self.assertIn("python syntax failed", failed.failures[0])

    def test_configured_validation_commands_run(self) -> None:
        result = ValidationHarness(self.root, self.broker, ["Get-ChildItem"]).validate()

        self.assertTrue(result.passed)
        self.assertEqual(result.command_results[0].exit_code, 0)

    def test_validation_does_not_auto_approve_mutating_or_denied_commands(self) -> None:
        result = ValidationHarness(self.root, self.broker, ["git add ."]).validate()
        self.assertFalse(result.passed)
        self.assertIn("validation command requires confirmation", result.failures[0])
        with self.assertRaises(DeniedByPolicy):
            ValidationHarness(self.root, self.broker, ["git reset --hard"]).validate()

    def test_validation_fails_when_git_worktree_has_unexpected_changes(self) -> None:
        subprocess.run(["git", "init"], cwd=self.root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        expected = self.root / "expected.py"
        unexpected = self.root / "unexpected.py"
        expected.write_text("value = 1\n", encoding="utf-8")
        unexpected.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        expected.write_text("value = 2\n", encoding="utf-8")
        unexpected.write_text("value = 2\n", encoding="utf-8")

        result = ValidationHarness(self.root, self.broker).validate([expected], expected_files=[expected])

        self.assertFalse(result.passed)
        self.assertEqual(result.unexpected_files, ["unexpected.py"])
        self.assertIn("unexpected files changed", result.failures[-1])


if __name__ == "__main__":
    unittest.main()
