"""Phase → dispatcher --mode mapping (iter-0044 per iter-0043 converged plan).

Single source of truth for translating `DispatchPacket.phase` into the
`--mode` argument the shell dispatchers (`_dispatch_codex.main`,
`_dispatch_gemini.main`) accept.

Per iter-0043 weighted-synthesis convergence:
  - PHASE_PROPOSE → "proposal" (workflow #4 round-1 author task)
  - PHASE_REVIEW → "review" (workflow #3 / post-review audit task)
  - PHASE_CONVERGE → "review" (interim mapping; the dispatcher only has
    two templates today. iter-0045 candidate: empirically evaluate
    whether convergence dispatches produce review-shaped output that
    fits the synthesis task, and if not, ship a third "converge" mode.)

Strict-dict lookup with explicit ValueError on unknown phases. Per the
disconfirming-evidence rule: silent default-to-review is what allowed
the original defect (CodexAdapter not forwarding --mode at all). Never
default; raise loudly.
"""
from __future__ import annotations

from consensus_mcp.contributors.base import (
    PHASE_CONVERGE,
    PHASE_PROPOSE,
    PHASE_REVIEW,
)


_PHASE_TO_MODE: dict[str, str] = {
    PHASE_PROPOSE: "proposal",
    PHASE_REVIEW: "review",
    PHASE_CONVERGE: "review",
}


def phase_to_mode(phase: str) -> str:
    """Translate DispatchPacket.phase into dispatcher --mode argument.

    Raises:
        ValueError: when `phase` is not one of PHASE_PROPOSE, PHASE_REVIEW,
            PHASE_CONVERGE. The exception message names the phase and the
            expected set so the caller can diagnose.
    """
    try:
        return _PHASE_TO_MODE[phase]
    except KeyError as exc:
        raise ValueError(
            f"phase_to_mode: unmapped phase {phase!r}; expected one of "
            f"{sorted(_PHASE_TO_MODE.keys())}. If you added a new phase, "
            f"update consensus_mcp/contributors/_phase_mode.py and its tests."
        ) from exc
