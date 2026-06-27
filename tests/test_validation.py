from __future__ import annotations

import sys
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


if __name__ == "__main__":
    unittest.main()
