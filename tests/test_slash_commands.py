from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.command_broker import CommandBroker
from codebuddy.edit_broker import EditBroker
from codebuddy.git_manager import GitManager
from codebuddy.journal import Journal
from codebuddy.paths import PathPolicy
from codebuddy.session import SessionManager
from codebuddy.slash import SlashCommandHandler
from codebuddy.workplan import WorkPlanManager


def init_repo_with_commit(root: Path, files: dict[str, str]) -> None:
    init = subprocess.run(["git", "init", "-b", "main"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if init.returncode != 0:
        subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True)
    config = root / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8") + "\n[user]\n\temail = test@example.com\n\tname = Test\n",
        encoding="utf-8",
    )
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", *files.keys()], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class SlashCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = SessionManager(self.root)
        self.ledger = self.manager.load_or_create()
        self.journal = Journal(self.manager.session_dir(self.ledger.session_id) / "journal.jsonl")
        self.command = CommandBroker(self.root, journal=self.journal, session_id=self.ledger.session_id)
        self.git = GitManager(self.root, command_broker=self.command)
        self.handler = SlashCommandHandler(self.root, self.ledger, self.manager, self.journal, self.git)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_status_clear_compact_and_yolo(self) -> None:
        self.ledger.mode = "execute"
        self.ledger.objective = "work"

        self.assertIn('"objective": "work"', self.handler.handle("/status").message)
        self.assertIn("Compacted Session State", self.handler.handle("/compact").message)
        self.assertIn("yolo: on", self.handler.handle("/yolo").message)
        self.assertIn("cleared", self.handler.handle("/clear").message)
        self.assertIsNone(self.ledger.objective)

    def test_approve_branch_sets_one_shot_dirty_branch_approval(self) -> None:
        self.ledger.objective = "update docs"
        self.ledger.pending_next_step = "approve dirty branch before execution"

        result = self.handler.handle("/a")

        self.assertIn("approved dirty worktree", result.message)
        self.assertTrue(self.ledger.approvals["dirty_branch"])
        self.assertIn("update docs", result.message)
        self.assertEqual(result.followup_prompt, "update docs")

    def test_approve_without_pending_request_does_not_create_new_approval(self) -> None:
        result = self.handler.handle("/a")

        self.assertIn("no pending approval", result.message)
        self.assertNotIn("dirty_branch", self.ledger.approvals)

    def test_pending_branch_approval_accepts_yes_shortcut(self) -> None:
        self.ledger.objective = "update docs"
        self.ledger.pending_next_step = "approve dirty branch before execution"

        result = self.handler.handle("y")

        self.assertTrue(result.handled)
        self.assertEqual(result.followup_prompt, "update docs")

    def test_pending_command_approval_runs_command_with_shortcut(self) -> None:
        self.ledger.objective = "run writer"
        self.ledger.pending_next_step = "approve command before execution"
        self.ledger.approvals["pending_command"] = "Write-Output hi > approved.txt"

        result = self.handler.handle("/a")

        self.assertIn("approved command", result.message)
        self.assertTrue((self.root / "approved.txt").exists())
        self.assertEqual(result.followup_prompt, "run writer")
        self.assertIsNone(self.ledger.pending_next_step)
        self.assertNotIn("pending_command", self.ledger.approvals)

        second = self.handler.handle("/a")

        self.assertIn("no pending approval", second.message)

    def test_pending_command_approval_accepts_yes_shortcut(self) -> None:
        self.ledger.objective = "run writer"
        self.ledger.pending_next_step = "approve command before execution"
        self.ledger.approvals["pending_command"] = "Write-Output hi > approved-yes.txt"

        result = self.handler.handle("y")

        self.assertIn("approved command", result.message)
        self.assertTrue((self.root / "approved-yes.txt").exists())
        self.assertEqual(result.followup_prompt, "run writer")
        self.assertIsNone(self.ledger.pending_next_step)

    def test_pending_command_approval_runs_in_recorded_project_subdirectory(self) -> None:
        subdir = self.root / "src"
        subdir.mkdir()
        self.ledger.pending_next_step = "approve command before execution"
        self.ledger.approvals["pending_command"] = "Write-Output hi > approved.txt"
        self.ledger.approvals["pending_command_cwd"] = str(subdir)

        result = self.handler.handle("/a")

        self.assertIn("approved command", result.message)
        self.assertTrue((subdir / "approved.txt").exists())
        self.assertFalse((self.root / "approved.txt").exists())
        self.assertNotIn("pending_command_cwd", self.ledger.approvals)

    def test_status_includes_active_workplan(self) -> None:
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        workplans = WorkPlanManager(self.root, self.ledger.session_id, PathPolicy(self.root))
        plan = workplans.plan_for_objective("Document each file in the codebase")
        self.assertIsNotNone(plan)
        workplans.save(plan)

        message = self.handler.handle("/status").message

        self.assertIn('"workplan": {', message)
        self.assertIn('"kind": "document_codebase"', message)
        self.assertIn('"target_path": "a.py"', message)

    def test_undo_session_reverts_reversible_edits(self) -> None:
        broker = EditBroker(PathPolicy(self.root), self.journal, self.ledger.session_id)
        path = self.root / "a.txt"
        path.write_text("one\n", encoding="utf-8")
        broker.exact_replace(path, "one", "two")

        message = self.handler.handle("/undo-session").message

        self.assertIn("undone session", message)
        self.assertEqual(path.read_text(encoding="utf-8"), "one\n")

    def test_git_slash_commands_and_commit_agent_paths(self) -> None:
        init_repo_with_commit(self.root, {"agent.txt": "base\n"})
        self.git.ensure_agent_branch("work")
        (self.root / "agent.txt").write_text("changed\n", encoding="utf-8")
        self.ledger.files_edited = ["agent.txt"]

        self.assertIn("codebuddy/", self.handler.handle("/branch").message)
        self.assertIn("changed", self.handler.handle("/diff").message)
        self.assertIn("committed", self.handler.handle("/commit test commit").message)


if __name__ == "__main__":
    unittest.main()
