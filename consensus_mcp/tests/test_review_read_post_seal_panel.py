"""P0.2: review.read_post_seal G1 mode must be panel-agnostic - it hardcoded
codex+claude and rejected every other family with `unknown_reviewer`. Now: serve
any named family once IT and >=1 OTHER distinct family have sealed (independence).
"""
from __future__ import annotations

import yaml

from consensus_mcp.tools import review_read_post_seal as t


def _setup(tmp_path, monkeypatch, sealed_families):
    state = tmp_path / "consensus-state"
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state))
    iter_dir = state / "active" / "iter-p"
    iter_dir.mkdir(parents=True)
    audit = {"audit_log": [
        {"event": "reviewer_invoked", "actor": f} for f in sealed_families
    ] + [
        {"event": "review_returned_and_sealed", "actor": f} for f in sealed_families
    ]}
    (iter_dir / "independence-audit.yaml").write_text(yaml.safe_dump(audit), encoding="utf-8")
    for f in sealed_families:
        (iter_dir / f"{f}-review.yaml").write_text(
            yaml.safe_dump({"reviewer_id": f, "findings": []}), encoding="utf-8")
    return iter_dir


def test_g1_serves_grok_when_two_families_sealed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, ["grok", "gemini"])
    res = t.handle(iteration_id="iter-p", reviewer="grok")
    assert res.get("error") is None, res  # grok no longer 'unknown_reviewer'


def test_g1_serves_kimi_when_two_families_sealed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, ["kimi", "codex"])
    res = t.handle(iteration_id="iter-p", reviewer="kimi")
    assert res.get("error") is None, res


def test_g1_blocks_when_only_named_family_sealed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, ["grok"])  # no independent cross-reviewer
    res = t.handle(iteration_id="iter-p", reviewer="grok")
    assert res["error"] == "both_reviews_not_sealed"
