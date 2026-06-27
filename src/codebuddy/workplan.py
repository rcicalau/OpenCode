from __future__ import annotations

import ast
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import PathPolicy
from .session import utc_now
from .textfile import is_probably_binary_file


CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".cs", ".go", ".rs", ".php", ".rb"}


@dataclass(slots=True)
class WorkItem:
    id: str
    action: str
    target_path: str
    symbol: str | None = None
    status: str = "pending"
    attempts: int = 0
    summary: str = ""
    last_error: str | None = None
    validation_passed: bool | None = None

    @property
    def label(self) -> str:
        return f"{self.action} {self.target_path}" + (f"::{self.symbol}" if self.symbol else "")


@dataclass(slots=True)
class WorkPlan:
    id: str
    session_id: str
    objective: str
    kind: str
    created_at: str
    updated_at: str
    items: list[WorkItem] = field(default_factory=list)

    def pending_items(self) -> list[WorkItem]:
        return [item for item in self.items if item.status in {"pending", "in_progress"}]

    def blocked_items(self) -> list[WorkItem]:
        return [item for item in self.items if item.status == "blocked"]

    def completed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "completed")

    def blocked_count(self) -> int:
        return sum(1 for item in self.items if item.status == "blocked")

    def next_item(self) -> WorkItem | None:
        for item in self.items:
            if item.status in {"pending", "in_progress"}:
                return item
        return None

    def touch(self) -> None:
        self.updated_at = utc_now()


