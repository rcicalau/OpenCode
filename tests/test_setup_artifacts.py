from __future__ import annotations

import unittest
from pathlib import Path


class SetupArtifactsTests(unittest.TestCase):
    def test_azure_auth_setup_artifacts_are_committed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        auth_template = root / "examples" / "azure_auth_example.py"
        config_template = root / "examples" / "project_config.azure_openai.toml"
        setup_script = root / "scripts" / "setup-azure-openai.ps1"
        runner = root / "run-buddy.cmd"

        self.assertTrue(auth_template.exists())
        self.assertTrue(config_template.exists())
        self.assertTrue(setup_script.exists())
        self.assertTrue(runner.exists())

        self.assertIn("class AzureAuthClient", auth_template.read_text(encoding="utf-8"))
        self.assertIn("def get_token", auth_template.read_text(encoding="utf-8"))
        self.assertIn('provider = "azure_openai"', config_template.read_text(encoding="utf-8"))
        self.assertIn("AZURE_OPENAI_BASE_URL", setup_script.read_text(encoding="utf-8"))
        self.assertIn('PYTHONPATH=%BUDDY_HOME%src', runner.read_text(encoding="utf-8"))
        self.assertIn('--root "%CD%" chat', runner.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
