from __future__ import annotations

import difflib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import EditConflict, FileSafetyError
from .hashutil import sha256_bytes
from .journal import Journal
from .paths import PathPolicy
from .textfile import TextSnapshot, encode_like, read_text_snapshot

MAX_OVERWRITE_BYTES = 2_000_000


@dataclass(slots=True)
class EditResult:
    path: Path
    before_hash: str
    after_hash: str
    diff: str


class EditBroker:
    def __init__(self, policy: PathPolicy, journal: Journal | None = None, session_id: str = "manual") -> None:
        self.policy = policy
        self.journal = journal
        self.session_id = session_id

    def read_text(self, path: str | Path) -> TextSnapshot:
        resolved = self.policy.ensure_read_allowed(path)
        return read_text_snapshot(resolved)

    def exact_replace(self, path: str | Path, old: str, new: str, expected_hash: str | None = None) -> EditResult:
        resolved = self.policy.ensure_write_allowed(path)
        snapshot = read_text_snapshot(resolved)
        if expected_hash and sha256_bytes(snapshot.raw) != expected_hash:
            raise EditConflict(f"file changed since it was read: {resolved}")
        old2, new2 = self._normalize_candidate_newlines(snapshot, old, new)
        count = snapshot.text.count(old2)
        if count == 0:
            raise EditConflict(f"exact block not found in {resolved}")
        if count > 1:
            raise EditConflict(f"exact block is not unique in {resolved}: {count} matches")
        updated = snapshot.text.replace(old2, new2, 1)
        return self._write_snapshot(snapshot, updated, "exact_replace")

    def create_file(self, path: str | Path, content: str, overwrite: bool = False, expected_hash: str | None = None) -> EditResult:
        resolved = self.policy.ensure_write_allowed(path)
        if resolved.exists() and not overwrite:
            raise FileSafetyError(f"file already exists: {resolved}")
        if resolved.exists():
            if resolved.stat().st_size > MAX_OVERWRITE_BYTES:
                raise FileSafetyError(f"existing file is too large for overwrite: {resolved}")
            snapshot = read_text_snapshot(resolved)
            before = snapshot.raw
            if not expected_hash:
                raise EditConflict(f"overwrite requires expected_hash for existing file: {resolved}")
            if sha256_bytes(before) != expected_hash:
                raise EditConflict(f"file changed since it was read: {resolved}")
        else:
            before = b""
        newline = "\r\n" if "\r\n" in content else "\n"
        normalized = content
        if newline == "\r\n":
            normalized = content.replace("\n", "\r\n").replace("\r\r\n", "\r\n")
        after = normalized.encode("utf-8")
        if self.journal:
            self.journal.record(
                self.session_id,
                "edit_intent",
                [str(resolved)],
                edit_action="create_file" if not before else "rewrite_file",
                before_hash=sha256_bytes(before),
            )
        self._atomic_write(resolved, after)
        diff = _unified_diff(before.decode("utf-8", errors="replace"), normalized, str(resolved))
        if self.journal:
            self.journal.record_file_change(self.session_id, "create_file" if not before else "rewrite_file", resolved, before, after, {"diff": diff})
        return EditResult(resolved, sha256_bytes(before), sha256_bytes(after), diff)

    def apply_unified_diff(self, path: str | Path, patch: str, expected_hash: str | None = None) -> EditResult:
        resolved = self.policy.ensure_write_allowed(path)
        snapshot = read_text_snapshot(resolved)
        if expected_hash and sha256_bytes(snapshot.raw) != expected_hash:
            raise EditConflict(f"file changed since it was read: {resolved}")
        updated = apply_unified_diff_to_text(snapshot.text, patch, snapshot.newline)
        return self._write_snapshot(snapshot, updated, "apply_unified_diff")

    def _write_snapshot(self, snapshot: TextSnapshot, updated_text: str, action: str) -> EditResult:
        before = snapshot.raw
        after = encode_like(snapshot, updated_text)
        if before == after:
            raise EditConflict(f"edit produced no change: {snapshot.path}")
        before_hash = sha256_bytes(before)
        if self.journal:
            self.journal.record(
                self.session_id,
                "edit_intent",
                [str(snapshot.path)],
                edit_action=action,
                before_hash=before_hash,
            )
        self._atomic_write(snapshot.path, after)
        reread = snapshot.path.read_bytes()
        after_hash = sha256_bytes(reread)
        if after_hash != sha256_bytes(after):
            raise FileSafetyError(f"post-write hash mismatch for {snapshot.path}")
        diff = _unified_diff(before.decode("utf-8", errors="replace"), updated_text, str(snapshot.path))
        if self.journal:
            self.journal.record_file_change(self.session_id, action, snapshot.path, before, after, {"diff": diff})
        return EditResult(snapshot.path, before_hash, after_hash, diff)

    @staticmethod
    def _normalize_candidate_newlines(snapshot: TextSnapshot, old: str, new: str) -> tuple[str, str]:
        if old in snapshot.text:
            return old, new
        if snapshot.newline == "\r\n":
            old2 = old.replace("\r\n", "\n").replace("\n", "\r\n")
            new2 = new.replace("\r\n", "\n").replace("\n", "\r\n")
            return old2, new2
        old2 = old.replace("\r\n", "\n").replace("\r", "\n")
        new2 = new.replace("\r\n", "\n").replace("\r", "\n")
        return old2, new2

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
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


