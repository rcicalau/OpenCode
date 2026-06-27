from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .agent import CodeBuddyAgent
from .auth import auth_check, auth_set, auth_status
from .chat_ui import ChatRenderer, help_message, read_prompt, welcome_message
from .command_broker import CommandBroker, CommandPolicy
from .compaction import compact_ledger
from .config import load_config, redact_config
from .config import project_config_path
from .errors import CodeBuddyError, ConfigError
from .events import AgentEvent
from .edit_broker import EditBroker
from .git_manager import GitManager
from .global_state import get_last_project_root, set_last_project_root
from .journal import Journal
from .llm import FakeLLMClient, OpenAICompatibleClient
from .paths import DEFAULT_SENSITIVE_PATTERNS, PathPolicy, resolve_project_root
from .project_context import ProjectContext, bootstrap_project_memory
from .project_session import ProjectSession
from .redaction import Redactor
from .search import Searcher
from .session import SessionManager
from .slash import SlashCommandHandler, _workplan_payload
from .validation import ValidationHarness
from .workplan import WorkPlanManager


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("")
        print("Interrupted.")
        return 130
    except EOFError:
        print("")
        return 0
    except CodeBuddyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--version":
        print(__version__)
        return 0
    try:
        explicit_root = _pop_option_value(argv, "--root") or _pop_option_value(argv, "--project")
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    pick_root = _pop_flag(argv, "--pick-root")
    new_session = False
    if "--new" in argv:
        new_session = True
        argv.remove("--new")
    command_names = {"doctor", "config", "status", "compact", "undo", "chat", "auth"}
    command = argv[0] if argv and argv[0] in command_names else None
    config_subcommand = argv[1] if len(argv) > 1 and command == "config" else "show"
    auth_subcommand = argv[1] if len(argv) > 1 and command == "auth" else "status"
    auth_provider = argv[2] if len(argv) > 2 and command == "auth" else None
    prompt_args = [] if command else argv

    root = resolve_project_root(explicit_root)
    interactive_work = (command == "chat" or bool(prompt_args)) and sys.stdin.isatty()
    has_fixed_root = bool(explicit_root or os.environ.get("CODEBUDDY_PROJECT_ROOT"))
    if interactive_work and not has_fixed_root:
        root = prompt_project_root(root)
    if interactive_work:
        set_last_project_root(root)
    try:
        load_result = load_config(root)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    config = load_result.config
    if command == "chat" and sys.stdin.isatty():
        maybe_configure_project_provider(root, config)
        load_result = load_config(root)
        config = load_result.config
        maybe_prompt_for_auth(config)
    project_session = ProjectSession.open(root, new=new_session)
    manager = project_session.manager
    ledger = project_session.ledger
    session_dir = manager.session_dir(ledger.session_id)
    journal = project_session.journal

    if command == "doctor":
        return doctor(root, config)
    if command == "config":
        return config_command(config_subcommand, root, config, load_result.sources)
    if command == "auth":
        return auth_command(auth_subcommand, auth_provider, config)
    if command == "status":
        git_config = config.get("git", {})
        git_manager = GitManager(
            root,
            git_config.get("branch_prefix", "codebuddy/"),
            git_config.get("protected_branches", ["main", "master", "develop"]),
            agent_branch_required=bool(git_config.get("agent_branch_required", True)),
        )
        git_status = git_manager.status()
        workplans = WorkPlanManager(root, ledger.session_id, build_path_policy(root, config))
        plan = workplans.load_current()
        print(
            json.dumps(
                {
                    "project_root": str(root),
                    "session_id": ledger.session_id,
                    "mode": ledger.mode,
                    "objective": ledger.objective,
                    "objective_state": ledger.objective_state,
                    "pending_next_step": ledger.pending_next_step,
                    "plan": [{"step": item.step, "status": item.status} for item in ledger.plan],
                    "workplan": _workplan_payload(workplans, plan),
                    "git": {"is_repo": git_status.is_repo, "branch": git_status.branch, "dirty": bool(git_status.porcelain.strip())},
                    "validation": ledger.validation_state,
                },
                indent=2,
            )
        )
        return 0
    if command == "compact":
        content = compact_ledger(ledger, session_dir / "compacted_state.md")
        print(content)
        return 0
    if command == "undo":
        path = journal.undo_last(ledger.session_id)
        print(f"undone: {path}")
        return 0
    if command == "chat":
        startup_context = bootstrap_memory(root, ledger, config, journal)
        return chat_loop(root, ledger, config, journal, startup_context)

    prompt = " ".join(prompt_args).strip()
    if prompt:
        if prompt == "/help":
            print(help_message())
            return 0
        slash = build_slash_handler(root, ledger, manager, journal, config)
        slash_result = slash.handle(prompt)
        if slash_result.handled:
            print(slash_result.message)
            if slash_result.followup_prompt:
                bootstrap_memory(root, ledger, config, journal)
                renderer = ChatRenderer()
                try:
                    renderer.thinking()
                    result = run_prompt(root, ledger, config, journal, slash_result.followup_prompt, event_sink=renderer.event)
                except CodeBuddyError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                manager.save(ledger)
                bootstrap_memory(root, ledger, config, journal)
                renderer.assistant(result.message)
            return 0
        bootstrap_memory(root, ledger, config, journal)
        renderer = ChatRenderer()
        try:
            renderer.thinking()
            result = run_prompt(root, ledger, config, journal, prompt, event_sink=renderer.event)
        except CodeBuddyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        manager.save(ledger)
        bootstrap_memory(root, ledger, config, journal)
        renderer.assistant(result.message)
        return 0
    print("Code Buddy ready. Use a subcommand or pass a prompt.")
    return 0


