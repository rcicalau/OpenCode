from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import PathPolicy
from .textfile import is_probably_binary_file, read_limited_text_bytes


@dataclass(slots=True)
class SearchMatch:
    path: str
    line: int
    text: str


class Searcher:
    def __init__(self, policy: PathPolicy) -> None:
        self.policy = policy

    def read_text(self, path: str | Path, max_chars: int = 20000) -> str:
        resolved = self.policy.ensure_read_allowed(path)
        if is_probably_binary_file(resolved):
            raise ValueError(f"binary file cannot be read as text: {resolved}")
        data = read_limited_text_bytes(resolved, max_chars)
        text = data.decode("utf-8", errors="replace")
        return text

    def search(self, pattern: str, max_matches: int = 50) -> list[SearchMatch]:
        try:
            completed = subprocess.run(
                [
                    "rg",
                    "--line-number",
                    "--no-heading",
                    "--glob",
                    "!.git/**",
                    "--glob",
                    "!.buddy/**",
                    "--",
                    pattern,
                    ".",
                ],
                cwd=str(self.policy.root),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            if completed.returncode in (0, 1):
                return self._parse_rg(completed.stdout, max_matches)
        except (OSError, subprocess.SubprocessError):
            pass
        return self._fallback_search(pattern, max_matches)

    def _parse_rg(self, output: str, max_matches: int) -> list[SearchMatch]:
        matches: list[SearchMatch] = []
        for line in output.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            path, line_no, text = parts
            try:
                candidate = Path(path)
                resolved = candidate.resolve() if candidate.is_absolute() else (self.policy.root / candidate).resolve()
                rel = resolved.relative_to(self.policy.root).as_posix()
                if self.policy.is_sensitive(rel):
                    continue
                matches.append(SearchMatch(rel, int(line_no), text))
            except (ValueError, TypeError):
                continue
            if len(matches) >= max_matches:
                break
        return matches

    def _fallback_search(self, pattern: str, max_matches: int) -> list[SearchMatch]:
        matches: list[SearchMatch] = []
        for path in self.policy.root.rglob("*"):
            if not path.is_file() or ".git" in path.parts or ".buddy" in path.parts:
                continue
            if self.policy.is_sensitive(path):
                continue
            if is_probably_binary_file(path):
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_no, line in enumerate(handle, start=1):
                        if pattern in line:
                            matches.append(SearchMatch(path.relative_to(self.policy.root).as_posix(), line_no, line.rstrip("\r\n")))
                            if len(matches) >= max_matches:
                                return matches
            except OSError:
                continue
        return matches
