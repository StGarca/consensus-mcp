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
        """Return tool descriptors in MCP tools/list wire format.

        Each entry is the flat {name, description, inputSchema} shape required
        by the MCP spec. Internally-registered SCHEMA dicts use snake_case
        (input_schema, output_schema) for readability; this method translates
        to camelCase at the wire boundary. output_schema is consensus-mcp
        internal and intentionally dropped - MCP tools/list does not surface it.
        """
        result: list[dict] = []
        for name, (schema, _) in self._tools.items():
            result.append({
                "name": schema.get("name", name),
                "description": schema.get("description", ""),
                "inputSchema": schema.get("input_schema", {"type": "object"}),
            })
        return result

    def get_handler(self, name: str) -> Callable[..., Any]:
        """Return handler callable for name; raises KeyError if not registered."""
        return self._tools[name][1]
