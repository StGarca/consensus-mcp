"""Tests for the advisory contributor-weights module (Plan 1).

Covers the converged weighted-consensus spec (2026-05-24): discount-only
posterior-mean mapping (D3/D4), Beta(2,2) cold-start (D2), same-family aggregate
cap (D4), and the two firewall invariants (D5): weights-off equivalence as a
permutation property, and no-self-grade as the structural absence of any
weight/credit write API. Learner + external ledger are Plan 2 (not tested here).
"""
from __future__ import annotations

import inspect

import pytest

from consensus_mcp import _contributor_weights as cw


@pytest.mark.parametrize("mean, expected", [
    (0.5, 1.0),     # neutral seed -> full (not discounted)
    (1.0, 1.0),     # proven-good -> still full (never amplified above CAP)
    (0.75, 1.0),    # above neutral -> capped at full (discount-only)
    (0.25, 0.625),  # below neutral -> linearly discounted: 0.25 + 0.75*(0.25/0.5)
    (0.0, 0.25),    # worst -> floor
])
def test_weight_from_mean_is_discount_only(mean, expected):
    assert cw.weight_from_mean(mean) == pytest.approx(expected)


@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
def test_weight_from_mean_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        cw.weight_from_mean(bad)


def test_seed_mean_is_neutral_and_weight_is_full():
    assert cw.seed_posterior_mean() == pytest.approx(0.5)
    assert cw.weight_for(contributor="codex", domain="security") == pytest.approx(1.0)
    assert cw.weight_for(contributor="gemini", domain="ux") == pytest.approx(1.0)


def test_order_by_weight_is_a_permutation_never_drops():
    findings = [
        {"id": "f1", "contributor": "codex", "domain": "security"},
        {"id": "f2", "contributor": "gemini", "domain": "ux"},
        {"id": "f3", "contributor": "host_peer", "domain": "security"},
    ]
    weights = {("codex", "security"): 1.0, ("gemini", "ux"): 0.5,
               ("host_peer", "security"): 0.25}
    ordered = cw.order_by_weight(findings, weights)
    assert {f["id"] for f in ordered} == {f["id"] for f in findings}
    assert len(ordered) == len(findings)
    assert [f["id"] for f in ordered] == ["f1", "f2", "f3"]


def test_order_by_weight_with_no_weights_is_identity():
    findings = [{"id": "a", "contributor": "x", "domain": "d"},
                {"id": "b", "contributor": "y", "domain": "d"}]
    assert cw.order_by_weight(findings, {}) == findings


def test_same_family_aggregate_capped_to_one_independent():
    raw = {"orchestrator": 1.0, "host_peer": 1.0, "codex": 1.0}
    families = {"orchestrator": "claude", "host_peer": "claude", "codex": "codex"}
    capped = cw.apply_same_family_cap(raw, families)
    assert capped["orchestrator"] + capped["host_peer"] == pytest.approx(1.0)
    assert capped["codex"] == pytest.approx(1.0)
    assert capped["orchestrator"] == pytest.approx(0.5)
    assert capped["host_peer"] == pytest.approx(0.5)


def test_same_family_under_cap_untouched():
    raw = {"orchestrator": 0.4, "host_peer": 0.4, "codex": 1.0}
    families = {"orchestrator": "claude", "host_peer": "claude", "codex": "codex"}
    capped = cw.apply_same_family_cap(raw, families)
    assert capped == raw  # aggregate 0.8 <= 1.0 -> no scaling


@pytest.mark.parametrize("name, expected", [
    ("codex-proposal.yaml", "codex"),
    ("/tmp/x/gemini-review.yaml", "gemini"),
    ("claude-orchestrator-proposal.yaml", "claude-orchestrator"),
    ("host_peer-proposal.yaml", "host_peer"),
    ("kimi-review-kimi-wcc-2-pass1.yaml", "kimi"),
])
def test_contributor_from_artifact_name(name, expected):
    assert cw.contributor_from_artifact_name(name) == expected


def test_order_proposal_paths_reorders_by_weight_stable_permutation():
    paths = ["weak-proposal.yaml", "strong-proposal.yaml", "mid-proposal.yaml"]
    weights = {"strong": 1.0, "mid": 0.6, "weak": 0.25}
    ordered = cw.order_proposal_paths(paths, weights)
    assert ordered == ["strong-proposal.yaml", "mid-proposal.yaml", "weak-proposal.yaml"]
    assert sorted(ordered) == sorted(paths)  # permutation: nothing dropped/added


def test_order_proposal_paths_no_weights_is_identity():
    paths = ["a-proposal.yaml", "b-proposal.yaml"]
    assert cw.order_proposal_paths(paths, None) == paths
    assert cw.order_proposal_paths(paths, {}) == paths


def test_module_exposes_no_weight_write_api():
    """no-self-grade (static form): no public callable writes/sets a contributor's
    weight or usefulness credit from caller input. A Plan-2 learner may only add a
    writer reading the external ledger — never an agent-callable setter. This locks
    that invariant so a future regression fails here."""
    forbidden_tokens = ("set_weight", "update_weight", "record_credit", "grade",
                        "set_credit", "update_credit", "write_weight")
    public = [n for n, _ in inspect.getmembers(cw, callable) if not n.startswith("_")]
    offending = [n for n in public
                 if any(tok in n.lower() for tok in forbidden_tokens)]
    assert offending == [], (
        f"weight/credit write API present (no-self-grade violation): {offending}"
    )


# ---- user-centric: scorecard (decision-support) + operator-declared lean ----

def test_build_scorecard_counts_per_contributor():
    outcomes = [
        {"contributor": "codex", "useful": True},
        {"contributor": "codex", "useful": False},
        {"contributor": "gemini", "useful": True},
    ]
    card = cw.build_scorecard(outcomes)
    assert card["codex"] == {"total": 2, "useful": 1, "useful_rate": pytest.approx(0.5)}
    assert card["gemini"]["useful_rate"] == pytest.approx(1.0)


def test_build_scorecard_empty():
    assert cw.build_scorecard([]) == {}


def test_lean_to_weights_descending_user_order():
    w = cw.lean_to_weights(["d", "b", "c", "a"])
    assert w["d"] == pytest.approx(cw.CAP)    # first = highest
    assert w["a"] == pytest.approx(cw.FLOOR)  # last = floor
    assert w["d"] > w["b"] > w["c"] > w["a"]  # strictly descending in declared order


def test_lean_to_weights_single_and_empty():
    assert cw.lean_to_weights(["solo"]) == {"solo": cw.CAP}
    assert cw.lean_to_weights([]) == {}


def test_declared_lean_drives_ordering_not_a_learned_weight():
    # the operator's declared lean (NOT a machine-learned weight) sets the reading order
    paths = ["a-proposal.yaml", "c-proposal.yaml", "d-proposal.yaml", "b-proposal.yaml"]
    ordered = cw.order_proposal_paths(paths, cw.lean_to_weights(["d", "b", "c", "a"]))
    assert [p.split("-")[0] for p in ordered] == ["d", "b", "c", "a"]
