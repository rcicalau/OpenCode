from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from .errors import FileSafetyError


DEFAULT_SENSITIVE_PATTERNS = [
    ".env",
    ".env.*",
    ".buddy/cache/**",
    ".buddy/index/**",
    ".buddy/logs/**",
    ".buddy/plans/**",
    ".buddy/sessions/**",
    ".buddy/workplans/**",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
    "*credentials*",
    "*token*",
]


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    boundaries = _home_boundaries()
    for path in [current, *current.parents]:
        if path in boundaries and current != path:
            break
        if is_project_marker(path):
            return path
    return current


def is_project_marker(path: Path) -> bool:
    return (
        (path / ".git").exists()
        or (path / ".buddy" / "config.toml").exists()
        or (path / ".buddy" / "sessions" / "current.json").exists()
        or (path / "BUDDY.md").exists()
        or (path / "SPEC.md").exists()
        or (path / "pyproject.toml").exists()
        or (path / "AGENTS.md").exists()
    )


def find_buddy_project_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    boundaries = _home_boundaries()
    for path in [current, *current.parents]:
        if path in boundaries and current != path:
            break
        if is_buddy_project_marker(path):
            return path
    return None


def is_buddy_project_marker(path: Path) -> bool:
    return (
        (path / ".buddy" / "config.toml").exists()
        or (path / ".buddy" / "sessions" / "current.json").exists()
        or (path / "BUDDY.md").exists()
    )


def resolve_launch_start_dir(start: str | Path | None = None) -> Path:
    if start:
        return Path(start).expanduser().resolve()
    cwd = Path.cwd().resolve()
    env_start = os.environ.get("CODEBUDDY_START_DIR")
    if not env_start:
        return cwd
    try:
        captured = Path(env_start).expanduser().resolve()
    except OSError:
        return cwd
    return captured if captured == cwd else cwd


def resolve_project_root(explicit_root: str | Path | None = None, start: Path | None = None) -> Path:
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()
    start_root = resolve_launch_start_dir(start)
    return find_buddy_project_root(start_root) or start_root


def _home_boundaries() -> set[Path]:
    boundaries = {
        Path(os.environ.get("USERPROFILE", str(Path.home()))).resolve(),
        Path.home().resolve(),
    }
    home_drive = os.environ.get("HOMEDRIVE")
    home_path = os.environ.get("HOMEPATH")
    if home_drive and home_path:
        boundaries.add(Path(home_drive + home_path).resolve())
    return boundaries


@dataclass(slots=True)
class PathPolicy:
    root: Path
    extra_read_roots: list[Path] = field(default_factory=list)
    extra_write_roots: list[Path] = field(default_factory=list)
    sensitive_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVE_PATTERNS))

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.extra_read_roots = [p.resolve() for p in self.extra_read_roots]
        self.extra_write_roots = [p.resolve() for p in self.extra_write_roots]

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        self._reject_windows_unsafe_path(candidate)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        self._reject_windows_unsafe_path(resolved)
        return resolved

    def relative(self, path: str | Path) -> str:
        resolved = self.resolve(path)
        try:
            return str(resolved.relative_to(self.root))
        except ValueError:
            return str(resolved)

    def ensure_read_allowed(self, path: str | Path) -> Path:
        resolved = self.resolve(path)
        if not self._under_any(resolved, [self.root, *self.extra_read_roots]):
            raise FileSafetyError(f"read outside workspace requires approval: {resolved}")
        if self.is_sensitive(resolved):
            raise FileSafetyError(f"sensitive file requires explicit approval: {resolved}")
        return resolved

    def ensure_write_allowed(self, path: str | Path) -> Path:
        resolved = self.resolve(path)
        if not self._under_any(resolved, [self.root, *self.extra_write_roots]):
            raise FileSafetyError(f"write outside workspace denied: {resolved}")
        if self.is_sensitive(resolved):
            raise FileSafetyError(f"sensitive file write requires explicit approval: {resolved}")
        return resolved

    def is_sensitive(self, path: str | Path) -> bool:
        resolved = self.resolve(path)
        name = resolved.name
        rel = self.relative(resolved).replace(os.sep, "/")
        for pattern in self.sensitive_patterns:
            normalized = pattern.replace(os.sep, "/")
            if fnmatch(name, normalized) or fnmatch(rel, normalized):
                return True
        return False

    @staticmethod
    def _under_any(path: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _reject_windows_unsafe_path(path: Path) -> None:
        raw = str(path)
        drive = Path(raw).drive
        rest = raw[len(drive) :] if drive else raw
        if ":" in rest:
            raise FileSafetyError(f"alternate data stream or unsafe path denied: {path}")
        reserved = {"con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "lpt1", "lpt2", "lpt3"}
        for part in path.parts:
            normalized = part.rstrip(" .").lower()
            if normalized in reserved:
                raise FileSafetyError(f"reserved Windows device path denied: {path}")
