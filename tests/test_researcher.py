from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.agent import CodeBuddyAgent
from codebuddy.cli import build_llm_client, build_researcher
from codebuddy.command_broker import CommandBroker, CommandPolicy
from codebuddy.config import load_config
from codebuddy.edit_broker import EditBroker
from codebuddy.git_manager import GitManager
from codebuddy.journal import Journal
from codebuddy.llm import FakeLLMClient, LLMResponse
from codebuddy.paths import PathPolicy
from codebuddy.researcher import ResearchBrief, Researcher
from codebuddy.search import Searcher
from codebuddy.session import SessionManager
from codebuddy.validation import ValidationHarness


class ResearcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_researcher_parses_json_brief_and_never_sends_tools(self) -> None:
        llm = FakeLLMClient(
            [
                LLMResponse(
                    """```json
{
  "summary": "The tests should focus on src/app.py.",
  "relevant_files": ["src/app.py", "tests/test_app.py"],
  "risks": ["Keep edits scoped."],
  "unknowns": ["Validation command is unknown."],
  "recommended_next_reads": ["pyproject.toml"]
}
```"""
                )
            ]
        )
        researcher = Researcher(llm, timeout_seconds=1)

        brief = researcher.research("create tests", "Project context\nsrc/app.py", "execute")

        self.assertIsNotNone(brief)
        assert brief is not None
        self.assertEqual(brief.summary, "The tests should focus on src/app.py.")
        self.assertEqual(brief.relevant_files, ["src/app.py", "tests/test_app.py"])
        self.assertEqual(llm.tool_requests, [[]])
        self.assertIn("read-only researcher", llm.calls[0][0].content.lower())

    def test_researcher_malformed_response_falls_back_to_raw_summary(self) -> None:
        llm = FakeLLMClient(["not json but still useful"])
        researcher = Researcher(llm, timeout_seconds=1)

        brief = researcher.research("inspect project", "Project context", "scope")

        self.assertIsNotNone(brief)
        assert brief is not None
        self.assertIn("not json", brief.summary)
        self.assertEqual(brief.relevant_files, [])
        self.assertIsNone(researcher.last_error)

    def test_build_llm_client_uses_role_model_import_over_provider_model(self) -> None:
        src = self.root / "src"
        src.mkdir()
        (src / "ai_mart.py").write_text(
            'base_url = "https://aimart.example/openai/v1"\n'
            'QWEN_RESEARCH_MODEL = "qwen/qwen3.6-27b"\n',
            encoding="utf-8",
        )
        (src / "azure_auth.py").write_text(
            "class AzureAuthClient:\n"
            "    def get_token(self):\n"
            "        return type('Token', (), {'access_token': 'token'})()\n",
            encoding="utf-8",
        )
        config = load_config(self.root).config
        config["_runtime_project_root"] = str(self.root)

        client = build_llm_client(config, role_name="researcher")

        self.assertEqual(client.model, "qwen/qwen3.6-27b")

    def test_build_researcher_is_optional_when_ai_mart_constant_is_missing(self) -> None:
        (self.root / "ai_mart.py").write_text('base_url = "https://aimart.example/openai/v1"\n', encoding="utf-8")
        config = load_config(self.root).config
        config["_runtime_project_root"] = str(self.root)

        self.assertIsNone(build_researcher(config))


class AgentResearchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        manager = SessionManager(self.root)
        self.ledger = manager.load_or_create()
        self.journal = Journal(manager.session_dir(self.ledger.session_id) / "journal.jsonl")
        self.policy = PathPolicy(self.root)
        self.edit = EditBroker(self.policy, self.journal, self.ledger.session_id)
        self.command = CommandBroker(self.root, CommandPolicy(default_timeout_seconds=10), self.journal, self.ledger.session_id)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_agent(self, llm, researcher=None) -> CodeBuddyAgent:
        return CodeBuddyAgent(
            self.root,
            self.ledger,
            llm,
            self.edit,
            self.command,
            GitManager(self.root),
            Searcher(self.policy),
            ValidationHarness(self.root, self.command),
            researcher=researcher,
        )

    def test_scope_prompt_adds_research_brief_to_main_model_context(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

        class StubResearcher:
            last_error = None

            def research(self, objective, project_context, mode):
                return ResearchBrief(
                    summary="Focus on src/app.py and existing tests.",
                    relevant_files=["src/app.py"],
                    risks=["Do not edit in scope mode."],
                    unknowns=[],
                    recommended_next_reads=["tests/test_app.py"],
                )

        llm = FakeLLMClient(["Research-aware answer."])
        agent = self.make_agent(llm, StubResearcher())

        result = agent.handle("/scope inspect the project")

        first_call = "\n".join(message.content for message in llm.calls[0])
        self.assertIn("Research brief", first_call)
        self.assertIn("Focus on src/app.py", first_call)
        self.assertIn("Research-aware answer.", result.message)
        self.assertTrue(any(event.kind == "research" and event.status == "done" for event in result.events))

    def test_researcher_failure_does_not_block_main_model(self) -> None:
        class FailingResearcher:
            last_error = "rate limited"

            def research(self, objective, project_context, mode):
                return None

        llm = FakeLLMClient(["Main model answered anyway."])
        agent = self.make_agent(llm, FailingResearcher())

        result = agent.handle("/scope inspect the project")

        self.assertIn("answered anyway", result.message)
        self.assertTrue(any(event.kind == "research" and event.status == "failed" for event in result.events))


if __name__ == "__main__":
    unittest.main()
