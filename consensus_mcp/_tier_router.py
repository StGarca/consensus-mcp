"""Operator-declared rigor tier + advisory suggestion.

Converged design (2026-05-24 SWOT, unanimous): the EFFECTIVE rigor tier is
OPERATOR-DECLARED - the system never INFERS it (heuristics are the shared-prior trap;
see workflow_engine._goal_risk_class, which deliberately refuses to infer risk). The
heuristic is demoted to a non-binding SUGGESTION the operator may consult but must
EXPLICITLY confirm; the engine routes on the DECLARED tier only (via effective_tier).
The single permitted automatic move is the MONOTONE governance/security auto-UPGRADE to
DEEP+locked - a safety FLOOR (rigor can only INCREASE, never be selected or lowered), so
it is a safeguard, not the inference the doctrine forbids.

Tiers are presets over the routing knobs: QUICK / STANDARD / DEEP.
"""
from __future__ import annotations

from copy import deepcopy

QUICK, STANDARD, DEEP = "quick", "standard", "deep"
_ORDER = {QUICK: 0, STANDARD: 1, DEEP: 2}
_TIERS = (QUICK, STANDARD, DEEP)

# Per-tier preset over the routing knobs (converged Q2/Q3).
_PRESET = {
    QUICK: {
        "workflow": "A", "panel_policy": "all-enabled",
        "minimum_independent_reviewers": 2, "path": "B", "compute_preset": QUICK,
    },
    STANDARD: {
        "workflow": "A", "panel_policy": "all-enabled",
        "minimum_independent_reviewers": 2, "path": "B", "compute_preset": STANDARD,
    },
    DEEP: {
        "workflow": "A", "panel_policy": "all-enabled",
        "minimum_independent_reviewers": 2, "path": "A", "compute_preset": DEEP,
    },
}

# The hard-problem tier keeps every provider on its newest suitable model and
# spends additional compute through provider-native effort controls. Kimi's
# current CLI exposes thinking as a boolean, so effort is provenance metadata
# and thinking=True is the executable control.
_MODEL_PRESETS = {
    QUICK: {
        "codex": {"model": "gpt-5.6-sol", "effort": "low"},
        "claude": {"model": "claude-fable-5", "effort": "low"},
        "gemini": {"model": "Gemini 3.5 Flash (Low)"},
        "grok": {"model": "grok-4.5", "effort": "low"},
        "kimi": {"effort": "low", "thinking": False},
    },
    STANDARD: {
        "codex": {"model": "gpt-5.6-sol", "effort": "medium"},
        "claude": {"model": "claude-fable-5", "effort": "medium"},
        "gemini": {"model": "Gemini 3.5 Flash (Medium)"},
        "grok": {"model": "grok-4.5", "effort": "medium"},
        "kimi": {"effort": "medium", "thinking": True},
    },
    DEEP: {
        "codex": {"model": "gpt-5.6-sol", "effort": "xhigh"},
        "claude": {"model": "claude-fable-5", "effort": "max"},
        "gemini": {"model": "Gemini 3.5 Flash (High)"},
        "grok": {"model": "grok-4.5", "effort": "max"},
        "kimi": {"effort": "high", "thinking": True},
    },
}

_TIMEOUT_PRESETS = {
    QUICK: {
        "iteration_timeout_seconds": 300,
        "stall_silence_seconds": 120,
        "pre_first_byte_silence_seconds": 300,
    },
    STANDARD: {
        "iteration_timeout_seconds": 1800,
        "stall_silence_seconds": 300,
        "pre_first_byte_silence_seconds": 900,
    },
    DEEP: {
        "iteration_timeout_seconds": 0,
        "stall_silence_seconds": 0,
        "pre_first_byte_silence_seconds": 0,
    },
}


def model_preset(tier: str) -> dict:
    """Return an isolated provider-settings mapping for ``tier``."""
    if tier not in _PRESET:
        raise ValueError(f"unknown tier {tier!r}")
    return {
        contributor: dict(settings)
        for contributor, settings in _MODEL_PRESETS.get(tier, {}).items()
    }


def apply_tier_config(config: dict, decision: dict) -> dict:
    """Apply an ``effective_tier`` decision to an isolated config copy."""
    tier = decision.get("tier")
    if tier not in _PRESET:
        raise ValueError(f"invalid effective tier decision: {tier!r}")

    resolved = deepcopy(config)
    contributors = resolved.setdefault("contributors", {})
    enabled = contributors.get("enabled") or []
    required_panel = _PRESET[tier]["minimum_independent_reviewers"]
    from consensus_mcp._contributor_profiles import (
        independent_count,
        load_builtin_profiles,
        merge_profiles,
    )
    profiles = merge_profiles(
        load_builtin_profiles(), contributors.get("profiles") or {},
    )
    actual_panel = independent_count(enabled, profiles)
    if actual_panel < required_panel:
        raise ValueError(
            f"tier {tier!r} requires at least {required_panel} independent reviewers; "
            f"got {actual_panel}"
        )

    adapters = contributors.setdefault("adapters", {})
    for contributor, settings in decision.get("model_settings", {}).items():
        if contributor in enabled:
            adapters[contributor] = {
                **(adapters.get(contributor) or {}),
                **settings,
            }

    timeout_settings = decision["timeout_settings"]
    resolved.setdefault("defaults", {}).update(timeout_settings)
    for contributor in enabled:
        adapter = adapters.setdefault(contributor, {})
        adapter["stall_silence_seconds"] = timeout_settings["stall_silence_seconds"]
        adapter["pre_first_byte_silence_seconds"] = timeout_settings[
            "pre_first_byte_silence_seconds"
        ]

    workflow = resolved.setdefault("workflow", {})
    workflow["max_convergence_rounds"] = _EXPECTED_ROUNDS[tier]
    return resolved


