from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.edit_broker import EditBroker
from codebuddy.errors import EditConflict, FileSafetyError, UndoError
from codebuddy.hashutil import sha256_bytes
from codebuddy.journal import Journal
from codebuddy.paths import PathPolicy


class EditBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.journal = Journal(self.root / "journal.jsonl")
        self.broker = EditBroker(PathPolicy(self.root), self.journal, "s1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_exact_replace_unique_preserves_utf8_bom_crlf_and_final_newline(self) -> None:
        path = self.root / "sample.py"
        path.write_bytes(b"\xef\xbb\xbfdef f():\r\n    return 1\r\n")

        result = self.broker.exact_replace("sample.py", "return 1", "return 2")

        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"return 1", raw)
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertNotEqual(result.before_hash, result.after_hash)
        actions = [entry.action for entry in self.journal.entries()]
        self.assertEqual(actions, ["edit_intent", "exact_replace"])

    def test_exact_replace_missing_and_duplicate_are_rejected(self) -> None:
        path = self.root / "sample.txt"
        path.write_text("a\nb\na\n", encoding="utf-8")

        with self.assertRaises(EditConflict):
            self.broker.exact_replace(path, "z", "x")
        with self.assertRaises(EditConflict):
            self.broker.exact_replace(path, "a", "x")

        self.assertEqual(path.read_text(encoding="utf-8"), "a\nb\na\n")

    def test_dry_run_exact_replace_returns_diff_without_writing_or_journaling(self) -> None:
        path = self.root / "sample.txt"
        path.write_text("alpha\nbeta\n", encoding="utf-8")

        preview = self.broker.dry_run_exact_replace("sample.txt", "beta", "BETA")

        self.assertTrue(preview.would_change)
        self.assertEqual(path.read_text(encoding="utf-8"), "alpha\nbeta\n")
        self.assertIn("-beta", preview.diff)
        self.assertIn("+BETA", preview.diff)
        self.assertEqual(self.journal.entries(), [])

    def test_apply_unified_diff_rejects_context_drift(self) -> None:
        path = self.root / "sample.txt"
        path.write_text("one\ntwo\nthree\n", encoding="utf-8")
        patch = """--- a/sample.txt
+++ b/sample.txt
@@ -1,3 +1,3 @@
 one
-TWO
+two changed
 three
"""

        with self.assertRaises(EditConflict):
            self.broker.apply_unified_diff(path, patch)

        self.assertEqual(path.read_text(encoding="utf-8"), "one\ntwo\nthree\n")

    def test_apply_unified_diff_preserves_unmodified_mixed_newlines(self) -> None:
        path = self.root / "mixed.txt"
        path.write_bytes(b"one\r\ntwo\nthree\r\n")
        patch = """--- a/mixed.txt
+++ b/mixed.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""

        self.broker.apply_unified_diff(path, patch)

        self.assertEqual(path.read_bytes(), b"one\r\nTWO\r\nthree\r\n")

    def test_apply_unified_diff_preserves_final_no_newline_replacement(self) -> None:
        path = self.root / "nofinal.txt"
        path.write_bytes(b"one\ntwo")
        patch = """--- a/nofinal.txt
+++ b/nofinal.txt
@@ -1,2 +1,2 @@
 one
