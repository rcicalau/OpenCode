from __future__ import annotations

import ast
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .paths import PathPolicy
from .project_context import KEY_FILES
from .textfile import is_probably_binary_file, read_limited_text_bytes


IGNORED_PARTS = {
    ".git",
    ".buddy",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
}


@dataclass(slots=True)
class Exploration:
    text: str
    files_scanned: int
    symbols_count: int
    key_files: list[str] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    stack_signals: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PythonScan:
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)


def explore_project(
    project_root: Path,
    policy: PathPolicy,
    *,
    focus: str = "",
    max_files: int = 800,
    max_symbols: int = 160,
) -> Exploration:
    root = project_root.resolve()
    max_files = _bounded_int(max_files, default=800, minimum=1, maximum=5000)
    max_symbols = _bounded_int(max_symbols, default=160, minimum=1, maximum=1000)
    files = _list_project_files(root, policy, max_files=max_files)
    key_files = _select_key_files(files)
    python_scan = _scan_python(root, policy, files, max_symbols=max_symbols)
    relevant_files = _rank_relevant_files(root, policy, files, focus, key_files)
    stack = _stack_signals(files, python_scan)
    tests = [path for path in files if _is_test_file(path)][:30]
    config_files = [path for path in files if _is_config_file(path)][:30]
    snippets = _read_snippets(root, policy, [*key_files, *relevant_files[:4]])

    sections = [
        "Project exploration",
        f"Root: {root}",
        f"Focus: {focus.strip() or 'general project understanding'}",
        f"Files scanned: {len(files)}",
        _line_list("Stack signals", stack),
        _line_list("Key files", key_files),
        _line_list("Entrypoints", python_scan.entrypoints),
        _line_list("Tests", tests),
        _line_list("Config files", config_files),
        _module_summary(files, python_scan.symbols),
        _line_list("Relevant files", relevant_files),
        _line_list("Python symbols", python_scan.symbols[:max_symbols]),
        _line_list("Important imports", python_scan.imports[:80]),
        _snippet_section(snippets),
        _recommended_next_reads(relevant_files, key_files, python_scan.entrypoints),
    ]
    text = "\n\n".join(section for section in sections if section.strip())
    return Exploration(
        text=text,
        files_scanned=len(files),
        symbols_count=len(python_scan.symbols),
        key_files=key_files,
        relevant_files=relevant_files,
        entrypoints=python_scan.entrypoints,
        stack_signals=stack,
    )


def _list_project_files(root: Path, policy: PathPolicy, max_files: int) -> list[str]:
    files = _list_with_rg(root, policy, max_files)
    if not files:
        files = _list_with_rglob(root, policy, max_files)
    return sorted(files, key=_file_priority)


def _bounded_int(value: int, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(minimum, min(maximum, value))


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
                "!.buddy/**",
                "--glob",
                "!__pycache__/**",
                "--glob",
                "!.venv/**",
                "--glob",
                "!venv/**",
                "--glob",
                "!node_modules/**",
                "--glob",
                "!dist/**",
                "--glob",
                "!build/**",
            ],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=12,
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
        candidates.append(path.relative_to(root).as_posix())
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
            policy.ensure_read_allowed(path)
        except Exception:
            continue
        files.append(rel)
        seen.add(rel)
        if len(files) >= max_files:
            break
    return files


def _ignored(rel: str) -> bool:
    return any(part in IGNORED_PARTS for part in Path(rel).parts)


def _file_priority(rel: str) -> tuple[int, str]:
    name = Path(rel).name
    if name in KEY_FILES:
        return (0, rel.lower())
    if rel.startswith(("src/", "app/", "lib/")):
        return (1, rel.lower())
    if rel.startswith("tests/") or _is_test_file(rel):
        return (2, rel.lower())
    return (3, rel.lower())


def _select_key_files(files: list[str], limit: int = 12) -> list[str]:
    lower_to_rel = {path.lower(): path for path in files}
    selected: list[str] = []
    for name in KEY_FILES:
        path = lower_to_rel.get(name.lower())
        if path:
            selected.append(path)
        if len(selected) >= limit:
            break
    return selected