class WorkPlanManager:
    def __init__(self, project_root: Path, session_id: str, policy: PathPolicy) -> None:
        self.project_root = project_root.resolve()
        self.session_id = session_id
        self.policy = policy
        self.base_dir = self.project_root / ".pyagent" / "workplans"
        self.current_path = self.base_dir / "current.json"

    def load_current(self) -> WorkPlan | None:
        if not self.current_path.exists():
            return None
        try:
            data = json.loads(self.current_path.read_text(encoding="utf-8"))
            return self._decode(data)
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    def save(self, plan: WorkPlan) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        plan.touch()
        data = asdict(plan)
        self.current_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        (self.base_dir / f"{plan.id}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    def active_or_new(self, objective: str) -> WorkPlan | None:
        existing = self.load_current()
        if existing and existing.blocked_items() and _is_retry_prompt(objective):
            _reset_blocked_items(existing)
            self.save(existing)
            return existing
        if existing and existing.blocked_items() and _same_objective(existing.objective, objective):
            _reset_blocked_items(existing)
            self.save(existing)
            return existing
        if existing and (existing.pending_items() or existing.blocked_items()) and _is_resume_prompt(objective):
            return existing
        if existing and (existing.pending_items() or existing.blocked_items()) and _same_objective(existing.objective, objective):
            return existing
        plan = self.plan_for_objective(objective)
        if plan:
            self.save(plan)
        return plan

    def plan_for_objective(self, objective: str) -> WorkPlan | None:
        lowered = objective.lower()
        if _looks_like_document_codebase(lowered):
            items = [
                WorkItem(uuid.uuid4().hex[:8], "document_file", rel)
                for rel in self._code_files()
            ]
            return self._new_plan(objective, "document_codebase", items) if items else None
        document_file = self._extract_document_file(objective)
        if document_file:
            item = WorkItem(uuid.uuid4().hex[:8], "document_file", document_file)
            return self._new_plan(objective, "document_file", [item])
        class_name = _extract_class_name(objective)
        if class_name:
            match = self._find_class(class_name)
            if match:
                path, symbol = match
                item = WorkItem(uuid.uuid4().hex[:8], "create_tests_for_class", path, symbol)
                return self._new_plan(objective, "test_class", [item])
        return None

    def item_prompt(self, plan: WorkPlan, item: WorkItem) -> str:
        if item.action == "document_file":
            return (
                f"Objective: {plan.objective}\n"
                f"Work item: document exactly one file: {item.target_path}\n"
                "Read the file if needed, then improve only this file's documentation. "
                "Prefer docstrings and useful comments; avoid noisy comments. "
                "For edits, use a raw <codebuddy_replace path=\"...\"> block with <old> and <new> sections; "
                "do not use JSON/native edit_exact_replace for multiline code. "
                "When done, validate if possible and give a concise slice summary."
            )
        if item.action == "create_tests_for_class":
            return (
                f"Objective: {plan.objective}\n"
                f"Work item: create or improve a focused test suite for class {item.symbol} in {item.target_path}.\n"
                "Read the class and nearby code first. Create or update a relevant test file under tests/. "
                "Cover important behavior and edge cases visible from the code. Validate when done."
            )
        return f"Objective: {plan.objective}\nWork item: {item.label}"

    def summary(self, plan: WorkPlan) -> str:
        total = len(plan.items)
        pending = len(plan.pending_items())
        completed = plan.completed_count()
        blocked = plan.blocked_count()
        next_item = plan.next_item()
        next_text = next_item.label if next_item else "none"
        return f"{completed}/{total} completed, {pending} pending, {blocked} blocked. Next: {next_text}"

    def _new_plan(self, objective: str, kind: str, items: list[WorkItem]) -> WorkPlan:
        return WorkPlan(
            id=uuid.uuid4().hex[:12],
            session_id=self.session_id,
            objective=objective,
            kind=kind,
            created_at=utc_now(),
            updated_at=utc_now(),
            items=items,
        )

    def _code_files(self) -> list[str]:
        result: list[str] = []
        for path in self.project_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in CODE_SUFFIXES:
                continue
            if any(part in {".git", ".pyagent", "__pycache__", ".venv", "venv", "node_modules"} for part in path.relative_to(self.project_root).parts):
                continue
            try:
                if self.policy.is_sensitive(path):
                    continue
            except Exception:
                continue
            if is_probably_binary_file(path):
                continue
            result.append(path.relative_to(self.project_root).as_posix())
        return sorted(result)

    def _find_class(self, class_name: str) -> tuple[str, str] | None:
        for rel in self._code_files():
            path = self.project_root / rel
            if path.suffix != ".py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name.lower() == class_name.lower():
                    return rel, node.name
        return None

    def _extract_document_file(self, objective: str) -> str | None:
        if "document" not in objective.lower() and "documentation" not in objective.lower():
            return None
        code_files = self._code_files()
        by_name = {Path(rel).name.lower(): rel for rel in code_files}
        by_rel = {rel.lower(): rel for rel in code_files}
        for rel_lower, rel in by_rel.items():
            if rel_lower in objective.lower().replace("\\", "/"):
                return rel
        for name, rel in by_name.items():
            if re.search(rf"\b{re.escape(name)}\b", objective, flags=re.IGNORECASE):
                return rel
        return None

    @staticmethod
    def _decode(data: dict) -> WorkPlan:
        data["items"] = [WorkItem(**item) for item in data.get("items", [])]
        return WorkPlan(**data)


def _looks_like_document_codebase(lowered: str) -> bool:
    return (
        any(phrase in lowered for phrase in ("document each file", "document every file", "document all files", "document the codebase"))
        or ("documentation" in lowered and "codebase" in lowered)
    )


def _extract_class_name(objective: str) -> str | None:
    if "test" not in objective.lower():
        return None
    match = re.search(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b", objective)
    if match:
        return match.group(1)
    match = re.search(r"\bfor\s+([A-Z][A-Za-z0-9_]*)\b", objective)
    if match:
        return match.group(1)
    return None


def _is_resume_prompt(objective: str) -> bool:
    return objective.strip().lower() in {"continue", "resume", "keep going", "next", "continue work"}


def _is_retry_prompt(objective: str) -> bool:
    return objective.strip().lower() in {"retry", "retry blocked", "try again", "retry failed", "resume blocked"}


def _same_objective(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _reset_blocked_items(plan: WorkPlan) -> None:
    for item in plan.blocked_items():
        item.status = "pending"
        item.last_error = None
