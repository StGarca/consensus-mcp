"""consensus.resume MCP tool - single-call operating-context snapshot.

Wraps consensus_mcp._resume.snapshot() as an MCP tool. See
docs/specs/consensus-resume-spec.md for the design.

Per spec section 6: implementation is read-only by construction. No side effects.
"""
from __future__ import annotations

from typing import Any

from consensus_mcp import _resume

SCHEMA = {
    "name": "consensus.resume",
    "description": (
        "Return a read-only operating-context snapshot for the current "
        "consensus-mcp iteration. Auto-detects the active iteration when "
        "iteration_id is not supplied. Includes goal, in-flight dispatches, "
        "open/closing/superseded reviews, closure-invariant status, and the "
        "expected next action."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration_id": {
                "type": ["string", "null"],
                "description": "Optional explicit iteration directory name; auto-detect when null.",
            },
            "include_streamed_lines": {
                "type": "boolean",
                "default": False,
            },
            "max_streamed_lines": {
                "type": "integer",
                "default": 50,
                "minimum": 0,
                "maximum": 500,
            },
            "prior_snapshot_watermark": {
                "type": ["string", "null"],
                "description": "Optional watermark from a prior snapshot; if unchanged, returns a fast-path response.",
            },
        },
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "description": "See docs/specs/consensus-resume-spec.md section 4 for the full schema.",
    },
}


def handle(
    iteration_id: str | None = None,
    include_streamed_lines: bool = False,
    max_streamed_lines: int = 50,
    prior_snapshot_watermark: str | None = None,
) -> dict[str, Any]:
    try:
        return _resume.snapshot(
            iteration_id=iteration_id,
            include_streamed_lines=include_streamed_lines,
            max_streamed_lines=max_streamed_lines,
            prior_snapshot_watermark=prior_snapshot_watermark,
        )
    except ValueError as exc:
        return {"error": "iteration_id_not_found", "message": str(exc)}


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
