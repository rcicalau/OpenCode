from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AgentEvent:
    kind: str
    title: str
    detail: str = ""
    status: str = "done"
