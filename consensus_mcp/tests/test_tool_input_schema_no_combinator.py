"""Guard: no MCP tool's input_schema may use a top-level oneOf/anyOf/allOf.

The Anthropic tool API rejects a top-level combinator in `input_schema`, which kills
any subagent granted that tool on launch (the v1.30.1 incident: `review.read_post_seal`
expressed "exactly one of pass_id|path" as a top-level `oneOf`). This data-driven test
enumerates EVERY tool module's SCHEMA and fails if any input_schema carries a top-level
combinator — so a newly-added tool can't reintroduce the class. (output_schema may use
oneOf; it is never sent to the API as the tool's input_schema.)
"""
from __future__ import annotations

import importlib
import pkgutil

import pytest

import consensus_mcp.tools as _tools

_COMBINATORS = {"oneOf", "anyOf", "allOf"}


def _tool_modules_with_schema():
    out = []
    for m in pkgutil.iter_modules(_tools.__path__):
        mod = importlib.import_module(f"consensus_mcp.tools.{m.name}")
        schema = getattr(mod, "SCHEMA", None)
        if isinstance(schema, dict) and isinstance(
            schema.get("input_schema") or schema.get("inputSchema"), dict
        ):
            out.append((m.name, schema))
    return out


def test_some_tools_were_discovered():
    # Guard against a vacuous pass if discovery silently finds nothing.
    assert _tool_modules_with_schema(), "no tool SCHEMAs discovered"


@pytest.mark.parametrize("name, schema",
                         _tool_modules_with_schema(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_input_schema_has_no_top_level_combinator(name, schema):
    ins = schema.get("input_schema") or schema.get("inputSchema")
    offending = _COMBINATORS & set(ins.keys())
    assert not offending, (
        f"tool {name!r} input_schema has top-level {sorted(offending)} — the Anthropic "
        f"tool API rejects it and any subagent granted this tool fails to launch. "
        f"Flatten it (optional properties + enforce the constraint in handle())."
    )
