from __future__ import annotations

import ast
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .hashutil import sha256_file
from .paths import PathPolicy
from .textfile import is_probably_binary_file

MAX_SYMBOL_BYTES = 2_000_000

@dataclass(slots=True)
class FileRecord:
    path: str
    sha256: str
    size: int


@dataclass(slots=True)
class SymbolRecord:
    path: str
    name: str
    kind: str
    line: int


@dataclass(slots=True)
class ProjectIndex:
    files: list[FileRecord] = field(default_factory=list)
    symbols: list[SymbolRecord] = field(default_factory=list)


class Indexer:
    def __init__(self, project_root: Path, policy: PathPolicy | None = None) -> None:
        self.project_root = project_root.resolve()
        self.policy = policy or PathPolicy(self.project_root)
        self.index_dir = self.project_root / ".buddy" / "index"

    def build(self) -> ProjectIndex:
        records: list[FileRecord] = []
        symbols: list[SymbolRecord] = []
        for path in self._iter_files():
            rel = path.relative_to(self.project_root).as_posix()
            size = path.stat().st_size
            records.append(FileRecord(path=rel, sha256=sha256_file(path), size=size))
            if path.suffix == ".py" and size <= MAX_SYMBOL_BYTES and not is_probably_binary_file(path):
                raw = path.read_bytes()
                symbols.extend(self._python_symbols(path, rel, raw))
        index = ProjectIndex(files=records, symbols=symbols)
        self.save(index)
        return index

    def save(self, index: ProjectIndex) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        (self.index_dir / "files.json").write_text(json.dumps([asdict(item) for item in index.files], indent=2), encoding="utf-8")
        (self.index_dir / "symbols.json").write_text(json.dumps([asdict(item) for item in index.symbols], indent=2), encoding="utf-8")

    def _iter_files(self):
        ignored = {".git", ".buddy", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv"}
        for path in self.project_root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in ignored for part in path.relative_to(self.project_root).parts):
                continue
            if self.policy.is_sensitive(path):
                continue
            yield path

    @staticmethod
    def _python_symbols(path: Path, rel: str, raw: bytes) -> list[SymbolRecord]:
        try:
            tree = ast.parse(raw.decode("utf-8"), filename=str(path))
        except (UnicodeDecodeError, SyntaxError):
            return []
        result: list[SymbolRecord] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                result.append(SymbolRecord(rel, node.name, "class", node.lineno))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result.append(SymbolRecord(rel, node.name, "function", node.lineno))
        return result
