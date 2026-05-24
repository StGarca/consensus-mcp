"""Tests for the external-outcome ledger + the Plan-2 learner.

Covers the converged weighted-consensus spec (2026-05-24): the no-self-grade
ledger firewall (D5b — only external adjudicators may write credit), and the Beta
learner (D1 GOLD>SECONDARY, D2 cold-start, D4 decay/min-sample/model-version-reset,
discount-only weights). Learner is pure-function + advisory; not wired into the
live engine here (that is the operator-reviewed final step).
"""
from __future__ import annotations

import pytest

from consensus_mcp import _outcome_ledger as ledger
from consensus_mcp import _contributor_weights as cw


def _rec(**over):
    base = dict(finding_id="f1", contributor="codex", domain="security",
                tier="gold", useful=False, iteration_index=10,
                model_version="codex-1.0", adjudicator="operator",
                evidence_ref="test://red-green/123")
    base.update(over)
    return base


# ---- ledger firewall ----

def test_ledger_round_trip(tmp_path):
    p = tmp_path / "outcomes.jsonl"
    ledger.append_outcome(p, _rec(finding_id="a"))
    ledger.append_outcome(p, _rec(finding_id="b", tier="secondary"))
    got = ledger.read_outcomes(p)
    assert [r["finding_id"] for r in got] == ["a", "b"]


def test_read_missing_ledger_is_empty(tmp_path):
    assert ledger.read_outcomes(tmp_path / "nope.jsonl") == []


@pytest.mark.parametrize("adjudicator", [
    "claude", "orchestrator", "host_peer", "codex", "gemini", "kimi",
    "claude-orchestrator", "some-AI-agent", "an llm judge",
])
def test_ledger_rejects_ai_adjudicator_no_self_grade(tmp_path, adjudicator):
    with pytest.raises(ValueError, match="no-self-grade"):
        ledger.append_outcome(tmp_path / "o.jsonl", _rec(adjudicator=adjudicator))


@pytest.mark.parametrize("bad", [
    {"tier": "bronze"},          # invalid tier
    {"evidence_ref": "   "},     # empty evidence
    {"useful": "yes"},           # non-bool
])
def test_ledger_rejects_malformed(tmp_path, bad):
    with pytest.raises(ValueError):
        ledger.append_outcome(tmp_path / "o.jsonl", _rec(**bad))


def test_ledger_rejects_missing_field(tmp_path):
    rec = _rec()
    del rec["domain"]
    with pytest.raises(ValueError, match="missing required fields"):
        ledger.append_outcome(tmp_path / "o.jsonl", rec)


# ---- learner ----

def test_cold_start_is_neutral_full_weight():
    assert cw.learned_posterior_mean([], "codex", "security",
                                     current_iteration=10,
                                     current_model_version="codex-1.0") == pytest.approx(0.5)
    assert cw.learned_weight_for([], "codex", "security",
                                 current_iteration=10,
                                 current_model_version="codex-1.0") == pytest.approx(1.0)


def test_proven_useful_stays_full_discount_only():
    outs = [_rec(finding_id=f"g{i}", useful=True, iteration_index=10) for i in range(6)]
    w = cw.learned_weight_for(outs, "codex", "security",
                              current_iteration=10, current_model_version="codex-1.0")
    assert w == pytest.approx(1.0)  # never amplified above baseline


def test_proven_unreliable_is_discounted():
    outs = [_rec(finding_id=f"g{i}", useful=False, iteration_index=10) for i in range(6)]
    w = cw.learned_weight_for(outs, "codex", "security",
                              current_iteration=10, current_model_version="codex-1.0")
    assert w < 1.0  # 6 GOLD failures -> mean 0.2 -> weight 0.55


def test_gold_discounts_more_than_secondary():
    gold = [_rec(finding_id=f"g{i}", tier="gold", useful=False, iteration_index=10)
            for i in range(6)]
    secondary = [_rec(finding_id=f"s{i}", tier="secondary", useful=False, iteration_index=10)
                 for i in range(6)]
    wg = cw.learned_weight_for(gold, "codex", "security",
                               current_iteration=10, current_model_version="codex-1.0")
    ws = cw.learned_weight_for(secondary, "codex", "security",
                               current_iteration=10, current_model_version="codex-1.0")
    assert wg < ws  # GOLD signal strength 1.0 > SECONDARY 0.5


