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
    """No inference, no silent default - a missing/invalid declaration escalates."""
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


def test_deep_tier_resolves_hard_problem_model_settings():
    decision = tr.effective_tier(tr.DEEP)
    assert decision["compute_preset"] == tr.DEEP
    assert decision["model_settings"] == {
        "codex": {"model": "gpt-5.6-sol", "effort": "xhigh"},
        "claude": {"model": "claude-fable-5", "effort": "max"},
        "gemini": {"model": "Gemini 3.5 Flash (High)"},
        "grok": {"model": "grok-4.5", "effort": "max"},
        "kimi": {"effort": "high", "thinking": True},
    }


def test_quick_and_standard_resolve_distinct_compute_and_timeouts():
    quick = tr.effective_tier(tr.QUICK)
    standard = tr.effective_tier(tr.STANDARD)
    assert quick["model_settings"]["codex"]["effort"] == "low"
    assert standard["model_settings"]["codex"]["effort"] == "medium"
    assert quick["model_settings"]["gemini"]["model"].endswith("(Low)")
    assert standard["model_settings"]["gemini"]["model"].endswith("(Medium)")
    assert quick["model_settings"]["kimi"] == {
        "effort": "low", "thinking": False,
    }
    assert standard["model_settings"]["kimi"] == {
        "effort": "medium", "thinking": True,
    }
    assert quick["timeout_settings"]["iteration_timeout_seconds"] == 300
    assert standard["timeout_settings"]["iteration_timeout_seconds"] == 1800
    assert tr.effective_tier(tr.DEEP)["timeout_settings"] == {
        "iteration_timeout_seconds": 0,
        "stall_silence_seconds": 0,
        "pre_first_byte_silence_seconds": 0,
    }


def test_apply_deep_tier_config_isolated_and_executable():
    config = {
        "workflow": {"mode": "post-review", "max_convergence_rounds": 1},
        "contributors": {
            "enabled": ["codex", "gemini", "grok", "kimi"],
            "adapters": {
                "codex": {"command": "codex", "model": "old"},
                "gemini": {"command": "agy"},
                "grok": {"command": "grok"},
                "kimi": {"command": "kimi"},
            },
        },
    }
    resolved = tr.apply_tier_config(config, tr.effective_tier(tr.DEEP))
    assert config["contributors"]["adapters"]["codex"]["model"] == "old"
    assert resolved["workflow"] == {
        "mode": "post-review", "max_convergence_rounds": 2,
    }
    assert resolved["contributors"]["adapters"]["codex"]["effort"] == "xhigh"
    grok = resolved["contributors"]["adapters"]["grok"]
    assert (grok["command"], grok["model"], grok["effort"]) == (
        "grok", "grok-4.5", "max",
    )
    assert resolved["defaults"] == {
        "iteration_timeout_seconds": 0,
        "stall_silence_seconds": 0,
        "pre_first_byte_silence_seconds": 0,
    }


def test_apply_tier_requires_two_independent_reviewers():
    config = {
        "workflow": {},
        "contributors": {"enabled": ["codex"]},
    }
    with pytest.raises(ValueError, match="at least 2 independent reviewers"):
        tr.apply_tier_config(config, tr.effective_tier(tr.DEEP))


def test_apply_tier_does_not_count_supplementary_host_peer():
    config = {
        "workflow": {},
        "contributors": {
            "enabled": ["codex", "claude-swe-reviewer"],
        },
    }
    with pytest.raises(ValueError, match="got 1"):
        tr.apply_tier_config(config, tr.effective_tier(tr.DEEP))


@pytest.mark.parametrize("reviewer_count", [2, 8])
def test_all_tiers_preserve_every_enabled_provider(reviewer_count):
    enabled = [f"provider-{i}" for i in range(reviewer_count)]
    config = {
        "workflow": {},
        "contributors": {"enabled": enabled, "adapters": {}},
    }
    for tier in tr._TIERS:
        decision = tr.effective_tier(tier)
        resolved = tr.apply_tier_config(config, decision)
        assert decision["panel_policy"] == "all-enabled"
        assert resolved["contributors"]["enabled"] == enabled


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
    d = tr.estimate_cost(tr.DEEP, independent_reviewers=8, median_dispatch_seconds=60)
    q = tr.estimate_cost(tr.QUICK, independent_reviewers=2, median_dispatch_seconds=60)
    assert q["panel_size"] == 2 and q["n_dispatches"] == 2
    assert q["est_wall_clock_s"] == 60.0 and q["token_band"] == "low"
    assert d["panel_size"] == 8 and d["n_dispatches"] == 16
    assert d["est_wall_clock_s"] == 120.0 and d["token_band"] == "high"


def test_cost_estimate_rejects_bad_inputs():
    with pytest.raises(ValueError):
        tr.estimate_cost("ultra", independent_reviewers=2, median_dispatch_seconds=60)
    with pytest.raises(ValueError):
        tr.estimate_cost(tr.QUICK, independent_reviewers=2, median_dispatch_seconds=-1)
    with pytest.raises(ValueError, match="at least 2"):
        tr.estimate_cost(tr.QUICK, independent_reviewers=1, median_dispatch_seconds=60)


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
