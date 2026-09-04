"""Single dispatch point for all tools across tiers.

The MCP low-level Server only allows one @list_tools and one @call_tool handler
per server, so each tier module pushes its tools into the shared registry and
the server wires the registry to the SDK once in server.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import mcp.types as types

if TYPE_CHECKING:
    from ..config import ServerConfig

ToolHandler = Callable[[dict[str, Any]], Awaitable[list[types.TextContent]]]

log = logging.getLogger("renpy_mcp.tools")

# The only two tools allowed to run while `config.is_bound()` is False —
# every other tool would otherwise either crash against a project_root
# that doesn't exist yet, or (the bug this gate exists to close) silently
# create one. Both are explicit, user-initiated ways to establish a
# project; neither runs implicitly.
_BINDING_TOOLS = frozenset({"new_project", "bind_project"})


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, config: ServerConfig | None = None) -> None:
        """``config`` is optional so unit tests that build a registry just to
        exercise one tier's handlers directly (bypassing the "no project
        bound" gate below) don't need to thread a config through. The real
        server (``server.py``) always passes one.
        """
        self._tools: dict[str, ToolDef] = {}
        self._config = config

    def add(self, tool: ToolDef) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def list(self) -> list[types.Tool]:
        return [
            types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.input_schema,
            )
            for t in self._tools.values()
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        if name not in self._tools:
            log.warning("unknown tool requested: %s", name)
            raise ValueError(f"unknown tool: {name}")
        if (
            self._config is not None
            and name not in _BINDING_TOOLS
            and not self._config.is_bound()
        ):
            log.info("tool call: %s rejected — no project bound", name)
            return _no_project_bound_error()
        log.info("tool call: %s args=%s", name, arguments)
        return await self._tools[name].handler(arguments or {})


def _no_project_bound_error() -> list[types.TextContent]:
    body = {
        "error": "no project bound",
        "hint": (
            "This server has no project bound yet — nothing has been read "
            "or written to disk. Call new_project(name=\"...\") to scaffold "
            "a new game, or bind_project(path=\"...\") to point at an "
            "existing one (its game/script.rpy must already exist)."
        ),
    }
    return [types.TextContent(type="text", text=json.dumps(body, indent=2, ensure_ascii=False))]
