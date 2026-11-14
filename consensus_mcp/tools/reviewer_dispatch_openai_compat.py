"""MCP wrapper for an OpenAI-compatible HTTP reviewer profile."""
from __future__ import annotations

from consensus_mcp import _dispatch_openai_compat
from consensus_mcp.tools._reviewer_dispatch_common import (
    provider_schema,
    run_provider_dispatch,
)

SCHEMA = provider_schema(
    "reviewer.dispatch_openai_compat",
    "Dispatch an OpenAI-compatible HTTP reviewer.",
    profile_required=True,
)


def handle(
    goal_packet_path: str,
    iteration_dir: str,
    profile: str,
    reviewer_id: str | None = None,
    pass_id: str | None = None,
    timeout_seconds: int | None = None,
    review_target_path: str | None = None,
    phase: str | None = None,
    mode: str | None = None,
) -> dict:
    return run_provider_dispatch(
        _dispatch_openai_compat,
        profile=profile,
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