def _pop_option_value(argv: list[str], name: str) -> str | None:
    if name not in argv:
        return None
    index = argv.index(name)
    try:
        value = argv[index + 1]
    except IndexError:
        raise ConfigError(f"{name} requires a folder path")
    del argv[index : index + 2]
    return value


def _pop_flag(argv: list[str], name: str) -> bool:
    if name not in argv:
        return False
    argv.remove(name)
    return True


def prompt_project_root(default_root: Path, picker=None) -> Path:
    last = get_last_project_root()
    proposed = last or default_root
    print("")
    print("Choose a project folder for Code Buddy in the folder picker.")
    print("All sessions, journals, indexes, and context for that project stay in <project>\\.pyagent.")
    if picker is None and _folder_picker_disabled_for_process():
        selected = proposed.resolve()
    else:
        selected = (picker or open_native_folder_picker)(proposed) or proposed.resolve()
    selected.mkdir(parents=True, exist_ok=True)
    set_last_project_root(selected)
    return selected


def open_native_folder_picker(initial_dir: Path) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        print(f"Folder picker unavailable: {exc}")
        return None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            title="Choose Code Buddy project folder",
            initialdir=str(initial_dir),
            mustexist=False,
        )
    finally:
        root.destroy()
    if not selected:
        return None
    return Path(selected).expanduser().resolve()


def _folder_picker_disabled_for_process() -> bool:
    return os.environ.get("CODEBUDDY_DISABLE_FOLDER_PICKER") == "1" or (
        "unittest" in sys.modules and os.environ.get("CODEBUDDY_ALLOW_TEST_FOLDER_PICKER") != "1"
    )


def maybe_configure_project_provider(root: Path, config: dict) -> None:
    target = project_config_path(root)
    if target.exists():
        return
    providers = config.get("model", {}).get("providers", {})
    default = "perplexity" if os.environ.get("PERPLEXITY_API_KEY") and not os.environ.get("OPENAI_API_KEY") else "openai"
    print("")
    print("No project config found.")
    print("Choose the LLM provider for this project.")
    print(f"Provider [{default}]: ", end="")
    answer = input().strip().lower() or default
    if answer not in providers:
        print(f"Unknown provider '{answer}', keeping default config.")
        return
    model = providers.get(answer, {}).get("model") or ("gpt-5.4" if answer == "openai" else "sonar-pro")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"[model.roles.main]\nprovider = \"{answer}\"\nmodel = \"{model}\"\n",
        encoding="utf-8",
    )
    print(f"Created {target}")


def maybe_prompt_for_auth(config: dict) -> None:
    role = config.get("model", {}).get("roles", {}).get("main", {})
    provider_name = str(role.get("provider", "openai"))
    provider = config.get("model", {}).get("providers", {}).get(provider_name, {})
    if not isinstance(provider, dict):
        return
    env_var = provider.get("api_key_env")
    if not env_var or os.environ.get(str(env_var)):
        return
    print("")
    print(f"{env_var} is not set for provider '{provider_name}'.")
    print("Save it now? [Y/n]: ", end="")
    answer = input().strip().lower()
    if answer in {"n", "no"}:
        return
    result = auth_set(config, provider_name)
    print(result.message)


