from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.paths import PathPolicy
from codebuddy.workplan import WorkPlanManager


class WorkPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        self.manager = WorkPlanManager(self.root, "s1", PathPolicy(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_document_codebase_objective_expands_to_file_queue_and_persists(self) -> None:
        plan = self.manager.active_or_new("Document each file in the codebase")

        self.assertIsNotNone(plan)
        self.assertEqual([item.target_path for item in plan.items], ["a.py", "b.py"])
        self.assertTrue((self.root / ".buddy" / "workplans" / "current.json").exists())
        saved = json.loads((self.root / ".buddy" / "workplans" / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["kind"], "document_codebase")

    def test_workplan_persists_resumable_execution_contract(self) -> None:
        plan = self.manager.active_or_new("Document each file in the codebase")

        self.assertIsNotNone(plan)
        active = self.root / ".buddy" / "plans" / "active.json"
        self.assertTrue(active.exists())
        saved = json.loads(active.read_text(encoding="utf-8"))
        self.assertEqual(saved["objective"], "Document each file in the codebase")
        self.assertEqual(saved["progress"]["total"], 2)
        self.assertIn("Work stays inside the selected project root.", saved["assumptions"])
        self.assertIn("All planned work items are completed or explicitly blocked with a reason.", saved["done_criteria"])
        self.assertTrue(any("validation" in item.lower() for item in saved["validation_strategy"]))

    def test_class_test_objective_finds_target_class(self) -> None:
        (self.root / "widget.py").write_text("class WidgetRunner:\n    pass\n", encoding="utf-8")

        plan = self.manager.active_or_new("Create a test suite for class WidgetRunner")

        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, "test_class")
        self.assertEqual(plan.items[0].target_path, "widget.py")
        self.assertEqual(plan.items[0].symbol, "WidgetRunner")

    def test_single_file_documentation_objective_finds_target_file(self) -> None:
        plan = self.manager.active_or_new("Add google style documentation to a.py")

        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, "document_file")
        self.assertEqual([item.target_path for item in plan.items], ["a.py"])

    def test_same_objective_retries_blocked_workplan_item(self) -> None:
        plan = self.manager.active_or_new("Add google style documentation to a.py")
        self.assertIsNotNone(plan)
        plan.items[0].status = "blocked"
        plan.items[0].last_error = "tool JSON failed"
        self.manager.save(plan)

        resumed = self.manager.active_or_new("Add google style documentation to a.py")

        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.items[0].status, "pending")
        self.assertIsNone(resumed.items[0].last_error)

    def test_resume_returns_blocked_plan_and_retry_reopens_items(self) -> None:
        plan = self.manager.active_or_new("Document each file in the codebase")
        self.assertIsNotNone(plan)
        plan.items[0].status = "blocked"
        plan.items[0].last_error = "no expected file change detected"
        for item in plan.items[1:]:
            item.status = "completed"
        self.manager.save(plan)

        resumed = self.manager.active_or_new("continue")
        retried = self.manager.active_or_new("retry blocked")

        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.items[0].status, "blocked")
        self.assertIsNotNone(retried)
        self.assertEqual(retried.items[0].status, "pending")
        self.assertIsNone(retried.items[0].last_error)

    def test_unrelated_new_objective_shelves_unfinished_workplan(self) -> None:
        plan = self.manager.active_or_new("Document each file in the codebase")
        self.assertIsNotNone(plan)
        plan.items[0].status = "blocked"
        plan.items[0].last_error = "wrong edit strategy"
        self.manager.save(plan)

        new_plan = self.manager.active_or_new("Add google style documentation to b.py")

        self.assertIsNotNone(new_plan)
        self.assertEqual(new_plan.objective, "Add google style documentation to b.py")
        self.assertEqual(new_plan.items[0].target_path, "b.py")
        self.assertIsNotNone(self.manager.last_shelved)
        self.assertEqual(self.manager.last_shelved["objective"], "Document each file in the codebase")
        index = json.loads((self.root / ".buddy" / "workplans" / "shelved.json").read_text(encoding="utf-8"))
        self.assertEqual(index[0]["objective"], "Document each file in the codebase")
        saved = json.loads((self.root / ".buddy" / "workplans" / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["objective"], "Add google style documentation to b.py")

    def test_resume_shelved_rebuilds_plan_from_current_project_state(self) -> None:
        plan = self.manager.active_or_new("Document each file in the codebase")
        self.assertIsNotNone(plan)
        plan.items[0].status = "blocked"
        plan.items[0].last_error = "old failure"
        self.manager.save(plan)
        self.assertIsNotNone(self.manager.active_or_new("Add google style documentation to b.py"))
        (self.root / "c.py").write_text("def c():\n    return 3\n", encoding="utf-8")

        resumed = self.manager.active_or_new("resume shelved")

        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.objective, "Document each file in the codebase")
        self.assertEqual([item.target_path for item in resumed.items], ["a.py", "b.py", "c.py"])
        self.assertTrue(all(item.status == "pending" for item in resumed.items))
        self.assertIsNotNone(self.manager.last_resumed_shelved)
        self.assertEqual(self.manager.last_resumed_shelved["status"], "resumed")

    def test_resume_shelved_without_shelved_work_keeps_current_plan(self) -> None:
        plan = self.manager.active_or_new("Document each file in the codebase")
        self.assertIsNotNone(plan)

        resumed = self.manager.active_or_new("resume shelved")

        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.objective, "Document each file in the codebase")
        self.assertFalse((self.root / ".buddy" / "workplans" / "shelved.json").exists())

    def test_repo_gitignore_excludes_workplan_state(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")

        self.assertIn(".buddy/workplans/", gitignore)


if __name__ == "__main__":
    unittest.main()
