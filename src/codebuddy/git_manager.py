from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .command_broker import CommandBroker
from .errors import ConfirmationRequired


@dataclass(slots=True)
class GitStatus:
    is_repo: bool
    root: Path | None = None
    branch: str | None = None
    porcelain: str = ""


class GitManager:
    def __init__(
        self,
        project_root: Path,
        branch_prefix: str = "codebuddy/",
        protected_branches: list[str] | None = None,
        command_broker: CommandBroker | None = None,
        agent_branch_required: bool = True,
    ) -> None:
        self.project_root = project_root.resolve()
        self.branch_prefix = branch_prefix
        self.protected_branches = set(protected_branches or ["main", "master", "develop"])
        self.command_broker = command_broker
        self.agent_branch_required = agent_branch_required

    def status(self) -> GitStatus:
        if not self._has_project_git_metadata():
            return GitStatus(is_repo=False)
        status = self._run(["git", "status", "--porcelain=v1", "--branch"], check=False)
        if status.returncode != 0:
            return GitStatus(is_repo=False)
        lines = status.stdout.splitlines()
        branch = _branch_from_status_header(lines[0] if lines else "")
        porcelain_lines = lines[1:] if lines and lines[0].startswith("## ") else lines
        porcelain = "\n".join(porcelain_lines)
        if porcelain:
            porcelain += "\n"
        return GitStatus(
            is_repo=True,
            root=self.project_root,
            branch=branch,
            porcelain=porcelain,
        )

    def ensure_agent_branch(self, objective: str, approve_protected: bool = False) -> str | None:
        status = self.status()
        if not status.is_repo:
            return None
        branch = status.branch
        if branch and branch.startswith(self.branch_prefix):
            return branch
        if _user_dirty_porcelain(status.porcelain).strip() and not approve_protected:
            raise ConfirmationRequired("dirty worktree requires explicit approval before creating an agent branch")
        if self.agent_branch_required or branch in self.protected_branches or branch is None:
            new_branch = self.make_branch_name(objective)
            self._git(["switch", "-c", new_branch], check=True, approve=approve_protected or self.agent_branch_required)
            return new_branch
        return branch

    def make_branch_name(self, objective: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", objective.lower()).strip("-")[:48] or "work"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"{self.branch_prefix}{stamp}-{slug}"

    def diff(self) -> str:
        return self._run(["git", "diff"], check=False).stdout

    def changed_files(self) -> list[str]:
        status = self._run(["git", "status", "--porcelain"], check=False)
        files: list[str] = []
        for line in status.stdout.splitlines():
            if len(line) >= 4:
                files.append(line[3:])
        return files

    def checkpoint_commit(self, message: str, paths: list[str]) -> bool:
        status = self.status()
        if not status.is_repo:
            return False
        if not status.branch or not status.branch.startswith(self.branch_prefix):
            raise RuntimeError("checkpoint commits require an agent-owned branch")
        if not paths:
            return False
        self._git(["add", "--", *paths], check=True, approve=True)
        staged = self._git(["diff", "--cached", "--name-only"], check=True).stdout.splitlines()
        if not staged:
            return False
        self._git(["commit", "-m", message], check=True, approve=True)
        return True

    def has_remote(self, name: str = "origin") -> bool:
        status = self.status()
        if not status.is_repo:
            return False
        remote = self._git(["remote"], check=False).stdout.splitlines()
        return name in {item.strip() for item in remote}

    def push_current_branch(self, remote: str = "origin") -> bool:
        status = self.status()
        if not status.is_repo or not status.branch or not status.branch.startswith(self.branch_prefix):
            return False
        if not self.has_remote(remote):
            return False
        self._git(["push", "-u", remote, status.branch], check=True, approve=True)
        return True

    def _run(self, args: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        completed, timed_out, _duration = self._run_git_process(args)
        if check and (timed_out or completed.returncode != 0):
            raise subprocess.CalledProcessError(completed.returncode, args, completed.stdout, completed.stderr)
        return completed

    def _git(self, args: list[str], check: bool, approve: bool = False) -> subprocess.CompletedProcess[str]:
        if not self.command_broker:
            return self._run(["git", *args], check=check)
        if isinstance(self.command_broker, CommandBroker):
            return self._run_journaled_git(args, check=check)
        result = self.command_broker.run("git " + _quote_args(args), cwd=self.project_root, approve=approve)
        returncode = -1 if result.exit_code is None else result.exit_code
        completed = subprocess.CompletedProcess(["git", *args], returncode, result.stdout, result.stderr)
        if check and (result.timed_out or returncode != 0):
            raise subprocess.CalledProcessError(returncode, ["git", *args], result.stdout, result.stderr)
        return completed

    def _run_journaled_git(self, args: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        assert isinstance(self.command_broker, CommandBroker)
        command = "git " + _quote_args(args)
        analysis = self.command_broker.analyze(command)
        if self.command_broker.journal:
            self.command_broker.journal.record(
                self.command_broker.session_id,
                "command_intent",
                [],
                command=self.command_broker.redactor.redact(command),
                cwd=str(self.project_root),
                risk=analysis.risk.value,
                reasons=analysis.reasons,
            )
        completed, timed_out, duration = self._run_git_process(["git", *args])
        stdout = self.command_broker.redactor.redact(completed.stdout)
        stderr = self.command_broker.redactor.redact(completed.stderr)
        completed = subprocess.CompletedProcess(completed.args, completed.returncode, stdout, stderr)
        if self.command_broker.journal:
            self.command_broker.journal.record(
                self.command_broker.session_id,
                "command_complete",
                [],
                command=self.command_broker.redactor.redact(command),
                cwd=str(self.project_root),
                risk=analysis.risk.value,
                exit_code=completed.returncode,
                duration_seconds=duration,
                timed_out=timed_out,
                stdout=stdout,
                stderr=stderr,
                truncated=False,
            )
        if check and (timed_out or completed.returncode != 0):
            raise subprocess.CalledProcessError(completed.returncode, ["git", *args], stdout, stderr)
        return completed

    def _run_git_process(self, args: list[str]) -> tuple[subprocess.CompletedProcess[str], bool, float]:
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                args,
                cwd=str(self.project_root),
                env=self._git_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._git_timeout_seconds(),
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr) + "\ngit command timed out."
            completed = subprocess.CompletedProcess(args, -1, stdout, stderr)
        return completed, timed_out, time.monotonic() - started

    def _git_timeout_seconds(self) -> int:
        if isinstance(self.command_broker, CommandBroker):
            return self.command_broker.policy.default_timeout_seconds
        return 30

    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        ceiling = str(self.project_root.parent)
        existing = env.get("GIT_CEILING_DIRECTORIES")
        env["GIT_CEILING_DIRECTORIES"] = ceiling if not existing else existing + os.pathsep + ceiling
        return env

    def _has_project_git_metadata(self) -> bool:
        return (self.project_root / ".git").exists()


def _quote_args(args: list[str]) -> str:
    quoted = []
    for arg in args:
        if re.search(r"\s|['\"]", arg):
            quoted.append("'" + arg.replace("'", "''") + "'")
        else:
            quoted.append(arg)
    return " ".join(quoted)


def _branch_from_status_header(header: str) -> str | None:
    if not header.startswith("## "):
        return None
    value = header[3:].strip()
    if value.startswith("No commits yet on "):
        return value.removeprefix("No commits yet on ").strip() or None
    if value.startswith("HEAD "):
        return None
    branch = value.split("...", 1)[0].strip()
    return branch or None


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _user_dirty_porcelain(porcelain: str) -> str:
    lines: list[str] = []
    for line in porcelain.splitlines():
        path = line[3:] if len(line) >= 4 else line
        normalized = path.replace("\\", "/")
        if normalized == ".buddy" or normalized.startswith(".buddy/"):
            continue
        lines.append(line)
    return "\n".join(lines)
