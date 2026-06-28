from __future__ import annotations

from pathlib import Path

from .conversation import conversation_path, render_conversation_history
from .session import SessionLedger


def compact_ledger(
    ledger: SessionLedger,
    output_path: Path,
    history_path: Path | None = None,
    max_tokens: int = 4000,
) -> str:
    max_chars = max(160, max_tokens * 4)
    lines = [
        "# Compacted Session State",
        "",
        f"- Session: {ledger.session_id}",
        f"- Mode: {ledger.mode}",
        f"- Objective: {ledger.objective or 'none'}",
        f"- Pending next step: {ledger.pending_next_step or 'none'}",
        "",
        "## Plan",
    ]
    if ledger.plan:
        lines.extend(f"- [{item.status}] {item.step}" for item in ledger.plan)
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Files Inspected")
    lines.extend(f"- {path}" for path in ledger.files_inspected) or lines.append("- none")
    lines.append("")
    lines.append("## Files Edited")
    lines.extend(f"- {path}" for path in ledger.files_edited) or lines.append("- none")
    lines.append("")
    lines.append("## Commands Run")
    lines.extend(f"- {cmd}" for cmd in ledger.commands_run) or lines.append("- none")
    lines.append("")
    lines.append("## Blockers")
    lines.extend(f"- {blocker}" for blocker in ledger.blockers) or lines.append("- none")
    lines.append("")
    history_budget = max(400, max_chars // 2)
    lines.append(render_conversation_history(history_path or conversation_path(output_path.parent), max_chars=history_budget))
    content = "\n".join(lines) + "\n"
    content = _trim_to_token_budget(content, max_chars)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return content


def _trim_to_token_budget(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    marker = "\n...[compacted state truncated to token budget]...\n"
    if len(marker) >= max_chars:
        return marker[-max_chars:]
    return content[: max_chars - len(marker)].rstrip() + marker
