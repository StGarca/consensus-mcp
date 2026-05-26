"""Unit tests for consensus_mcp.contributors.grok.GrokAdapter.

Mirrors test_kimi_adapter.py — focused on the adapter's job:
  - phase → --mode forwarding (propose / review / converge)
  - non-JSON stdout → DispatchError
  - non-zero rc → DispatchError
  - sealed artifact round-trip
"""
from __future__ import annotations

import json

import pytest

from consensus_mcp.contributors.base import (
    DispatchError,
    DispatchPacket,
    PHASE_CONVERGE,
    PHASE_PROPOSE,
    PHASE_REVIEW,
)


def _packet(phase, tmp_path):
    return DispatchPacket(
        phase=phase, contributor="grok", iteration_dir=tmp_path,
        goal_packet_path=tmp_path / "goal.yaml", review_target_path=None,
        reviewer_id="grok-test-1", pass_id="grok-test-1-pass1", timeout_seconds=600,
    )


def _fake_main(captured, tmp_path, *, ok=True, rc=0, non_json=False):
    sealed = tmp_path / "grok-sealed.yaml"
    def main(argv):
        captured.extend(argv)
        sealed.write_text(
            "findings: []\ngoal_satisfied: true\nblocking_objections: []\n",
            encoding="utf-8",
        )
        if non_json:
            print("not json at all")
        else:
            print(json.dumps({
                "ok": ok, "pass_id": "grok-test-1-pass1",
                "sealed_path": str(sealed), "archive_sealed_path": None,
                "packet_sha256": "0" * 64,
            }))
        return rc
    return main


def test_grok_adapter_propose_forwards_mode_proposal(monkeypatch, tmp_path):
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_grok.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.grok import GrokAdapter
    art = GrokAdapter().dispatch(_packet(PHASE_PROPOSE, tmp_path))
    assert cap[cap.index("--mode") + 1] == "proposal"
    assert art.contributor == "grok"
    assert art.parsed["goal_satisfied"] is True


def test_grok_adapter_review_forwards_mode_review(monkeypatch, tmp_path):
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_grok.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.grok import GrokAdapter
    GrokAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))
    assert cap[cap.index("--mode") + 1] == "review"


def test_grok_adapter_converge_forwards_mode_review(monkeypatch, tmp_path):
    """CONVERGE → 'review' (grok-valid). Guards the iter-0044 bug class:
    an invalid mode like 'converge' would SystemExit → DispatchError."""
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_grok.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.grok import GrokAdapter
    GrokAdapter().dispatch(_packet(PHASE_CONVERGE, tmp_path))
    assert cap[cap.index("--mode") + 1] == "review"


def test_grok_adapter_non_json_stdout_raises_dispatcherror(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "consensus_mcp._dispatch_grok.main",
        _fake_main([], tmp_path, non_json=True),
    )
    from consensus_mcp.contributors.grok import GrokAdapter
    with pytest.raises(DispatchError, match="non-JSON"):
        GrokAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))


def test_grok_adapter_rc_nonzero_raises_dispatcherror(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "consensus_mcp._dispatch_grok.main",
        _fake_main([], tmp_path, ok=False, rc=1),
    )
    from consensus_mcp.contributors.grok import GrokAdapter
    with pytest.raises(DispatchError, match="grok dispatch failed"):
        GrokAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))


def test_grok_adapter_returns_sealed_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr("consensus_mcp._dispatch_grok.main", _fake_main([], tmp_path))
    from consensus_mcp.contributors.grok import GrokAdapter
    art = GrokAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))
    assert art.pass_id == "grok-test-1-pass1"
    assert art.sealed_path.name == "grok-sealed.yaml"
    assert art.packet_sha256 == "0" * 64


def test_grok_adapter_passes_model_when_configured(monkeypatch, tmp_path):
    """Adapter-level model override propagates to dispatcher argv."""
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_grok.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.grok import GrokAdapter
    GrokAdapter(adapter_config={"model": "grok-4-fast"}).dispatch(_packet(PHASE_REVIEW, tmp_path))
    assert "--model" in cap
    assert cap[cap.index("--model") + 1] == "grok-4-fast"


def test_grok_adapter_packet_options_override_adapter_config(monkeypatch, tmp_path):
    """packet.adapter_options.model wins over adapter_config.model (codex-rev-003 pattern)."""
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_grok.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.grok import GrokAdapter
    pkt = DispatchPacket(
        phase=PHASE_REVIEW, contributor="grok", iteration_dir=tmp_path,
        goal_packet_path=tmp_path / "goal.yaml", review_target_path=None,
        reviewer_id=None, pass_id=None, timeout_seconds=600,
        adapter_options={"model": "grok-4-mini"},
    )
    GrokAdapter(adapter_config={"model": "grok-4-fast"}).dispatch(pkt)
    assert cap[cap.index("--model") + 1] == "grok-4-mini"
