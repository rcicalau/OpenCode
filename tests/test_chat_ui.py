from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.chat_ui import SLASH_COMMANDS, ChatRenderer, build_prompt_key_bindings, help_message, read_prompt, welcome_message
from codebuddy.cli import main


class ChatUiTests(unittest.TestCase):
    def test_welcome_message_sets_context_and_commands(self) -> None:
        message = welcome_message(Path("C:/repo"), "s1", "perplexity", "sonar-pro")

        self.assertIn("Code Buddy", message)
        self.assertIn(f"Project: {Path('C:/repo')}", message)
        self.assertIn("Session: s1", message)
        self.assertIn("Model: perplexity/sonar-pro", message)
        self.assertIn("Shift+Enter", message)

    def test_help_message_lists_core_commands(self) -> None:
        message = help_message()

        self.assertIn("/clear", message)
        self.assertIn("/status", message)
        self.assertIn("/a", message)
        self.assertIn("/approve", message)
        self.assertIn("/undo-session", message)
        self.assertIn("/exit", message)
        self.assertIn("Shift+Enter", message)

    def test_slash_commands_include_short_approval_aliases_for_completion(self) -> None:
        self.assertIn("/a", SLASH_COMMANDS)
        self.assertIn("/approve", SLASH_COMMANDS)
        self.assertIn("/approve-branch", SLASH_COMMANDS)

    def test_read_prompt_single_line_sends_on_enter(self) -> None:
        values = iter(["hello"])

        prompt = read_prompt(lambda _prompt: next(values), lambda _text: None)

        self.assertEqual(prompt.text, "hello")
        self.assertFalse(prompt.exit_requested)

    def test_read_prompt_supports_paste_mode_until_send(self) -> None:
        values = iter(["/paste", "line one", "line two", "/send"])

        prompt = read_prompt(lambda _prompt: next(values), lambda _text: None)

        self.assertEqual(prompt.text, "line one\nline two")

    def test_read_prompt_supports_external_editor(self) -> None:
        values = iter(["/editor"])

        prompt = read_prompt(lambda _prompt: next(values), lambda _text: None, editor_func=lambda: "edited prompt\n")

        self.assertEqual(prompt.text, "edited prompt")

    def test_read_prompt_accepts_multiline_from_interactive_prompt(self) -> None:
        prompt = read_prompt(prompt_func=lambda _prompt: "line one\nline two")

        self.assertEqual(prompt.text, "line one\nline two")

    def test_read_prompt_backslash_continuation(self) -> None:
        values = iter(["line one\\", "line two"])

        prompt = read_prompt(lambda _prompt: next(values), lambda _text: None)

        self.assertEqual(prompt.text, "line one\nline two")

    def test_read_prompt_exit(self) -> None:
        values = iter(["/exit"])

        prompt = read_prompt(lambda _prompt: next(values), lambda _text: None)

        self.assertTrue(prompt.exit_requested)

    def test_cli_help_is_local_not_model_backed(self) -> None:
        self.assertEqual(main(["/help"]), 0)

    def test_prompt_key_bindings_are_constructible(self) -> None:
        bindings = build_prompt_key_bindings()

        self.assertGreaterEqual(len(bindings.bindings), 3)

    def test_renderer_prints_tool_events_without_crashing(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            ChatRenderer().events([SimpleNamespace(kind="edit", title="Edit", detail="app.py (+1/-1)", status="done")])

        self.assertIn("Edit", stdout.getvalue())

    def test_renderer_can_stream_assistant_chunks(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            collected = ChatRenderer().assistant_stream(["Hel", "lo"])

        self.assertEqual(collected, "Hello")
        self.assertIn("Hello", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
