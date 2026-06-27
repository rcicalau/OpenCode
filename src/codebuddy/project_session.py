from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .journal import Journal
from .session import SessionLedger, SessionManager


@dataclass(slots=True)
class ProjectSession:
    root: Path
    manager: SessionManager
    ledger: SessionLedger
    journal: Journal

    @classmethod
    def open(cls, root: Path, new: bool = False) -> "ProjectSession":
        resolved = root.resolve()
        manager = SessionManager(resolved)
        ledger = manager.load_or_create(new=new)
        session_dir = manager.session_dir(ledger.session_id)
        journal = Journal(session_dir / "journal.jsonl")
        return cls(resolved, manager, ledger, journal)
