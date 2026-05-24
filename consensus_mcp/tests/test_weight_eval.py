"""Tests for the advisory-weight A/B evaluation (Plan 2, converged-spec step 5)."""
from __future__ import annotations

import pytest

from consensus_mcp import _weight_eval as we


def test_weighting_surfaces_useful_findings_earlier_beats_uniform():
    # Original order puts the useful (high-weight contributor) finding LAST; learned
    # weights should reorder it earlier -> weighted mean rank < uniform.
    findings = [
        {"id": "noise1", "contributor": "weak", "domain": "d", "useful": False},
        {"id": "noise2", "contributor": "weak", "domain": "d", "useful": False},
        {"id": "signal", "contributor": "strong", "domain": "d", "useful": True},
    ]
    weights = {("strong", "d"): 1.0, ("weak", "d"): 0.25}
    res = we.beats_uniform(findings, weights)
    assert res["uniform_mean_rank"] == 2.0   # 'signal' last under original order
    assert res["weighted_mean_rank"] == 0.0  # reordered to first
    assert res["beats"] is True


def test_uninformative_weights_do_not_beat_uniform():
    findings = [
        {"id": "a", "contributor": "x", "domain": "d", "useful": True},
        {"id": "b", "contributor": "y", "domain": "d", "useful": False},
    ]
    res = we.beats_uniform(findings, {})  # no weights -> identity order
    assert res["beats"] is False
    assert res["weighted_mean_rank"] == res["uniform_mean_rank"] == 0.0


def test_mean_rank_none_when_no_useful():
    findings = [{"id": "a", "contributor": "x", "domain": "d", "useful": False}]
    assert we.mean_rank_of_useful(findings, {}) is None
    assert we.beats_uniform(findings, {})["beats"] is False
