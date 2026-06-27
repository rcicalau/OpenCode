from __future__ import annotations

import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Thread
from typing import Any


@dataclass(slots=True)
class ReplayOutcome:
    message: str = ""
    mode: str | None = None
    changed_files: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    objective_state: str | None = None
    timed_out: bool = False
    crashed: bool = False
    error: str | None = None
    duration_seconds: float = 0.0

    @property
    def clean_exit(self) -> bool:
        return not self.timed_out and not self.crashed


def run_agent_replay(agent: Any, prompt: str, timeout_seconds: float = 30) -> ReplayOutcome:
    started = time.monotonic()
    queue: Queue = Queue(maxsize=1)

    def worker() -> None:
        try:
            queue.put(("ok", agent.handle(prompt)), block=False)
        except Exception as exc:
            queue.put(("error", exc), block=False)

    Thread(target=worker, name="codebuddy-replay", daemon=True).start()
    try:
        status, payload = queue.get(timeout=timeout_seconds)
    except Empty:
        return ReplayOutcome(timed_out=True, duration_seconds=time.monotonic() - started)
    duration = time.monotonic() - started
    if status == "error":
        return ReplayOutcome(crashed=True, error=str(payload), duration_seconds=duration)
    ledger = getattr(agent, "ledger", None)
    return ReplayOutcome(
        message=getattr(payload, "message", ""),
        mode=getattr(payload, "mode", None),
        changed_files=list(getattr(payload, "changed_files", []) or []),
        events=[getattr(event, "title", str(event)) for event in getattr(payload, "events", [])],
        objective_state=getattr(ledger, "objective_state", None),
        duration_seconds=duration,
    )
