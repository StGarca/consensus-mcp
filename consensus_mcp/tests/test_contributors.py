"""Unit tests for consensus_mcp.contributors.* — adapter layer.

Per iter-0016b: test the abstract interface + fake adapters + ClaudeAdapter's
callback contract + codex/gemini adapter argv translation (without spawning
real subprocesses).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from consensus_mcp.contributors import (
    PHASE_CONVERGE,
    PHASE_PROPOSE,
    PHASE_REVIEW,
    ContributorAdapter,
    DispatchError,
    SealedArtifact,
)
from consensus_mcp.contributors.base import (
    DispatchPacket,
    FakeAlwaysApprove,
    FakeAlwaysBlock,
    FakeRaisesDispatchError,
)
from consensus_mcp.contributors.claude import ClaudeAdapter
from consensus_mcp.contributors.codex import CodexAdapter
from consensus_mcp.contributors.gemini import GeminiAdapter


# ---------- Phase constants ----------

def test_phase_constants():
    assert PHASE_PROPOSE == "propose"
    assert PHASE_REVIEW == "review"
    assert PHASE_CONVERGE == "converge"


# ---------- FakeAlwaysApprove / FakeAlwaysBlock ----------

def _make_packet(tmp_path: Path, phase: str = PHASE_REVIEW) -> DispatchPacket:
    iter_dir = tmp_path / "iter-test"
    iter_dir.mkdir(parents=True, exist_ok=True)
    goal = iter_dir / "goal_packet.yaml"
    goal.write_text("pilot_id: iter-test\n", encoding="utf-8")
    return DispatchPacket(
        phase=phase,
        contributor="<test>",
        iteration_dir=iter_dir,
        goal_packet_path=goal,
        review_target_path=None,
        reviewer_id=None,
        pass_id=None,
        timeout_seconds=60,
        adapter_options=None,
    )


def test_fake_approve_returns_clean(tmp_path):
    adapter = FakeAlwaysApprove()
    packet = _make_packet(tmp_path, PHASE_REVIEW)
    art = adapter.dispatch(packet)
    assert art.contributor == "fake-approve"
    assert art.phase == PHASE_REVIEW
    assert art.parsed["goal_satisfied"] is True
    assert art.parsed["blocking_objections"] == []
    assert art.sealed_path.exists()


def test_fake_block_returns_blocking_finding(tmp_path):
    adapter = FakeAlwaysBlock()
    packet = _make_packet(tmp_path, PHASE_REVIEW)
    art = adapter.dispatch(packet)
    assert art.parsed["goal_satisfied"] is False
    assert art.parsed["blocking_objections"] == ["fake-block-rev-001"]


def test_fake_raise_raises_dispatch_error(tmp_path):
    adapter = FakeRaisesDispatchError()
    packet = _make_packet(tmp_path)
    with pytest.raises(DispatchError):
        adapter.dispatch(packet)


# ---------- ContributorAdapter convenience methods ----------

def test_propose_calls_dispatch_with_propose_phase(tmp_path):
    adapter = FakeAlwaysApprove()
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal.yaml").write_text("pilot: x\n", encoding="utf-8")
    problem = tmp_path / "problem.md"
    problem.write_text("# challenge\n", encoding="utf-8")
    art = adapter.propose(iter_dir, iter_dir / "goal.yaml", problem)
    assert art.phase == PHASE_PROPOSE


def test_review_calls_dispatch_with_review_phase(tmp_path):
    adapter = FakeAlwaysApprove()
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal.yaml").write_text("pilot: x\n", encoding="utf-8")
    target = tmp_path / "target.yaml"
    target.write_text("k: v\n", encoding="utf-8")
    art = adapter.review(iter_dir, iter_dir / "goal.yaml", target)
    assert art.phase == PHASE_REVIEW


def test_converge_calls_dispatch_with_converge_phase(tmp_path):
    adapter = FakeAlwaysApprove()
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal.yaml").write_text("pilot: x\n", encoding="utf-8")
    conv = tmp_path / "convergence-packet.yaml"
    conv.write_text("k: v\n", encoding="utf-8")
    art = adapter.converge(iter_dir, iter_dir / "goal.yaml", conv, round_number=1)
    assert art.phase == PHASE_CONVERGE


# ---------- ClaudeAdapter ----------

def test_claude_without_callback_raises(tmp_path):
    adapter = ClaudeAdapter()
    packet = _make_packet(tmp_path)
    with pytest.raises(DispatchError, match="artifact_callback"):
        adapter.dispatch(packet)


def test_claude_with_callback_seals_artifact(tmp_path):
    """ClaudeAdapter now routes through T6 (codex-rev-001 round-1 fix); mock T6 in tests.

    Archive filename must contain iteration_id + reviewer_id + pass_id tokens
    (codex-rev-001 round-2 confinement check).
    """
    def cb(packet):
        return {
            "findings": [],
            "goal_satisfied": True,
            "goal_satisfied_rationale": "claude approves",
            "blocking_objections": [],
        }
    def fake_t6(iteration_id, reviewer_id, pass_id, packet):
        # Archive filename must contain the tokens for confinement check to pass.
        archive_path = tmp_path / f"2026-05-13-{iteration_id}-{reviewer_id}-pass.yaml"
        archive_path.write_text(yaml.safe_dump(packet), encoding="utf-8")
        return {"sealed_path": str(archive_path), "packet_sha256": "fakehash"}
    with patch("consensus_mcp.tools.review_write_and_seal.handle", side_effect=fake_t6):
        adapter = ClaudeAdapter(artifact_callback=cb)
        packet = _make_packet(tmp_path, PHASE_PROPOSE)
        art = adapter.dispatch(packet)
    assert art.contributor == "claude"
    assert art.sealed_path.name == "claude-propose.yaml"
    assert art.parsed["goal_satisfied"] is True


def test_claude_rejects_t6_sealed_path_missing_tokens(tmp_path):
    """codex-rev-001 round-2 fix: confinement check rejects archive paths whose
    filenames don't encode the expected iteration/reviewer/pass tokens."""
    def cb(packet):
        return {"findings": [], "goal_satisfied": True, "blocking_objections": []}
    # T6 returns an archive path that DOESN'T contain the iteration_id token.
    rogue_path = tmp_path / "unrelated-file.yaml"
    rogue_path.write_text("secret: data\n", encoding="utf-8")
    def fake_t6(iteration_id, reviewer_id, pass_id, packet):
        return {"sealed_path": str(rogue_path), "packet_sha256": "x"}
    with patch("consensus_mcp.tools.review_write_and_seal.handle", side_effect=fake_t6):
        adapter = ClaudeAdapter(artifact_callback=cb)
        packet = _make_packet(tmp_path, PHASE_PROPOSE)
        with pytest.raises(DispatchError, match="confinement"):
            adapter.dispatch(packet)


