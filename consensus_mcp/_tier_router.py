"""Rule-based tier router (sp-consensus-optimization B3 core).

The converged tiers spec (2026-05-23): match rigor to risk instead of applying the
heavy path uniformly. Three tiers — QUICK / STANDARD / DEEP — each a preset over
the routing knobs (workflow A/B, panel size, path A/B). This module is the pure
CLASSIFIER + the gate-consistency rule; the pre-commit cost estimate, the
AskUserQuestion picker, and wiring into the live orchestration flow are deliberately
out of scope here (operator-gated integration steps).

GATE-CONSISTENCY HARD RULE (unanimous in both consults): a change touching the
governance machinery (hooks / gates / .consensus config / dispatchers / the engine)
or that is security/irreversible is AUTO-UPGRADED to DEEP and LOCKED — the operator
may upgrade further but can never downgrade it. Tiers set rigor ABOVE the floor,
never whether the cross-family gate applies.
"""
from __future__ import annotations

QUICK, STANDARD, DEEP = "quick", "standard", "deep"
_ORDER = {QUICK: 0, STANDARD: 1, DEEP: 2}

# Per-tier preset over the routing knobs (converged Q2/Q3).
_PRESET = {
    QUICK:    {"workflow": "B", "panel_size": 1, "path": "B"},
    STANDARD: {"workflow": "A", "panel_size": 3, "path": "B"},
    DEEP:     {"workflow": "A", "panel_size": 4, "path": "A"},
}


def classify(*, intent_class: str, files_touched: int,
             touches_governance_surface: bool,
             security_or_irreversible: bool = False) -> dict:
    """Classify a change into a tier + its routing preset.

    intent_class: 'hotfix' | 'bounded_feature' | 'architectural'.
    Returns: {tier, workflow, panel_size, path, locked, reason}. ``locked`` True means
    a governance/security/irreversible auto-upgrade the operator cannot downgrade."""
    if touches_governance_surface or security_or_irreversible:
        why = ("touches governance machinery (hooks/gates/config/dispatch/engine)"
               if touches_governance_surface else "security/irreversible")
        return _result(DEEP, locked=True,
                       reason=f"auto-upgraded to DEEP and locked: {why}")
    if intent_class == "architectural":
        return _result(DEEP, locked=False,
                       reason="architectural change -> DEEP (operator may adjust)")
    if intent_class == "hotfix" or files_touched <= 1:
        return _result(QUICK, locked=False,
                       reason="hotfix / single-file -> QUICK")
    return _result(STANDARD, locked=False,
                   reason="multi-file bounded feature -> STANDARD (default)")


def _result(tier: str, *, locked: bool, reason: str) -> dict:
    return {"tier": tier, **_PRESET[tier], "locked": locked, "reason": reason}


def is_downgrade_allowed(classified: dict, target_tier: str) -> bool:
    """Whether the operator may move ``classified`` to ``target_tier``. Upgrades are
    always allowed; a downgrade is REFUSED when the classification is locked (the
    gate-consistency hard rule)."""
    if target_tier not in _ORDER:
        raise ValueError(f"unknown tier {target_tier!r}")
    if _ORDER[target_tier] >= _ORDER[classified["tier"]]:
        return True  # same or heavier rigor is always fine
    return not classified["locked"]
