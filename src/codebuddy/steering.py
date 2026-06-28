from __future__ import annotations

from pathlib import Path

from .session import utc_now


class SteeringInbox:
    """Project-local user steering for active and resumed agent loops."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.base_dir = self.project_root / ".buddy" / "steering"
        self.active_path = self.base_dir / "active.md"

    def read(self, max_chars: int = 4000) -> str:
        try:
            text = self.active_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if len(text) <= max_chars:
            return text
        return text[-max_chars:].lstrip()

    def append(self, text: str) -> Path:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("steering text cannot be empty")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        existing = self.read(max_chars=20000)
        entry = f"## {utc_now()}\n\n{cleaned}\n"
        content = f"{existing.rstrip()}\n\n{entry}" if existing else f"# User Steering\n\n{entry}"
        self.active_path.write_text(content, encoding="utf-8")
        return self.active_path

    def clear(self) -> bool:
        try:
            self.active_path.unlink()
            return True
        except FileNotFoundError:
            return False
