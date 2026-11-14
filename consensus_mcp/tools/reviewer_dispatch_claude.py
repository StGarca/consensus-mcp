"""MCP wrapper for the dispatchable Claude CLI reviewer."""
from __future__ import annotations

from consensus_mcp import _dispatch_claude
from consensus_mcp.tools._reviewer_dispatch_common import (
    provider_schema,
    run_provider_dispatch,
)

SCHEMA = provider_schema(
    "reviewer.dispatch_claude", "Dispatch the Claude CLI reviewer."
)


def handle(
    goal_packet_path: str,
    iteration_dir: str,
    reviewer_id: str | None = None,
    pass_id: str | None = None,
    timeout_seconds: int | None = None,
    review_target_path: str | None = None,
    phase: str | None = None,
    mode: str | None = None,
) -> dict:
    return run_provider_dispatch(
        _dispatch_claude,
        goal_packet_path=goal_packet_path,
        iteration_dir=iteration_dir,
        reviewer_id=reviewer_id,
        pass_id=pass_id,
        timeout_seconds=timeout_seconds,
        review_target_path=review_target_path,
        phase=phase,
        mode=mode,
    )


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
