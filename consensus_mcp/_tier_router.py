"""Operator-declared rigor tier + advisory suggestion.

Converged design (2026-05-24 SWOT, unanimous): the EFFECTIVE rigor tier is
OPERATOR-DECLARED — the system never INFERS it (heuristics are the shared-prior trap;
see workflow_engine._goal_risk_class, which deliberately refuses to infer risk). The
heuristic is demoted to a non-binding SUGGESTION the operator may consult but must
EXPLICITLY confirm; the engine routes on the DECLARED tier only (via effective_tier).
The single permitted automatic move is the MONOTONE governance/security auto-UPGRADE to
DEEP+locked — a safety FLOOR (rigor can only INCREASE, never be selected or lowered), so
it is a safeguard, not the inference the doctrine forbids.

Tiers are presets over the routing knobs: QUICK / STANDARD / DEEP.
"""
from __future__ import annotations

QUICK, STANDARD, DEEP = "quick", "standard", "deep"
_ORDER = {QUICK: 0, STANDARD: 1, DEEP: 2}
_TIERS = (QUICK, STANDARD, DEEP)

# Per-tier preset over the routing knobs (converged Q2/Q3).
_PRESET = {
    QUICK:    {"workflow": "B", "panel_size": 1, "path": "B"},
    STANDARD: {"workflow": "A", "panel_size": 3, "path": "B"},
    DEEP:     {"workflow": "A", "panel_size": 4, "path": "A"},
}


def governance_floor(*, touches_governance_surface: bool,
                     security_or_irreversible: bool = False) -> str | None:
    """The SOLE permitted automatic move: a MONOTONE safety floor. Returns DEEP when a
    change touches governance machinery (hooks / gates / .consensus config / dispatchers
    / the engine) or is security/irreversible — else None. It can only RAISE rigor, never
    select or lower it, so it is a safeguard, not the inference the doctrine forbids."""
    if touches_governance_surface or security_or_irreversible:
        return DEEP
    return None


def effective_tier(declared_tier: str | None, *,
                   touches_governance_surface: bool = False,
                   security_or_irreversible: bool = False) -> dict:
    """The AUTHORITATIVE tier the engine routes on.

    The tier MUST be operator-DECLARED: a missing/invalid declaration RAISES (no
    inference, no silent default — the caller must escalate / require a declaration).
    The monotone governance floor may only RAISE the declared tier, never lower it.

    Returns {tier, workflow, panel_size, path, locked, source}. ``locked`` is True iff a
    governance/security surface is present (the operator cannot downgrade below the
    floor)."""
    if declared_tier not in _TIERS:
        raise ValueError(
            f"rigor tier must be operator-DECLARED as one of {list(_TIERS)}; got "
            f"{declared_tier!r}. The system does NOT infer it (escalate / require a "
            f"declaration) — heuristics are the shared-prior trap."
        )
    floor = governance_floor(touches_governance_surface=touches_governance_surface,
                             security_or_irreversible=security_or_irreversible)
    locked = floor is not None
    tier = declared_tier
    if floor is not None and _ORDER[floor] > _ORDER[declared_tier]:
        tier = floor  # monotone: floor can only raise
    source = ("operator-declared" if floor is None
              else f"operator-declared {declared_tier}; governance safety floor locks >= {floor} (effective {tier})")
    return {"tier": tier, **_PRESET[tier], "locked": locked, "source": source}


def suggest_tier(*, intent_class: str, files_touched: int,
                 touches_governance_surface: bool = False,
                 security_or_irreversible: bool = False) -> dict:
    """A NON-BINDING suggestion to help the operator DECLARE a tier.

    Returns a clearly ADVISORY object — note ``advisory: True`` and ``suggested_tier``
    (NOT an authoritative ``tier``). The engine never reads this to route; only
    effective_tier(declared) routes. The operator must EXPLICITLY declare the tier; a UI
    surfacing this suggestion must never pre-fill, pre-select, or default to it."""
    floor = governance_floor(touches_governance_surface=touches_governance_surface,
                             security_or_irreversible=security_or_irreversible)
    if floor is not None:
        suggested, reason = DEEP, "governance/security surface — suggest DEEP (also the safety floor)"
    elif intent_class == "architectural":
        suggested, reason = DEEP, "architectural change — suggest DEEP"
    elif intent_class == "hotfix" or files_touched <= 1:
        suggested, reason = QUICK, "hotfix / single-file — suggest QUICK"
    else:
        suggested, reason = STANDARD, "multi-file bounded feature — suggest STANDARD"
    return {"advisory": True, "suggested_tier": suggested, "reason": reason,
            "preset_preview": dict(_PRESET[suggested])}


# Expected convergence rounds per tier (round-1-only unless DEEP goes multi-round).
_EXPECTED_ROUNDS = {QUICK: 1, STANDARD: 1, DEEP: 2}
_TOKEN_BAND = {QUICK: "low", STANDARD: "medium", DEEP: "high"}


def estimate_cost(tier: str, *, median_dispatch_seconds: float) -> dict:
    """Pre-commit cost estimate for a tier, surfaced BEFORE any dispatch (converged Q3
    "show the estimate first"). Uses the observed median dispatch wall-clock (from
    telemetry) — a BAND, not a false-precision token promise.

    n_dispatches = panel_size x expected_rounds; est_wall_clock_s = median x
    expected_rounds (round-1 peers run in parallel, so wall-clock scales with ROUNDS, not
    panel size)."""
    if tier not in _PRESET:
        raise ValueError(f"unknown tier {tier!r}")
    if median_dispatch_seconds < 0:
        raise ValueError("median_dispatch_seconds must be non-negative")
    rounds = _EXPECTED_ROUNDS[tier]
    panel = _PRESET[tier]["panel_size"]
    return {
        "tier": tier,
        "n_dispatches": panel * rounds,
        "expected_rounds": rounds,
        "est_wall_clock_s": round(median_dispatch_seconds * rounds, 1),
        "token_band": _TOKEN_BAND[tier],
    }


def is_downgrade_allowed(decided: dict, target_tier: str) -> bool:
    """Whether the operator may move ``decided`` (an effective_tier result) to
    ``target_tier``. Upgrades are always allowed; a downgrade is REFUSED when the
    decision is locked (a governance/security safety floor is in force)."""
    if target_tier not in _ORDER:
        raise ValueError(f"unknown tier {target_tier!r}")
    if _ORDER[target_tier] >= _ORDER[decided["tier"]]:
        return True  # same or heavier rigor is always fine
    return not decided["locked"]
