from __future__ import annotations

import sys
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence


InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]
PromptFunc = Callable[[str], str]
EditorFunc = Callable[[], str]

SLASH_COMMANDS = [
    "/a",
    "/approve",
    "/approve-branch",
    "/branch",
    "/clear",
    "/compact",
    "/commit",
    "/diff",
    "/editor",
    "/exit",
    "/help",
    "/merge-ready",
    "/review",
    "/skills",
    "/steer",
    "/steer-clear",
    "/status",
    "/coding-standards",
    "/debugging",
    "/development",
    "/documentation",
    "/reasoning",
    "/test-writing",
    "/testing",
    "/undo",
    "/undo-session",
    "/yolo",
]


@dataclass(slots=True)
class ChatPrompt:
    text: str
    exit_requested: bool = False


def welcome_message(project_root: Path, session_id: str, provider: str, model: str) -> str:
    return (
        "Code Buddy\n"
        f"Project: {project_root}\n"
        f"Session: {session_id}\n"
        f"Model: {provider}/{model}\n"
        "Enter sends. Shift+Enter adds a line. Paste multiline text normally. Type /help or /skills."
    )


def help_message() -> str:
    return (
        "Input:\n"
        "  Enter         Send message.\n"
        "  Shift+Enter   Insert newline.\n"
        "  Paste         Multiline paste is inserted as-is.\n\n"
        "Commands:\n"
        "  /help          Show this help.\n"
        "  /editor        Compose the next prompt in an external editor.\n"
        "  /clear         Clear active chat context.\n"
        "  /status        Show session, git, validation, and mode.\n"
        "  /compact       Compact session state.\n"
        "  /undo          Undo the last reversible mutation.\n"
        "  /undo-session  Undo reversible mutations from this session.\n"
        "  /diff          Show git review with staged, unstaged, and untracked files.\n"
        "  /branch        Show current branch.\n"
        "  /commit MSG    Commit agent-edited files on the agent branch.\n"
        "  /skills        List project skills. Use /skill-name PROMPT to invoke one.\n"
        "  /steer TEXT    Add project-local guidance for the active loop.\n"
        "  /steer-clear   Clear active steering guidance.\n"
        "  /a, /approve  Approve pending action and continue.\n"
        "  /yolo          Toggle confirmation-skipping mode for confirm-level actions.\n"
        "  /exit          Quit."
    )


class ChatRenderer:
    def __init__(self) -> None:
        try:
            from rich.console import Console
            from rich.markdown import Markdown
            from rich.text import Text
        except ImportError:
            self.console = None
            self.markdown = None
            self.text = None
            return
        self.console = Console()
        self.markdown = Markdown
        self.text = Text

    def welcome(self, message: str) -> None:
        if not self.console:
            print(message)
            return
        lines = message.splitlines()
        if not lines:
            return
        self.console.print(self.text(lines[0], style="bold cyan"))
        for line in lines[1:]:
            label, _, value = line.partition(":")
            if value:
                self.console.print(self.text(f"{label}: ", style="dim") + self.text(value.strip(), style="white"))
            else:
                self.console.print(self.text(line, style="dim"))

    def thinking(self) -> None:
        if self.console:
            self.console.print(self.text("Thinking...", style="bold magenta"))
        else:
            print("Thinking...")

    def events(self, events: Sequence[Any]) -> None:
        for event in events:
            self.event(event)

    def event(self, event: Any) -> None:
        kind = getattr(event, "kind", "tool")
        title = getattr(event, "title", "Tool")
        detail = getattr(event, "detail", "")
        status = getattr(event, "status", "done")
        body = getattr(event, "body", "")
        style = {
            "context": "blue",
            "read": "cyan",
            "search": "cyan",
            "edit": "green",
            "shell": "yellow",
            "validate": "green" if status == "done" else "red",
            "git": "magenta",
            "model": "magenta",
        }.get(kind, "white")
        if self.console:
            prefix = self.text(f"{title:<8}", style=f"bold {style}")
            body_style = "red" if status == "failed" else "dim"
            self.console.print(prefix + self.text(str(detail), style=body_style))
            if body:
                self.console.print(str(body), style="dim")
        else:
            print(f"{title:<8}{detail}")
            if body:
                print(str(body))

    def assistant(self, message: str) -> None:
        if self.console:
            self.console.print(self.text("Buddy", style="bold cyan"))
            self.console.print(self.markdown(message or ""))
        else:
            print(message)

    def assistant_stream(self, chunks) -> str:
        collected: list[str] = []
        if self.console:
            self.console.print(self.text("Buddy", style="bold cyan"))
            for chunk in chunks:
                collected.append(str(chunk))
                self.console.print(str(chunk), end="", highlight=False, markup=False)
            self.console.print("")
        else:
            print("Buddy")
            for chunk in chunks:
                collected.append(str(chunk))
                print(str(chunk), end="", flush=True)
            print("")
        return "".join(collected)


