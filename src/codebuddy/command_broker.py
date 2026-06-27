from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path

from .errors import ConfirmationRequired, DeniedByPolicy
from .journal import Journal
from .paths import PathPolicy
from .redaction import Redactor


class Risk(str, Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(slots=True)
class CommandPolicy:
    default_timeout_seconds: int = 120
    max_output_chars: int = 20000
    yolo: bool = False
    hard_deny_requires_final_approval: bool = True
    network_allowed: bool = False
    package_installs_require_confirmation: bool = True


@dataclass(slots=True)
class CommandAnalysis:
    command: str
    risk: Risk
    reasons: list[str] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CommandResult:
    command: str
    cwd: Path
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    risk: Risk
    truncated: bool = False


ALIASES = {
    "rm": "remove-item",
    "ri": "remove-item",
    "rmdir": "remove-item",
    "del": "remove-item",
    "erase": "remove-item",
    "mv": "move-item",
    "move": "move-item",
    "cp": "copy-item",
    "copy": "copy-item",
    "cat": "get-content",
    "type": "get-content",
    "ls": "get-childitem",
    "dir": "get-childitem",
    "gci": "get-childitem",
    "iwr": "invoke-webrequest",
    "wget": "invoke-webrequest",
    "curl": "invoke-webrequest",
    "iex": "invoke-expression",
    "saps": "start-process",
}

_POWERSHELL_EXECUTABLE: str | None = None

AUTO_COMMANDS = {
    "git",
    "rg",
    "grep",
    "get-childitem",
    "get-content",
    "pytest",
    "ruff",
    "mypy",
}

CONFIRM_VERBS = {
    "set-content",
    "add-content",
    "move-item",
    "copy-item",
    "new-item",
    "git checkout",
    "git switch",
    "git commit",
    "git add",
    "git clean",
    "git merge",
    "git rebase",
    "ruff format",
    "black",
    "isort",
    "pip",
    "pipx",
    "npm",
    "python -m pip",
    "set-item",
    "set-itemproperty",
}

DENY_PATTERNS = [
    ("remove-item", "file deletion requires final approval"),
    ("git reset --hard", "hard reset can discard work"),
    ("git clean -fd", "git clean can delete untracked files"),
    ("git clean -fdx", "git clean can delete ignored files"),
    ("git push --force", "force push rewrites remote history"),
    ("invoke-expression", "dynamic code execution is dangerous"),
    ("invoke-webrequest | invoke-expression", "download-and-execute pattern is dangerous"),
    ("encodedcommand", "encoded PowerShell commands are opaque"),
    ("start-process", "background process launch requires final approval"),
    ("reg ", "registry mutation is outside project scope"),
]


class CommandBroker:
    def __init__(
        self,
        project_root: Path,
        policy: CommandPolicy | None = None,
        journal: Journal | None = None,
        session_id: str = "manual",
        redactor: Redactor | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.path_policy = PathPolicy(self.project_root)
        self.policy = policy or CommandPolicy()
        self.journal = journal
        self.session_id = session_id
        self.redactor = redactor or Redactor().from_environment()

    def analyze(self, command: str) -> CommandAnalysis:
        tokens = powershell_tokens(command)
        normalized_tokens = [ALIASES.get(token.lower(), token.lower()) for token in tokens]
        normalized = " ".join(normalized_tokens)
        reasons: list[str] = []
        risk = Risk.CONFIRM

        for pattern, reason in DENY_PATTERNS:
            if pattern in normalized:
                return CommandAnalysis(command, Risk.DENY, [reason], normalized_tokens)

        if self._is_allowlisted_auto(normalized_tokens):
            risk = Risk.AUTO
            reasons.append("allowlisted read-only or validation command")

        if re.search(r">\s*[^&]", normalized) or "out-file" in normalized:
            risk = Risk.CONFIRM
            reasons.append("command redirects or writes output")

        read_path_reason = self._read_only_path_reason(normalized_tokens)
        if read_path_reason:
            risk = Risk.DENY
            reasons.append(read_path_reason)

        for phrase in CONFIRM_VERBS:
            if phrase in normalized:
                risk = Risk.CONFIRM
                reasons.append(f"mutating command pattern: {phrase}")

        if not self.policy.network_allowed and any(token in normalized_tokens for token in ("invoke-webrequest", "invoke-restmethod", "ssh", "scp")):
            risk = Risk.DENY
            reasons.append("network commands are disabled by policy")

        if "pytest" in normalized_tokens or "ruff" in normalized_tokens or "mypy" in normalized_tokens:
            if risk == Risk.AUTO:
                reasons.append("validation command")

        if risk == Risk.CONFIRM and not reasons:
            reasons.append("unknown command requires confirmation")

        return CommandAnalysis(command, risk, reasons, normalized_tokens)

    def _is_allowlisted_auto(self, tokens: list[str]) -> bool:
        if any(token in tokens for token in {"|", ";", "&"}):
            return False
        meaningful = [token for token in tokens if token not in {"|", ";", "&"}]
        if not meaningful:
            return False
        command = meaningful[0]
        if command == "git" and len(meaningful) >= 2:
            return meaningful[1] in {"status", "diff", "log", "branch", "rev-parse"}
        if command in {"rg", "grep", "get-childitem", "get-content", "pytest", "mypy"}:
            return True
        if command == "ruff" and len(meaningful) >= 2:
            return meaningful[1] == "check"
        if command in {"python", "py"}:
            module_index = _python_module_flag_index(meaningful)
            return module_index is not None and len(meaningful) > module_index + 1 and meaningful[module_index + 1] == "unittest"
        return False

    def _read_only_path_reason(self, tokens: list[str]) -> str | None:
        if not tokens:
            return None
        command = tokens[0]
        if command in {"get-content", "get-childitem"}:
            for token in self._powershell_path_args(tokens[1:]):
                reason = self._path_arg_reason(token, command)
                if reason:
                    return reason
        elif command in {"rg", "grep"}:
            for token in self._search_path_args(command, tokens[1:]):
                reason = self._path_arg_reason(token, command)
                if reason:
                    return reason
        return None

    @staticmethod
    def _powershell_path_args(tokens: list[str]) -> list[str]:
        paths: list[str] = []
        for token in tokens:
            if token in {"|", ";", "&"}:
                break
            if token.startswith("-"):
                continue
            paths.append(token)
        return paths

    @staticmethod
    def _search_path_args(command: str, tokens: list[str]) -> list[str]:
        positional: list[str] = []
        skip_next = False
        option_value_flags = {"-g", "--glob", "-t", "--type", "-T", "--type-not", "-e", "--regexp"}
        for token in tokens:
            if token in {"|", ";", "&"}:
                break
            if skip_next:
                skip_next = False
                continue
            if token in option_value_flags:
                skip_next = True
                continue
            if token.startswith("-") and token != "-":
                continue
            positional.append(token)
        if not positional:
            return []
        return positional[1:] if command in {"rg", "grep"} else positional

    def _path_arg_reason(self, token: str, command: str) -> str | None:
        if token in {"."}:
            return None
        if token.startswith("$"):
            return f"{command} dynamic path denied"
        if any(char in token for char in "*?["):
            return self._wildcard_path_reason(token, command)
        try:
            resolved = self.path_policy.resolve(token)
        except Exception:
            return f"{command} unsafe path denied"
        if not PathPolicy._under_any(resolved, [self.project_root]):
            return f"{command} outside workspace denied"
        if self.path_policy.is_sensitive(resolved):
            return f"{command} sensitive path denied"
        return None

    def _wildcard_path_reason(self, token: str, command: str) -> str | None:
        candidate = Path(token)
        parts = candidate.parts
        wildcard_index = next((index for index, part in enumerate(parts) if any(char in part for char in "*?[")), len(parts))
        base = Path(*parts[:wildcard_index]) if wildcard_index else Path(".")
        try:
            resolved_base = self.path_policy.resolve(base)
        except Exception:
            return f"{command} unsafe wildcard path denied"
        if not PathPolicy._under_any(resolved_base, [self.project_root]):
            return f"{command} wildcard outside workspace denied"
        pattern = str(candidate).replace(os.sep, "/")
        name_pattern = candidate.name
        for sensitive in self.path_policy.sensitive_patterns:
            normalized = sensitive.replace(os.sep, "/")
            if fnmatch(name_pattern, normalized) or fnmatch(pattern, normalized):
                return f"{command} sensitive wildcard denied"
        try:
            matches = list((self.project_root if not candidate.is_absolute() else Path(candidate.anchor)).glob(pattern if not candidate.is_absolute() else str(candidate.relative_to(Path(candidate.anchor)))))
        except (OSError, ValueError):
            matches = []
        for match in matches[:200]:
            try:
                if self.path_policy.is_sensitive(match):
                    return f"{command} sensitive wildcard match denied"
                if not PathPolicy._under_any(match.resolve(), [self.project_root]):
                    return f"{command} wildcard outside workspace denied"
            except Exception:
                return f"{command} unsafe wildcard match denied"
        return None

    def run(
        self,
        command: str,
        cwd: Path | None = None,
        timeout_seconds: int | None = None,
        approve: bool = False,
        final_approval: bool = False,
    ) -> CommandResult:
        cwd = (cwd or self.project_root).resolve()
        if not PathPolicy._under_any(cwd, [self.project_root]):
            raise DeniedByPolicy(f"command cwd outside workspace denied: {cwd}")
        analysis = self.analyze(command)
        if analysis.risk == Risk.CONFIRM and not (approve or self.policy.yolo):
            raise ConfirmationRequired("; ".join(analysis.reasons), analysis.risk.value)
        if analysis.risk == Risk.DENY and (not self.policy.hard_deny_requires_final_approval or not final_approval):
            raise DeniedByPolicy("; ".join(analysis.reasons))

        if self.journal:
            self.journal.record(
                self.session_id,
                "command_intent",
                [],
                command=self.redactor.redact(command),
                cwd=str(cwd),
                risk=analysis.risk.value,
                reasons=analysis.reasons,
            )

        started = time.monotonic()
        timed_out = False
        exit_code: int | None
        stdout = ""
        stderr = ""
        try:
            completed = subprocess.run(
                [
                    _powershell_executable(),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                cwd=str(cwd),
                env=self._command_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds or self.policy.default_timeout_seconds,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr) + "\nCommand timed out."
        duration = time.monotonic() - started
        stdout = self.redactor.redact(stdout)
        stderr = self.redactor.redact(stderr)
        stdout, stdout_truncated = _truncate(stdout, self.policy.max_output_chars)
        stderr, stderr_truncated = _truncate(stderr, self.policy.max_output_chars)
        result = CommandResult(
            command=command,
            cwd=cwd,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
            risk=analysis.risk,
            truncated=stdout_truncated or stderr_truncated,
        )
        if self.journal:
            self.journal.record(
                self.session_id,
                "command_complete",
                [],
                command=self.redactor.redact(command),
                cwd=str(cwd),
                risk=analysis.risk.value,
                exit_code=exit_code,
                duration_seconds=duration,
                timed_out=timed_out,
                stdout=stdout,
                stderr=stderr,
                truncated=result.truncated,
            )
        return result

    def _command_env(self) -> dict[str, str]:
        env = os.environ.copy()
        ceiling = str(self.project_root.parent)
        existing = env.get("GIT_CEILING_DIRECTORIES")
        env["GIT_CEILING_DIRECTORIES"] = ceiling if not existing else existing + os.pathsep + ceiling
        return env


def powershell_tokens(command: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "`":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char.isspace() or char in "|;&(){}":
            if current:
                tokens.append("".join(current))
                current = []
            if char in "|;&":
                tokens.append(char)
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def _powershell_executable() -> str:
    global _POWERSHELL_EXECUTABLE
    if _POWERSHELL_EXECUTABLE is None:
        _POWERSHELL_EXECUTABLE = "pwsh" if _command_exists("pwsh") else "powershell"
    return _POWERSHELL_EXECUTABLE


def _command_exists(command: str) -> bool:
    try:
        subprocess.run([command, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.Major"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    half = max_chars // 2
    return text[:half] + "\n...[truncated]...\n" + text[-half:], True


def _python_module_flag_index(tokens: list[str]) -> int | None:
    for index, token in enumerate(tokens[1:], start=1):
        if token == "-m":
            return index
        if token in {"-c", "-"}:
            return None
        if token.startswith("-") and re.fullmatch(r"-\d+(?:\.\d+)?", token):
            continue
        if token.startswith("-"):
            continue
        return None
    return None
