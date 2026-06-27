from __future__ import annotations

import ast
import json
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import PathPolicy
from .session import SessionLedger, utc_now
from .textfile import is_probably_binary_file, read_limited_text_bytes


IGNORED_PARTS = {
    ".git",
    ".pyagent",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
}

KEY_FILES = [
    "README.md",
    "README.rst",
    "README.txt",
    "AGENTS.md",
    "CLAUDE.md",
    "SPEC.md",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
]

@dataclass(slots=True)
class ProjectContext:
    text: str
    files_count: int
    key_files: list[str] = field(default_factory=list)
    symbols_count: int = 0
    module_summaries: list["ModuleSummary"] = field(default_factory=list)
    saved_map_path: Path | None = None


@dataclass(slots=True)
class ModuleSummary:
    module: str
    files_count: int
    file_types: dict[str, int]
    key_files: list[str]
    symbols: list[str]


@dataclass(slots=True)
class ProjectMemoryMetadata:
    updated_at: str
    project_root: str
    files_count: int
    key_files: list[str]
    symbols_count: int
    active_session_id: str
    objective: str | None
    pending_next_step: str | None


def bootstrap_project_memory(project_root: Path, ledger: SessionLedger, policy: PathPolicy | None = None, max_chars: int = 12000) -> ProjectContext:
    policy = policy or PathPolicy(project_root)
    context = build_project_context(project_root, policy, ledger, max_chars)
    save_project_memory(project_root, context, ledger)
    return context


def project_memory_paths(project_root: Path) -> tuple[Path, Path]:
    index_dir = project_root.resolve() / ".pyagent" / "index"
    return index_dir / "project_map.md", index_dir / "project_memory.json"


def save_project_memory(project_root: Path, context: ProjectContext, ledger: SessionLedger) -> None:
    map_path, metadata_path = project_memory_paths(project_root)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(context.text + "\n", encoding="utf-8")
    metadata = ProjectMemoryMetadata(
        updated_at=utc_now(),
        project_root=str(project_root.resolve()),
        files_count=context.files_count,
        key_files=context.key_files,
        symbols_count=context.symbols_count,
        active_session_id=ledger.session_id,
        objective=ledger.objective,
        pending_next_step=ledger.pending_next_step,
    )
    metadata_path.write_text(json.dumps(asdict(metadata), indent=2), encoding="utf-8")
    modules_path = map_path.parent / "module_summaries.json"
    modules_path.write_text(json.dumps([asdict(item) for item in context.module_summaries], indent=2), encoding="utf-8")
    context.saved_map_path = map_path


def load_project_memory(project_root: Path) -> str | None:
    map_path, _metadata_path = project_memory_paths(project_root)
    if not map_path.exists():
        return None
    return map_path.read_text(encoding="utf-8")


def build_project_context(project_root: Path, policy: PathPolicy, ledger: SessionLedger | None = None, max_chars: int = 12000) -> ProjectContext:
    root = project_root.resolve()
    files = _list_project_files(root, policy)
    key_files = _select_key_files(files)
    snippets = _read_key_file_snippets(root, policy, key_files)
    symbols = _python_symbols(root, policy, files)
    module_summaries = _module_summaries(files, symbols)

    sections = [
        "Project context",
        f"Root: {root}",
        _session_section(ledger),
        _shape_section(files),
        _module_section(module_summaries),
        _tree_section(files),
    ]
    if snippets:
        sections.append("Key files:\n" + "\n\n".join(snippets))
    if symbols:
        sections.append("Python symbols:\n" + "\n".join(symbols))

    text = "\n\n".join(section for section in sections if section.strip())
    if len(text) > max_chars:
        text = text[: max_chars - 40].rstrip() + "\n...[project context truncated]..."
    return ProjectContext(
        text=text,
        files_count=len(files),
        key_files=key_files,
        symbols_count=len(symbols),
        module_summaries=module_summaries,
    )


def _session_section(ledger: SessionLedger | None) -> str:
    if ledger is None:
        return ""
    lines = [
        "Active session:",
        f"- Session: {ledger.session_id}",
        f"- Mode: {ledger.mode}",
        f"- Objective: {ledger.objective or 'none'}",
        f"- Pending next step: {ledger.pending_next_step or 'none'}",
    ]
    if ledger.plan:
        lines.append("- Plan:")
        lines.extend(f"  - [{item.status}] {item.step}" for item in ledger.plan)
    if ledger.files_inspected:
        lines.append("- Files inspected: " + ", ".join(ledger.files_inspected[:20]))
    if ledger.files_edited:
        lines.append("- Files edited: " + ", ".join(ledger.files_edited[:20]))
    if ledger.commands_run:
        lines.append("- Commands run: " + ", ".join(ledger.commands_run[:10]))
    if ledger.blockers:
        lines.append("- Blockers: " + ", ".join(ledger.blockers[:10]))
    return "\n".join(lines)


def _list_project_files(root: Path, policy: PathPolicy, max_files: int = 500) -> list[str]:
    files = _list_with_rg(root, policy, max_files)
    if files:
        return files
    return _list_with_rglob(root, policy, max_files)


