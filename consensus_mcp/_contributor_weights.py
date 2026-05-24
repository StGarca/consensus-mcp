"""Advisory contributor weights (static structure + firewall).

Per the weighted-consensus convergence consult (2026-05-24, sealed at
``consensus-state/active/iteration-weighted-consensus-converge-2026-05-24/converged-plan.yaml``):
weights are ADVISORY ONLY. They re-order findings for synthesis prominence; they
NEVER add/remove a finding, feed the convergence rule, or affect the cross-family
gate. This module is the static "weak-prior + caps" structure — NO learner and NO
external ledger (those are Plan 2). Discount-only: a contributor can lose
attention by being proven unreliable, but is never amplified above baseline.

Firewall invariants this module upholds (locked by tests):
  - weights-off equivalence: ``order_by_weight`` is a STABLE PERMUTATION — the set
    of findings the gate/convergence rule sees is unchanged.
  - no-self-grade: there is NO public API to write/set a contributor's weight or
    usefulness credit from caller input. A Plan-2 learner may only add a writer
    that reads the external (non-AI) ledger — never an agent-callable setter.
"""
from __future__ import annotations

NEUTRAL_MEAN = 0.5
SEED_ALPHA, SEED_BETA = 2.0, 2.0
FLOOR = 0.25
CAP = 1.0
SAME_FAMILY_AGGREGATE_CAP = 1.0


def weight_from_mean(posterior_mean: float) -> float:
    """Map a Beta posterior mean to a discount-only advisory weight in [FLOOR, CAP].

    Neutral (0.5) and above map to CAP (full, not discounted); below neutral is
    linearly discounted toward FLOOR. Never amplifies above CAP."""
    if not 0.0 <= posterior_mean <= 1.0:
        raise ValueError(f"posterior_mean must be in [0,1], got {posterior_mean}")
    raw = FLOOR + (CAP - FLOOR) * (posterior_mean / NEUTRAL_MEAN)
    return max(FLOOR, min(CAP, raw))


def seed_posterior_mean() -> float:
    """Beta(2,2) seed mean. Plan 1 has no learner, so every cell sits here."""
    return SEED_ALPHA / (SEED_ALPHA + SEED_BETA)


def weight_for(contributor: str, domain: str) -> float:
    """Advisory weight for a (contributor, domain) cell. Plan 1: always the seed
    (no learning). Plan 2 replaces the mean source with the ledger-fed posterior."""
    return weight_from_mean(seed_posterior_mean())


def order_by_weight(findings: list[dict],
                    weights: dict[tuple[str, str], float]) -> list[dict]:
    """Return findings re-ordered by descending advisory weight.

    This is the ONLY effect weights have: a STABLE permutation for synthesis
    reading-order. It never adds, removes, or mutates a finding (weights-off
    equivalence: the SET the gate and convergence rule see is unchanged). Unknown
    cells default to the neutral seed weight."""
    neutral = weight_from_mean(seed_posterior_mean())

    def key(item):
        i, f = item
        w = weights.get((f.get("contributor"), f.get("domain")), neutral)
        return (-w, i)  # higher weight first; original index = stable tie-break

    return [f for _, f in sorted(enumerate(findings), key=key)]


def apply_same_family_cap(weights_by_contributor: dict[str, float],
                          family_by_contributor: dict[str, str]) -> dict[str, float]:
    """Scale each family's contributors down proportionally so no single family's
    aggregate effective weight exceeds SAME_FAMILY_AGGREGATE_CAP. Families at or
    under the cap are untouched."""
    by_family: dict[str, list[str]] = {}
    for c, fam in family_by_contributor.items():
        by_family.setdefault(fam, []).append(c)
    out = dict(weights_by_contributor)
    for fam, contributors in by_family.items():
        total = sum(out.get(c, 0.0) for c in contributors)
        if total > SAME_FAMILY_AGGREGATE_CAP and total > 0:
            scale = SAME_FAMILY_AGGREGATE_CAP / total
            for c in contributors:
                out[c] = out.get(c, 0.0) * scale
    return out
