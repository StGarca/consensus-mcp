"""A/B evaluation for advisory weights (Plan 2, converged-spec step 5).

The converged weighted-consensus spec mandates "measure the measurement": keep the
learner ONLY if it beats the equal-weight baseline. Since the sole effect of
weights is synthesis reading-ORDER, the honest metric is whether useful findings
are surfaced EARLIER under learned weights than under uniform. If weighting does
not lower the mean rank of useful findings, it adds nothing and should be reverted.

Pure functions; no I/O. The real go/revert decision runs this over a historical
corpus with real ledger-derived weights.
"""
from __future__ import annotations

from consensus_mcp import _contributor_weights as cw


def mean_rank_of_useful(findings: list[dict],
                        weights: dict[tuple[str, str], float]) -> float | None:
    """Mean 0-based rank of findings marked ``useful=True`` after advisory ordering
    by ``weights``. Lower is better (useful findings surfaced earlier). Returns
    None if no finding is marked useful."""
    ordered = cw.order_by_weight(findings, weights)
    ranks = [i for i, f in enumerate(ordered) if f.get("useful")]
    if not ranks:
        return None
    return sum(ranks) / len(ranks)


def beats_uniform(findings: list[dict],
                  weights: dict[tuple[str, str], float]) -> dict:
    """Compare learned-weight ordering vs the uniform (equal-weight) baseline.

    Returns a dict with both mean ranks and ``beats`` (True iff learned weighting
    surfaces useful findings strictly earlier). Uniform = empty weights (every cell
    neutral => order_by_weight is the identity/original order)."""
    weighted = mean_rank_of_useful(findings, weights)
    uniform = mean_rank_of_useful(findings, {})
    beats = (weighted is not None and uniform is not None and weighted < uniform)
    return {"weighted_mean_rank": weighted, "uniform_mean_rank": uniform, "beats": beats}