def _unified_diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path}:before",
            tofile=f"{path}:after",
        )
    )


HUNK_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")


def apply_unified_diff_to_text(text: str, patch: str, newline: str = "\n") -> str:
    original_lines = _split_lines_with_eol(text)
    original_bodies = [body for body, _eol in original_lines]
    patch_lines = patch.splitlines()
    hunks: list[list[tuple[str, str]]] = []
    index = 0
    while index < len(patch_lines):
        line = patch_lines[index]
        if line.startswith("---") or line.startswith("+++"):
            index += 1
            continue
        match = HUNK_RE.match(line)
        if not match:
            index += 1
            continue
        index += 1
        ops: list[tuple[str, str]] = []
        while index < len(patch_lines) and not patch_lines[index].startswith("@@"):
            hline = patch_lines[index]
            if hline == r"\ No newline at end of file":
                index += 1
                continue
            if not hline:
                raise EditConflict("invalid empty hunk line")
            prefix, body = hline[0], hline[1:]
            if prefix not in (" ", "-", "+"):
                raise EditConflict(f"invalid hunk prefix: {prefix!r}")
            ops.append((prefix, body))
            index += 1
        hunks.append(ops)
    if not hunks:
        raise EditConflict("patch contained no hunks")
    lines = list(original_lines)
    bodies = list(original_bodies)
    search_from = 0
    for ops in hunks:
        old_seq = [body for prefix, body in ops if prefix in (" ", "-")]
        match_at = _find_unique_sequence(bodies, old_seq, search_from)
        old_pointer = match_at
        replacement: list[tuple[str, str]] = []
        last_removed_eol = newline
        for prefix, body in ops:
            if prefix == " ":
                replacement.append(lines[old_pointer])
                old_pointer += 1
            elif prefix == "-":
                last_removed_eol = lines[old_pointer][1]
                old_pointer += 1
            elif prefix == "+":
                replacement.append((body, "" if last_removed_eol == "" else newline))
        old_len = len(old_seq)
        lines = lines[:match_at] + replacement + lines[match_at + old_len :]
        bodies = [body for body, _eol in lines]
        search_from = match_at + len(replacement)
    return "".join(body + eol for body, eol in lines)


def _split_lines_with_eol(text: str) -> list[tuple[str, str]]:
    if text == "":
        return []
    result: list[tuple[str, str]] = []
    for line in text.splitlines(keepends=True):
        if line.endswith("\r\n"):
            result.append((line[:-2], "\r\n"))
        elif line.endswith("\n") or line.endswith("\r"):
            result.append((line[:-1], line[-1]))
        else:
            result.append((line, ""))
    return result


def _find_unique_sequence(lines: list[str], needle: list[str], search_from: int) -> int:
    if not needle:
        raise EditConflict("empty old hunk sequence is not supported")
    matches = []
    for i in range(search_from, len(lines) - len(needle) + 1):
        if lines[i : i + len(needle)] == needle:
            matches.append(i)
    if not matches:
        raise EditConflict("patch context not found")
    if len(matches) > 1:
        raise EditConflict(f"patch context is not unique: {len(matches)} matches")
    return matches[0]
