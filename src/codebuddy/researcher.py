from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Thread
from typing import Any

from .errors import CodeBuddyError
from .llm import LLMClient, Message


@dataclass(slots=True)
class ResearchBrief:
    summary: str = ""
    relevant_files: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    recommended_next_reads: list[str] = field(default_factory=list)
    raw: str = ""

    def to_context(self) -> str:
        lines = ["Research brief:"]
        if self.summary:
            lines.append(f"Summary: {self.summary}")
        _extend_section(lines, "Relevant files", self.relevant_files)
        _extend_section(lines, "Risks", self.risks)
        _extend_section(lines, "Unknowns", self.unknowns)
        _extend_section(lines, "Recommended next reads", self.recommended_next_reads)
        return "\n".join(lines)


class Researcher:
    """Read-only secondary-model helper for project analysis.

    The researcher never receives tools and never mutates files. Its output is
    only added as extra context for the main model, so a Qwen outage or bad JSON
    cannot block the user's task.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        timeout_seconds: float = 120,
        rate_limit_retries: int = 2,
        rate_limit_backoff_seconds: float = 1,
        max_context_chars: int = 60000,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if rate_limit_retries < 0:
            raise ValueError("rate_limit_retries must be non-negative")
        if rate_limit_backoff_seconds < 0:
            raise ValueError("rate_limit_backoff_seconds must be non-negative")
        if max_context_chars <= 0:
            raise ValueError("max_context_chars must be positive")
        self.llm = llm
        self.timeout_seconds = float(timeout_seconds)
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_backoff_seconds = float(rate_limit_backoff_seconds)
        self.max_context_chars = max_context_chars
        self.last_error: str | None = None

    def research(self, objective: str, project_context: str, mode: str) -> ResearchBrief | None:
        self.last_error = None
        messages = [
            Message(
                "system",
                "You are Code Buddy's read-only researcher. Never edit files, never run commands, "
                "and never request tools. Analyze only the supplied project context and objective. "
                "Return only a compact JSON object with these keys: summary, relevant_files, risks, "
                "unknowns, recommended_next_reads. Use string lists for every list field. Only name "
                "files that appear in the supplied project context.",
            ),
            Message(
                "user",
                "Mode: "
                + mode
                + "\nObjective:\n"
                + objective.strip()
                + "\n\nProject context:\n"
                + _clip(project_context, self.max_context_chars),
            ),
        ]
        attempts = self.rate_limit_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = self._complete_once(messages)
                return parse_research_brief(response.content)
            except Exception as exc:
                if _is_rate_limit_error(exc) and attempt < attempts:
                    delay = self.rate_limit_backoff_seconds * attempt
                    if delay:
                        time.sleep(delay)
                    continue
                self.last_error = str(exc)
                return None
        self.last_error = "research request failed after retries"
        return None

    def _complete_once(self, messages: list[Message]):
        result_queue: Queue = Queue(maxsize=1)

        def run_request() -> None:
            try:
                result_queue.put(("ok", self.llm.complete(messages, tools=None)), block=False)
            except Exception as exc:
                result_queue.put(("error", exc), block=False)

        Thread(target=run_request, name="codebuddy-researcher", daemon=True).start()
        try:
            status, payload = result_queue.get(timeout=self.timeout_seconds)
        except Empty as exc:
            raise CodeBuddyError(f"research model request timed out after {self.timeout_seconds:g}s") from exc
        if status == "ok":
            return payload
        if isinstance(payload, CodeBuddyError):
            raise payload
        raise CodeBuddyError(f"research model request failed: {payload}") from payload


def parse_research_brief(text: str) -> ResearchBrief:
    raw = text.strip()
    try:
        payload = json.loads(_extract_json_object(raw))
    except (TypeError, json.JSONDecodeError, ValueError):
        return ResearchBrief(summary=_fallback_summary(raw), raw=raw)
    if not isinstance(payload, dict):
        return ResearchBrief(summary=_fallback_summary(raw), raw=raw)
    summary = str(payload.get("summary") or "").strip() or _fallback_summary(raw)
    return ResearchBrief(
        summary=summary,
        relevant_files=_as_string_list(payload.get("relevant_files")),
        risks=_as_string_list(payload.get("risks")),
        unknowns=_as_string_list(payload.get("unknowns")),
        recommended_next_reads=_as_string_list(payload.get("recommended_next_reads")),
        raw=raw,
    )


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found")
    return stripped[start : end + 1]


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    cleaned: list[str] = []
    for item in values:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned[:50]


def _fallback_summary(text: str) -> str:
    return _clip(" ".join(text.split()), 1000)


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 80)] + "\n\n[research context truncated to configured character budget]"


def _extend_section(lines: list[str], title: str, values: list[str]) -> None:
    if not values:
        return
    lines.append(f"{title}:")
    lines.extend(f"- {value}" for value in values)


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ["429", "rate limit", "rate_limit", "too many requests", "quota exceeded", "request limit"])
