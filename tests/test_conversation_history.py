from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.cli import main
from codebuddy.compaction import compact_ledger
from codebuddy.conversation import append_turn, conversation_path
from codebuddy.paths import PathPolicy
from codebuddy.project_context import build_project_context
from codebuddy.session import SessionManager


class ConversationHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.old_userprofile = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = str(self.home)

    def tearDown(self) -> None:
        if self.old_userprofile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = self.old_userprofile
        self.tmp.cleanup()

    def test_one_shot_prompt_writes_conversation_jsonl(self) -> None:
        os.environ["CODEBUDDY_FAKE_LLM_RESPONSE"] = "This repo is a widget service."
        try:
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["--root", str(self.root), "What", "does", "this", "project", "do?"]), 0)
        finally:
            os.environ.pop("CODEBUDDY_FAKE_LLM_RESPONSE", None)

        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        history_path = conversation_path(manager.session_dir(ledger.session_id))
        records = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(records[-1]["type"], "turn")
        self.assertEqual(records[-1]["user"], "What does this project do?")
        self.assertEqual(records[-1]["assistant"], "This repo is a widget service.")
        self.assertEqual(records[-1]["mode"], "chat")
        self.assertTrue(any(event["title"] == "Context" for event in records[-1]["events"]))

    def test_compact_includes_conversation_history(self) -> None:
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        session_dir = manager.session_dir(ledger.session_id)
        append_turn(
            session_dir,
            user="remember that tests use pytest",
            assistant="I will use pytest for future tests.",
            mode="chat",
            events=[],
            changed_files=[],
        )

        content = compact_ledger(ledger, session_dir / "compacted_state.md")

        self.assertIn("## Conversation History", content)
        self.assertIn("remember that tests use pytest", content)
        self.assertIn("I will use pytest for future tests.", content)

    def test_conversation_history_redacts_secret_like_values(self) -> None:
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        session_dir = manager.session_dir(ledger.session_id)

        append_turn(
            session_dir,
            user="my api_key=supersecretvalue",
            assistant="noted token=anothersecretvalue",
            mode="chat",
            events=[],
            changed_files=[],
        )

        text = conversation_path(session_dir).read_text(encoding="utf-8")
        self.assertIn("api_key=<redacted>", text)
        self.assertIn("token=<redacted>", text)
        self.assertNotIn("supersecretvalue", text)
        self.assertNotIn("anothersecretvalue", text)

    def test_project_context_includes_compacted_or_recent_conversation(self) -> None:
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        session_dir = manager.session_dir(ledger.session_id)
        append_turn(
            session_dir,
            user="prefer google docstrings",
            assistant="I will use Google style docstrings.",
            mode="chat",
            events=[],
            changed_files=[],
        )
        compact_ledger(ledger, session_dir / "compacted_state.md")

        context = build_project_context(self.root, PathPolicy(self.root), ledger)

        self.assertIn("Compacted conversation memory", context.text)
        self.assertIn("prefer google docstrings", context.text)


if __name__ == "__main__":
    unittest.main()
