from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class RequirementsFileTests(unittest.TestCase):
    def test_requirements_file_lists_runtime_dependencies(self) -> None:
        requirements = Path(__file__).resolve().parents[1] / "requirements.txt"

        content = requirements.read_text(encoding="utf-8")

        self.assertIn("openai", content)
        self.assertIn("httpx", content)
        self.assertIn("prompt_toolkit", content)
        self.assertIn("rich", content)


if __name__ == "__main__":
    unittest.main()