def test_claude_rejects_t6_returning_non_file(tmp_path):
    """codex-rev-001 round-2 fix: T6 returning a directory path is rejected."""
    def cb(packet):
        return {"findings": [], "goal_satisfied": True, "blocking_objections": []}
    # Return a directory instead of a file.
    rogue_dir = tmp_path / "iter-test-claude-something-pass"
    rogue_dir.mkdir(exist_ok=True)
    def fake_t6(iteration_id, reviewer_id, pass_id, packet):
        return {"sealed_path": str(rogue_dir), "packet_sha256": "x"}
    with patch("consensus_mcp.tools.review_write_and_seal.handle", side_effect=fake_t6):
        adapter = ClaudeAdapter(artifact_callback=cb)
        packet = _make_packet(tmp_path, PHASE_PROPOSE)
        with pytest.raises(DispatchError, match="non-file"):
            adapter.dispatch(packet)


def test_claude_rejects_callback_missing_required_field(tmp_path):
    def cb(packet):
        # missing 'blocking_objections'
        return {"findings": [], "goal_satisfied": True}
    adapter = ClaudeAdapter(artifact_callback=cb)
    packet = _make_packet(tmp_path)
    with pytest.raises(DispatchError, match="blocking_objections"):
        adapter.dispatch(packet)


