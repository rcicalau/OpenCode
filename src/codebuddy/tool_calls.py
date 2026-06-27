from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .errors import CodeBuddyError


TOOL_BLOCK_RE = re.compile(r"<tool_call>\s*(?P<body>.*?)\s*</tool_call>", re.DOTALL)
REPLACE_BLOCK_RE = re.compile(
    r"<codebuddy_replace\s+path=(?P<quote>['\"])(?P<path>.*?)(?P=quote)\s*>\s*"
    r"<old>\n?(?P<old>.*?)\n?</old>\s*"
    r"<new>\n?(?P<new>.*?)\n?</new>\s*"
    r"</codebuddy_replace>",
    re.DOTALL,
)
PATCH_BLOCK_RE = re.compile(
    r"<codebuddy_patch\s+path=(?P<quote>['\"])(?P<path>.*?)(?P=quote)\s*>\n?(?P<patch>.*?)\n?</codebuddy_patch>",
    re.DOTALL,
)
REWRITE_BLOCK_RE = re.compile(
    r"<codebuddy_rewrite\s+path=(?P<quote>['\"])(?P<path>.*?)(?P=quote)"
    r"(?:\s+expected_hash=(?P<hash_quote>['\"])(?P<expected_hash>.*?)(?P=hash_quote))?\s*>\n?"
    r"(?P<content>.*?)\n?</codebuddy_rewrite>",
    re.DOTALL,
)
MALFORMED_TOOL_CALL_NAME = "__malformed_tool_call__"


@dataclass(slots=True)
class ParsedToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


def parse_text_tool_calls(text: str) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []
    for match in TOOL_BLOCK_RE.finditer(text):
        body = match.group("body")
        try:
            parsed = _loads_tool_payload(body)
        except (json.JSONDecodeError, SyntaxError, ValueError) as exc:
            raise CodeBuddyError(f"malformed tool call JSON: {exc}") from exc
        if isinstance(parsed, dict) and "name" in parsed:
            calls.append(ParsedToolCall(str(parsed["name"]), dict(parsed.get("arguments", {}))))
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and "name" in item:
                    calls.append(ParsedToolCall(str(item["name"]), dict(item.get("arguments", {}))))
    return calls


def parse_text_edit_blocks(text: str) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []
    for match in REPLACE_BLOCK_RE.finditer(text):
        calls.append(
            ParsedToolCall(
                "edit_exact_replace",
                {
                    "path": match.group("path"),
                    "old": _trim_block(match.group("old")),
                    "new": _trim_block(match.group("new")),
                },
            )
        )
    for match in PATCH_BLOCK_RE.finditer(text):
        calls.append(
            ParsedToolCall(
                "apply_unified_diff",
                {
                    "path": match.group("path"),
                    "patch": _trim_block(match.group("patch")),
                },
            )
        )
    for match in REWRITE_BLOCK_RE.finditer(text):
        arguments = {
            "path": match.group("path"),
            "content": _trim_block(match.group("content")),
        }
        expected_hash = match.group("expected_hash")
        if expected_hash:
            arguments["expected_hash"] = expected_hash
        calls.append(ParsedToolCall("rewrite_file", arguments))
    return calls


def parse_native_tool_calls(message: dict[str, Any], tolerate_malformed: bool = False) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []
    for item in message.get("tool_calls") or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not name:
            continue
        raw_arguments = function.get("arguments") or "{}"
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments, strict=False)
            except json.JSONDecodeError as exc:
                if tolerate_malformed:
                    calls.append(
                        ParsedToolCall(
                            MALFORMED_TOOL_CALL_NAME,
                            {
                                "name": str(name),
                                "error": str(exc),
                                "raw_arguments": raw_arguments,
                            },
                            str(item.get("id")) if item.get("id") else None,
                        )
                    )
                    continue
                raise CodeBuddyError(f"malformed native tool arguments for {name}: {exc}") from exc
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            arguments = {}
        calls.append(ParsedToolCall(str(name), dict(arguments), str(item.get("id")) if item.get("id") else None))
    return calls


def strip_tool_calls(text: str) -> str:
    text = TOOL_BLOCK_RE.sub("", text)
    text = REPLACE_BLOCK_RE.sub("", text)
    text = PATCH_BLOCK_RE.sub("", text)
    text = REWRITE_BLOCK_RE.sub("", text)
    return text.strip()


def _trim_block(value: str) -> str:
    if value.startswith("\r\n"):
        value = value[2:]
    elif value.startswith("\n"):
        value = value[1:]
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return value


def _loads_tool_payload(body: str):
    try:
        return json.loads(body, strict=False)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(body)
    except (SyntaxError, ValueError):
        repaired = _quote_bare_object_keys(body)
        try:
            return json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            return ast.literal_eval(repaired)


def _quote_bare_object_keys(body: str) -> str:
    return re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', body)
