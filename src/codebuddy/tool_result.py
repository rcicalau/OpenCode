from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolResult:
    tool: str
    ok: bool
    content: str = ""
    error_type: str | None = None
    retryable: bool = False
    changed_files: list[str] = field(default_factory=list)
    next_action: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_prompt(self) -> str:
        payload = {
            "tool": self.tool,
            "ok": self.ok,
            "error_type": self.error_type,
            "retryable": self.retryable,
            "changed_files": self.changed_files,
            "next_action": self.next_action,
            "metadata": self.metadata,
        }
        return "tool_result " + json.dumps(payload, sort_keys=True) + "\n" + self.content
