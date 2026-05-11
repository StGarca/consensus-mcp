"""tool_registry.py - Tool registration framework for the consensus pipeline MCP server.

Per spec docs/architecture/phase-1-completion.md
section 3 (architecture). Skeleton only; no tools registered. T2-T7 add them.
"""
from __future__ import annotations
from collections.abc import Callable
from typing import Any


class ToolRegistry:
    """Register, list, and dispatch MCP tools.

    T2-T7 call registry.register(...) at import time to add tools.
    The skeleton has an empty registry.
    """

    def __init__(self) -> None:
        self._tools: dict[str, tuple[dict, Callable[..., Any]]] = {}  # name -> (schema, handler)

    def register(self, name: str, schema: dict, handler: Callable[..., Any]) -> None:
        """Register a tool by name with its JSON schema and callable handler."""
        self._tools[name] = (schema, handler)

    def list_tools(self) -> list[dict]:
        """Return list of tool descriptors (name + schema) for MCP tools/list."""
        return [
            {"name": name, "schema": schema}
            for name, (schema, _) in self._tools.items()
        ]

    def get_handler(self, name: str) -> Callable[..., Any]:
        """Return handler callable for name; raises KeyError if not registered."""
        return self._tools[name][1]