def test_claude_rejects_non_dict_callback_return(tmp_path):
    adapter = ClaudeAdapter(artifact_callback=lambda p: "not a dict")
    packet = _make_packet(tmp_path)
    with pytest.raises(DispatchError, match="must return dict"):
        adapter.dispatch(packet)


def test_claude_wraps_callback_exception(tmp_path):
    def cb(packet):
        raise ValueError("test failure")
    adapter = ClaudeAdapter(artifact_callback=cb)
    packet = _make_packet(tmp_path)
    with pytest.raises(DispatchError, match="ValueError"):
        adapter.dispatch(packet)


def test_claude_artifact_filename_per_phase(tmp_path):
    def cb(packet):
        return {"findings": [], "goal_satisfied": True, "blocking_objections": []}
    def fake_t6(iteration_id, reviewer_id, pass_id, packet):
        archive = tmp_path / f"2026-05-13-{iteration_id}-{reviewer_id}-pass.yaml"
        archive.write_text(yaml.safe_dump(packet), encoding="utf-8")
        return {"sealed_path": str(archive), "packet_sha256": "h"}
    with patch("consensus_mcp.tools.review_write_and_seal.handle", side_effect=fake_t6):
        adapter = ClaudeAdapter(artifact_callback=cb)
        for phase in (PHASE_PROPOSE, PHASE_REVIEW, PHASE_CONVERGE):
            packet = _make_packet(tmp_path / phase, phase)
            packet.iteration_dir.mkdir(parents=True, exist_ok=True)
            art = adapter.dispatch(packet)
            assert art.sealed_path.name == f"claude-{phase}.yaml"


# ---------- CodexAdapter / GeminiAdapter argv translation ----------

