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


def evaluate_history(outcomes: list[dict], host_dispositions: list[dict]) -> dict:
    """Evaluate reading order within each iteration, training on earlier ones.

    Gold and host labels have separate reports. Neither evaluation affects the
    engine; the comparison is advisory and excludes current/future labels from
    training to avoid grading a model on its own training data.
    """
    reports = {}
    for signal, labels in (
        ("gold", [o for o in outcomes if o["tier"] == "gold"]),
        ("host_disposition", [{**o, "useful": o["disposition"] == "accepted"}
                              for o in host_dispositions]),
    ):
        weighted_sum = uniform_sum = 0.0
        useful_count = 0
        for index in sorted({o["iteration_index"] for o in labels}):
            current = [o for o in labels if o["iteration_index"] == index]
            prior = [o for o in outcomes if o["iteration_index"] < index]
            prior_host = [o for o in host_dispositions if o["iteration_index"] < index]
            weights = {(o["contributor"], o["domain"]): cw.learned_weight_for(
                prior, o["contributor"], o["domain"], current_iteration=index,
                current_model_version=o["model_version"], host_dispositions=prior_host)
                for o in current}
            comparison = beats_uniform(current, weights)
            n = sum(o["useful"] for o in current)
            if n:
                weighted_sum += comparison["weighted_mean_rank"] * n
                uniform_sum += comparison["uniform_mean_rank"] * n
                useful_count += n
        reports[signal] = {
            "weighted_mean_rank": weighted_sum / useful_count if useful_count else None,
            "uniform_mean_rank": uniform_sum / useful_count if useful_count else None,
            "beats": weighted_sum < uniform_sum if useful_count else None,
            "useful_ideas": useful_count,
        }
    return {"advisory": True, **reports}
