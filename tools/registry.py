"""Tool registry and execution framework."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("zariff.tools")


class PermissionLevel(Enum):
    SAFE = 1
    MODERATE = 2
    DANGEROUS = 3


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, str]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    permission: PermissionLevel = PermissionLevel.SAFE
    requires_confirmation: bool = False

    def schema_text(self) -> str:
        params = ", ".join(f"{k}: {v}" for k, v in self.parameters.items())
        confirm = " [CONFIRM]" if self.requires_confirmation else ""
        return f"- {self.name}({params}) — {self.description}{confirm}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def descriptions(self) -> str:
        return "\n".join(t.schema_text() for t in self._tools.values())

    def validate_parameters(self, tool: ToolDefinition, params: dict[str, Any]) -> str | None:
        for key in tool.parameters:
            if key not in params or params[key] in (None, ""):
                return f"Missing required parameter: {key}"
        return None


registry = ToolRegistry()


def register_tool(
    name: str,
    description: str,
    parameters: dict[str, str],
    permission: PermissionLevel = PermissionLevel.SAFE,
    requires_confirmation: bool = False,
):
    def decorator(fn: Callable[[dict[str, Any]], dict[str, Any]]):
        registry.register(
            ToolDefinition(
                name=name,
                description=description,
                parameters=parameters,
                handler=fn,
                permission=permission,
                requires_confirmation=requires_confirmation,
            )
        )
        return fn

    return decorator