def test_codex_adapter_constructs_argv(tmp_path):
    """Mock _dispatch_codex.main to capture argv without spawning subprocess."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    goal = iter_dir / "goal_packet.yaml"
    goal.write_text("pilot_id: iter\n", encoding="utf-8")
    target = iter_dir / "review-packet.yaml"
    target.write_text("k: v\n", encoding="utf-8")

    # Fake sealed YAML the dispatch would have written.
    sealed = iter_dir / "codex-review.yaml"
    sealed.write_text(yaml.safe_dump({"iteration_id": "iter", "findings": []}), encoding="utf-8")

    captured_argv = []
    def fake_main(argv):
        captured_argv.extend(argv)
        import json
        print(json.dumps({
            "ok": True,
            "pass_id": "codex-iter-review-1-pass1",
            "sealed_path": str(sealed),
            "archive_sealed_path": None,
            "packet_sha256": "deadbeef",
        }))
        return 0

    packet = DispatchPacket(
        phase=PHASE_REVIEW,
        contributor="codex",
        iteration_dir=iter_dir,
        goal_packet_path=goal,
        review_target_path=target,
        reviewer_id=None,
        pass_id=None,
        timeout_seconds=600,
        adapter_options=None,
    )
    with patch("consensus_mcp._dispatch_codex.main", side_effect=fake_main):
        art = CodexAdapter().dispatch(packet)
    assert "--goal-packet" in captured_argv
    assert "--review-target" in captured_argv
    assert "--timeout-seconds" in captured_argv
    assert "600" in captured_argv
    assert art.contributor == "codex"
    assert art.parsed["iteration_id"] == "iter"


def test_codex_adapter_raises_on_failure(tmp_path):
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal_packet.yaml").write_text("pilot: x\n", encoding="utf-8")

    def fake_main(argv):
        import json
        print(json.dumps({"ok": False, "error": "simulated", "error_type": "CodexInvocationError"}))
        return 1

    packet = DispatchPacket(
        phase=PHASE_REVIEW, contributor="codex",
        iteration_dir=iter_dir, goal_packet_path=iter_dir / "goal_packet.yaml",
        review_target_path=None, reviewer_id=None, pass_id=None,
        timeout_seconds=60, adapter_options=None,
    )
    with patch("consensus_mcp._dispatch_codex.main", side_effect=fake_main):
        with pytest.raises(DispatchError, match="simulated"):
            CodexAdapter().dispatch(packet)


def test_codex_adapter_wraps_systemexit(tmp_path):
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal_packet.yaml").write_text("pilot: x\n", encoding="utf-8")

    def fake_main(argv):
        raise SystemExit(2)

    packet = DispatchPacket(
        phase=PHASE_REVIEW, contributor="codex",
        iteration_dir=iter_dir, goal_packet_path=iter_dir / "goal_packet.yaml",
        review_target_path=None, reviewer_id=None, pass_id=None,
        timeout_seconds=60, adapter_options=None,
    )
    with patch("consensus_mcp._dispatch_codex.main", side_effect=fake_main):
        with pytest.raises(DispatchError, match="SystemExit"):
            CodexAdapter().dispatch(packet)


def test_gemini_adapter_packet_options_override_config(tmp_path):
    """codex-rev-003 round-1 fix: packet.adapter_options.model overrides adapter_config.model."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal_packet.yaml").write_text("pilot: x\n", encoding="utf-8")
    sealed = iter_dir / "gemini-review.yaml"
    sealed.write_text(yaml.safe_dump({"iteration_id": "iter", "findings": []}), encoding="utf-8")

    captured_argv = []
    def fake_main(argv):
        captured_argv.extend(argv)
        import json
        print(json.dumps({"ok": True, "pass_id": "p1", "sealed_path": str(sealed),
                          "archive_sealed_path": None, "packet_sha256": "deadbeef"}))
        return 0

    packet = DispatchPacket(
        phase=PHASE_REVIEW, contributor="gemini",
        iteration_dir=iter_dir, goal_packet_path=iter_dir / "goal_packet.yaml",
        review_target_path=None, reviewer_id=None, pass_id=None,
        timeout_seconds=60,
        adapter_options={"model": "gemini-2.5-flash"},  # per-packet override
    )
    adapter = GeminiAdapter(adapter_config={"model": "gemini-2.5-pro"})  # static config
    with patch("consensus_mcp._dispatch_gemini.main", side_effect=fake_main):
        adapter.dispatch(packet)
    # packet's option wins.
    assert "--model" in captured_argv
    assert "gemini-2.5-flash" in captured_argv
    assert "gemini-2.5-pro" not in captured_argv