def _scan_python(root: Path, policy: PathPolicy, files: list[str], max_symbols: int) -> PythonScan:
    scan = PythonScan()
    seen_imports: set[str] = set()
    seen_frameworks: set[str] = set()
    for rel in [path for path in files if path.endswith(".py")]:
        path = root / rel
        try:
            if path.stat().st_size > 2_000_000 or is_probably_binary_file(path):
                continue
            policy.ensure_read_allowed(path)
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        if "if __name__" in text and "__main__" in text:
            scan.entrypoints.append(f"{rel}: script entrypoint")
        if "FastAPI(" in text:
            scan.entrypoints.append(f"{rel}: FastAPI app")
            seen_frameworks.add("FastAPI")
        if "Flask(" in text:
            scan.entrypoints.append(f"{rel}: Flask app")
            seen_frameworks.add("Flask")
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if len(scan.symbols) < max_symbols:
                    scan.symbols.append(f"{rel}:{node.lineno} class {node.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if len(scan.symbols) < max_symbols:
                    scan.symbols.append(f"{rel}:{node.lineno} function {node.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".", 1)[0]
                    if name not in seen_imports:
                        seen_imports.add(name)
                        scan.imports.append(f"{rel}: import {name}")
                    _record_framework(name, seen_frameworks)
            elif isinstance(node, ast.ImportFrom) and node.module:
                name = node.module.split(".", 1)[0]
                if name not in seen_imports:
                    seen_imports.add(name)
                    scan.imports.append(f"{rel}: from {name}")
                _record_framework(name, seen_frameworks)
    scan.frameworks = sorted(seen_frameworks)
    return scan


def _record_framework(name: str, seen: set[str]) -> None:
    frameworks = {
        "fastapi": "FastAPI",
        "flask": "Flask",
        "django": "Django",
        "pytest": "pytest",
        "unittest": "unittest",
        "playwright": "Playwright",
        "typer": "Typer",
        "click": "Click",
        "streamlit": "Streamlit",
    }
    label = frameworks.get(name.lower())
    if label:
        seen.add(label)


def _stack_signals(files: list[str], python_scan: PythonScan) -> list[str]:
    signals: list[str] = []
    names = {Path(path).name.lower() for path in files}
    suffixes = {Path(path).suffix.lower() for path in files}
    if ".py" in suffixes or "pyproject.toml" in names or "requirements.txt" in names:
        signals.append("Python")
    if "package.json" in names:
        signals.append("JavaScript/TypeScript")
    if "go.mod" in names:
        signals.append("Go")
    if "cargo.toml" in names:
        signals.append("Rust")
    for framework in python_scan.frameworks:
        if framework not in signals:
            signals.append(framework)
    return signals or ["unknown"]


def _rank_relevant_files(root: Path, policy: PathPolicy, files: list[str], focus: str, key_files: list[str]) -> list[str]:
    tokens = [token for token in _focus_tokens(focus) if len(token) >= 3]
    scores: dict[str, int] = {}
    for index, rel in enumerate(files):
        score = max(0, 12 - index // 20)
        lowered = rel.lower()
        score += sum(10 for token in tokens if token in lowered)
        if rel in key_files:
            score += 20
        if _is_test_file(rel):
            score += 2
        path = root / rel
        if tokens and path.suffix.lower() in {".py", ".md", ".toml", ".txt", ".yaml", ".yml", ".json"}:
            try:
                if path.stat().st_size <= 500_000 and not is_probably_binary_file(path):
                    text = read_limited_text_bytes(path, 12000).decode("utf-8", errors="replace").lower()
                    score += sum(15 for token in tokens if token in text)
            except OSError:
                pass
        if score > 0:
            scores[rel] = score
    ranked = sorted(scores, key=lambda rel: (-scores[rel], _file_priority(rel), rel.lower()))
    return ranked[:30]


def _focus_tokens(focus: str) -> list[str]:
    cleaned = "".join(char.lower() if char.isalnum() else " " for char in focus)
    stop = {"the", "and", "for", "with", "this", "that", "project", "codebase", "what", "does"}
    return [part for part in cleaned.split() if part not in stop]


def _read_snippets(root: Path, policy: PathPolicy, files: list[str]) -> dict[str, str]:
    snippets: dict[str, str] = {}
    for rel in list(dict.fromkeys(files))[:10]:
        path = root / rel
        try:
            policy.ensure_read_allowed(path)
            if is_probably_binary_file(path):
                continue
            text = read_limited_text_bytes(path, 1200).decode("utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            snippets[rel] = text[:1200].rstrip()
    return snippets


def _is_test_file(rel: str) -> bool:
    path = Path(rel)
    return "test" in path.parts or path.name.startswith("test_") or path.name.endswith("_test.py") or rel.startswith("tests/")


def _is_config_file(rel: str) -> bool:
    name = Path(rel).name.lower()
    return name in {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "requirements-dev.txt",
        "package.json",
        "tsconfig.json",
        "go.mod",
        "cargo.toml",
        "dockerfile",
        "docker-compose.yml",
    }


def _module_summary(files: list[str], symbols: list[str]) -> str:
    if not files:
        return ""
    modules: Counter[str] = Counter()
    for rel in files:
        parts = Path(rel).parts
        modules[parts[0] if len(parts) > 1 else "."] += 1
    symbol_modules: Counter[str] = Counter()
    for symbol in symbols:
        rel = symbol.split(":", 1)[0]
        parts = Path(rel).parts
        symbol_modules[parts[0] if len(parts) > 1 else "."] += 1
    lines = ["Modules"]
    for module, count in modules.most_common(12):
        symbol_count = symbol_modules.get(module, 0)
        suffix = f", {symbol_count} symbols" if symbol_count else ""
        lines.append(f"- {module}: {count} files{suffix}")
    return "\n".join(lines)


def _snippet_section(snippets: dict[str, str]) -> str:
    if not snippets:
        return ""
    lines = ["Important snippets"]
    for rel, text in snippets.items():
        lines.append(f"{rel}:\n{text}")
    return "\n\n".join(lines)


def _recommended_next_reads(relevant_files: list[str], key_files: list[str], entrypoints: list[str]) -> str:
    candidates: list[str] = []
    candidates.extend(path.split(":", 1)[0] for path in entrypoints)
    candidates.extend(relevant_files)
    candidates.extend(key_files)
    ordered = list(dict.fromkeys(candidates))[:8]
    return _line_list("Recommended next reads", ordered)


def _line_list(title: str, values: list[str]) -> str:
    if not values:
        return ""
    return title + "\n" + "\n".join(f"- {value}" for value in values)
