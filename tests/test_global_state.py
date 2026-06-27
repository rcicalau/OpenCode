from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.global_state import get_last_project_root, set_last_project_root, user_state_path


class GlobalStateTests(unittest.TestCase):
    def test_last_project_root_is_saved_under_user_pyagent_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "project"

            set_last_project_root(project, home)

            self.assertEqual(get_last_project_root(home), project.resolve())
            self.assertEqual(user_state_path(home), home / ".pyagent" / "state.json")


if __name__ == "__main__":
    unittest.main()