def run_prompt(root: Path, ledger, config: dict, journal: Journal, prompt: str, yolo_enabled: bool | None = None, event_sink=None):
    edit_broker, command_broker = build_brokers(root, ledger.session_id, config, journal, yolo_enabled)
    llm = build_llm_client(config)
    git_config = config.get("git", {})
    agent = CodeBuddyAgent(
        root,
        ledger,
        llm,
        edit_broker,
        command_broker,
        GitManager(
            root,
            git_config.get("branch_prefix", "codebuddy/"),
            git_config.get("protected_branches", ["main", "master", "develop"]),
            command_broker,
            bool(git_config.get("agent_branch_required", True)),
        ),
        Searcher(edit_broker.policy),
        ValidationHarness(root, command_broker, config.get("validation", {}).get("commands", [])),
        config.get("tools", {}),
        model_timeout_seconds=float(config.get("model", {}).get("timeout_seconds", 75)),
    )
    return agent.handle(prompt, event_sink=event_sink)


def chat_loop(root: Path, ledger, config: dict, journal: Journal, startup_context: ProjectContext | None = None) -> int:
    manager = SessionManager(root)
    yolo_state = {"enabled": bool(config.get("commands", {}).get("yolo", False))}
    slash = build_slash_handler(root, ledger, manager, journal, config, yolo_state)
    role = config.get("model", {}).get("roles", {}).get("main", {})
    renderer = ChatRenderer()
    renderer.welcome(welcome_message(root, ledger.session_id, role.get("provider", "openai"), role.get("model", "gpt-5.4")))
    if startup_context:
        renderer.event(
            AgentEvent(
                "context",
                "Context",
                f"mapped {startup_context.files_count} files, {len(startup_context.key_files)} key files, {startup_context.symbols_count} symbols",
            )
        )
    while True:
        prompt_result = read_prompt()
        if prompt_result.exit_requested:
            manager.save(ledger)
            return 0
        prompt = prompt_result.text
        if not prompt:
            continue
        slash_result = slash.handle(prompt)
        if slash_result.handled:
            print(slash_result.message)
            if slash_result.exit_requested:
                return 0
            if not slash_result.followup_prompt:
                continue
            prompt = slash_result.followup_prompt
        try:
            renderer.thinking()
            result = run_prompt(root, ledger, config, journal, prompt, yolo_state.get("enabled", False), renderer.event)
        except CodeBuddyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            continue
        manager.save(ledger)
        bootstrap_memory(root, ledger, config, journal)
        print("")
        renderer.assistant(result.message)
        print("")


def build_llm_client(config: dict):
    fake_response = os.environ.get("CODEBUDDY_FAKE_LLM_RESPONSE")
    if fake_response is not None:
        return FakeLLMClient([fake_response])
    roles = config.get("model", {}).get("roles", {})
    main_role = roles.get("main", {})
    provider_name = main_role.get("provider", "openai")
    model = main_role.get("model", "gpt-5.4")
    provider = config.get("model", {}).get("providers", {}).get(provider_name)
    if not isinstance(provider, dict):
        raise ConfigError(f"unknown provider: {provider_name}")
    provider = dict(provider)
    provider.setdefault("timeout_seconds", config.get("model", {}).get("timeout_seconds", 75))
    return OpenAICompatibleClient.from_provider_config(provider, model)


def bootstrap_memory(root: Path, ledger, config: dict, journal: Journal) -> ProjectContext:
    context = bootstrap_project_memory(root, ledger, build_path_policy(root, config))
    journal.record(
        ledger.session_id,
        "project_context_refreshed",
        [],
        files_count=context.files_count,
        key_files=context.key_files,
        symbols_count=context.symbols_count,
        map_path=str(context.saved_map_path) if context.saved_map_path else None,
    )
    return context


