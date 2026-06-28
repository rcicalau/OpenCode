from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .compaction import compact_ledger
from .git_manager import GitManager
from .journal import Journal
from .objective_state import IDLE
from .paths import PathPolicy
from .session import SessionLedger, SessionManager
from .steering import SteeringInbox
from .workplan import WorkPlanManager


@dataclass(slots=True)
class SlashResult:
    handled: bool
    exit_requested: bool = False
    message: str = ""
    followup_prompt: str | None = None


class SlashCommandHandler:
    def __init__(
        self,
        project_root: Path,
        ledger: SessionLedger,
        manager: SessionManager,
        journal: Journal,
        git_manager: GitManager,
        yolo_state: dict[str, bool] | None = None,
        compact_max_tokens: int = 4000,
    ) -> None:
        self.project_root = project_root
        self.ledger = ledger
        self.manager = manager
        self.journal = journal
        self.git_manager = git_manager
        self.yolo_state = yolo_state if yolo_state is not None else {"enabled": False}
        self.compact_max_tokens = compact_max_tokens

    def handle(self, text: str) -> SlashResult:
        stripped = text.strip()
        if _is_pending_command_approval(self.ledger, stripped):
            return self._approve_pending_command()
        if _is_pending_dirty_branch_approval(self.ledger, stripped):
            return self._approve_dirty_branch()
        if not stripped.startswith("/"):
            return SlashResult(False)
        parts = stripped.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if command == "/help":
            return SlashResult(False)
        if command == "/exit":
            self.manager.save(self.ledger)
            return SlashResult(True, True, "bye")
        if command == "/clear":
            self._clear_active_work()
            self.manager.save(self.ledger)
            return SlashResult(True, False, "cleared active context")
        if command == "/status":
            status = self.git_manager.status()
            workplans = WorkPlanManager(self.project_root, self.ledger.session_id, PathPolicy(self.project_root))
            plan = workplans.load_current()
            payload = {
                "session_id": self.ledger.session_id,
                "mode": self.ledger.mode,
                "objective": self.ledger.objective,
                "objective_state": self.ledger.objective_state,
                "plan": [{"step": item.step, "status": item.status} for item in self.ledger.plan],
                "workplan": _workplan_payload(workplans, plan),
                "git": {
                    "is_repo": status.is_repo,
                    "branch": status.branch,
                    "dirty": bool(status.porcelain.strip()),
                    "remote": _remote_payload(self.git_manager.remote_info()) if status.is_repo else None,
                },
                "validation": self.ledger.validation_state,
                "yolo": self.yolo_state.get("enabled", False),
                "steering_active": bool(SteeringInbox(self.project_root).read()),
            }
            return SlashResult(True, False, json.dumps(payload, indent=2))
        if command == "/compact":
            content = compact_ledger(
                self.ledger,
                self.manager.session_dir(self.ledger.session_id) / "compacted_state.md",
                max_tokens=self.compact_max_tokens,
            )
            return SlashResult(True, False, content)
        if command == "/undo":
            if arg == "session" or command == "/undo-session":
                paths = self.journal.undo_session(self.ledger.session_id)
                return SlashResult(True, False, "undone session:\n" + "\n".join(str(path) for path in paths))
            path = self.journal.undo_last(self.ledger.session_id)
            return SlashResult(True, False, f"undone: {path}")
        if command == "/undo-session":
            paths = self.journal.undo_session(self.ledger.session_id)
            return SlashResult(True, False, "undone session:\n" + "\n".join(str(path) for path in paths))
        if command == "/yolo":
            self.yolo_state["enabled"] = not self.yolo_state.get("enabled", False)
            if self.yolo_state["enabled"]:
                if self.ledger.pending_next_step == "approve command before execution":
                    result = self._approve_pending_command()
                    result.message = f"yolo: on; {result.message}"
                    return result
                if self.ledger.pending_next_step == "approve dirty branch before execution":
                    result = self._approve_dirty_branch()
                    result.message = f"yolo: on; {result.message}"
                    return result
            return SlashResult(True, False, f"yolo: {'on' if self.yolo_state['enabled'] else 'off'}")
        if command in {"/approve-branch", "/approve-dirty-branch", "/approve", "/a"}:
            if self.ledger.pending_next_step == "approve command before execution":
                return self._approve_pending_command()
            if self.ledger.pending_next_step == "approve dirty branch before execution":
                return self._approve_dirty_branch()
            if command in {"/approve-branch", "/approve-dirty-branch"}:
                return self._approve_dirty_branch()
            return SlashResult(True, False, "no pending approval found")
        if command == "/diff":
            status = self.git_manager.status()
            if not status.is_repo:
                return SlashResult(True, False, "not a git repository")
            return SlashResult(True, False, self.git_manager.diff() or "no diff")
        if command == "/branch":
            status = self.git_manager.status()
            if not status.is_repo:
                return SlashResult(True, False, "not a git repository")
            return SlashResult(True, False, status.branch or "detached HEAD")
        if command == "/review":
            return SlashResult(True, False, self.git_manager.diff() or "no diff to review")
        if command == "/merge-ready":
            status = self.git_manager.status()
            dirty = bool(status.porcelain.strip()) if status.is_repo else False
            validation = self.ledger.validation_state
            return SlashResult(True, False, f"merge_ready={status.is_repo and not dirty and bool(validation.get('passed', False))}")
        if command == "/commit":
            if not self.ledger.files_edited:
                return SlashResult(True, False, "no agent-edited files to commit")
            message = arg or "Code Buddy checkpoint"
            committed = self.git_manager.checkpoint_commit(message, self.ledger.files_edited)
            return SlashResult(True, False, "committed" if committed else "nothing to commit")
        if command == "/editor":
            return SlashResult(True, False, "/editor is available inside interactive chat")
        if command == "/skills":
            return SlashResult(True, False, self._skills_listing())
        if command == "/steer":
            inbox = SteeringInbox(self.project_root)
            if not arg.strip():
                current = inbox.read()
                return SlashResult(True, False, current or "no active steering")
            path = inbox.append(arg)
            return SlashResult(True, False, f"steering updated: {path.relative_to(self.project_root).as_posix()}")
        if command == "/steer-clear":
            cleared = SteeringInbox(self.project_root).clear()
            return SlashResult(True, False, "steering cleared" if cleared else "no active steering")
        skill = self._skill_for_command(command)
        if skill:
            skill_name, skill_path, content = skill
            if not arg.strip():
                return SlashResult(True, False, content)
            prompt = (
                f"Use project skill /{skill_name} from {skill_path.relative_to(self.project_root).as_posix()}.\n\n"
                f"{content.strip()}\n\n"
                "User request:\n"
                f"{arg.strip()}"
            )
            return SlashResult(True, False, f"using skill /{skill_name}", prompt)
        return SlashResult(True, False, f"unknown slash command: {command}")

    def _clear_active_work(self) -> None:
        self.ledger.mode = "chat"
        self.ledger.objective = None
        self.ledger.objective_state = IDLE
        self.ledger.plan.clear()
        self.ledger.pending_next_step = None
        self.ledger.blockers.clear()
        self.ledger.assumptions.clear()
        self.ledger.approvals.clear()
        self.ledger.validation_state.clear()
        WorkPlanManager(self.project_root, self.ledger.session_id, PathPolicy(self.project_root)).clear_current()

    def _skills_listing(self) -> str:
        skills = self._project_skills()
        if not skills:
            return "no project skills found"
        lines = ["Project skills:"]
        lines.extend(f"- /{name} ({path.relative_to(self.project_root).as_posix()})" for name, path in skills)
        return "\n".join(lines)

    def _skill_for_command(self, command: str) -> tuple[str, Path, str] | None:
        name = command.removeprefix("/").strip().lower()
        if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in name):
            return None
        path = self.project_root / ".buddy" / "skills" / f"{name}.md"
        if not path.exists() or not path.is_file():
            return None
        return name, path, path.read_text(encoding="utf-8")

    def _project_skills(self) -> list[tuple[str, Path]]:
        skills_dir = self.project_root / ".buddy" / "skills"
        if not skills_dir.exists():
            return []
        return [
            (path.stem, path)
            for path in sorted(skills_dir.glob("*.md"))
            if path.is_file() and path.stem.lower() != "readme"
        ]

    def _approve_dirty_branch(self) -> SlashResult:
        self.ledger.approvals["dirty_branch"] = True
        self.manager.save(self.ledger)
        if self.ledger.objective:
            return SlashResult(
                True,
                False,
                f"approved dirty worktree branch creation. Continuing objective: {self.ledger.objective}",
                self.ledger.objective,
            )
        return SlashResult(True, False, "approved dirty worktree branch creation for the next execute prompt.")

    def _approve_pending_command(self) -> SlashResult:
        command = str(self.ledger.approvals.pop("pending_command", ""))
        cwd_value = self.ledger.approvals.pop("pending_command_cwd", None)
        if not command:
            self.ledger.pending_next_step = None
            self.manager.save(self.ledger)
            return SlashResult(True, False, "no pending command approval found")
        cwd = Path(str(cwd_value)).expanduser().resolve() if cwd_value else self.project_root
        result = self.git_manager.command_broker.run(command, cwd=cwd, approve=True) if self.git_manager.command_broker else None
        self.ledger.pending_next_step = None
        if command not in self.ledger.commands_run:
            self.ledger.commands_run.append(command)
        self.manager.save(self.ledger)
        detail = "" if result is None else f" (exit {result.exit_code})"
        return SlashResult(True, False, f"approved command: {command}{detail}", self.ledger.objective)


def _workplan_payload(manager: WorkPlanManager, plan) -> dict | None:
    if plan is None:
        return None
    return {
        "id": plan.id,
        "kind": plan.kind,
        "objective": plan.objective,
        "summary": manager.summary(plan),
        "items": [
            {
                "id": item.id,
                "action": item.action,
                "target_path": item.target_path,
                "symbol": item.symbol,
                "status": item.status,
                "attempts": item.attempts,
                "last_error": item.last_error,
            }
            for item in plan.items
        ],
    }


def _remote_payload(info) -> dict | None:
    if info is None:
        return None
    return {
        "provider": info.provider,
        "host": info.host,
        "owner": info.owner,
        "repo": info.repo,
    }


def _is_pending_dirty_branch_approval(ledger: SessionLedger, text: str) -> bool:
    if ledger.pending_next_step != "approve dirty branch before execution":
        return False
    return text.strip().lower() in {"y", "yes", "1", "approve", "approved"}


def _is_pending_command_approval(ledger: SessionLedger, text: str) -> bool:
    if ledger.pending_next_step != "approve command before execution":
        return False
    return text.strip().lower() in {"y", "yes", "1", "approve", "approved"}
