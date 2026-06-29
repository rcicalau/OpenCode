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
TEST_PATH_PARTS = {"tests", "test", "__tests__", "spec", "specs"}


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
    recovery_history: list[dict[str, str]] = field(default_factory=list)

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
    assumptions: list[str] = field(default_factory=list)
    done_criteria: list[str] = field(default_factory=list)
    validation_strategy: list[str] = field(default_factory=list)

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
        self.base_dir = self.project_root / ".buddy" / "workplans"
        self.current_path = self.base_dir / "current.json"
        self.shelved_dir = self.base_dir / "shelved"
        self.shelved_index_path = self.base_dir / "shelved.json"
        self.active_plan_path = self.project_root / ".buddy" / "plans" / "active.json"
        self.last_shelved: dict | None = None
        self.last_resumed_shelved: dict | None = None

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
        self.active_plan_path.parent.mkdir(parents=True, exist_ok=True)
        active = dict(data)
        active["progress"] = self.progress(plan)
        active["summary"] = self.summary(plan)
        self.active_plan_path.write_text(json.dumps(active, indent=2), encoding="utf-8")

    def clear_current(self) -> None:
        for path in [self.current_path, self.active_plan_path]:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    def active_or_new(self, objective: str) -> WorkPlan | None:
        self.last_shelved = None
        self.last_resumed_shelved = None
        existing = self.load_current()
        if _is_resume_shelved_prompt(objective):
            if not self._has_shelved_objective():
                return existing if existing and _has_unfinished_items(existing) else None
            excluded_shelved_ids: set[str] = set()
            if existing and _has_unfinished_items(existing):
                self._shelve_current(existing, "shelved before resuming another objective", objective)
                if self.last_shelved and self.last_shelved.get("id"):
                    excluded_shelved_ids.add(str(self.last_shelved["id"]))
            resumed = self._resume_latest_shelved(excluded_shelved_ids)
            if resumed:
                self.save(resumed)
            return resumed
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
        if existing and _has_unfinished_items(existing):
            self._shelve_current(existing, "replaced by a new objective", objective)
        plan = self.plan_for_objective(objective)
        if plan:
            self.save(plan)
        return plan

    def _shelve_current(self, plan: WorkPlan, reason: str, replacement_objective: str) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.shelved_dir.mkdir(parents=True, exist_ok=True)
        shelved_id = uuid.uuid4().hex[:12]
        snapshot = asdict(plan)
        snapshot_path = self.shelved_dir / f"{shelved_id}.json"
        snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        record = {
            "id": shelved_id,
            "status": "shelved",
            "shelved_at": utc_now(),
            "session_id": self.session_id,
            "plan_id": plan.id,
            "objective": plan.objective,
            "kind": plan.kind,
            "reason": reason,
            "replacement_objective": replacement_objective,
            "summary": self.summary(plan),
            "progress": self.progress(plan),
            "snapshot": f"shelved/{shelved_id}.json",
            "completed_items": [item.label for item in plan.items if item.status == "completed"],
            "pending_items": [item.label for item in plan.pending_items()],
            "blocked_items": [f"{item.label}: {item.last_error or 'blocked'}" for item in plan.blocked_items()],
        }
        records = self._load_shelved_index()
        records.append(record)
        self._write_shelved_index(records)
        self.last_shelved = record
        self.clear_current()

    def _resume_latest_shelved(self, excluded_ids: set[str] | None = None) -> WorkPlan | None:
        excluded_ids = excluded_ids or set()
        records = self._load_shelved_index()
        for index in range(len(records) - 1, -1, -1):
            record = records[index]
            if record.get("status") != "shelved":
                continue
            if str(record.get("id", "")) in excluded_ids:
                continue
            objective = str(record.get("objective") or "").strip()
            plan = self.plan_for_objective(objective) or self._plan_from_shelved_snapshot(record)
            if not plan:
                continue
            record = dict(record)
            record["status"] = "resumed"
            record["resumed_at"] = utc_now()
            record["resumed_session_id"] = self.session_id
            records[index] = record
            self._write_shelved_index(records)
            self.last_resumed_shelved = record
            plan.assumptions.append(f"Resumed shelved objective from {record.get('shelved_at', 'unknown time')}: {record.get('summary', '')}")
            return plan
        return None

    def _plan_from_shelved_snapshot(self, record: dict) -> WorkPlan | None:
        snapshot = record.get("snapshot")
        if not isinstance(snapshot, str):
            return None
        path = self.base_dir / snapshot
        try:
            plan = self._decode(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            return None
        plan.id = uuid.uuid4().hex[:12]
        plan.session_id = self.session_id
        plan.created_at = utc_now()
        plan.updated_at = utc_now()
        for item in plan.items:
            if item.status != "completed":
                item.status = "pending"
            item.attempts = 0
            item.last_error = None
            item.validation_passed = None
        return plan

    def _load_shelved_index(self) -> list[dict]:
        if not self.shelved_index_path.exists():
            return []
        try:
            data = json.loads(self.shelved_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _write_shelved_index(self, records: list[dict]) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.shelved_index_path.write_text(json.dumps(records, indent=2), encoding="utf-8")

    def _has_shelved_objective(self) -> bool:
        return any(record.get("status") == "shelved" for record in self._load_shelved_index())

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
        if _looks_like_test_suite(lowered):
            items = [
                WorkItem(uuid.uuid4().hex[:8], "create_tests_for_file", rel)
                for rel in self._source_files_for_test_objective(objective)
            ]
            return self._new_plan(objective, "test_project", items) if items else None
        return None

    def item_prompt(self, plan: WorkPlan, item: WorkItem) -> str:
        retry_note = _retry_note(item)
        if item.action == "document_file":
            return (
                f"Objective: {plan.objective}\n"
                f"Work item: document exactly one file: {item.target_path}\n"
                f"Done criteria: {'; '.join(plan.done_criteria)}\n"
                f"Validation strategy: {'; '.join(plan.validation_strategy)}\n"
                f"{self._skill_guidance('documentation', 'coding-standards')}\n"
                f"{retry_note}"
                "Read the file if needed, then improve only this file's documentation. "
                "Use Google style docstrings. Prefer docstrings and useful comments; avoid noisy comments. "
                "For edits, use a raw <codebuddy_replace path=\"...\"> block with <old> and <new> sections; "
                "do not use JSON/native edit_exact_replace for multiline code. "
                "When done, validate if possible and give a concise slice summary."
            )
        if item.action == "create_tests_for_class":
            return (
                f"Objective: {plan.objective}\n"
                f"Work item: create or improve a focused test suite for class {item.symbol} in {item.target_path}.\n"
                f"Done criteria: {'; '.join(plan.done_criteria)}\n"
                f"Validation strategy: {'; '.join(plan.validation_strategy)}\n"
                f"{self._skill_guidance('test-writing', 'coding-standards')}\n"
                f"{retry_note}"
                "Read the class and nearby code first. Create or update a relevant test file under tests/. "
                "Cover important behavior and edge cases visible from the code. Validate when done."
            )
        if item.action == "create_tests_for_file":
            return (
                f"Objective: {plan.objective}\n"
                f"Work item: create or improve tests for source file {item.target_path}.\n"
                f"Done criteria: {'; '.join(plan.done_criteria)}\n"
                f"Validation strategy: {'; '.join(plan.validation_strategy)}\n"
                f"{self._skill_guidance('test-writing', 'coding-standards')}\n"
                f"{retry_note}"
                "Read the source file and existing tests first. Create or update a relevant test file under tests/. "
                "Follow the project's existing test style, prefer narrow tests for visible behavior, "
                "and do not edit production code unless the validation failure proves a real bug. Validate when done."
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

    def progress(self, plan: WorkPlan) -> dict[str, int]:
        return {
            "total": len(plan.items),
            "completed": plan.completed_count(),
            "pending": len(plan.pending_items()),
            "blocked": plan.blocked_count(),
        }

    def _new_plan(self, objective: str, kind: str, items: list[WorkItem]) -> WorkPlan:
        assumptions, done_criteria, validation_strategy = _contract_for_kind(kind)
        return WorkPlan(
            id=uuid.uuid4().hex[:12],
            session_id=self.session_id,
            objective=objective,
            kind=kind,
            created_at=utc_now(),
            updated_at=utc_now(),
            items=items,
            assumptions=assumptions,
            done_criteria=done_criteria,
            validation_strategy=validation_strategy,
        )

    def _code_files(self) -> list[str]:
        result: list[str] = []
        for path in self.project_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in CODE_SUFFIXES:
                continue
            if any(part in {".git", ".buddy", "__pycache__", ".venv", "venv", "node_modules"} for part in path.relative_to(self.project_root).parts):
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

    def _python_source_files(self) -> list[str]:
        return [
            rel
            for rel in self._code_files()
            if Path(rel).suffix == ".py" and not _is_test_path(rel)
        ]

    def _source_files_for_test_objective(self, objective: str) -> list[str]:
        target = self._extract_test_target_file(objective)
        if target:
            return [target]
        lowered = objective.lower().replace("\\", "/")
        source_files = self._python_source_files()
        directory_matches = [
            rel
            for rel in source_files
            if any(part.lower() in lowered for part in Path(rel).parts[:-1])
        ]
        return sorted(directory_matches or source_files)

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
        return _extract_file_reference(objective, code_files)

    def _extract_test_target_file(self, objective: str) -> str | None:
        if "test" not in objective.lower() and "coverage" not in objective.lower():
            return None
        return _extract_file_reference(objective, self._python_source_files())

    def _skill_guidance(self, *names: str, max_chars_per_file: int = 2200) -> str:
        chunks: list[str] = []
        for name in names:
            path = self.project_root / ".buddy" / "skills" / f"{name}.md"
            if not path.exists() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if len(text) > max_chars_per_file:
                text = text[:max_chars_per_file].rstrip() + "\n...[truncated]..."
            chunks.append(f"/{name}:\n{text}")
        if not chunks:
            return ""
        return "Skill guidance:\n" + "\n\n".join(chunks) + "\n\n"

    @staticmethod
    def _decode(data: dict) -> WorkPlan:
        data["items"] = [WorkItem(**item) for item in data.get("items", [])]
        return WorkPlan(**data)


def _extract_file_reference(objective: str, code_files: list[str]) -> str | None:
    lowered = objective.lower().replace("\\", "/")
    by_name = {Path(rel).name.lower(): rel for rel in code_files}
    by_rel = {rel.lower(): rel for rel in code_files}
    for rel_lower, rel in by_rel.items():
        if rel_lower in lowered:
            return rel
    for name, rel in by_name.items():
        if re.search(rf"\b{re.escape(name)}\b", objective, flags=re.IGNORECASE):
            return rel
    for rel in code_files:
        stem = Path(rel).stem
        if re.search(rf"\b{re.escape(stem)}\b", objective, flags=re.IGNORECASE):
            return rel
    return None


def _looks_like_document_codebase(lowered: str) -> bool:
    return (
        any(phrase in lowered for phrase in ("document each file", "document every file", "document all files", "document the codebase"))
        or ("documentation" in lowered and "codebase" in lowered)
    )


def _looks_like_test_suite(lowered: str) -> bool:
    return ("test" in lowered or "coverage" in lowered) and any(
        phrase in lowered
        for phrase in (
            "full suite",
            "test suite",
            "tests for",
            "create tests",
            "write tests",
            "add tests",
            "coverage",
        )
    )


def _contract_for_kind(kind: str) -> tuple[list[str], list[str], list[str]]:
    assumptions = [
        "Work stays inside the selected project root.",
        "Existing user changes are preserved unless the objective explicitly asks to modify them.",
    ]
    done_criteria = [
        "All planned work items are completed or explicitly blocked with a reason.",
        "No unexpected files are changed outside the planned scope.",
    ]
    validation_strategy = [
        "Run syntax checks for touched Python files when present.",
        "Run configured validation commands after each completed slice.",
        "Record validation failures in the active plan before stopping.",
    ]
    if kind in {"document_codebase", "document_file"}:
        done_criteria.append("Target files contain useful docstrings or comments without noisy restatement.")
    if kind in {"test_class", "test_project"}:
        done_criteria.append("A focused test file exists under tests/ or an existing relevant test file is improved.")
        validation_strategy.append("Prefer the narrowest relevant test command before broader test runs.")
    return assumptions, done_criteria, validation_strategy


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


def _is_resume_shelved_prompt(objective: str) -> bool:
    return objective.strip().lower() in {"resume shelved", "continue shelved", "resume previous", "continue previous objective"}


def _is_retry_prompt(objective: str) -> bool:
    return objective.strip().lower() in {"retry", "retry blocked", "try again", "retry failed", "resume blocked"}


def _same_objective(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _has_unfinished_items(plan: WorkPlan) -> bool:
    return bool(plan.pending_items() or plan.blocked_items())


def _reset_blocked_items(plan: WorkPlan) -> None:
    for item in plan.blocked_items():
        item.status = "pending"
        item.last_error = None


def _retry_note(item: WorkItem) -> str:
    if not item.last_error:
        return ""
    return (
        f"Previous attempt failed: {item.last_error}\n"
        "Recover by inspecting the failing file/output, changing strategy, and validating again. "
        "Do not repeat the same failed edit.\n\n"
    )


def _is_test_path(rel: str) -> bool:
    path = Path(rel)
    lowered_parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    return bool(lowered_parts & TEST_PATH_PARTS) or name.startswith("test_") or name.endswith("_test.py")