def read_prompt(
    input_func: InputFunc = input,
    output_func: OutputFunc = print,
    prompt_func: PromptFunc | None = None,
    editor_func: EditorFunc | None = None,
) -> ChatPrompt:
    line = _read_line(input_func, prompt_func)
    stripped = line.strip()
    if stripped in {"/exit", "exit", "quit"}:
        return ChatPrompt("", True)
    if stripped == "/help":
        output_func(help_message())
        return ChatPrompt("")
    if stripped == "/editor":
        text = (editor_func or read_external_editor_prompt)().strip()
        return ChatPrompt(text)
    if stripped == "/paste":
        output_func("Paste your prompt. Finish with a line containing only /send. Use /cancel to abort.")
        lines: list[str] = []
        while True:
            part = input_func("paste> ")
            marker = part.strip()
            if marker == "/cancel":
                output_func("cancelled")
                return ChatPrompt("")
            if marker == "/send":
                return ChatPrompt("\n".join(lines).strip())
            lines.append(part)
    lines = [line]
    while lines[-1].endswith("\\"):
        lines[-1] = lines[-1][:-1]
        lines.append(input_func("... "))
    return ChatPrompt("\n".join(lines).strip())


def read_external_editor_prompt() -> str:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "notepad"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", prefix="codebuddy-prompt-", delete=False, encoding="utf-8") as handle:
        path = Path(handle.name)
        handle.write("# Write your Code Buddy prompt below. Save and close to send.\n\n")
    try:
        subprocess.run([editor, str(path)], check=False)
        text = path.read_text(encoding="utf-8")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    lines = text.splitlines()
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    return "\n".join(lines).strip()


def _read_line(input_func: InputFunc, prompt_func: PromptFunc | None) -> str:
    if prompt_func is not None:
        return prompt_func("buddy> ")
    if input_func is input and sys.stdin.isatty() and sys.stdout.isatty():
        return read_interactive_prompt("buddy> ")
    return input_func("buddy> ")


def read_interactive_prompt(message: str) -> str:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.patch_stdout import patch_stdout
    except ImportError:
        return input(message)

    completer = WordCompleter(SLASH_COMMANDS, ignore_case=True, sentence=True, match_middle=False)
    session = PromptSession(
        multiline=True,
        key_bindings=build_prompt_key_bindings(),
        completer=completer,
        complete_while_typing=True,
    )
    with patch_stdout():
        return session.prompt(message)


def build_prompt_key_bindings():
    try:
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        return SimpleNamespace(bindings=["enter", "shift-enter", "escape-enter"])

    bindings = KeyBindings()

    @bindings.add("enter")
    def _(event) -> None:
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "[", "1", "3", ";", "2", "u")
    @bindings.add("escape", "[", "1", "3", ";", "2", "~")
    def _(event) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("escape", "enter")
    def _(event) -> None:
        event.current_buffer.insert_text("\n")

    return bindings
