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


# --- Plan 2: the learner (reads the external outcome ledger; ADVISORY only) ---
# Per the converged spec (2026-05-24): credit comes ONLY from external outcomes
# (see _outcome_ledger; no AI writes credit). GOLD (objective test/falsification)
# outranks SECONDARY (audited operator disposition). Decay half-life 20, hard
# model-version reset, min-sample 5 with linear dampening toward the neutral seed.

SIGNAL_STRENGTH = {"gold": 1.0, "secondary": 0.5}
DECAY_HALFLIFE = 20.0
MIN_SAMPLE = 5.0


def learned_posterior_mean(outcomes: list[dict], contributor: str, domain: str, *,
                           current_iteration: int, current_model_version: str) -> float:
    """Beta posterior mean for a (contributor, domain) cell from EXTERNAL outcome
    records. Decay (half-life 20) down-weights stale outcomes; GOLD outweighs
    SECONDARY; only the current model_version counts (hard reset on a version bump);
    below MIN_SAMPLE effective observations the mean is linearly dampened toward the
    neutral seed (cold-start = neutral, never amplified noise)."""
    alpha, beta = SEED_ALPHA, SEED_BETA
    eff_n = 0.0
    for o in outcomes:
        if o.get("contributor") != contributor or o.get("domain") != domain:
            continue
        if o.get("model_version") != current_model_version:
            continue  # a weight earned by vN must not bind vN+1
        age = max(0, current_iteration - int(o.get("iteration_index", current_iteration)))
        strength = SIGNAL_STRENGTH.get(o.get("tier"), 0.0) * (0.5 ** (age / DECAY_HALFLIFE))
        if strength <= 0:
            continue
        eff_n += strength
        if o.get("useful"):
            alpha += strength
        else:
            beta += strength
    learned = alpha / (alpha + beta)
    if eff_n >= MIN_SAMPLE:
        return learned
    frac = eff_n / MIN_SAMPLE
    return (1.0 - frac) * NEUTRAL_MEAN + frac * learned


def learned_weight_for(outcomes: list[dict], contributor: str, domain: str, *,
                       current_iteration: int, current_model_version: str) -> float:
    """Advisory weight from the learned posterior mean (discount-only, [FLOOR, CAP])."""
    return weight_from_mean(
        learned_posterior_mean(outcomes, contributor, domain,
                               current_iteration=current_iteration,
                               current_model_version=current_model_version)
    )


# --- engine wiring: advisory reading-order for whole proposal files ---

def contributor_from_artifact_name(name: str) -> str:
    """Extract the contributor key from a proposal/review artifact filename, e.g.
    'codex-proposal.yaml' -> 'codex', 'claude-orchestrator-review.yaml' ->
    'claude-orchestrator'."""
    stem = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    if stem.endswith(".yaml"):
        stem = stem[:-5]
    for suffix in ("-proposal", "-review"):
        if stem.endswith(suffix):
            return stem[:-len(suffix)]
    return stem.split("-")[0]


def order_proposal_paths(paths, contributor_weights: dict[str, float] | None):
    """Advisory reading-ORDER for whole proposal files by contributor weight.

    A STABLE permutation (never drops/adds a path); unknown contributors default to
    the neutral seed weight. With no weights it is the identity. This is the ONLY
    way weights touch the engine — the convergence evaluation never receives weights,
    so pass/fail is byte-identical regardless (weights-off equivalence)."""
    if not contributor_weights:
        return list(paths)
    neutral = weight_from_mean(seed_posterior_mean())

    def key(item):
        i, p = item
        c = contributor_from_artifact_name(p)
        return (-contributor_weights.get(c, neutral), i)

    return [p for _, p in sorted(enumerate(paths), key=key)]
