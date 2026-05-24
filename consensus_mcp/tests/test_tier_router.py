"""Tests for the operator-declared rigor tier router (operator-declared-rigor iteration).

Converged SWOT design (2026-05-24, unanimous): effective tier is operator-DECLARED (no
inference, no silent default); the heuristic is an advisory SUGGESTION only; the sole
automatic move is the MONOTONE governance safety floor (DEEP+locked, can only raise).
"""
from __future__ import annotations

import pytest

from consensus_mcp import _tier_router as tr


# ---- governance_floor: the sole (monotone) automatic move ----

def test_governance_surface_floor_is_deep():
    assert tr.governance_floor(touches_governance_surface=True) == tr.DEEP


def test_security_irreversible_floor_is_deep():
    assert tr.governance_floor(touches_governance_surface=False,
                               security_or_irreversible=True) == tr.DEEP


def test_no_governance_no_floor():
    assert tr.governance_floor(touches_governance_surface=False) is None


# ---- effective_tier: operator-declared, no inference ----

@pytest.mark.parametrize("bad", [None, "", "ultra", "QUICK", 2])
def test_effective_tier_requires_explicit_declaration(bad):
    """No inference, no silent default — a missing/invalid declaration escalates."""
    with pytest.raises(ValueError):
        tr.effective_tier(bad)


@pytest.mark.parametrize("declared", [tr.QUICK, tr.STANDARD, tr.DEEP])
def test_effective_tier_honors_declaration_when_no_governance(declared):
    r = tr.effective_tier(declared)
    assert r["tier"] == declared and r["locked"] is False
    assert r["workflow"] == tr._PRESET[declared]["workflow"]


def test_governance_floor_raises_and_locks_a_low_declaration():
    r = tr.effective_tier(tr.QUICK, touches_governance_surface=True)
    assert r["tier"] == tr.DEEP and r["locked"] is True


def test_declared_deep_with_governance_is_deep_and_locked():
    r = tr.effective_tier(tr.DEEP, touches_governance_surface=True)
    assert r["tier"] == tr.DEEP and r["locked"] is True


def test_effective_tier_is_monotone_max_of_declared_and_floor():
    # property: effective order == max(declared order, floor order)
    for declared in tr._TIERS:
        for gov in (False, True):
            r = tr.effective_tier(declared, touches_governance_surface=gov)
            floor = tr.governance_floor(touches_governance_surface=gov)
            expected = max(tr._ORDER[declared], tr._ORDER[floor] if floor else -1)
            assert tr._ORDER[r["tier"]] == expected
            # floor never LOWERS the declared tier
            assert tr._ORDER[r["tier"]] >= tr._ORDER[declared]


# ---- suggest_tier: advisory only, never the effective tier ----

def test_suggest_tier_is_advisory_not_authoritative():
    s = tr.suggest_tier(intent_class="bounded_feature", files_touched=4)
    assert s["advisory"] is True
    assert "suggested_tier" in s
    assert "tier" not in s  # distinguishable from effective_tier; cannot be mistaken for the decision


@pytest.mark.parametrize("kwargs, expected", [
    ({"intent_class": "hotfix", "files_touched": 1}, tr.QUICK),
    ({"intent_class": "bounded_feature", "files_touched": 4}, tr.STANDARD),
    ({"intent_class": "architectural", "files_touched": 9}, tr.DEEP),
    ({"intent_class": "hotfix", "files_touched": 1, "touches_governance_surface": True}, tr.DEEP),
])
def test_suggest_tier_values(kwargs, expected):
    assert tr.suggest_tier(**kwargs)["suggested_tier"] == expected


# ---- estimate_cost ----

def test_cost_estimate_scales_with_tier():
    d = tr.estimate_cost(tr.DEEP, median_dispatch_seconds=60)
    s = tr.estimate_cost(tr.STANDARD, median_dispatch_seconds=60)
    assert s["n_dispatches"] == 3 and s["est_wall_clock_s"] == 60.0 and s["token_band"] == "medium"
    assert d["n_dispatches"] == 8 and d["est_wall_clock_s"] == 120.0 and d["token_band"] == "high"


def test_cost_estimate_rejects_bad_inputs():
    with pytest.raises(ValueError):
        tr.estimate_cost("ultra", median_dispatch_seconds=60)
    with pytest.raises(ValueError):
        tr.estimate_cost(tr.QUICK, median_dispatch_seconds=-1)


# ---- is_downgrade_allowed on effective_tier results ----

def test_locked_decision_refuses_downgrade():
    r = tr.effective_tier(tr.QUICK, touches_governance_surface=True)  # DEEP + locked
    assert tr.is_downgrade_allowed(r, tr.QUICK) is False
    assert tr.is_downgrade_allowed(r, tr.STANDARD) is False
    assert tr.is_downgrade_allowed(r, tr.DEEP) is True


def test_unlocked_decision_allows_downgrade():
    r = tr.effective_tier(tr.DEEP)  # declared deep, no governance -> not locked
    assert tr.is_downgrade_allowed(r, tr.STANDARD) is True


def test_upgrade_always_allowed():
    r = tr.effective_tier(tr.QUICK)
    assert tr.is_downgrade_allowed(r, tr.DEEP) is True


def test_unknown_target_tier_raises():
    r = tr.effective_tier(tr.STANDARD)
    with pytest.raises(ValueError):
        tr.is_downgrade_allowed(r, "ultra")
