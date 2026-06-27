from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .events import AgentEvent
from .redaction import Redactor
from .session import utc_now


def conversation_path(session_dir: Path) -> Path:
    return session_dir / "conversation.jsonl"


def append_turn(
    session_dir: Path,
    *,
    user: str,
    assistant: str,
    mode: str,
    events: Iterable[AgentEvent | dict[str, Any]],
    changed_files: list[str],
    redactor: Redactor | None = None,
) -> Path:
    path = conversation_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    redactor = redactor or Redactor().from_environment()
    record = {
        "type": "turn",
        "timestamp": utc_now(),
        "mode": mode,
        "user": redactor.redact(user),
        "assistant": redactor.redact(assistant),
        "changed_files": changed_files,
        "events": [_redact_event(_event_payload(event), redactor) for event in events],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def read_turns(path: Path, max_turns: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    turns: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "turn":
            turns.append(record)
    if max_turns is not None:
        return turns[-max_turns:]
    return turns


def render_conversation_history(path: Path, *, max_turns: int = 12, max_chars: int = 8000) -> str:
    turns = read_turns(path, max_turns=max_turns)
    lines = ["## Conversation History"]
    if not turns:
        lines.append("- none")
    for index, turn in enumerate(turns, start=1):
        lines.append(f"### Turn {index}")
        lines.append(f"- Mode: {turn.get('mode', 'chat')}")
        lines.append(f"- User: {_single_line(turn.get('user', ''))}")
        lines.append(f"- Assistant: {_single_line(turn.get('assistant', ''))}")
        changed = turn.get("changed_files") or []
        if changed:
            lines.append("- Changed files: " + ", ".join(str(path) for path in changed[:20]))
        events = turn.get("events") or []
        if events:
            rendered_events = []
            for event in events[:20]:
                title = event.get("title", "Tool") if isinstance(event, dict) else "Tool"
                detail = event.get("detail", "") if isinstance(event, dict) else ""
                status = event.get("status", "done") if isinstance(event, dict) else "done"
                rendered_events.append(f"{title}({status}): {_single_line(detail)}")
            lines.append("- Events: " + "; ".join(rendered_events))
    content = "\n".join(lines)
    if len(content) > max_chars:
        content = content[: max_chars - 36].rstrip() + "\n...[conversation truncated]..."
    return content


def load_session_memory(session_dir: Path, *, max_chars: int = 8000) -> str:
    compacted = session_dir / "compacted_state.md"
    if compacted.exists():
        content = compacted.read_text(encoding="utf-8")
        if len(content) > max_chars:
            content = content[: max_chars - 36].rstrip() + "\n...[session memory truncated]..."
        return "Compacted conversation memory:\n" + content
    history = render_conversation_history(conversation_path(session_dir), max_turns=8, max_chars=max_chars)
    if history.strip() == "## Conversation History\n- none":
        return ""
    return "Recent conversation memory:\n" + history


def _event_payload(event: AgentEvent | dict[str, Any]) -> dict[str, Any]:
    if isinstance(event, dict):
        return dict(event)
    try:
        return asdict(event)
    except TypeError:
        return {
            "kind": getattr(event, "kind", "tool"),
            "title": getattr(event, "title", "Tool"),
            "detail": getattr(event, "detail", ""),
            "status": getattr(event, "status", "done"),
        }


def _redact_event(event: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    return {
        key: redactor.redact(value) if isinstance(value, str) else value
        for key, value in event.items()
    }


def _single_line(value: Any, max_length: int = 1000) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = " / ".join(part.strip() for part in text.split("\n") if part.strip())
    if len(text) > max_length:
        return text[: max_length - 20].rstrip() + "...[truncated]"
    return text