def build_slash_handler(root: Path, ledger, manager: SessionManager, journal: Journal, config: dict, yolo_state: dict[str, bool] | None = None) -> SlashCommandHandler:
    _edit, command_broker = build_brokers(root, ledger.session_id, config, journal)
    git_config = config.get("git", {})
    git_manager = GitManager(
        root,
        git_config.get("branch_prefix", "codebuddy/"),
        git_config.get("protected_branches", ["main", "master", "develop"]),
        command_broker,
        bool(git_config.get("agent_branch_required", True)),
    )
    return SlashCommandHandler(root, ledger, manager, journal, git_manager, yolo_state)


def doctor(root: Path, config: dict) -> int:
    checks = {
        "python": sys.version.split()[0],
        "project_root": str(root),
        "powershell": shutil.which("pwsh") or shutil.which("powershell"),
        "git": shutil.which("git"),
        "rg": shutil.which("rg"),
    }
    providers = config.get("model", {}).get("providers", {})
    role = config.get("model", {}).get("roles", {}).get("main", {})
    provider_name = role.get("provider", "openai")
    provider = providers.get(provider_name, {})
    provider_config = provider if isinstance(provider, dict) else {}
    key_env = provider_config.get("api_key_env")
    base_url = provider_config.get("base_url") or (
        os.environ.get(str(provider_config.get("base_url_env"))) if provider_config.get("base_url_env") else None
    )
    checks["provider"] = provider_name
    checks["api_key"] = "set" if key_env and os.environ.get(str(key_env)) else f"missing {key_env}"
    checks["base_url"] = "set" if base_url else f"missing {provider_config.get('base_url_env') or 'base_url'}"
    print(json.dumps(checks, indent=2))
    return 0 if checks["powershell"] and checks["api_key"].startswith("set") and checks["base_url"].startswith("set") else 1


def auth_command(command: str, provider_name: str | None, config: dict) -> int:
    role = config.get("model", {}).get("roles", {}).get("main", {})
    provider = provider_name or str(role.get("provider", "openai"))
    try:
        if command == "status":
            result = auth_status(config, provider)
        elif command == "set":
            result = auth_set(config, provider)
        elif command == "check":
            result = auth_check(config, provider)
        else:
            print("usage: codebuddy auth [status|set|check] [provider]", file=sys.stderr)
            return 2
    except ConfigError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return 2
    print(result.message)
    return 0


def config_command(command: str, root: Path, config: dict, sources: list[Path]) -> int:
    if command == "show":
        print(json.dumps(redact_config(config), indent=2))
        return 0
    if command == "validate":
        print("config valid")
        return 0
    if command == "paths":
        print(
            json.dumps(
                {
                    "project_root": str(root),
                    "global_config": str(Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".pyagent" / "config.toml"),
                    "project_config": str(root / ".pyagent" / "config.toml"),
                    "sources": [str(source) for source in sources],
                },
                indent=2,
            )
        )
        return 0
    if command == "init":
        target = root / ".pyagent" / "config.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("# Code Buddy project config\n", encoding="utf-8")
        print(str(target))
        return 0
    return 2


def build_brokers(root: Path, session_id: str, config: dict, journal: Journal, yolo_enabled: bool | None = None) -> tuple[EditBroker, CommandBroker]:
    policy = build_path_policy(root, config)
    commands_config = config.get("commands", {})
    yolo = bool(commands_config.get("yolo", False)) if yolo_enabled is None else bool(yolo_enabled)
    return (
        EditBroker(policy, journal, session_id),
        CommandBroker(
            root,
            CommandPolicy(
                default_timeout_seconds=int(commands_config.get("default_timeout_seconds", 120)),
                max_output_chars=int(commands_config.get("max_output_chars", 20000)),
                yolo=yolo,
                hard_deny_requires_final_approval=bool(commands_config.get("hard_deny_requires_final_approval", True)),
                network_allowed=bool(commands_config.get("network_allowed", False)),
                package_installs_require_confirmation=bool(commands_config.get("package_installs_require_confirmation", True)),
            ),
            journal=journal,
            session_id=session_id,
            redactor=Redactor().from_environment(),
        ),
    )


def build_path_policy(root: Path, config: dict) -> PathPolicy:
    workspace = config.get("workspace", {})
    return PathPolicy(
        root=root,
        extra_read_roots=[Path(p) for p in workspace.get("extra_read_roots", [])],
        extra_write_roots=[Path(p) for p in workspace.get("extra_write_roots", [])],
        sensitive_patterns=list(dict.fromkeys([*DEFAULT_SENSITIVE_PATTERNS, *workspace.get("sensitive_paths", [])])),
    )
