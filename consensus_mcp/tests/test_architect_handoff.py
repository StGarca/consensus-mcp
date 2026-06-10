"""Tests for the HANDOFF.md renderer (spec section 7 + consult Q7)."""
from __future__ import annotations

from pathlib import Path

from consensus_mcp import _architect_paths as ap
from consensus_mcp import _architect_handoff as hf


def _goal_with_cycles(tmp_path: Path, n_cycles: int) -> Path:
    goal = ap.goal_dir(tmp_path, "g1")
    goal.mkdir(parents=True)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do the thing"})
    ap.seal_artifact(
        goal / ap.SPEC_APPROVAL_FILENAME,
        {"spec_sha256": "abc", "base_sha": "f" * 40, "approver": "operator"},
    )
    for i in range(1, n_cycles + 1):
        c = ap.cycle_dir(goal, i)
        c.mkdir()
        ap.seal_artifact(
            c / ap.BUILD_RESULT_FILENAME,
            {"summary": f"cycle {i} work", "pushback": None,
             "lane_head_sha": f"{i:040d}"},
        )
        ap.seal_artifact(
            c / ap.RULING_FILENAME,
            {"disposition": "revise", "reason": f"more in cycle {i}"},
        )
    return goal


def test_handoff_contains_spec_and_cycles(tmp_path: Path):
    goal = _goal_with_cycles(tmp_path, 2)
    text = hf.render_handoff(goal, roles={"architect": "claude", "builder": "codex", "reviewer": "codex"})
    assert "do the thing" in text
    assert "cycle-1" in text and "cycle-2" in text
    assert "revise" in text


def test_handoff_rolling_window_caps_inline_cycles(tmp_path: Path):
    goal = _goal_with_cycles(tmp_path, 7)
    text = hf.render_handoff(goal, roles={"architect": "claude", "builder": "codex", "reviewer": "codex"})
    # window=5: cycles 3..7 inline, 1..2 summarized as pointers
    assert "cycle-7" in text and "cycle-3" in text
    assert "cycle 1 work" not in text and "cycle 2 work" not in text
    assert "older cycles" in text.lower()


def test_handoff_flags_host_only_cross_family_signer(tmp_path: Path):
    goal = _goal_with_cycles(tmp_path, 1)
    text = hf.render_handoff(
        goal, roles={"architect": "claude", "builder": "codex", "reviewer": "codex"}
    )
    assert "only cross-family signer" in text.lower()


def test_write_handoff_writes_file(tmp_path: Path):
    goal = _goal_with_cycles(tmp_path, 1)
    hf.write_handoff(goal, roles={"architect": "claude", "builder": "codex", "reviewer": "codex"})
    assert (goal / ap.HANDOFF_FILENAME).exists()
