from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import SessionRootMismatch
from .objective_state import IDLE


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class PlanItem:
    step: str
    status: str = "pending"


@dataclass(slots=True)
class SessionLedger:
    session_id: str
    project_root: str
    created_at: str
    updated_at: str
    mode: str = "chat"
    objective: str | None = None
    objective_state: str = IDLE
    plan: list[PlanItem] = field(default_factory=list)
    completed_actions: list[str] = field(default_factory=list)
    pending_next_step: str | None = None
    files_inspected: list[str] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    validation_state: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    approvals: dict[str, Any] = field(default_factory=dict)
    shelved_objectives: list[dict[str, Any]] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = utc_now()


class SessionManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.base_dir = self.project_root / ".buddy" / "sessions"
        self.current_file = self.base_dir / "current.json"

    def load_or_create(self, new: bool = False) -> SessionLedger:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if not new and self.current_file.exists():
            current = json.loads(self.current_file.read_text(encoding="utf-8"))
            ledger_path = self.base_dir / current["session_id"] / "ledger.json"
            if ledger_path.exists():
                ledger = self._read_ledger(ledger_path)
                self._ensure_project_root_matches(ledger)
                return ledger
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        ledger = SessionLedger(
            session_id=session_id,
            project_root=str(self.project_root),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.save(ledger)
        return ledger

    def save(self, ledger: SessionLedger) -> None:
        self._ensure_project_root_matches(ledger)
        session_dir = self.base_dir / ledger.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        ledger.touch()
        data = asdict(ledger)
        (session_dir / "ledger.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.current_file.write_text(json.dumps({"session_id": ledger.session_id}, indent=2), encoding="utf-8")

    def session_dir(self, session_id: str) -> Path:
        return self.base_dir / session_id

    def _ensure_project_root_matches(self, ledger: SessionLedger) -> None:
        ledger_root = Path(ledger.project_root).expanduser().resolve()
        if ledger_root != self.project_root:
            raise SessionRootMismatch(f"session belongs to {ledger_root}, not {self.project_root}")

    @staticmethod
    def _read_ledger(path: Path) -> SessionLedger:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["plan"] = [PlanItem(**item) for item in data.get("plan", [])]
        return SessionLedger(**data)
