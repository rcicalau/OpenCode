from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


ToolFunc = Callable[..., Any]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    func: ToolFunc
    enabled: bool = True


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def call(self, name: str, **kwargs: Any) -> Any:
        tool = self._tools[name]
        if not tool.enabled:
            raise ValueError(f"tool disabled: {name}")
        return tool.func(**kwargs)

    def native_schemas(self) -> list[dict[str, Any]]:
        schemas = []
        for tool in self._tools.values():
            if tool.enabled:
                schemas.append({"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.schema}})
        return schemas

    def text_instructions(self) -> str:
        lines = []
        for tool in self._tools.values():
            if tool.enabled:
                lines.append(f"- {tool.name}: {tool.description}; schema={tool.schema}")
        return "\n".join(lines)

