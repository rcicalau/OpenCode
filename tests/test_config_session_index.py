from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.compaction import compact_ledger
from codebuddy.config import load_config
from codebuddy.errors import SessionRootMismatch
from codebuddy.indexer import Indexer
from codebuddy.paths import PathPolicy, find_project_root, resolve_project_root
from codebuddy.project_context import bootstrap_project_memory
from codebuddy.project_scaffold import ensure_buddy_scaffold
from codebuddy.project_session import ProjectSession
from codebuddy.session import PlanItem, SessionManager


class ConfigSessionIndexTests(unittest.TestCase):
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

    def test_project_config_overrides_global_config(self) -> None:
        global_config = self.root / "global.toml"
        global_config.write_text("[commands]\ndefault_timeout_seconds = 5\n", encoding="utf-8")
        project_config = self.root / ".buddy" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text("[commands]\ndefault_timeout_seconds = 9\n", encoding="utf-8")

        loaded = load_config(self.root, global_config)

        self.assertEqual(loaded.config["commands"]["default_timeout_seconds"], 9)
        self.assertEqual(loaded.sources, [global_config, project_config])

    def test_azure_auth_openai_gpt54_is_deployment_default(self) -> None:
        loaded = load_config(self.root)

        self.assertEqual(loaded.config["model"]["roles"]["main"]["provider"], "azure_openai")
        self.assertEqual(loaded.config["model"]["roles"]["main"]["model"], "openai/gpt-5.4")
        provider = loaded.config["model"]["providers"]["azure_openai"]
        self.assertEqual(provider["base_url_import"], "ai_mart:base_url")
        self.assertNotIn("base_url_env", provider)
        self.assertEqual(provider["auth_client"], "azure_auth:AzureAuthClient")
        self.assertEqual(provider["token_method"], "get_token")
        self.assertFalse(provider["verify_ssl"])

    def test_perplexity_provider_default_uses_base_url_and_endpoint_path(self) -> None:
        loaded = load_config(self.root)
        provider = loaded.config["model"]["providers"]["perplexity"]

        self.assertEqual(provider["base_url"], "https://api.perplexity.ai")
        self.assertEqual(provider["endpoint_path"], "/chat/completions")
        self.assertEqual(provider["api_key_env"], "PERPLEXITY_API_KEY")

    def test_project_root_detection_honors_buddy_config(self) -> None:
        project = self.root / "project"
        project.mkdir()
        config = project / ".buddy" / "config.toml"
        config.parent.mkdir()
        config.write_text("# config\n", encoding="utf-8")

        self.assertEqual(find_project_root(project), project.resolve())

    def test_project_root_detection_does_not_capture_home_git_parent(self) -> None:
        project = self.home / "scratch-project"
        project.mkdir()
        (self.home / ".git").mkdir()

        self.assertEqual(find_project_root(project), project.resolve())

    def test_resolve_project_root_honors_explicit_and_environment(self) -> None:
        explicit = self.root / "explicit"
        env_root = self.root / "env-root"
        explicit.mkdir()
        env_root.mkdir()
        old_root = os.environ.get("CODEBUDDY_PROJECT_ROOT")
        os.environ["CODEBUDDY_PROJECT_ROOT"] = str(env_root)

        try:
            self.assertEqual(resolve_project_root(explicit), explicit.resolve())
            self.assertEqual(resolve_project_root(), env_root.resolve())
        finally:
            if old_root is None:
                os.environ.pop("CODEBUDDY_PROJECT_ROOT", None)
            else:
                os.environ["CODEBUDDY_PROJECT_ROOT"] = old_root

    def test_session_is_implicitly_restored(self) -> None:
        manager = SessionManager(self.root)
        first = manager.load_or_create()
        first.objective = "do work"
        manager.save(first)

        second = SessionManager(self.root).load_or_create()

        self.assertEqual(second.session_id, first.session_id)
        self.assertEqual(second.objective, "do work")

    def test_session_root_mismatch_is_blocked(self) -> None:
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        target.mkdir()
        ledger = SessionManager(source).load_or_create()
        ledger.objective = "source work"
        SessionManager(source).save(ledger)
        shutil.copytree(source / ".buddy", target / ".buddy")

        with self.assertRaises(SessionRootMismatch):
            SessionManager(target).load_or_create()

    def test_project_session_centralizes_root_ledger_and_journal(self) -> None:
        session = ProjectSession.open(self.root)

        self.assertEqual(session.root, self.root.resolve())
        self.assertEqual(Path(session.ledger.project_root), self.root.resolve())
        self.assertEqual(session.journal.path.parent, self.root / ".buddy" / "sessions" / session.ledger.session_id)

    def test_compaction_preserves_plan_and_working_set(self) -> None:
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        ledger.mode = "execute"
        ledger.objective = "add tests"
        ledger.plan = [PlanItem("find code", "completed"), PlanItem("write tests", "pending")]
        ledger.files_inspected.append("src/app.py")
        ledger.files_edited.append("tests/test_app.py")

        content = compact_ledger(ledger, self.root / ".buddy" / "sessions" / ledger.session_id / "compacted_state.md")

        self.assertIn("add tests", content)
        self.assertIn("[completed] find code", content)
        self.assertIn("tests/test_app.py", content)

    def test_compaction_respects_max_token_budget(self) -> None:
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        ledger.objective = "x" * 1000
        ledger.files_inspected.extend(f"src/file_{index}.py" for index in range(100))
        ledger.blockers.extend("very long blocker " + ("x" * 200) for _ in range(20))

        content = compact_ledger(
            ledger,
            self.root / ".buddy" / "sessions" / ledger.session_id / "compacted_state.md",
            max_tokens=120,
        )

        self.assertLessEqual(len(content), 120 * 4)
        self.assertIn("compacted state truncated to token budget", content)

    def test_indexer_records_files_and_python_symbols(self) -> None:
        source = self.root / "pkg.py"
        source.write_text("class A:\n    pass\n\ndef f():\n    return 1\n", encoding="utf-8")

        index = Indexer(self.root).build()

        self.assertEqual([file.path for file in index.files], ["pkg.py"])
        self.assertEqual({symbol.name for symbol in index.symbols}, {"A", "f"})
        stored = json.loads((self.root / ".buddy" / "index" / "symbols.json").read_text(encoding="utf-8"))
        self.assertEqual({item["name"] for item in stored}, {"A", "f"})

    def test_indexer_skips_sensitive_files(self) -> None:
        (self.root / ".env").write_text("API_KEY=secretsecret\n", encoding="utf-8")
        (self.root / "visible.py").write_text("x = 1\n", encoding="utf-8")

        index = Indexer(self.root).build()

        self.assertEqual([file.path for file in index.files], ["visible.py"])

    def test_bootstrap_project_memory_persists_map_and_resume_state(self) -> None:
        (self.root / "README.md").write_text("# Widget Service\n\nProcesses widget invoices.\n", encoding="utf-8")
        src = self.root / "src"
        src.mkdir()
        (src / "app.py").write_text("class WidgetRunner:\n    pass\n", encoding="utf-8")
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_app.py").write_text("def test_widget():\n    assert True\n", encoding="utf-8")
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        ledger.objective = "Add invoice tests"
        ledger.pending_next_step = "Write pytest coverage"
        ledger.plan = [PlanItem("Inspect invoice flow", "completed"), PlanItem("Write pytest coverage", "pending")]
        manager.save(ledger)

        context = bootstrap_project_memory(self.root, ledger)

        map_path = self.root / ".buddy" / "index" / "project_map.md"
        metadata_path = self.root / ".buddy" / "index" / "project_memory.json"
        modules_path = self.root / ".buddy" / "index" / "module_summaries.json"
        self.assertTrue(map_path.exists())
        self.assertTrue(metadata_path.exists())
        self.assertTrue(modules_path.exists())
        saved_map = map_path.read_text(encoding="utf-8")
        saved_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        saved_modules = json.loads(modules_path.read_text(encoding="utf-8"))
        self.assertIn("Processes widget invoices.", saved_map)
        self.assertIn("src/app.py", saved_map)
        self.assertIn("WidgetRunner", saved_map)
        self.assertIn("Module summaries", saved_map)
        self.assertTrue({"src", "tests"}.issubset({item["module"] for item in saved_modules}))
        self.assertIn("Add invoice tests", saved_map)
        self.assertIn("Write pytest coverage", saved_map)
        self.assertEqual(saved_metadata["active_session_id"], ledger.session_id)
        self.assertEqual(saved_metadata["objective"], "Add invoice tests")
        self.assertIn("Project context", context.text)

    def test_buddy_scaffold_is_created_without_overwriting_existing_instructions(self) -> None:
        buddy = self.root / "BUDDY.md"
        buddy.write_text("# Existing Buddy Rules\n\nKeep this.\n", encoding="utf-8")

        ensure_buddy_scaffold(self.root)

        self.assertEqual(buddy.read_text(encoding="utf-8"), "# Existing Buddy Rules\n\nKeep this.\n")
        self.assertTrue((self.root / ".buddy" / "skills").is_dir())
        self.assertTrue((self.root / ".buddy" / "templates").is_dir())
        self.assertTrue((self.root / ".buddy" / "validators").is_dir())
        self.assertTrue((self.root / ".buddy" / "tools").is_dir())
        self.assertTrue((self.root / ".buddy" / "steering").is_dir())
        self.assertTrue((self.root / ".buddy" / "skills" / "reasoning.md").exists())
        self.assertTrue((self.root / ".buddy" / "skills" / "development.md").exists())
        self.assertTrue((self.root / ".buddy" / "skills" / "testing.md").exists())
        self.assertTrue((self.root / ".buddy" / "skills" / "debugging.md").exists())
        self.assertTrue((self.root / ".buddy" / "skills" / "documentation.md").exists())
        self.assertTrue((self.root / ".buddy" / "skills" / "test-writing.md").exists())
        self.assertTrue((self.root / ".buddy" / "skills" / "coding-standards.md").exists())

    def test_default_config_has_long_task_workplan_limits(self) -> None:
        loaded = load_config(self.root)

        self.assertEqual(loaded.config["agent"]["max_tool_iterations"], 200)
        self.assertEqual(loaded.config["agent"]["max_work_items_per_prompt"], 200)
        self.assertEqual(loaded.config["agent"]["max_item_attempts"], 3)

    def test_project_context_includes_buddy_md_and_skills(self) -> None:
        ensure_buddy_scaffold(self.root)
        (self.root / "BUDDY.md").write_text("# Buddy Rules\n\nPrefer pytest.\n", encoding="utf-8")
        skill = self.root / ".buddy" / "skills" / "docs.md"
        skill.write_text("# Docs Skill\n\nUse tutorial docstrings.\n", encoding="utf-8")
        ledger = SessionManager(self.root).load_or_create()

        context = bootstrap_project_memory(self.root, ledger, PathPolicy(self.root), max_chars=20000)

        self.assertIn("BUDDY.md", context.key_files)
        self.assertIn("Prefer pytest.", context.text)
        self.assertIn("Project skills:", context.text)
        self.assertIn("Use tutorial docstrings.", context.text)


if __name__ == "__main__":
    unittest.main()
