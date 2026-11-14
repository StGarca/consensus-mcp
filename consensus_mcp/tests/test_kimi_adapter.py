from __future__ import annotations

import json
from pathlib import Path

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
        phase=phase, contributor="kimi", iteration_dir=tmp_path,
        goal_packet_path=tmp_path / "goal.yaml", review_target_path=None,
        reviewer_id="kimi-test-1", pass_id="kimi-test-1-pass1", timeout_seconds=600,
    )


def _fake_main(captured, tmp_path, *, ok=True, rc=0, non_json=False):
    sealed = tmp_path / "kimi-sealed.yaml"
    def main(argv):
        captured.extend(argv)
        sealed.write_text("findings: []\ngoal_satisfied: true\nblocking_objections: []\n", encoding="utf-8")
        if non_json:
            print("not json at all")
        else:
            print(json.dumps({"ok": ok, "pass_id": "kimi-test-1-pass1",
                              "sealed_path": str(sealed), "archive_sealed_path": None,
                              "packet_sha256": "0" * 64}))
        return rc
    return main


def test_kimi_adapter_propose_forwards_mode_proposal(monkeypatch, tmp_path):
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.kimi import KimiAdapter
    art = KimiAdapter().dispatch(_packet(PHASE_PROPOSE, tmp_path))
    assert cap[cap.index("--mode") + 1] == "proposal"
    assert art.contributor == "kimi"
    assert art.parsed["goal_satisfied"] is True


def test_kimi_adapter_review_forwards_mode_review(monkeypatch, tmp_path):
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.kimi import KimiAdapter
    KimiAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))
    assert cap[cap.index("--mode") + 1] == "review"


def test_kimi_adapter_converge_forwards_mode_review(monkeypatch, tmp_path):
    """CONVERGE -> 'review' (kimi-valid). Guards the iter-0044 bug class:
    a kimi-INVALID mode like 'converge' would SystemExit -> DispatchError."""
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.kimi import KimiAdapter
    KimiAdapter().dispatch(_packet(PHASE_CONVERGE, tmp_path))
    assert cap[cap.index("--mode") + 1] == "review"


def test_kimi_adapter_non_json_stdout_raises_dispatcherror(monkeypatch, tmp_path):
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main([], tmp_path, non_json=True))
    from consensus_mcp.contributors.kimi import KimiAdapter
    with pytest.raises(DispatchError, match="non-JSON"):
        KimiAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))


def test_kimi_adapter_rc_nonzero_raises_dispatcherror(monkeypatch, tmp_path):
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main([], tmp_path, ok=False, rc=1))
    from consensus_mcp.contributors.kimi import KimiAdapter
    with pytest.raises(DispatchError, match="kimi dispatch failed"):
        KimiAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))


def test_kimi_adapter_returns_sealed_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main([], tmp_path))
    from consensus_mcp.contributors.kimi import KimiAdapter
    art = KimiAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))
    assert art.pass_id == "kimi-test-1-pass1"
    assert art.sealed_path.name == "kimi-sealed.yaml"
    assert art.packet_sha256 == "0" * 64
