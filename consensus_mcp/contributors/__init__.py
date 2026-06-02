"""Contributor adapter layer - pluggable AI participants for the workflow engine.

Per iter-0015 converged-plan Section C: each contributor (claude, codex,
gemini, future AIs) exposes a uniform `ContributorAdapter` interface so the
workflow engine can dispatch them generically regardless of underlying
implementation (in-process for claude, subprocess for codex/gemini).
"""
from consensus_mcp.contributors.base import (
    ContributorAdapter,
    DispatchError,
    Phase,
    PHASE_PROPOSE,
    PHASE_REVIEW,
    PHASE_CONVERGE,
    SealedArtifact,
)

__all__ = [
    "ContributorAdapter",
    "DispatchError",
    "Phase",
    "PHASE_PROPOSE",
    "PHASE_REVIEW",
    "PHASE_CONVERGE",
    "SealedArtifact",
]
