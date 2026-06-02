"""Tests for per-iteration cost/outcome telemetry (sp-consensus-optimization B5)."""
from __future__ import annotations

import pytest

from consensus_mcp import _iteration_telemetry as tel


def test_record_and_read_round_trip(tmp_path):
    p = tmp_path / "telemetry.jsonl"
    tel.record_iteration(p, {"iteration_id": "i1", "tier": "standard",
                             "n_dispatches": 3, "wall_clock_s": 120,
                             "blocking_findings": 1})
    tel.record_iteration(p, {"iteration_id": "i2", "tier": "deep",
                             "n_dispatches": 5, "blocking_findings": 0})
    got = tel.read_iterations(p)
    assert [r["iteration_id"] for r in got] == ["i1", "i2"]
    assert got[0]["panel_size"] == 0  # numeric default


def test_missing_required_field_raises(tmp_path):
    with pytest.raises(ValueError, match="missing required"):
        tel.record_iteration(tmp_path / "t.jsonl", {"tier": "standard"})


@pytest.mark.parametrize("bad", [{"n_dispatches": -1}, {"wall_clock_s": "lots"},
                                 {"blocking_findings": True}])
def test_bad_numeric_raises(tmp_path, bad):
    rec = {"iteration_id": "i", "tier": "quick", **bad}
    with pytest.raises(ValueError):
        tel.record_iteration(tmp_path / "t.jsonl", rec)


def test_summarize_cost_per_blocking_finding_by_tier():
    records = [
        {"iteration_id": "a", "tier": "standard", "n_dispatches": 3, "blocking_findings": 1},
        {"iteration_id": "b", "tier": "standard", "n_dispatches": 3, "blocking_findings": 2},
        {"iteration_id": "c", "tier": "deep", "n_dispatches": 8, "blocking_findings": 0},
    ]
    s = tel.summarize_by_tier(records)
    assert s["standard"]["iterations"] == 2
    assert s["standard"]["dispatches"] == 6
    assert s["standard"]["blocking_findings"] == 3
    assert s["standard"]["dispatches_per_blocking_finding"] == pytest.approx(2.0)
    # deep caught zero blocking findings -> cost-with-no-payoff signalled as None
    assert s["deep"]["dispatches_per_blocking_finding"] is None


def test_read_missing_is_empty(tmp_path):
    assert tel.read_iterations(tmp_path / "nope.jsonl") == []


def test_read_skips_corrupt_rows_and_rollup_survives(tmp_path):
    """codex-rev-002: validate at the READ boundary - a hand-edited row with a bad
    numeric (or missing required field) is skipped, so summarize_by_tier never crashes
    or reports nonsense."""
    import json as _json
    p = tmp_path / "telemetry.jsonl"
    tel.record_iteration(p, {"iteration_id": "good", "tier": "standard",
                             "n_dispatches": 3, "blocking_findings": 1})
    with p.open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps({"iteration_id": "neg", "tier": "deep",
                              "n_dispatches": -5, "blocking_findings": 1}) + "\n")
        fh.write(_json.dumps({"iteration_id": "str", "tier": "deep",
                              "n_dispatches": "lots"}) + "\n")
        fh.write(_json.dumps({"tier": "deep"}) + "\n")  # missing iteration_id
        fh.write("{not json\n")
    got = tel.read_iterations(p)
    assert [r["iteration_id"] for r in got] == ["good"]
    s = tel.summarize_by_tier(got)  # must not raise
    assert s["standard"]["dispatches_per_blocking_finding"] == 3.0
