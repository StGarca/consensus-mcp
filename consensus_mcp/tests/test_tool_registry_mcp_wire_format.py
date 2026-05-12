"""Asserts ToolRegistry.list_tools() returns MCP-spec-compliant tools/list entries.

Reason this test exists: a malformed list_tools() shape (nested {"name","schema"})
causes Claude Code's MCP client to silently treat the server as zero-tool. The
server handshake otherwise succeeds, so this failure mode is invisible without
an explicit shape contract. The test below pins the wire contract independent
of any caller, so future regressions surface immediately.

MCP tools/list per spec: each entry has {name, description, inputSchema} flat
at top level. inputSchema is camelCase.
"""
from __future__ import annotations

from consensus_mcp.tool_registry import ToolRegistry


def _noop(**_kwargs):
    return None


def test_list_tools_returns_mcp_wire_shape() -> None:
    """Each entry must have name + description + inputSchema flat at top level."""
    reg = ToolRegistry()
    reg.register(
        "demo.echo",
        {
            "name": "demo.echo",
            "description": "Echo input back to caller.",
            "input_schema": {
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
        },
        _noop,
    )

    [entry] = reg.list_tools()

    assert set(entry.keys()) == {"name", "description", "inputSchema"}, (
        f"MCP tools/list entry must be {{name, description, inputSchema}}; got {sorted(entry.keys())}"
    )
    assert entry["name"] == "demo.echo"
    assert entry["description"] == "Echo input back to caller."
    assert entry["inputSchema"] == {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }


def test_list_tools_does_not_nest_under_schema_key() -> None:
    """Regression guard: the old broken shape was [{'name', 'schema': {...}}]."""
    reg = ToolRegistry()
    reg.register(
        "demo.echo",
        {"name": "demo.echo", "description": "x", "input_schema": {"type": "object"}},
        _noop,
    )
    [entry] = reg.list_tools()
    assert "schema" not in entry, "list_tools() must not nest under a 'schema' key (MCP wire format)"


def test_list_tools_defaults_description_when_missing() -> None:
    """Missing description must not omit the field; empty string is the MCP-safe default."""
    reg = ToolRegistry()
    reg.register("demo.bare", {"name": "demo.bare", "input_schema": {"type": "object"}}, _noop)
    [entry] = reg.list_tools()
    assert entry["description"] == ""


def test_list_tools_defaults_inputschema_when_missing() -> None:
    """Missing input_schema must materialize as {"type": "object"} per MCP spec."""
    reg = ToolRegistry()
    reg.register("demo.bare", {"name": "demo.bare"}, _noop)
    [entry] = reg.list_tools()
    assert entry["inputSchema"] == {"type": "object"}


def test_list_tools_drops_internal_output_schema() -> None:
    """output_schema is consensus-mcp internal; MCP tools/list does not include it."""
    reg = ToolRegistry()
    reg.register(
        "demo.echo",
        {
            "name": "demo.echo",
            "description": "x",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object", "properties": {"result": {"type": "string"}}},
        },
        _noop,
    )
    [entry] = reg.list_tools()
    assert "output_schema" not in entry and "outputSchema" not in entry
