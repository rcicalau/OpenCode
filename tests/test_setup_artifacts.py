from __future__ import annotations

import unittest
from pathlib import Path


class SetupArtifactsTests(unittest.TestCase):
    def test_runtime_setup_artifacts_are_committed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config_template = root / "examples" / "project_config.azure_openai.toml"
        runner = root / "run-buddy.cmd"
        installer = root / "install-buddy.cmd"
        buddy_installer = root / "buddy-install.cmd"
        pyproject = root / "pyproject.toml"
        bundled_auth = root / "src" / "codebuddy" / "azure_auth.py"
        ai_mart_auth = root / "src" / "codebuddy" / "ai_mart.py"

        self.assertTrue(config_template.exists())
        self.assertTrue(runner.exists())
        self.assertTrue(installer.exists())
        self.assertTrue(buddy_installer.exists())
        self.assertFalse(bundled_auth.exists())
        self.assertFalse(ai_mart_auth.exists())

        self.assertIn('provider = "azure_openai"', config_template.read_text(encoding="utf-8"))
        self.assertIn('base_url_import = "ai_mart:base_url"', config_template.read_text(encoding="utf-8"))
        self.assertIn('auth_client = "azure_auth:AzureAuthClient"', config_template.read_text(encoding="utf-8"))
        self.assertIn('PYTHONPATH=%BUDDY_HOME%src', runner.read_text(encoding="utf-8"))
        self.assertIn('%CODEBUDDY_START_DIR%;%CODEBUDDY_START_DIR%\\src', runner.read_text(encoding="utf-8"))
        self.assertIn('CODEBUDDY_START_DIR=%CD%', runner.read_text(encoding="utf-8"))
        self.assertIn("Python 3.12 or newer", runner.read_text(encoding="utf-8"))
        self.assertIn("-m codebuddy chat", runner.read_text(encoding="utf-8"))
        self.assertNotIn('--root "%CD%"', runner.read_text(encoding="utf-8"))
        self.assertIn("install-buddy.cmd", buddy_installer.read_text(encoding="utf-8"))
        self.assertIn('call "%BUDDY_HOME%buddy.cmd" %%*', installer.read_text(encoding="utf-8"))
        self.assertIn('\nbuddy = "codebuddy.cli:main"', pyproject.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