-two
+TWO
"""

        self.broker.apply_unified_diff(path, patch)

        self.assertEqual(path.read_bytes(), b"one\nTWO")

    def test_expected_hash_rejects_stale_edit(self) -> None:
        path = self.root / "sample.txt"
        path.write_text("before\n", encoding="utf-8")
        stale = sha256_bytes(path.read_bytes())
        path.write_text("changed\n", encoding="utf-8")

        with self.assertRaises(EditConflict):
            self.broker.exact_replace(path, "changed", "new", expected_hash=stale)

    def test_binary_sensitive_outside_and_ads_paths_are_rejected(self) -> None:
        (self.root / "bin.dat").write_bytes(b"abc\x00def")
        (self.root / ".env").write_text("API_KEY=secretsecret\n", encoding="utf-8")
        outside = self.root.parent / "outside.txt"
        outside.write_text("x", encoding="utf-8")

        with self.assertRaises(FileSafetyError):
            self.broker.exact_replace("bin.dat", "abc", "xyz")
        with self.assertRaises(FileSafetyError):
            self.broker.exact_replace(".env", "secret", "public")
        with self.assertRaises(FileSafetyError):
            self.broker.create_file(outside, "x")
        with self.assertRaises(FileSafetyError):
            self.broker.create_file("safe.txt:stream", "x")

    def test_create_file_overwrite_requires_text_file_and_matching_hash(self) -> None:
        path = self.root / "existing.txt"
        path.write_text("old\n", encoding="utf-8")
        before_hash = sha256_bytes(path.read_bytes())
        binary = self.root / "existing.bin"
        binary.write_bytes(b"abc\x00def")

        with self.assertRaises(EditConflict):
            self.broker.create_file("existing.txt", "new\n", overwrite=True)
        with self.assertRaises(EditConflict):
            self.broker.create_file("existing.txt", "new\n", overwrite=True, expected_hash="bad")
        with self.assertRaises(FileSafetyError):
            self.broker.create_file("existing.bin", "new\n", overwrite=True, expected_hash=sha256_bytes(binary.read_bytes()))

        self.broker.create_file("existing.txt", "new\n", overwrite=True, expected_hash=before_hash)

        self.assertEqual(path.read_text(encoding="utf-8"), "new\n")

    def test_rewrite_file_requires_hash_rejects_noop_and_preserves_newline_style(self) -> None:
        path = self.root / "existing.txt"
        path.write_bytes(b"old\r\n")
        before_hash = sha256_bytes(path.read_bytes())

        with self.assertRaises(EditConflict):
            self.broker.rewrite_file("existing.txt", "new\n")
        with self.assertRaises(EditConflict):
            self.broker.rewrite_file("existing.txt", "old\n", expected_hash=before_hash)

        result = self.broker.rewrite_file("existing.txt", "new\n", expected_hash=before_hash)

        self.assertEqual(path.read_bytes(), b"new\r\n")
        self.assertNotEqual(result.before_hash, result.after_hash)
        self.assertEqual([entry.action for entry in self.journal.entries()], ["edit_intent", "rewrite_file"])

    def test_python_edits_reject_invalid_syntax_without_writing(self) -> None:
        path = self.root / "sample.py"
        original = "def f():\n    return 1\n"
        path.write_text(original, encoding="utf-8")

        with self.assertRaises(EditConflict):
            self.broker.exact_replace("sample.py", "return 1", "return (")

        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_python_file_creation_rejects_invalid_syntax_without_writing(self) -> None:
        with self.assertRaises(EditConflict):
            self.broker.create_file("broken.py", "def f(:\n    return 1\n")

        self.assertFalse((self.root / "broken.py").exists())

    def test_buddy_state_files_are_protected_from_edit_broker(self) -> None:
        target = self.root / ".buddy" / "sessions" / "current.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")

        with self.assertRaises(FileSafetyError):
            self.broker.exact_replace(target, "{}", '{"x": 1}')

    def test_buddy_project_skills_can_be_edited(self) -> None:
        target = self.root / ".buddy" / "skills" / "docs.md"
        target.parent.mkdir(parents=True)
        target.write_text("Prefer terse docs.\n", encoding="utf-8")

        self.broker.exact_replace(target, "terse", "tutorial")

        self.assertEqual(target.read_text(encoding="utf-8"), "Prefer tutorial docs.\n")

    def test_undo_restores_file_and_refuses_after_drift(self) -> None:
        path = self.root / "sample.txt"
        path.write_text("before\n", encoding="utf-8")
        self.broker.exact_replace(path, "before", "after")

        undone = self.journal.undo_last("s1")
        self.assertEqual(undone, path)
        self.assertEqual(path.read_text(encoding="utf-8"), "before\n")

        self.broker.exact_replace(path, "before", "after")
        path.write_text("someone else\n", encoding="utf-8")
        with self.assertRaises(UndoError):
            self.journal.undo_last("s1")

    def test_corrupt_journal_tail_does_not_prevent_undo_of_valid_entry(self) -> None:
        path = self.root / "sample.txt"
        path.write_text("before\n", encoding="utf-8")
        self.broker.exact_replace(path, "before", "after")
        with self.journal.path.open("a", encoding="utf-8") as handle:
            handle.write("{not valid json")

        self.journal.undo_last("s1")

        self.assertEqual(path.read_text(encoding="utf-8"), "before\n")

    def test_journal_redacts_secret_like_file_contents_and_skips_raw_undo(self) -> None:
        path = self.root / "config_sample.txt"
        secret = "supersecretvalue"
        path.write_text(f"API_KEY={secret}\nvalue=1\n", encoding="utf-8")

        self.broker.exact_replace(path, "value=1", "value=2")

        journal_text = self.journal.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, journal_text)
        self.assertIn("<redacted>", journal_text)
        entries = self.journal.entries()
        self.assertIsNone(entries[-1].undo)
        with self.assertRaises(UndoError):
            self.journal.undo_last("s1")


if __name__ == "__main__":
    unittest.main()