def test_below_min_sample_is_dampened_toward_neutral():
    two = [_rec(finding_id=f"g{i}", useful=False, iteration_index=10) for i in range(2)]
    # eff_n = 2 < MIN_SAMPLE(5) -> dampened toward neutral, so weight is closer to 1.0
    # than the undampened learned weight would be.
    damped = cw.learned_weight_for(two, "codex", "security",
                                   current_iteration=10, current_model_version="codex-1.0")
    undamped = cw.weight_from_mean(2.0 / 6.0)  # alpha=2,beta=4 -> learned mean if not dampened
    assert damped > undamped


def test_model_version_reset_ignores_prior_version():
    outs = [_rec(finding_id=f"g{i}", useful=False, iteration_index=10,
                 model_version="codex-0.9") for i in range(6)]
    # current version differs -> prior-version outcomes ignored -> cold-start neutral
    w = cw.learned_weight_for(outs, "codex", "security",
                              current_iteration=10, current_model_version="codex-1.0")
    assert w == pytest.approx(1.0)


def test_recent_failures_discount_more_than_stale_ones():
    recent = [_rec(finding_id=f"r{i}", useful=False, iteration_index=100) for i in range(6)]
    stale = [_rec(finding_id=f"s{i}", useful=False, iteration_index=20) for i in range(6)]
    w_recent = cw.learned_weight_for(recent, "codex", "security",
                                     current_iteration=100, current_model_version="codex-1.0")
    w_stale = cw.learned_weight_for(stale, "codex", "security",
                                    current_iteration=100, current_model_version="codex-1.0")
    assert w_recent < w_stale  # decay: age ~80 (4 half-lives) softens the stale failures


# ---- codex Workflow B review fixes (2026-05-24) ----

@pytest.mark.parametrize("adjudicator", ["panel", "assistant", "gpt", "chatgpt",
                                         "an assistant model"])
def test_ledger_rejects_more_ai_aliases(tmp_path, adjudicator):
    """codex-rev-002: panel/assistant/gpt/chatgpt are also AI/panel identities."""
    with pytest.raises(ValueError, match="no-self-grade"):
        ledger.append_outcome(tmp_path / "o.jsonl", _rec(adjudicator=adjudicator))


def test_read_quarantines_ai_authored_and_malformed_rows(tmp_path):
    """codex-rev-001: the no-self-grade firewall holds at the READ boundary too. A
    tampered/legacy AI-authored row or malformed JSON already in the file is skipped
    (quarantined), never fed to the learner."""
    import json as _json
    p = tmp_path / "o.jsonl"
    ledger.append_outcome(p, _rec(finding_id="good"))            # legit, via the writer
    with p.open("a", encoding="utf-8") as fh:                    # tamper past the writer
        fh.write(_json.dumps(_rec(finding_id="evil", adjudicator="codex")) + "\n")
        fh.write("{not valid json\n")
        bad = _rec(finding_id="notier"); del bad["tier"]
        fh.write(_json.dumps(bad) + "\n")
    got = ledger.read_outcomes(p)
    assert [r["finding_id"] for r in got] == ["good"]


def test_learner_ignores_quarantined_ai_authored_row(tmp_path):
    """End-to-end: an injected AI-authored GOLD failure does NOT discount the learner
    (it is quarantined on read), so the cell stays at the neutral cold-start weight."""
    import json as _json
    p = tmp_path / "o.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for i in range(6):
            fh.write(_json.dumps(_rec(finding_id=f"evil{i}", adjudicator="codex",
                                      useful=False)) + "\n")
    outs = ledger.read_outcomes(p)
    assert outs == []  # all quarantined
    w = cw.learned_weight_for(outs, "codex", "security",
                              current_iteration=10, current_model_version="codex-1.0")
    assert w == pytest.approx(1.0)  # neutral — the injected discount was firewalled out