def governance_floor(*, touches_governance_surface: bool,
                     security_or_irreversible: bool = False) -> str | None:
    """The SOLE permitted automatic move: a MONOTONE safety floor. Returns DEEP when a
    change touches governance machinery (hooks / gates / .consensus config / dispatchers
    / the engine) or is security/irreversible - else None. It can only RAISE rigor, never
    select or lower it, so it is a safeguard, not the inference the doctrine forbids."""
    if touches_governance_surface or security_or_irreversible:
        return DEEP
    return None


def effective_tier(declared_tier: str | None, *,
                   touches_governance_surface: bool = False,
                   security_or_irreversible: bool = False) -> dict:
    """The AUTHORITATIVE tier the engine routes on.

    The tier MUST be operator-DECLARED: a missing/invalid declaration RAISES (no
    inference, no silent default - the caller must escalate / require a declaration).
    The monotone governance floor may only RAISE the declared tier, never lower it.

    All tiers use every enabled independent reviewer, with a minimum of two.
    ``locked`` is True iff a governance/security surface is present (the operator
    cannot downgrade below the floor)."""
    if declared_tier not in _TIERS:
        raise ValueError(
            f"rigor tier must be operator-DECLARED as one of {list(_TIERS)}; got "
            f"{declared_tier!r}. The system does NOT infer it (escalate / require a "
            f"declaration) - heuristics are the shared-prior trap."
        )
    floor = governance_floor(touches_governance_surface=touches_governance_surface,
                             security_or_irreversible=security_or_irreversible)
    locked = floor is not None
    tier = declared_tier
    if floor is not None and _ORDER[floor] > _ORDER[declared_tier]:
        tier = floor  # monotone: floor can only raise
    source = ("operator-declared" if floor is None
              else f"operator-declared {declared_tier}; governance safety floor locks >= {floor} (effective {tier})")
    return {
        "tier": tier,
        **_PRESET[tier],
        "model_settings": model_preset(tier),
        "timeout_settings": dict(_TIMEOUT_PRESETS[tier]),
        "locked": locked,
        "source": source,
    }


def suggest_tier(*, intent_class: str, files_touched: int,
                 touches_governance_surface: bool = False,
                 security_or_irreversible: bool = False) -> dict:
    """A NON-BINDING suggestion to help the operator DECLARE a tier.

    Returns a clearly ADVISORY object - note ``advisory: True`` and ``suggested_tier``
    (NOT an authoritative ``tier``). The engine never reads this to route; only
    effective_tier(declared) routes. The operator must EXPLICITLY declare the tier; a UI
    surfacing this suggestion must never pre-fill, pre-select, or default to it."""
    floor = governance_floor(touches_governance_surface=touches_governance_surface,
                             security_or_irreversible=security_or_irreversible)
    if floor is not None:
        suggested, reason = DEEP, "governance/security surface - suggest DEEP (also the safety floor)"
    elif intent_class == "architectural":
        suggested, reason = DEEP, "architectural change - suggest DEEP"
    elif intent_class == "hotfix" or files_touched <= 1:
        suggested, reason = QUICK, "hotfix / single-file - suggest QUICK"
    else:
        suggested, reason = STANDARD, "multi-file bounded feature - suggest STANDARD"
    return {"advisory": True, "suggested_tier": suggested, "reason": reason,
            "preset_preview": dict(_PRESET[suggested])}


# Expected convergence rounds per tier (round-1-only unless DEEP goes multi-round).
_EXPECTED_ROUNDS = {QUICK: 1, STANDARD: 1, DEEP: 2}
_TOKEN_BAND = {QUICK: "low", STANDARD: "medium", DEEP: "high"}


def estimate_cost(
    tier: str,
    *,
    independent_reviewers: int,
    median_dispatch_seconds: float,
) -> dict:
    """Pre-commit cost estimate for a tier, surfaced BEFORE any dispatch (converged Q3
    "show the estimate first"). Uses the observed median dispatch wall-clock (from
    telemetry) - a BAND, not a false-precision token promise.

    n_dispatches = actual independent reviewers x expected_rounds; est_wall_clock_s = median x
    expected_rounds (round-1 peers run in parallel, so wall-clock scales with ROUNDS, not
    panel size)."""
    if tier not in _PRESET:
        raise ValueError(f"unknown tier {tier!r}")
    if median_dispatch_seconds < 0:
        raise ValueError("median_dispatch_seconds must be non-negative")
    if independent_reviewers < 2:
        raise ValueError("consensus requires at least 2 independent reviewers")
    rounds = _EXPECTED_ROUNDS[tier]
    return {
        "tier": tier,
        "panel_size": independent_reviewers,
        "n_dispatches": independent_reviewers * rounds,
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
