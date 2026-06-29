from __future__ import annotations

import unittest
from pathlib import Path


class SetupArtifactsTests(unittest.TestCase):
    def test_runtime_setup_artifacts_are_committed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config_template = root / "examples" / "project_config.azure_openai.toml"
        runner = root / "run-buddy.cmd"
        direct_runner = root / "buddy.cmd"
        installer = root / "install-buddy.cmd"
        buddy_installer = root / "buddy-install.cmd"
        buddy_uninstaller = root / "buddy-uninstall.cmd"
        pyproject = root / "pyproject.toml"
        bundled_auth = root / "src" / "codebuddy" / "azure_auth.py"
        ai_mart_auth = root / "src" / "codebuddy" / "ai_mart.py"

        self.assertTrue(config_template.exists())
        self.assertTrue(runner.exists())
        self.assertTrue(direct_runner.exists())
        self.assertTrue(installer.exists())
        self.assertTrue(buddy_installer.exists())
        self.assertTrue(buddy_uninstaller.exists())
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
        self.assertIn('set "CODEBUDDY_START_DIR=%CD%"', direct_runner.read_text(encoding="utf-8"))
        self.assertNotIn("if not defined CODEBUDDY_START_DIR", direct_runner.read_text(encoding="utf-8"))
        self.assertIn("install-buddy.cmd", buddy_installer.read_text(encoding="utf-8"))
        self.assertIn("buddy-uninstall.cmd", buddy_installer.read_text(encoding="utf-8"))
        self.assertIn("Microsoft\\WindowsApps", buddy_uninstaller.read_text(encoding="utf-8"))
        self.assertIn("pip uninstall -y codebuddy", buddy_uninstaller.read_text(encoding="utf-8"))
        self.assertNotIn("\npy ", buddy_uninstaller.read_text(encoding="utf-8"))
        self.assertIn('call "%BUDDY_HOME%buddy.cmd" %%*', installer.read_text(encoding="utf-8"))
        self.assertIn('\nbuddy = "codebuddy.cli:main"', pyproject.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
