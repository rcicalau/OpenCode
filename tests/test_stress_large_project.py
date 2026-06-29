from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.compaction import compact_ledger
from codebuddy.explorer import explore_project
from codebuddy.indexer import Indexer
from codebuddy.paths import PathPolicy
from codebuddy.project_context import build_project_context
from codebuddy.search import Searcher
from codebuddy.session import PlanItem, SessionManager
from codebuddy.workplan import WorkPlanManager


class LargeProjectStressTests(unittest.TestCase):
    def test_index_search_and_compact_larger_project_without_sensitive_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "pkg"
            package.mkdir()
            for i in range(160):
                (package / f"module_{i}.py").write_text(
                    f"class Thing{i}:\n    def value(self):\n        return {i}\n\nTARGET_{i} = {i}\n",
                    encoding="utf-8",
                )
            (root / ".env").write_text("API_KEY=supersecretvalue\nTARGET_SECRET=1\n", encoding="utf-8")
            policy = PathPolicy(root)

            index = Indexer(root, policy).build()
            matches = Searcher(policy).search("TARGET_159")
            manager = SessionManager(root)
            ledger = manager.load_or_create()
            ledger.objective = "large project stress"
            ledger.plan = [PlanItem("index project", "completed"), PlanItem("search target", "completed")]
            ledger.files_inspected = [match.path for match in matches]
            compacted = compact_ledger(ledger, manager.session_dir(ledger.session_id) / "compacted_state.md")

            self.assertEqual(len(index.files), 160)
            self.assertTrue(any(symbol.name == "Thing159" for symbol in index.symbols))
            self.assertEqual(matches[0].path, "pkg/module_159.py")
            self.assertNotIn(".env", [record.path for record in index.files])
            self.assertIn("large project stress", compacted)

    def test_large_files_are_indexed_without_polluting_context_or_workplan_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            huge = root / "huge.py"
            huge.write_text("class Huge:\n    pass\n" + ("# filler\n" * 300000), encoding="utf-8")
            small = root / "small.py"
            small.write_text("def small():\n    return 1\n", encoding="utf-8")
            policy = PathPolicy(root)
            manager = SessionManager(root)
            ledger = manager.load_or_create()

            index = Indexer(root, policy).build()
            context = build_project_context(root, policy, ledger)
            workplan = WorkPlanManager(root, ledger.session_id, policy).active_or_new("Document each file in the codebase")
            read = Searcher(policy).read_text("huge.py", max_chars=1000)

            self.assertIn("huge.py", [record.path for record in index.files])
            self.assertFalse(any(symbol.path == "huge.py" for symbol in index.symbols))
            self.assertLess(len(context.text), 12050)
            self.assertIsNotNone(workplan)
            self.assertIn("huge.py", [item.target_path for item in workplan.items])
            self.assertIn("[truncated]", read)

    def test_explore_project_scales_without_sensitive_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "pkg"
            package.mkdir()
            for index in range(220):
                (package / f"module_{index}.py").write_text(
                    f"import pytest\n\nclass Thing{index}:\n    def value(self):\n        return {index}\n\nTARGET_{index} = {index}\n",
                    encoding="utf-8",
                )
            (root / "README.md").write_text("# Large App\n\nTARGET_219 handles the final case.\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=TARGET_219\n", encoding="utf-8")
            policy = PathPolicy(root)

            exploration = explore_project(root, policy, focus="TARGET_219 final case", max_files=300, max_symbols=80)

            self.assertEqual(exploration.files_scanned, 221)
            self.assertIn("Project exploration", exploration.text)
            self.assertIn("Thing219", exploration.text)
            self.assertIn("pytest", exploration.text)
            self.assertNotIn(".env", exploration.text)
            self.assertNotIn("SECRET", exploration.text)
            self.assertLess(len(exploration.text), 60000)


if __name__ == "__main__":
    unittest.main()
