"""iter-0044: tests that adapter + MCP wrapper layers forward --mode
correctly per packet.phase / phase parameter.

Why this test module exists: the original defect (iter-0043 design
consult) was that test_dispatch_codex_proposal_mode.py only tested the
dispatcher's own argparse behavior in isolation; the adapter boundary
was completely uncovered. CI was green while the integration boundary
was broken. This module asserts the adapter-to-dispatcher contract
directly via mock-based argv capture.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from consensus_mcp.contributors._phase_mode import phase_to_mode
from consensus_mcp.contributors.base import (
    DispatchPacket,
    PHASE_CONVERGE,
    PHASE_PROPOSE,
    PHASE_REVIEW,
)


# ---------- phase_to_mode unit tests ----------

def test_phase_propose_maps_to_proposal():
    assert phase_to_mode(PHASE_PROPOSE) == "proposal"


def test_phase_review_maps_to_review():
    assert phase_to_mode(PHASE_REVIEW) == "review"


def test_phase_converge_maps_to_review():
    """Interim mapping per iter-0043 weighted-synthesis convergence:
    PHASE_CONVERGE → review until empirical evidence justifies a
    dedicated converge mode (iter-0045 candidate)."""
    assert phase_to_mode(PHASE_CONVERGE) == "review"


def test_unknown_phase_raises_value_error():
    """Strict-dict lookup: silent default-to-review is what allowed the
    original defect. Never default; raise loudly."""
    with pytest.raises(ValueError, match="unmapped phase"):
        phase_to_mode("not_a_real_phase")


def test_value_error_message_lists_valid_phases():
    """Caller should be able to diagnose from the error message alone."""
    with pytest.raises(ValueError) as excinfo:
        phase_to_mode("xyz")
    msg = str(excinfo.value)
    assert PHASE_PROPOSE in msg
    assert PHASE_REVIEW in msg
    assert PHASE_CONVERGE in msg


# ---------- adapter argv-forwarding (the original defect) ----------

def _make_fake_dispatcher_main(captured_argv: list[str], tmp_path: Path):
    """Build a fake _dispatch_*.main that captures argv and writes a
    minimal sealed YAML so the adapter's post-dispatch parsing succeeds."""
    sealed_path = tmp_path / "fake-sealed.yaml"

    def fake_main(argv):
        captured_argv.extend(argv)
        # Adapters require a YAML file at sealed_path with at least one mapping
        sealed_path.write_text(
            "findings: []\ngoal_satisfied: true\nblocking_objections: []\n",
            encoding="utf-8",
        )
        # Adapters parse stdout as JSON
        print(json.dumps({
            "ok": True,
            "pass_id": "test-pass-1",
            "sealed_path": str(sealed_path),
            "archive_sealed_path": None,
            "packet_sha256": "0" * 64,
        }))
        return 0

    return fake_main


def _make_packet(phase: str, tmp_path: Path) -> DispatchPacket:
    return DispatchPacket(
        phase=phase,
        contributor="codex",  # ignored by the dispatch path
        iteration_dir=tmp_path,
        goal_packet_path=tmp_path / "goal.yaml",
        review_target_path=None,
        reviewer_id="test-reviewer-1",
        pass_id="test-reviewer-1-pass1",
        timeout_seconds=600,
    )


def _assert_mode_in_argv(captured_argv: list[str], expected_mode: str) -> None:
    assert "--mode" in captured_argv, (
        f"argv missing --mode; got {captured_argv!r}"
    )
    idx = captured_argv.index("--mode")
    assert captured_argv[idx + 1] == expected_mode, (
        f"argv has --mode {captured_argv[idx + 1]!r}; expected {expected_mode!r}"
    )


def test_codex_adapter_phase_propose_forwards_mode_proposal(monkeypatch, tmp_path):
    captured: list[str] = []
    monkeypatch.setattr(
        "consensus_mcp._dispatch_codex.main",
        _make_fake_dispatcher_main(captured, tmp_path),
    )
    from consensus_mcp.contributors.codex import CodexAdapter
    CodexAdapter().dispatch(_make_packet(PHASE_PROPOSE, tmp_path))
    _assert_mode_in_argv(captured, "proposal")


def test_codex_adapter_phase_review_forwards_mode_review(monkeypatch, tmp_path):
    captured: list[str] = []
    monkeypatch.setattr(
        "consensus_mcp._dispatch_codex.main",
        _make_fake_dispatcher_main(captured, tmp_path),
    )
    from consensus_mcp.contributors.codex import CodexAdapter
    CodexAdapter().dispatch(_make_packet(PHASE_REVIEW, tmp_path))
    _assert_mode_in_argv(captured, "review")


def test_codex_adapter_phase_converge_forwards_mode_review(monkeypatch, tmp_path):
    captured: list[str] = []
    monkeypatch.setattr(
        "consensus_mcp._dispatch_codex.main",
        _make_fake_dispatcher_main(captured, tmp_path),
    )
    from consensus_mcp.contributors.codex import CodexAdapter
    CodexAdapter().dispatch(_make_packet(PHASE_CONVERGE, tmp_path))
    _assert_mode_in_argv(captured, "review")


