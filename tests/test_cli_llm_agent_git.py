from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.agent import AgentResult, CodeBuddyAgent, route_intent
from codebuddy.auth import auth_check, auth_set, auth_status
from codebuddy.cli import build_brokers, main, maybe_prompt_resume, prompt_project_root
from codebuddy.command_broker import CommandBroker, CommandResult, Risk
from codebuddy.edit_broker import EditBroker
from codebuddy.errors import ConfirmationRequired
from codebuddy.git_manager import GitManager
from codebuddy.global_state import set_last_project_root
from codebuddy.journal import Journal
from codebuddy.llm import FakeLLMClient
from codebuddy.paths import PathPolicy, resolve_project_root
from codebuddy.project_scaffold import ensure_buddy_scaffold
from codebuddy.project_session import ProjectSession
from codebuddy.session import SessionManager
from codebuddy.slash import SlashCommandHandler
from codebuddy.workplan import WorkPlanManager


def init_empty_repo(root: Path) -> None:
    init = subprocess.run(["git", "init", "-b", "main"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if init.returncode != 0:
        subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True)


def init_repo_with_commit(root: Path, files: dict[str, str]) -> None:
    init_empty_repo(root)
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


class CliLlmAgentGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.old_userprofile = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = str(self.home)
        self.old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        if self.old_userprofile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = self.old_userprofile
        self.tmp.cleanup()

    def test_route_intent_distinguishes_chat_scope_execute(self) -> None:
        self.assertEqual(route_intent("What does this do?"), "chat")
        self.assertEqual(route_intent("Explore options, do not write code yet"), "scope")
        self.assertEqual(route_intent("Add tests for parser"), "execute")

    def test_cli_version_config_and_status(self) -> None:
        self.assertEqual(main(["--version"]), 0)
        self.assertEqual(main(["config", "validate"]), 0)
        self.assertEqual(main(["status"]), 0)

    def test_no_args_from_tty_defaults_to_chat(self) -> None:
        import codebuddy.cli as cli_module

        original_stdin = sys.stdin
        original_configure = cli_module.maybe_configure_project_provider
        original_prompt_auth = cli_module.maybe_prompt_for_auth
        original_chat_loop = cli_module.chat_loop
        called = []

        class TtyStdin:
            def isatty(self):
                return True

        def fake_chat_loop(root, ledger, config, journal, startup_context, startup_prompt=None):
            called.append((root, ledger.session_id, startup_context, startup_prompt))
            return 0

        sys.stdin = TtyStdin()
        cli_module.maybe_configure_project_provider = lambda _root, _config: None
        cli_module.maybe_prompt_for_auth = lambda _config: None
        cli_module.chat_loop = fake_chat_loop
        try:
            self.assertEqual(main([]), 0)
        finally:
            sys.stdin = original_stdin
            cli_module.maybe_configure_project_provider = original_configure
            cli_module.maybe_prompt_for_auth = original_prompt_auth
            cli_module.chat_loop = original_chat_loop

        self.assertEqual(called[0][0], self.root.resolve())
        self.assertIsNone(called[0][3])
        self.assertTrue((self.root / ".buddy" / "sessions" / "current.json").exists())

    def test_chat_startup_yes_passes_resume_prompt_to_chat_loop(self) -> None:
        import codebuddy.cli as cli_module

        session = ProjectSession.open(self.root)
        session.ledger.objective = "finish pending docs"
        session.ledger.pending_next_step = "document src/app.py"
        session.manager.save(session.ledger)
        original_stdin = sys.stdin
        original_configure = cli_module.maybe_configure_project_provider
        original_prompt_auth = cli_module.maybe_prompt_for_auth
        original_chat_loop = cli_module.chat_loop
        called = []

        class TtyStdin:
            def isatty(self):
                return True

            def readline(self):
                return "y\n"

        def fake_chat_loop(root, ledger, config, journal, startup_context, startup_prompt=None):
            called.append((ledger.session_id, startup_prompt))
            return 0

        sys.stdin = TtyStdin()
        cli_module.maybe_configure_project_provider = lambda _root, _config: None
        cli_module.maybe_prompt_for_auth = lambda _config: None
        cli_module.chat_loop = fake_chat_loop
        try:
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["chat"]), 0)
        finally:
            sys.stdin = original_stdin
            cli_module.maybe_configure_project_provider = original_configure
            cli_module.maybe_prompt_for_auth = original_prompt_auth
            cli_module.chat_loop = original_chat_loop

        self.assertEqual(called, [(session.ledger.session_id, "finish pending docs")])

    def test_ctrl_c_exits_cleanly_without_traceback(self) -> None:
        import codebuddy.cli as cli_module

        original = cli_module._main

        def raise_keyboard_interrupt(_argv):
            raise KeyboardInterrupt

        cli_module._main = raise_keyboard_interrupt
        stdout = StringIO()
        try:
            with redirect_stdout(stdout):
                code = main(["chat"])
        finally:
            cli_module._main = original

        self.assertEqual(code, 130)
        self.assertIn("Interrupted.", stdout.getvalue())

    def test_eof_exits_cleanly_without_traceback(self) -> None:
        import codebuddy.cli as cli_module

        original = cli_module._main

        def raise_eof(_argv):
            raise EOFError

        cli_module._main = raise_eof
        try:
            self.assertEqual(main(["chat"]), 0)
        finally:
            cli_module._main = original

    def test_root_option_binds_state_to_that_project(self) -> None:
        project = Path(self.tmp.name) / "bound-project"
        project.mkdir()

        self.assertEqual(main(["--root", str(project), "status"]), 0)

        self.assertTrue((project / ".buddy" / "sessions" / "current.json").exists())
        self.assertFalse((self.root / ".buddy" / "sessions" / "current.json").exists())

    def test_prompt_bootstraps_project_memory_for_future_launches(self) -> None:
        project = Path(self.tmp.name) / "mapped-project"
        project.mkdir()
        (project / "README.md").write_text("# Mapped Project\n\nDoes useful work.\n", encoding="utf-8")
        os.environ["CODEBUDDY_FAKE_LLM_RESPONSE"] = "It does useful work."
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["--root", str(project), "What", "does", "this", "project", "do?"]), 0)
        finally:
            os.environ.pop("CODEBUDDY_FAKE_LLM_RESPONSE", None)

        project_map = project / ".buddy" / "index" / "project_map.md"
        self.assertTrue(project_map.exists())
        self.assertIn("Mapped Project", project_map.read_text(encoding="utf-8"))

    def test_project_root_env_does_not_override_terminal_cwd(self) -> None:
        stale = Path(self.tmp.name) / "env-project"
        stale.mkdir()
        old = os.environ.get("CODEBUDDY_PROJECT_ROOT")
        os.environ["CODEBUDDY_PROJECT_ROOT"] = str(stale)
        stdout = StringIO()
        try:
            with redirect_stdout(stdout):
                self.assertEqual(main(["status"]), 0)
        finally:
            if old is None:
                os.environ.pop("CODEBUDDY_PROJECT_ROOT", None)
            else:
                os.environ["CODEBUDDY_PROJECT_ROOT"] = old

        payload = json.loads(stdout.getvalue())
        self.assertEqual(Path(payload["project_root"]), self.root.resolve())
        self.assertTrue((self.root / ".buddy" / "sessions" / "current.json").exists())
        self.assertFalse((stale / ".buddy").exists())

    def test_start_dir_env_matching_cwd_is_harmless(self) -> None:
        caller_project = Path(self.tmp.name) / "caller-project"
        caller_project.mkdir()
        (caller_project / "pyproject.toml").write_text("[project]\nname='target'\n", encoding="utf-8")
        old = os.environ.get("CODEBUDDY_START_DIR")
        os.environ["CODEBUDDY_START_DIR"] = str(caller_project)
        try:
            os.chdir(caller_project)
            resolved = resolve_project_root()
        finally:
            if old is None:
                os.environ.pop("CODEBUDDY_START_DIR", None)
            else:
                os.environ["CODEBUDDY_START_DIR"] = old

        self.assertEqual(resolved, caller_project.resolve())

    def test_stale_start_dir_env_does_not_override_terminal_cwd(self) -> None:
        stale = Path(self.tmp.name) / "stale-buddy-root"
        caller_project = Path(self.tmp.name) / "new-terminal-project"
        stale.mkdir()
        caller_project.mkdir()
        (stale / "BUDDY.md").write_text("# Old Buddy Project\n", encoding="utf-8")
        old = os.environ.get("CODEBUDDY_START_DIR")
        os.environ["CODEBUDDY_START_DIR"] = str(stale)
        stdout = StringIO()

        try:
            os.chdir(caller_project)
            with redirect_stdout(stdout):
                self.assertEqual(main(["status"]), 0)
        finally:
            if old is None:
                os.environ.pop("CODEBUDDY_START_DIR", None)
            else:
                os.environ["CODEBUDDY_START_DIR"] = old

        payload = json.loads(stdout.getvalue())
        self.assertEqual(Path(payload["project_root"]), caller_project.resolve())
        self.assertTrue((caller_project / ".buddy" / "sessions" / "current.json").exists())
        self.assertFalse((stale / ".buddy").exists())

    def test_last_project_root_does_not_override_terminal_cwd(self) -> None:
        stale = Path(self.tmp.name) / "last-project"
        caller_project = Path(self.tmp.name) / "fresh-project"
        stale.mkdir()
        caller_project.mkdir()
        set_last_project_root(stale)
        stdout = StringIO()

        os.chdir(caller_project)
        with redirect_stdout(stdout):
            self.assertEqual(main(["status"]), 0)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(Path(payload["project_root"]), caller_project.resolve())
        self.assertTrue((caller_project / ".buddy" / "sessions" / "current.json").exists())
        self.assertFalse((stale / ".buddy").exists())

    def test_start_dir_binds_exact_launch_folder_even_under_parent_repo(self) -> None:
        parent = Path(self.tmp.name) / "parent-repo"
        caller_project = parent / "project-1"
        caller_project.mkdir(parents=True)
        (parent / ".git").mkdir()
        old = os.environ.get("CODEBUDDY_START_DIR")
        os.environ["CODEBUDDY_START_DIR"] = str(caller_project)
        stdout = StringIO()

        try:
            os.chdir(caller_project)
            with redirect_stdout(stdout):
                self.assertEqual(main(["status"]), 0)
        finally:
            if old is None:
                os.environ.pop("CODEBUDDY_START_DIR", None)
            else:
                os.environ["CODEBUDDY_START_DIR"] = old

        payload = json.loads(stdout.getvalue())
        self.assertEqual(Path(payload["project_root"]), caller_project.resolve())
        self.assertTrue((caller_project / ".buddy" / "sessions" / "current.json").exists())
        self.assertFalse((parent / ".buddy").exists())

    def test_start_dir_does_not_capture_parent_buddy_project(self) -> None:
        parent = Path(self.tmp.name) / "code"
        caller_project = parent / "project-x"
        caller_project.mkdir(parents=True)
        (parent / "BUDDY.md").write_text("# Parent Buddy Project\n", encoding="utf-8")
        (parent / ".buddy").mkdir()
        (parent / ".buddy" / "config.toml").write_text("# parent config\n", encoding="utf-8")
        old = os.environ.get("CODEBUDDY_START_DIR")
        os.environ["CODEBUDDY_START_DIR"] = str(caller_project)
        stdout = StringIO()

        try:
            os.chdir(caller_project)
            with redirect_stdout(stdout):
                self.assertEqual(main(["status"]), 0)
        finally:
            if old is None:
                os.environ.pop("CODEBUDDY_START_DIR", None)
            else:
                os.environ["CODEBUDDY_START_DIR"] = old

        payload = json.loads(stdout.getvalue())
        self.assertEqual(Path(payload["project_root"]), caller_project.resolve())
        self.assertTrue((caller_project / ".buddy" / "sessions" / "current.json").exists())
        self.assertFalse((parent / ".buddy" / "sessions" / "current.json").exists())

    def test_prompt_project_root_uses_native_picker_selection(self) -> None:
        selected = Path(self.tmp.name) / "picked-project"
        stdout = StringIO()

        with redirect_stdout(stdout):
            result = prompt_project_root(self.root, picker=lambda _initial: selected)

        self.assertEqual(result, selected.resolve())
        self.assertTrue(selected.exists())
        self.assertIn("folder picker", stdout.getvalue())

    def test_prompt_project_root_initial_dir_is_launch_default_not_last_project(self) -> None:
        last_project = Path(self.tmp.name) / "old-last-project"
        last_project.mkdir()
        set_last_project_root(last_project)
        seen_initial = []

        def picker(initial):
            seen_initial.append(Path(initial).resolve())
            return self.root

        result = prompt_project_root(self.root, picker=picker)

        self.assertEqual(result, self.root.resolve())
        self.assertEqual(seen_initial, [self.root.resolve()])

    def test_interactive_prompt_skips_folder_picker_for_configured_project(self) -> None:
        import codebuddy.cli as cli_module

        (self.root / "pyproject.toml").write_text("[project]\nname='configured'\n", encoding="utf-8")
        SessionManager(self.root).load_or_create()
        original_picker = cli_module.open_native_folder_picker
        original_stdin = sys.stdin
        os.environ["CODEBUDDY_FAKE_LLM_RESPONSE"] = "configured project answer"
        os.environ["CODEBUDDY_ALLOW_TEST_FOLDER_PICKER"] = "1"

        class TtyStdin:
            def isatty(self):
                return True

        cli_module.open_native_folder_picker = lambda _initial: (_ for _ in ()).throw(AssertionError("picker should not open"))
        sys.stdin = TtyStdin()
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["What", "is", "here?"]), 0)
        finally:
            cli_module.open_native_folder_picker = original_picker
            sys.stdin = original_stdin
            os.environ.pop("CODEBUDDY_FAKE_LLM_RESPONSE", None)
            os.environ.pop("CODEBUDDY_ALLOW_TEST_FOLDER_PICKER", None)

        self.assertIn("configured project answer", stdout.getvalue())
        self.assertTrue((self.root / ".buddy" / "sessions" / "current.json").exists())

    def test_interactive_prompt_binds_to_spawned_project_without_folder_picker(self) -> None:
        import codebuddy.cli as cli_module

        original_picker = cli_module.open_native_folder_picker
        original_stdin = sys.stdin
        os.environ["CODEBUDDY_FAKE_LLM_RESPONSE"] = "spawned project answer"
        os.environ["CODEBUDDY_ALLOW_TEST_FOLDER_PICKER"] = "1"

        class TtyStdin:
            def isatty(self):
                return True

        cli_module.open_native_folder_picker = lambda _initial: (_ for _ in ()).throw(AssertionError("picker should not open"))
        sys.stdin = TtyStdin()
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["What", "is", "here?"]), 0)
        finally:
            cli_module.open_native_folder_picker = original_picker
            sys.stdin = original_stdin
            os.environ.pop("CODEBUDDY_FAKE_LLM_RESPONSE", None)
            os.environ.pop("CODEBUDDY_ALLOW_TEST_FOLDER_PICKER", None)

        self.assertIn("spawned project answer", stdout.getvalue())
        self.assertTrue((self.root / ".buddy" / "sessions" / "current.json").exists())

    def test_interactive_prompt_ignores_stale_launch_binding(self) -> None:
        import codebuddy.cli as cli_module

        stale = Path(self.tmp.name) / "stale-target"
        stale.mkdir()
        from codebuddy.global_state import set_project_binding

        set_project_binding(self.root, stale)
        original_picker = cli_module.open_native_folder_picker
        original_stdin = sys.stdin
        os.environ["CODEBUDDY_FAKE_LLM_RESPONSE"] = "local project answer"
        os.environ["CODEBUDDY_ALLOW_TEST_FOLDER_PICKER"] = "1"

        class TtyStdin:
            def isatty(self):
                return True

        cli_module.open_native_folder_picker = lambda _initial: (_ for _ in ()).throw(AssertionError("picker should not open"))
        sys.stdin = TtyStdin()
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["What", "is", "here?"]), 0)
        finally:
            cli_module.open_native_folder_picker = original_picker
            sys.stdin = original_stdin
            os.environ.pop("CODEBUDDY_FAKE_LLM_RESPONSE", None)
            os.environ.pop("CODEBUDDY_ALLOW_TEST_FOLDER_PICKER", None)

        self.assertIn("local project answer", stdout.getvalue())
        self.assertTrue((self.root / ".buddy" / "sessions" / "current.json").exists())
        self.assertFalse((stale / ".buddy" / "sessions" / "current.json").exists())

    def test_interactive_prompt_uses_cwd_project_when_cwd_is_a_project(self) -> None:
        import codebuddy.cli as cli_module

        (self.root / "pyproject.toml").write_text("[project]\nname='right-root'\n", encoding="utf-8")
        original_picker = cli_module.open_native_folder_picker
        original_stdin = sys.stdin
        os.environ["CODEBUDDY_FAKE_LLM_RESPONSE"] = "cwd project answer"
        os.environ["CODEBUDDY_ALLOW_TEST_FOLDER_PICKER"] = "1"

        class TtyStdin:
            def isatty(self):
                return True

        cli_module.open_native_folder_picker = lambda _initial: (_ for _ in ()).throw(AssertionError("picker should not open"))
        sys.stdin = TtyStdin()
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["What", "is", "here?"]), 0)
        finally:
            cli_module.open_native_folder_picker = original_picker
            sys.stdin = original_stdin
            os.environ.pop("CODEBUDDY_FAKE_LLM_RESPONSE", None)
            os.environ.pop("CODEBUDDY_ALLOW_TEST_FOLDER_PICKER", None)

        self.assertIn("cwd project answer", stdout.getvalue())
        self.assertTrue((self.root / ".buddy" / "sessions" / "current.json").exists())

    def test_interactive_edit_objective_writes_spawned_project(self) -> None:
        import codebuddy.cli as cli_module

        (self.root / "pyproject.toml").write_text("[project]\nname='target-root'\n", encoding="utf-8")
        (self.root / "agent.py").write_text("def handle():\n    return 'target'\n", encoding="utf-8")
        original_picker = cli_module.open_native_folder_picker
        original_stdin = sys.stdin
        original_build_llm = cli_module.build_llm_client
        os.environ["CODEBUDDY_ALLOW_TEST_FOLDER_PICKER"] = "1"

        class TtyStdin:
            def isatty(self):
                return True

        raw_edit = """<codebuddy_replace path="agent.py">
<old>
def handle():
    return 'target'
</old>
<new>
def handle():
    \"\"\"Return the target project value.\"\"\"
    return 'target'
</new>
</codebuddy_replace>"""
        cli_module.open_native_folder_picker = lambda _initial: (_ for _ in ()).throw(AssertionError("picker should not open"))
        cli_module.build_llm_client = lambda _config: FakeLLMClient([raw_edit, "Documented selected agent.py."])
        sys.stdin = TtyStdin()
        try:
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["Add", "google", "style", "documentation", "to", "agent.py"]), 0)
        finally:
            cli_module.open_native_folder_picker = original_picker
            cli_module.build_llm_client = original_build_llm
            sys.stdin = original_stdin
            os.environ.pop("CODEBUDDY_ALLOW_TEST_FOLDER_PICKER", None)

        self.assertIn('"""Return the target project value."""', (self.root / "agent.py").read_text(encoding="utf-8"))

    def test_folder_picker_is_disabled_under_unittest_without_explicit_allow(self) -> None:
        import codebuddy.cli as cli_module

        original_picker = cli_module.open_native_folder_picker
        cli_module.open_native_folder_picker = lambda _initial: (_ for _ in ()).throw(AssertionError("picker should not open"))
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                result = prompt_project_root(self.root)
        finally:
            cli_module.open_native_folder_picker = original_picker

        self.assertEqual(result, self.root.resolve())

    def test_doctor_checks_provider_key_and_base_url(self) -> None:
        config = self.root / ".buddy" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            """
[model.roles.main]
provider = "perplexity"
model = "sonar-pro"
""",
            encoding="utf-8",
        )
        os.environ["PERPLEXITY_API_KEY"] = "test-perplexity-key"
        stdout = StringIO()
        try:
            with redirect_stdout(stdout):
                code = main(["doctor"])
        finally:
            os.environ.pop("PERPLEXITY_API_KEY", None)

        self.assertEqual(code, 0)
        self.assertIn('"provider": "perplexity"', stdout.getvalue())
        self.assertIn('"api_key": "set"', stdout.getvalue())
        self.assertIn('"base_url": "set"', stdout.getvalue())

    def test_doctor_checks_external_ai_mart_import_contract(self) -> None:
        (self.root / "ai_mart.py").write_text('base_url = "https://aimark.example/openai/v1"\n', encoding="utf-8")
        (self.root / "azure_auth.py").write_text(
            "class AzureAuthClient:\n"
            "    def get_token(self):\n"
            "        return type('Token', (), {'access_token': 'azure-token'})()\n",
            encoding="utf-8",
        )
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["doctor"])

        self.assertEqual(code, 0)
        self.assertIn('"provider": "azure_openai"', stdout.getvalue())
        self.assertIn('"auth": "client azure_auth:AzureAuthClient"', stdout.getvalue())
        self.assertIn('"base_url": "set"', stdout.getvalue())

    def test_doctor_uses_start_dir_project_src_for_ai_mart_imports(self) -> None:
        target = Path(self.tmp.name) / "other-project"
        target_src = target / "src"
        target_src.mkdir(parents=True)
        (target_src / "ai_mart.py").write_text(
            'base_url = "https://other-project.example/openai/v1"\n'
            "class AuthClient:\n"
            "    def authenticate_broker(self):\n"
            "        return type('Token', (), {'access_token': 'other-project-token'})()\n"
            "auth_client = AuthClient()\n",
            encoding="utf-8",
        )
        (target_src / "azure_auth.py").write_text(
            "from ai_mart import auth_client\n\n"
            "class AzureAuthClient:\n"
            "    def get_token(self):\n"
            "        return auth_client.authenticate_broker()\n",
            encoding="utf-8",
        )
        old_start = os.environ.get("CODEBUDDY_START_DIR")
        os.environ["CODEBUDDY_START_DIR"] = str(target)
        stdout = StringIO()

        try:
            os.chdir(target)
            with redirect_stdout(stdout):
                code = main(["doctor"])
        finally:
            if old_start is None:
                os.environ.pop("CODEBUDDY_START_DIR", None)
            else:
                os.environ["CODEBUDDY_START_DIR"] = old_start

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(Path(payload["project_root"]), target.resolve())
        self.assertEqual(payload["base_url"], "set")
        self.assertNotIn("auth_error", payload)

    def test_doctor_does_not_capture_parent_buddy_project_for_ai_mart(self) -> None:
        parent = Path(self.tmp.name) / "configured-parent"
        target = parent / "child-project"
        target_src = target / "src"
        parent.mkdir()
        target_src.mkdir(parents=True)
        (parent / "BUDDY.md").write_text("# Configured Parent\n", encoding="utf-8")
        (parent / ".buddy").mkdir()
        (parent / ".buddy" / "config.toml").write_text("# parent config\n", encoding="utf-8")
        (target_src / "ai_mart.py").write_text(
            'base_url = "https://child-project.example/openai/v1"\n'
            "class AuthClient:\n"
            "    def authenticate_broker(self):\n"
            "        return type('Token', (), {'access_token': 'child-token'})()\n"
            "auth_client = AuthClient()\n",
            encoding="utf-8",
        )
        (target_src / "azure_auth.py").write_text(
            "from ai_mart import auth_client\n\n"
            "class AzureAuthClient:\n"
            "    def get_token(self):\n"
            "        return auth_client.authenticate_broker()\n",
            encoding="utf-8",
        )
        old_start = os.environ.get("CODEBUDDY_START_DIR")
        os.environ["CODEBUDDY_START_DIR"] = str(target)
        stdout = StringIO()

        try:
            os.chdir(target)
            with redirect_stdout(stdout):
                code = main(["doctor"])
        finally:
            if old_start is None:
                os.environ.pop("CODEBUDDY_START_DIR", None)
            else:
                os.environ["CODEBUDDY_START_DIR"] = old_start

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(Path(payload["project_root"]), target.resolve())
        self.assertEqual(payload["base_url"], "set")
        self.assertNotIn("auth_error", payload)
        self.assertFalse((parent / ".buddy" / "sessions" / "current.json").exists())

    def test_auth_set_persists_provider_key_via_writer_without_project_config_secret(self) -> None:
        written: dict[str, str] = {}
        old_key = os.environ.get("PERPLEXITY_API_KEY")

        try:
            result = auth_set(
                {"model": {"providers": {"perplexity": {"api_key_env": "PERPLEXITY_API_KEY"}}}},
                "perplexity",
                prompt=lambda _prompt: "test-key",
                writer=lambda key, value: written.__setitem__(key, value),
            )

            self.assertEqual(result.env_var, "PERPLEXITY_API_KEY")
            self.assertEqual(os.environ["PERPLEXITY_API_KEY"], "test-key")
            self.assertEqual(written, {"PERPLEXITY_API_KEY": "test-key"})
            self.assertIn("PERPLEXITY_API_KEY", auth_status({"model": {"providers": {"perplexity": {"api_key_env": "PERPLEXITY_API_KEY"}}}}, "perplexity").message)
        finally:
            if old_key is None:
                os.environ.pop("PERPLEXITY_API_KEY", None)
            else:
                os.environ["PERPLEXITY_API_KEY"] = old_key

    def test_auth_check_reports_provider_rejected_key_without_leaking_secret(self) -> None:
        old_key = os.environ.get("PERPLEXITY_API_KEY")
        os.environ["PERPLEXITY_API_KEY"] = "pplx-secret-test-key"
        try:
            result = auth_check(
                {
                    "model": {
                        "roles": {"main": {"model": "sonar-pro"}},
                        "providers": {
                            "perplexity": {
                                "base_url": "https://api.perplexity.ai",
                                "endpoint_path": "/chat/completions",
                                "api_key_env": "PERPLEXITY_API_KEY",
                                "model": "sonar-pro",
                            }
                        },
                    }
                },
                "perplexity",
                poster=lambda _url, _headers, _payload, _timeout: (401, '{"error":"bad"}'),
            )
        finally:
            if old_key is None:
                os.environ.pop("PERPLEXITY_API_KEY", None)
            else:
                os.environ["PERPLEXITY_API_KEY"] = old_key

        self.assertIn("provider rejected", result.message)
        self.assertIn("401", result.message)
        self.assertNotIn("pplx-secret-test-key", result.message)

    def test_cli_prompt_uses_fake_llm_response(self) -> None:
        os.environ["CODEBUDDY_FAKE_LLM_RESPONSE"] = "fake answer"
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["What", "does", "this", "do?"]), 0)
            self.assertIn("fake answer", stdout.getvalue())
        finally:
            os.environ.pop("CODEBUDDY_FAKE_LLM_RESPONSE", None)

    def test_one_shot_execution_refreshes_project_memory_after_edits(self) -> None:
        import codebuddy.cli as cli_module

        (self.root / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        original_build_llm = cli_module.build_llm_client
        cli_module.build_llm_client = lambda _config: FakeLLMClient(
            [
                '<tool_call>{"name":"edit_exact_replace","arguments":{"path":"app.py","old":"return 1","new":"return 2"}}</tool_call>',
                "Edited app.py.",
            ]
        )
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["--root", str(self.root), "Change", "f", "to", "return", "2"]), 0)
        finally:
            cli_module.build_llm_client = original_build_llm

        project_map = (self.root / ".buddy" / "index" / "project_map.md").read_text(encoding="utf-8")
        metadata = (self.root / ".buddy" / "index" / "project_memory.json").read_text(encoding="utf-8")
        self.assertIn("Files edited: app.py", project_map)
        self.assertIn("Change f to return 2", metadata)

    def test_approve_branch_one_shot_auto_continues_saved_objective(self) -> None:
        init_empty_repo(self.root)
        (self.root / "README.md").write_text("dirty\n", encoding="utf-8")
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        ledger.objective = "Update docs"
        manager.save(ledger)
        os.environ["CODEBUDDY_FAKE_LLM_RESPONSE"] = "continued automatically"
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["--root", str(self.root), "/approve-branch"]), 0)
        finally:
            os.environ.pop("CODEBUDDY_FAKE_LLM_RESPONSE", None)
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.root, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()

        self.assertTrue(branch.startswith("codebuddy/"))
        self.assertIn("continued automatically", stdout.getvalue())

    def test_yolo_approves_pending_command_and_continues_objective(self) -> None:
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        ledger.objective = "write approved file"
        ledger.pending_next_step = "approve command before execution"
        ledger.approvals["pending_command"] = "Set-Content -Path approved.txt -Value yes"
        ledger.approvals["pending_command_cwd"] = str(self.root)
        manager.save(ledger)
        journal = Journal(manager.session_dir(ledger.session_id) / "journal.jsonl")
        broker = CommandBroker(self.root, journal=journal, session_id=ledger.session_id)
        handler = SlashCommandHandler(
            self.root,
            ledger,
            manager,
            journal,
            GitManager(self.root, command_broker=broker),
            {"enabled": False},
        )

        result = handler.handle("/yolo")

        self.assertTrue((self.root / "approved.txt").exists())
        self.assertTrue(handler.yolo_state["enabled"])
        self.assertEqual(ledger.pending_next_step, None)
        self.assertEqual(result.followup_prompt, "write approved file")

    def test_slash_command_can_call_project_skill(self) -> None:
        ensure_buddy_scaffold(self.root)
        skill = self.root / ".buddy" / "skills" / "docs.md"
        skill.write_text("# Docs Skill\n\nWrite tutorial docstrings.\n", encoding="utf-8")
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        journal = Journal(manager.session_dir(ledger.session_id) / "journal.jsonl")
        handler = SlashCommandHandler(
            self.root,
            ledger,
            manager,
            journal,
            GitManager(self.root, command_broker=CommandBroker(self.root, journal=journal, session_id=ledger.session_id)),
        )

        listing = handler.handle("/skills")
        result = handler.handle("/docs document agent.py")

        self.assertIn("/docs", listing.message)
        self.assertIn("Write tutorial docstrings.", result.followup_prompt)
        self.assertIn("document agent.py", result.followup_prompt)

    def test_slash_steer_persists_and_clears_project_steering(self) -> None:
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        journal = Journal(manager.session_dir(ledger.session_id) / "journal.jsonl")
        handler = SlashCommandHandler(
            self.root,
            ledger,
            manager,
            journal,
            GitManager(self.root, command_broker=CommandBroker(self.root, journal=journal, session_id=ledger.session_id)),
        )

        result = handler.handle("/steer Prefer tiny commits during this loop")
        steering_path = self.root / ".buddy" / "steering" / "active.md"
        content = steering_path.read_text(encoding="utf-8")
        cleared = handler.handle("/steer-clear")

        self.assertIn("steering updated", result.message)
        self.assertIn("Prefer tiny commits", content)
        self.assertIn("steering cleared", cleared.message)
        self.assertFalse(steering_path.exists())

    def test_resume_prompt_can_start_fresh_session_when_previous_work_is_pending(self) -> None:
        first = ProjectSession.open(self.root)
        first.ledger.objective = "finish pending docs"
        first.ledger.pending_next_step = "document src/app.py"
        first.manager.save(first.ledger)
        prompts: list[str] = []

        decision = maybe_prompt_resume(
            self.root,
            first,
            input_func=lambda: "n",
            output_func=prompts.append,
        )

        second = decision.session
        self.assertNotEqual(second.ledger.session_id, first.ledger.session_id)
        self.assertIsNone(second.ledger.objective)
        self.assertIsNone(decision.followup_prompt)
        self.assertTrue(any("finish pending docs" in line for line in prompts))

    def test_resume_prompt_yes_schedules_saved_objective(self) -> None:
        first = ProjectSession.open(self.root)
        first.ledger.objective = "finish pending docs"
        first.ledger.pending_next_step = "document src/app.py"
        first.manager.save(first.ledger)

        decision = maybe_prompt_resume(
            self.root,
            first,
            input_func=lambda: "y",
            output_func=lambda _line: None,
        )

        self.assertEqual(decision.session.ledger.session_id, first.ledger.session_id)
        self.assertEqual(decision.followup_prompt, "finish pending docs")

    def test_resume_prompt_yes_schedules_continue_for_active_workplan(self) -> None:
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        first = ProjectSession.open(self.root)
        first.ledger.objective = "Document each file in the codebase"
        first.ledger.pending_next_step = "document_file a.py"
        first.manager.save(first.ledger)
        workplans = WorkPlanManager(self.root, first.ledger.session_id, PathPolicy(self.root))
        self.assertIsNotNone(workplans.active_or_new(first.ledger.objective))

        decision = maybe_prompt_resume(
            self.root,
            first,
            input_func=lambda: "",
            output_func=lambda _line: None,
        )

        self.assertEqual(decision.followup_prompt, "continue")

    def test_chat_loop_runs_startup_resume_before_reading_prompt(self) -> None:
        import codebuddy.cli as cli_module

        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        journal = Journal(manager.session_dir(ledger.session_id) / "journal.jsonl")
        config = {
            "commands": {},
            "model": {"roles": {"main": {"provider": "fake", "model": "fake-model"}}},
            "git": {},
            "workspace": {},
            "validation": {"commands": []},
            "agent": {},
            "tools": {},
        }
        prompts: list[str] = []
        original_run_prompt = cli_module.run_prompt
        original_read_prompt = cli_module.read_prompt

        def fake_run_prompt(root, ledger, config, journal, prompt, yolo_enabled=False, event_sink=None):
            prompts.append(prompt)
            return AgentResult("execute", "continued", [], [])

        def fake_read_prompt():
            if not prompts:
                raise AssertionError("chat loop read input before running startup continuation")
            return SimpleNamespace(text="", exit_requested=True)

        cli_module.run_prompt = fake_run_prompt
        cli_module.read_prompt = fake_read_prompt
        try:
            with redirect_stdout(StringIO()):
                self.assertEqual(cli_module.chat_loop(self.root, ledger, config, journal, startup_prompt="continue"), 0)
        finally:
            cli_module.run_prompt = original_run_prompt
            cli_module.read_prompt = original_read_prompt

        self.assertEqual(prompts, ["continue"])

    def test_cli_prompt_missing_provider_credentials_fails_cleanly(self) -> None:
        config = self.root / ".buddy" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            """
[model.roles.main]
provider = "blocked"
model = "x"

[model.providers.blocked]
base_url = "https://example.invalid"
api_key_env = "CODEBUDDY_MISSING_TEST_KEY"
""",
            encoding="utf-8",
        )

        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(["What", "does", "this", "do?"]), 2)
        self.assertIn("CODEBUDDY_MISSING_TEST_KEY", stderr.getvalue())

    def test_build_brokers_uses_command_policy_from_config(self) -> None:
        journal = Journal(self.root / "journal.jsonl")
        config = {
            "workspace": {"extra_read_roots": [], "extra_write_roots": [], "sensitive_paths": []},
            "commands": {"default_timeout_seconds": 7, "max_output_chars": 33, "network_allowed": True},
        }

        _edit, command = build_brokers(self.root, "s1", config, journal)

        self.assertEqual(command.policy.default_timeout_seconds, 7)
        self.assertEqual(command.policy.max_output_chars, 33)
        self.assertTrue(command.policy.network_allowed)

    def test_runtime_yolo_state_reaches_command_policy(self) -> None:
        journal = Journal(self.root / "journal.jsonl")
        config = {
            "workspace": {"extra_read_roots": [], "extra_write_roots": [], "sensitive_paths": []},
            "commands": {"default_timeout_seconds": 7, "max_output_chars": 33, "network_allowed": False},
        }

        _edit, command = build_brokers(self.root, "s1", config, journal, yolo_enabled=True)

        self.assertTrue(command.policy.yolo)

    def test_agent_uses_fake_llm_and_creates_agent_branch_for_execution(self) -> None:
        init_empty_repo(self.root)
        manager = SessionManager(self.root)
        ledger = manager.load_or_create()
        journal = Journal(manager.session_dir(ledger.session_id) / "journal.jsonl")
        agent = CodeBuddyAgent(
            self.root,
            ledger,
            FakeLLMClient(["done"]),
            EditBroker(PathPolicy(self.root), journal, ledger.session_id),
            CommandBroker(self.root, journal=journal, session_id=ledger.session_id),
            GitManager(self.root, command_broker=CommandBroker(self.root, journal=journal, session_id=ledger.session_id)),
        )

        result = agent.handle("Add tests for parser")
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.root, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()

        self.assertEqual(result.mode, "execute")
        self.assertTrue(branch.startswith("codebuddy/"))
        self.assertTrue(any(entry.action == "command_complete" and "git switch" in entry.details.get("command", "") for entry in journal.entries()))

    def test_git_manager_does_not_capture_parent_repo(self) -> None:
        parent = Path(self.tmp.name) / "parent-repo"
        child = parent / "child-project"
        child.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=parent, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        status = GitManager(child).status()

        self.assertFalse(status.is_repo)

    def test_git_status_uses_single_status_probe(self) -> None:
        class CountingGitManager(GitManager):
            def __init__(self, project_root: Path) -> None:
                super().__init__(project_root)
                self.calls: list[list[str]] = []

            def _has_project_git_metadata(self) -> bool:
                return True

            def _run(self, args, check):
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 0, "## main\n M README.md\n", "")

        git = CountingGitManager(self.root)

        status = git.status()

        self.assertTrue(status.is_repo)
        self.assertEqual(status.branch, "main")
        self.assertEqual(status.porcelain, " M README.md\n")
        self.assertEqual(git.calls, [["git", "status", "--porcelain=v1", "--branch"]])

    def test_git_manager_uses_direct_journaled_git_for_real_command_broker(self) -> None:
        class CountingBroker(CommandBroker):
            def __init__(self, project_root: Path, journal: Journal) -> None:
                super().__init__(project_root, journal=journal, session_id="s1")
                self.run_calls = 0

            def run(self, command, cwd=None, timeout_seconds=None, approve=False, final_approval=False):
                self.run_calls += 1
                return super().run(command, cwd, timeout_seconds, approve, final_approval)

        init_empty_repo(self.root)
        journal = Journal(self.root / "journal.jsonl")
        broker = CountingBroker(self.root, journal)

        branch = GitManager(self.root, command_broker=broker).ensure_agent_branch("work")

        self.assertIsNotNone(branch)
        self.assertEqual(broker.run_calls, 0)
        self.assertTrue(any(entry.action == "command_complete" and "git switch" in entry.details.get("command", "") for entry in journal.entries()))

    def test_agent_branch_creation_requires_approval_on_dirty_non_agent_branch(self) -> None:
        init_empty_repo(self.root)
        (self.root / "README.md").write_text("dirty\n", encoding="utf-8")

        with self.assertRaises(ConfirmationRequired):
            GitManager(self.root).ensure_agent_branch("work")

    def test_agent_branch_creation_ignores_codebuddy_state_only(self) -> None:
        init_empty_repo(self.root)
        (self.root / ".buddy" / "sessions").mkdir(parents=True)
        (self.root / ".buddy" / "sessions" / "current.json").write_text("{}", encoding="utf-8")

        branch = GitManager(self.root).ensure_agent_branch("work")

        self.assertIsNotNone(branch)
        self.assertTrue(branch.startswith("codebuddy/"))

    def test_git_command_timeout_is_failure(self) -> None:
        root = self.root

        class TimeoutBroker:
            def run(self, command, cwd=None, approve=False):
                return CommandResult(command, cwd or root, None, "", "timed out", 1.0, True, Risk.CONFIRM)

        git = GitManager(self.root, command_broker=TimeoutBroker())

        with self.assertRaises(subprocess.CalledProcessError):
            git._git(["status"], check=True)

    def test_checkpoint_commit_stages_only_agent_paths(self) -> None:
        init_repo_with_commit(self.root, {"agent.txt": "base\n", "user.txt": "base\n"})
        journal = Journal(self.root / "journal.jsonl")
        git = GitManager(self.root, command_broker=CommandBroker(self.root, journal=journal, session_id="s1"))
        git.ensure_agent_branch("work")
        (self.root / "agent.txt").write_text("agent change\n", encoding="utf-8")
        (self.root / "user.txt").write_text("user change\n", encoding="utf-8")

        self.assertTrue(git.checkpoint_commit("checkpoint", ["agent.txt"]))
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.root, check=True, text=True, stdout=subprocess.PIPE).stdout
        last_commit_files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.root, check=True, text=True, stdout=subprocess.PIPE).stdout

        self.assertIn("user.txt", status)
        self.assertIn("agent.txt", last_commit_files)
        self.assertNotIn("user.txt", last_commit_files)
        commands = [entry.details.get("command", "") for entry in journal.entries() if entry.action == "command_complete"]
        self.assertTrue(any(command.startswith("git add") for command in commands))
        self.assertTrue(any(command.startswith("git commit") for command in commands))

    def test_checkpoint_commit_blocks_preexisting_staged_changes(self) -> None:
        init_repo_with_commit(self.root, {"agent.txt": "base\n", "user.txt": "base\n"})
        git = GitManager(self.root)
        git.ensure_agent_branch("work")
        (self.root / "user.txt").write_text("user staged change\n", encoding="utf-8")
        subprocess.run(["git", "add", "user.txt"], cwd=self.root, check=True)
        (self.root / "agent.txt").write_text("agent change\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "pre-existing staged changes"):
            git.checkpoint_commit("checkpoint", ["agent.txt"])

        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.root, check=True, text=True, stdout=subprocess.PIPE).stdout
        self.assertIn("M  user.txt", status)
        self.assertIn(" M agent.txt", status)

    def test_git_manager_records_branch_commit_and_push_state(self) -> None:
        init_repo_with_commit(self.root, {"agent.txt": "base\n"})
        remote = Path(self.tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.root, check=True)
        git = GitManager(self.root)

        branch = git.ensure_agent_branch("stateful work")
        (self.root / "agent.txt").write_text("agent change\n", encoding="utf-8")
        self.assertTrue(git.checkpoint_commit("checkpoint", ["agent.txt"]))
        self.assertTrue(git.push_current_branch("origin"))

        state = git.session_state()
        self.assertEqual(state["agent_branch"], branch)
        self.assertEqual(state["starting_branch"], "main")
        self.assertEqual(state["checkpoints"][0]["message"], "checkpoint")
        self.assertEqual(state["checkpoints"][0]["files"], ["agent.txt"])
        self.assertEqual(state["pushes"][0]["remote"], "origin")
        self.assertEqual(state["pushes"][0]["branch"], branch)

    def test_agent_branch_checkpoint_can_push_to_origin(self) -> None:
        init_repo_with_commit(self.root, {"agent.txt": "base\n"})
        remote = Path(self.tmp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.root, check=True)
        journal = Journal(self.root / "journal.jsonl")
        git = GitManager(self.root, command_broker=CommandBroker(self.root, journal=journal, session_id="s1"))
        git.ensure_agent_branch("work")
        (self.root / "agent.txt").write_text("agent change\n", encoding="utf-8")

        self.assertTrue(git.checkpoint_commit("checkpoint", ["agent.txt"]))
        self.assertTrue(git.push_current_branch("origin"))
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.root, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        remote_branches = subprocess.run(
            ["git", "--git-dir", str(remote), "branch", "--list", branch],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout

        self.assertIn(branch, remote_branches)

    def test_git_remote_info_detects_github_and_gitlab_from_origin(self) -> None:
        init_empty_repo(self.root)
        subprocess.run(["git", "remote", "add", "origin", "git@gitlab.example.com:team/project.git"], cwd=self.root, check=True)

        gitlab = GitManager(self.root).remote_info()

        self.assertIsNotNone(gitlab)
        self.assertEqual(gitlab.provider, "gitlab")
        self.assertEqual(gitlab.host, "gitlab.example.com")
        self.assertEqual(gitlab.owner, "team")
        self.assertEqual(gitlab.repo, "project")

        subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/acme/widgets.git"], cwd=self.root, check=True)

        github = GitManager(self.root).remote_info()

        self.assertIsNotNone(github)
        self.assertEqual(github.provider, "github")
        self.assertEqual(github.host, "github.com")
        self.assertEqual(github.owner, "acme")
        self.assertEqual(github.repo, "widgets")

    def test_agent_branch_required_branches_from_user_feature_branch(self) -> None:
        init_empty_repo(self.root)
        subprocess.run(["git", "switch", "-c", "feature/user-work"], cwd=self.root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        journal = Journal(self.root / "journal.jsonl")
        git = GitManager(self.root, command_broker=CommandBroker(self.root, journal=journal, session_id="s1"), agent_branch_required=True)

        branch = git.ensure_agent_branch("do work")

        self.assertIsNotNone(branch)
        self.assertTrue(branch.startswith("codebuddy/"))


if __name__ == "__main__":
    unittest.main()