def _list_with_rg(root: Path, policy: PathPolicy, max_files: int) -> list[str]:
    try:
        completed = subprocess.run(
            [
                "rg",
                "--files",
                "--hidden",
                "--glob",
                "!.git/**",
                "--glob",
                "!.pyagent/**",
                "--glob",
                "!__pycache__/**",
                "--glob",
                "!.venv/**",
                "--glob",
                "!venv/**",
                "--glob",
                "!node_modules/**",
            ],
            cwd=str(root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode not in (0, 1):
        return []
    return _filter_files(root, policy, completed.stdout.splitlines(), max_files)


def _list_with_rglob(root: Path, policy: PathPolicy, max_files: int) -> list[str]:
    candidates: list[str] = []
    for path in root.rglob("*"):
        if len(candidates) >= max_files:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _ignored(rel):
            continue
        candidates.append(rel)
    return _filter_files(root, policy, candidates, max_files)


def _filter_files(root: Path, policy: PathPolicy, candidates: list[str], max_files: int) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for rel in candidates:
        rel = rel.replace("\\", "/").strip()
        if not rel or rel in seen or _ignored(rel):
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            if policy.is_sensitive(path):
                continue
        except Exception:
            continue
        files.append(rel)
        seen.add(rel)
        if len(files) >= max_files:
            break
    return sorted(files, key=_file_priority)


def _ignored(rel: str) -> bool:
    return any(part in IGNORED_PARTS for part in Path(rel).parts)


def _file_priority(rel: str) -> tuple[int, str]:
    name = Path(rel).name
    if name in KEY_FILES:
        return (0, rel.lower())
    if rel.startswith(("src/", "lib/", "app/", "tests/")):
        return (1, rel.lower())
    return (2, rel.lower())


def _select_key_files(files: list[str], max_files: int = 6) -> list[str]:
    by_lower = {rel.lower(): rel for rel in files}
    selected: list[str] = []
    for name in KEY_FILES:
        rel = by_lower.get(name.lower())
        if rel:
            selected.append(rel)
        if len(selected) >= max_files:
            break
    return selected


def _read_key_file_snippets(root: Path, policy: PathPolicy, key_files: list[str], max_chars_per_file: int = 2200) -> list[str]:
    snippets: list[str] = []
    for rel in key_files:
        path = root / rel
        try:
            if policy.is_sensitive(path):
                continue
            if is_probably_binary_file(path):
                continue
            raw = read_limited_text_bytes(path, max_chars_per_file)
            text = raw.decode("utf-8", errors="replace").strip()
        except OSError:
            continue
        if len(text) > max_chars_per_file:
            text = text[:max_chars_per_file].rstrip() + "\n...[truncated]..."
        snippets.append(f"{rel}:\n{text}")
    return snippets


def _shape_section(files: list[str]) -> str:
    if not files:
        return "Files: no readable project files found."
    suffixes = Counter(Path(rel).suffix.lower() or "<none>" for rel in files)
    top_suffixes = ", ".join(f"{suffix}={count}" for suffix, count in suffixes.most_common(8))
    top_dirs = Counter(Path(rel).parts[0] for rel in files if len(Path(rel).parts) > 1)
    dirs = ", ".join(f"{name}/={count}" for name, count in top_dirs.most_common(8)) or "single-folder project"
    return f"Files: {len(files)} indexed. Types: {top_suffixes}. Top folders: {dirs}."


def _tree_section(files: list[str], max_files: int = 80) -> str:
    if not files:
        return ""
    shown = files[:max_files]
    suffix = "" if len(files) <= max_files else f"\n... {len(files) - max_files} more files omitted"
    return "File map:\n" + "\n".join(f"- {rel}" for rel in shown) + suffix


def _module_summaries(files: list[str], symbols: list[str], max_modules: int = 20, max_symbols_per_module: int = 12) -> list[ModuleSummary]:
    grouped: dict[str, list[str]] = {}
    for rel in files:
        parts = Path(rel).parts
        module = parts[0] if len(parts) > 1 else "."
        grouped.setdefault(module, []).append(rel)
    symbol_groups: dict[str, list[str]] = {}
    for symbol in symbols:
        rel = symbol.removeprefix("- ").split(":", 1)[0]
        parts = Path(rel).parts
        module = parts[0] if len(parts) > 1 else "."
        symbol_groups.setdefault(module, []).append(symbol)
    summaries: list[ModuleSummary] = []
    for module, module_files in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))[:max_modules]:
        suffixes = Counter(Path(rel).suffix.lower() or "<none>" for rel in module_files)
        key_files = [rel for rel in module_files if Path(rel).name in KEY_FILES][:6]
        summaries.append(
            ModuleSummary(
                module=module,
                files_count=len(module_files),
                file_types=dict(suffixes.most_common(8)),
                key_files=key_files,
                symbols=symbol_groups.get(module, [])[:max_symbols_per_module],
            )
        )
    return summaries


def _module_section(module_summaries: list[ModuleSummary]) -> str:
    if not module_summaries:
        return ""
    lines = ["Module summaries:"]
    for item in module_summaries:
        types = ", ".join(f"{suffix}={count}" for suffix, count in item.file_types.items()) or "unknown"
        lines.append(f"- {item.module}: {item.files_count} files ({types})")
        if item.key_files:
            lines.append("  key files: " + ", ".join(item.key_files))
        if item.symbols:
            lines.append("  symbols: " + "; ".join(symbol.removeprefix("- ") for symbol in item.symbols[:5]))
    return "\n".join(lines)


def _python_symbols(root: Path, policy: PathPolicy, files: list[str], max_files: int = 30, max_symbols: int = 80) -> list[str]:
    symbols: list[str] = []
    python_files = [rel for rel in files if Path(rel).suffix == ".py"]
    for rel in python_files[:max_files]:
        path = root / rel
        try:
            if policy.is_sensitive(path):
                continue
            if path.stat().st_size > 2_000_000 or is_probably_binary_file(path):
                continue
            raw = path.read_bytes()
            tree = ast.parse(raw.decode("utf-8", errors="replace"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                symbols.append(f"- {rel}:{node.lineno} class {node.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(f"- {rel}:{node.lineno} function {node.name}")
            if len(symbols) >= max_symbols:
                return symbols
    return symbols
