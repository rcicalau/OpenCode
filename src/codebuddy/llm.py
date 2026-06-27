from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Protocol

from .errors import ConfigError, CodeBuddyError
from .tool_calls import ParsedToolCall, parse_native_tool_calls


@dataclass(slots=True)
class Message:
    role: str
    content: str


@dataclass(slots=True)
class LLMResponse:
    content: str
    raw: dict[str, Any] | None = None
    tool_calls: list[ParsedToolCall] | None = None


class LLMClient(Protocol):
    def complete(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        ...


class FakeLLMClient:
    def __init__(self, responses: Iterable[str | LLMResponse]) -> None:
        self._responses = iter(responses)
        self.calls: list[list[Message]] = []
        self.tool_requests: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        self.calls.append([Message(message.role, message.content) for message in messages])
        self.tool_requests.append(list(tools or []))
        response = next(self._responses)
        if isinstance(response, LLMResponse):
            return response
        return LLMResponse(response)


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        endpoint_path: str = "/chat/completions",
        timeout_seconds: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint_path = endpoint_path
        self.api_key = api_key.strip()
        self.model = model
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_provider_config(cls, provider: dict[str, Any], model: str) -> "OpenAICompatibleClient":
        api_key = provider.get("api_key")
        api_key_env = provider.get("api_key_env")
        if not api_key and api_key_env:
            api_key = os.environ.get(str(api_key_env), "").strip()
        if not api_key:
            raise ConfigError(f"missing API key; set {api_key_env or 'configured provider api_key'}")
        base_url = provider.get("base_url")
        base_url_env = provider.get("base_url_env")
        if not base_url and base_url_env:
            base_url = os.environ.get(str(base_url_env))
        if not base_url:
            raise ConfigError("missing provider base_url")
        endpoint_path = str(provider.get("endpoint_path", "/chat/completions"))
        timeout_seconds = provider.get("timeout_seconds", 60)
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ConfigError("provider timeout_seconds must be a positive number")
        return cls(
            base_url=str(base_url),
            api_key=str(api_key),
            model=str(provider.get("model", model)),
            endpoint_path=endpoint_path,
            timeout_seconds=int(timeout_seconds),
        )

    def complete(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
        }
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            self.base_url + self.endpoint_path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CodeBuddyError(f"provider HTTP error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CodeBuddyError(f"provider unavailable: {exc}") from exc
        try:
            content = raw["choices"][0]["message"]["content"]
            message = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CodeBuddyError("provider returned malformed response") from exc
        return LLMResponse(content="" if content is None else str(content), raw=raw, tool_calls=parse_native_tool_calls(message, tolerate_malformed=True))

    def stream_complete(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> Iterator[str]:
        request = self._stream_request(messages, tools)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                yield from iter_sse_content(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CodeBuddyError(f"provider HTTP error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CodeBuddyError(f"provider unavailable: {exc}") from exc

    def stream_response(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        request = self._stream_request(messages, tools)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return collect_sse_response(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CodeBuddyError(f"provider HTTP error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CodeBuddyError(f"provider unavailable: {exc}") from exc

    def _stream_request(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> urllib.request.Request:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            self.base_url + self.endpoint_path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        return request


def iter_sse_content(lines: Iterable[bytes | str]) -> Iterator[str]:
    for chunk in _iter_sse_deltas(lines):
        content = chunk.get("content")
        if content:
            yield str(content)


def collect_sse_response(lines: Iterable[bytes | str]) -> LLMResponse:
    content_parts: list[str] = []
    tool_parts: dict[int, dict[str, str]] = {}
    for delta in _iter_sse_deltas(lines):
        content = delta.get("content")
        if content:
            content_parts.append(str(content))
        for item in delta.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            index = int(item.get("index", 0))
            current = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if item.get("id"):
                current["id"] += str(item["id"])
            function = item.get("function")
            if isinstance(function, dict):
                if function.get("name"):
                    current["name"] += str(function["name"])
                if function.get("arguments"):
                    current["arguments"] += str(function["arguments"])
    tool_calls: list[ParsedToolCall] = []
    for index in sorted(tool_parts):
        item = tool_parts[index]
        if not item["name"]:
            continue
        tool_calls.extend(
            parse_native_tool_calls(
                {
                    "tool_calls": [
                        {
                            "id": item["id"] or None,
                            "type": "function",
                            "function": {"name": item["name"], "arguments": item["arguments"] or "{}"},
                        }
                    ]
                },
                tolerate_malformed=True,
            )
        )
    return LLMResponse("".join(content_parts), tool_calls=tool_calls)


def _iter_sse_deltas(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any]]:
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        delta = parsed.get("choices", [{}])[0].get("delta", {})
        if isinstance(delta, dict):
            yield delta
