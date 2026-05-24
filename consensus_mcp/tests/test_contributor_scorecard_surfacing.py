"""Tests for surfacing the contributor performance scorecard in `consensus results`."""
from __future__ import annotations

from consensus_mcp import _results_rollup as rr
from consensus_mcp import _outcome_ledger as ledger


def _rec(**over):
    base = dict(finding_id="f", contributor="codex", domain="security", tier="gold",
                useful=True, iteration_index=1, model_version="v1",
                adjudicator="operator", evidence_ref="test://x")
    base.update(over)
    return base


def test_no_ledger_renders_nothing(tmp_path):
    assert rr.render_contributor_scorecard(tmp_path) == ""


def test_scorecard_surfaces_rate_and_insufficient_data(tmp_path):
    p = rr._outcome_ledger_path(tmp_path)
    # codex: 6 outcomes (>=5) -> shows a rate; gemini: 2 (<5) -> insufficient data
    for i in range(6):
        ledger.append_outcome(p, _rec(finding_id=f"c{i}", contributor="codex",
                                      useful=(i < 5)))  # 5/6 useful
    for i in range(2):
        ledger.append_outcome(p, _rec(finding_id=f"g{i}", contributor="gemini",
                                      useful=True))
    out = rr.render_contributor_scorecard(tmp_path)
    assert "CONTRIBUTOR PERFORMANCE" in out
    assert "codex" in out and "useful 5/6" in out and "83%" in out
    assert "gemini" in out and "insufficient data" in out
    # descriptive-only disclaimer present (score never judges an individual finding)
    assert "never judges an individual finding" in out