def test_gemini_adapter_phase_propose_forwards_mode_proposal(monkeypatch, tmp_path):
    captured: list[str] = []
    monkeypatch.setattr(
        "consensus_mcp._dispatch_gemini.main",
        _make_fake_dispatcher_main(captured, tmp_path),
    )
    from consensus_mcp.contributors.gemini import GeminiAdapter
    GeminiAdapter().dispatch(_make_packet(PHASE_PROPOSE, tmp_path))
    _assert_mode_in_argv(captured, "proposal")


def test_gemini_adapter_phase_review_forwards_mode_review(monkeypatch, tmp_path):
    captured: list[str] = []
    monkeypatch.setattr(
        "consensus_mcp._dispatch_gemini.main",
        _make_fake_dispatcher_main(captured, tmp_path),
    )
    from consensus_mcp.contributors.gemini import GeminiAdapter
    GeminiAdapter().dispatch(_make_packet(PHASE_REVIEW, tmp_path))
    _assert_mode_in_argv(captured, "review")


def test_gemini_adapter_phase_converge_forwards_mode_review(monkeypatch, tmp_path):
    captured: list[str] = []
    monkeypatch.setattr(
        "consensus_mcp._dispatch_gemini.main",
        _make_fake_dispatcher_main(captured, tmp_path),
    )
    from consensus_mcp.contributors.gemini import GeminiAdapter
    GeminiAdapter().dispatch(_make_packet(PHASE_CONVERGE, tmp_path))
    _assert_mode_in_argv(captured, "review")


# ---------- MCP wrapper _resolve_mode + _build_argv ----------

def test_codex_wrapper_resolve_mode_phase_propose():
    from consensus_mcp.tools.reviewer_dispatch_codex import _resolve_mode
    assert _resolve_mode(phase="propose", mode=None) == "proposal"


def test_codex_wrapper_resolve_mode_phase_review():
    from consensus_mcp.tools.reviewer_dispatch_codex import _resolve_mode
    assert _resolve_mode(phase="review", mode=None) == "review"


def test_codex_wrapper_resolve_mode_explicit_mode_wins():
    """When both phase and mode are passed, mode wins (escape hatch)."""
    from consensus_mcp.tools.reviewer_dispatch_codex import _resolve_mode
    assert _resolve_mode(phase="propose", mode="review") == "review"
    assert _resolve_mode(phase="review", mode="proposal") == "proposal"


def test_codex_wrapper_resolve_mode_neither_returns_none():
    """When neither is set, return None (caller omits --mode; dispatcher
    default 'review' applies for backward compat)."""
    from consensus_mcp.tools.reviewer_dispatch_codex import _resolve_mode
    assert _resolve_mode(phase=None, mode=None) is None


def test_gemini_wrapper_resolve_mode_phase_propose():
    from consensus_mcp.tools.reviewer_dispatch_gemini import _resolve_mode
    assert _resolve_mode(phase="propose", mode=None) == "proposal"


def test_gemini_wrapper_resolve_mode_explicit_mode_wins():
    from consensus_mcp.tools.reviewer_dispatch_gemini import _resolve_mode
    assert _resolve_mode(phase="propose", mode="review") == "review"


def test_codex_wrapper_build_argv_includes_mode_when_phase_set():
    from consensus_mcp.tools.reviewer_dispatch_codex import _build_argv
    argv = _build_argv(
        goal_packet_path="g.yaml", iteration_dir="d",
        reviewer_id=None, pass_id=None, timeout_seconds=None,
        review_target_path=None, smoke=None,
        phase="propose",
    )
    assert "--mode" in argv
    assert argv[argv.index("--mode") + 1] == "proposal"


def test_codex_wrapper_build_argv_omits_mode_when_neither_set():
    """Backward compat: pre-iter-0044 callers that pass neither phase
    nor mode get the same argv shape they always did."""
    from consensus_mcp.tools.reviewer_dispatch_codex import _build_argv
    argv = _build_argv(
        goal_packet_path="g.yaml", iteration_dir="d",
        reviewer_id=None, pass_id=None, timeout_seconds=None,
        review_target_path=None, smoke=None,
    )
    assert "--mode" not in argv


def test_gemini_wrapper_build_argv_includes_mode_when_phase_set():
    from consensus_mcp.tools.reviewer_dispatch_gemini import _build_argv
    argv = _build_argv(
        goal_packet_path="g.yaml", iteration_dir="d",
        reviewer_id=None, pass_id=None, timeout_seconds=None,
        review_target_path=None, model=None, smoke=None,
        phase="propose",
    )
    assert "--mode" in argv
    assert argv[argv.index("--mode") + 1] == "proposal"


def test_gemini_wrapper_build_argv_omits_mode_when_neither_set():
    from consensus_mcp.tools.reviewer_dispatch_gemini import _build_argv
    argv = _build_argv(
        goal_packet_path="g.yaml", iteration_dir="d",
        reviewer_id=None, pass_id=None, timeout_seconds=None,
        review_target_path=None, model=None, smoke=None,
    )
    assert "--mode" not in argv
