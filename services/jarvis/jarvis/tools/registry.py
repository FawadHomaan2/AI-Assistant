"""Tool registry.

Holds every tool the core knows about and reports which are usable right now.
Later phases register more; nothing else changes.
"""

from __future__ import annotations

from typing import Any

from jarvis.tools.base import Tool, ToolError
from jarvis.util.logging import get_logger

log = get_logger(__name__)


class ToolNotFound(ToolError):
    code = "jarvis.tool.not_found"
    http_status = 404


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = tool
        log.debug("registered tool", tool=name, risk=tool.spec.risk.label)

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            raise ToolNotFound(f"There is no tool called {name!r}. Available: {known}.")
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec.to_dict() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)