def test_codex_adapter_raises_on_missing_sealed_path(tmp_path):
    """codex-rev-002 round-1 fix: missing sealed_path becomes DispatchError."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal_packet.yaml").write_text("pilot: x\n", encoding="utf-8")
    def fake_main(argv):
        import json
        # ok=True but no sealed_path (shouldn't happen but defensive)
        print(json.dumps({"ok": True, "pass_id": "p1"}))
        return 0
    packet = DispatchPacket(
        phase=PHASE_REVIEW, contributor="codex",
        iteration_dir=iter_dir, goal_packet_path=iter_dir / "goal_packet.yaml",
        review_target_path=None, reviewer_id=None, pass_id=None,
        timeout_seconds=60, adapter_options=None,
    )
    with patch("consensus_mcp._dispatch_codex.main", side_effect=fake_main):
        with pytest.raises(DispatchError, match="sealed_path"):
            CodexAdapter().dispatch(packet)


def test_codex_adapter_raises_on_unreadable_sealed_path(tmp_path):
    """codex-rev-002 round-1 fix: unreadable sealed YAML becomes DispatchError."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal_packet.yaml").write_text("pilot: x\n", encoding="utf-8")
    def fake_main(argv):
        import json
        # Point at a nonexistent sealed_path.
        print(json.dumps({"ok": True, "pass_id": "p1",
                          "sealed_path": str(tmp_path / "does-not-exist.yaml"),
                          "archive_sealed_path": None, "packet_sha256": "deadbeef"}))
        return 0
    packet = DispatchPacket(
        phase=PHASE_REVIEW, contributor="codex",
        iteration_dir=iter_dir, goal_packet_path=iter_dir / "goal_packet.yaml",
        review_target_path=None, reviewer_id=None, pass_id=None,
        timeout_seconds=60, adapter_options=None,
    )
    with patch("consensus_mcp._dispatch_codex.main", side_effect=fake_main):
        with pytest.raises(DispatchError, match="unreadable"):
            CodexAdapter().dispatch(packet)


def test_gemini_adapter_includes_model_when_configured(tmp_path):
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal_packet.yaml").write_text("pilot: x\n", encoding="utf-8")
    sealed = iter_dir / "gemini-review.yaml"
    sealed.write_text(yaml.safe_dump({"iteration_id": "iter", "findings": []}), encoding="utf-8")

    captured_argv = []
    def fake_main(argv):
        captured_argv.extend(argv)
        import json
        print(json.dumps({
            "ok": True, "pass_id": "gemini-iter-review-1-pass1",
            "sealed_path": str(sealed), "archive_sealed_path": None,
            "packet_sha256": "deadbeef",
        }))
        return 0

    packet = DispatchPacket(
        phase=PHASE_REVIEW, contributor="gemini",
        iteration_dir=iter_dir, goal_packet_path=iter_dir / "goal_packet.yaml",
        review_target_path=None, reviewer_id=None, pass_id=None,
        timeout_seconds=60, adapter_options=None,
    )
    adapter = GeminiAdapter(adapter_config={"model": "gemini-2.5-pro"})
    with patch("consensus_mcp._dispatch_gemini.main", side_effect=fake_main):
        art = adapter.dispatch(packet)
    assert "--model" in captured_argv
    assert "gemini-2.5-pro" in captured_argv
    assert art.contributor == "gemini"


def test_gemini_adapter_omits_model_when_not_configured(tmp_path):
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "goal_packet.yaml").write_text("pilot: x\n", encoding="utf-8")
    sealed = iter_dir / "gemini-review.yaml"
    sealed.write_text(yaml.safe_dump({"iteration_id": "iter", "findings": []}), encoding="utf-8")

    captured_argv = []
    def fake_main(argv):
        captured_argv.extend(argv)
        import json
        print(json.dumps({
            "ok": True, "pass_id": "p1",
            "sealed_path": str(sealed), "archive_sealed_path": None,
            "packet_sha256": "deadbeef",
        }))
        return 0

    packet = DispatchPacket(
        phase=PHASE_REVIEW, contributor="gemini",
        iteration_dir=iter_dir, goal_packet_path=iter_dir / "goal_packet.yaml",
        review_target_path=None, reviewer_id=None, pass_id=None,
        timeout_seconds=60, adapter_options=None,
    )
    with patch("consensus_mcp._dispatch_gemini.main", side_effect=fake_main):
        GeminiAdapter().dispatch(packet)
    assert "--model" not in captured_argv


# ---------- Adapter names ----------

def test_adapter_names():
    assert CodexAdapter.name == "codex"
    assert GeminiAdapter.name == "gemini"
    assert ClaudeAdapter.name == "claude"
    assert FakeAlwaysApprove.name == "fake-approve"
    assert FakeAlwaysBlock.name == "fake-block"
    assert FakeRaisesDispatchError.name == "fake-raise"
