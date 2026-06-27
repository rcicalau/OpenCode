from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import UndoError
from .hashutil import sha256_bytes
from .redaction import Redactor
from .session import utc_now


@dataclass(slots=True)
class JournalEntry:
    timestamp: str
    session_id: str
    action: str
    target_paths: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    undo: dict[str, Any] | None = None


class Journal:
    def __init__(self, path: Path, redactor: Redactor | None = None) -> None:
        self.path = path
        self.redactor = redactor or Redactor().from_environment()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: JournalEntry) -> None:
        entry = JournalEntry(
            timestamp=entry.timestamp,
            session_id=entry.session_id,
            action=entry.action,
            target_paths=entry.target_paths,
            details=self._redact_obj(entry.details),
            undo=entry.undo,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), sort_keys=True) + "\n")

    def record(self, session_id: str, action: str, target_paths: list[str] | None = None, **details: Any) -> None:
        self.append(
            JournalEntry(
                timestamp=utc_now(),
                session_id=session_id,
                action=action,
                target_paths=target_paths or [],
                details=details,
            )
        )

    def record_file_change(
        self,
        session_id: str,
        action: str,
        path: Path,
        before: bytes,
        after: bytes,
        details: dict[str, Any] | None = None,
    ) -> None:
        contains_secret = self._bytes_look_secret(before) or self._bytes_look_secret(after)
        entry = JournalEntry(
            timestamp=utc_now(),
            session_id=session_id,
            action=action,
            target_paths=[str(path)],
            details={
                **(details or {}),
                "before_hash": sha256_bytes(before),
                "after_hash": sha256_bytes(after),
                "undo_available": not contains_secret,
            },
            undo=None
            if contains_secret
            else {
                "kind": "restore_bytes",
                "path": str(path),
                "before_b64": base64.b64encode(before).decode("ascii"),
                "before_hash": sha256_bytes(before),
                "after_hash": sha256_bytes(after),
            },
        )
        self.append(entry)

    def entries(self) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        result: list[JournalEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    data = json.loads(line)
                    result.append(JournalEntry(**data))
                except (json.JSONDecodeError, TypeError):
                    continue
        return result

    def undo_last(self, session_id: str) -> Path:
        entries = self.entries()
        for entry in reversed(entries):
            if entry.session_id == session_id and entry.undo:
                undone = self._apply_undo(entry)
                self.record(session_id, "undo", [str(undone)], undone_action=entry.action)
                return undone
        raise UndoError("no reversible journal entry found")

    def undo_session(self, session_id: str) -> list[Path]:
        undone: list[Path] = []
        while True:
            try:
                undone.append(self.undo_last(session_id))
            except UndoError:
                break
        if not undone:
            raise UndoError("no reversible journal entries found")
        return undone

    @staticmethod
    def _apply_undo(entry: JournalEntry) -> Path:
        undo = entry.undo or {}
        if undo.get("kind") != "restore_bytes":
            raise UndoError(f"unsupported undo kind: {undo.get('kind')}")
        path = Path(undo["path"])
        if not path.exists():
            raise UndoError(f"cannot undo because file is missing: {path}")
        current = path.read_bytes()
        if sha256_bytes(current) != undo["after_hash"]:
            raise UndoError(f"cannot undo because file has changed since journal entry: {path}")
        before = base64.b64decode(undo["before_b64"])
        _atomic_write_bytes(path, before)
        return path

    def _bytes_look_secret(self, data: bytes) -> bool:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return self.redactor.redact(text) != text

    def _redact_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor.redact(value)
        if isinstance(value, list):
            return [self._redact_obj(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact_obj(item) for key, item in value.items()}
        return value


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)
