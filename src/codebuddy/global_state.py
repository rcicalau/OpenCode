from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def user_state_path(home: Path | None = None) -> Path:
    base = home or Path(os.environ.get("USERPROFILE", str(Path.home())))
    return base / ".pyagent" / "state.json"


def load_user_state(home: Path | None = None) -> dict[str, Any]:
    path = user_state_path(home)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_user_state(state: dict[str, Any], home: Path | None = None) -> None:
    path = user_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_last_project_root(home: Path | None = None) -> Path | None:
    value = load_user_state(home).get("last_project_root")
    return Path(value) if value else None


def set_last_project_root(root: Path, home: Path | None = None) -> None:
    state = load_user_state(home)
    state["last_project_root"] = str(root.resolve())
    save_user_state(state, home)

