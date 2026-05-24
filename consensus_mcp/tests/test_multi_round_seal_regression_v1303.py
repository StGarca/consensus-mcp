"""v1.30.3 dogfood regression: a multi-round convergence consult must SEAL every round.

Bug A (fixed v1.30.2): the convergence reviewer_id was hardcoded with round `-1` regardless
of the actual round, so round 2's seal re-used round 1's pass_id with DIFFERENT content ->
T6 index_collision -> the consult could never converge past round 1 (the ebook2audiobook
dogfood). The fix: the codex/gemini/kimi adapters key reviewer_id off
`adapter_options.round_number`.

This test locks the END-TO-END consequence at the SEAL boundary: round-keyed ids -> distinct
pass_ids -> EVERY round seals; plus a NEGATIVE control proving that WITHOUT round-keying the
exact original symptom (index_collision) returns -- so this regression test is non-vacuous.

Ties together the two halves proven separately elsewhere:
  - test_bugfix_spec_v1302.test_bug_a_converge_reviewer_id_is_round_keyed
      (the adapter actually PRODUCES these round-keyed ids)
  - test_seal_collision_fix.test_same_reviewer_distinct_pass_ids_no_collision
      (the seal ACCEPTS distinct pass_ids)
"""
from __future__ import annotations

import pytest

from consensus_mcp.tools import review_write_and_seal as t6


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # _paths lazy-resolvers re-read env each call — use setenv (not setattr).
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    return tmp_path


def _reviewer_id(iter_name: str, phase: str, round_number: int) -> str:
    # The SAME format the codex/gemini/kimi converge adapters use (Bug A fix):
    #   reviewer_id = f"{adapter}-{iteration_dir.name}-{phase}-{round_number}"
    return f"kimi-{iter_name}-{phase}-{round_number}"


def _packet(iteration_id, reviewer_id, pass_id, findings):
    return {
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "findings": findings,
        "goal_satisfied": True,
        "blocking_objections": [],
    }


def test_multi_round_converge_both_rounds_seal(repo):
    iteration_id = "iteration-emotion-engine-0001"
    phase = "converge"

    rid1 = _reviewer_id(iteration_id, phase, 1)
    rid2 = _reviewer_id(iteration_id, phase, 2)
    assert rid1 != rid2  # round-keyed -> distinct reviewer_ids (Bug A fix)

    pid1, pid2 = f"{rid1}-pass1", f"{rid2}-pass1"
    r1 = t6.handle(iteration_id, rid1, pid1, _packet(iteration_id, rid1, pid1, [{"round": 1}]))
    r2 = t6.handle(iteration_id, rid2, pid2,
                   _packet(iteration_id, rid2, pid2, [{"round": 2, "diff": True}]))
    assert "error" not in r1, r1
    assert "error" not in r2, r2                     # round 2 SEALS (was index_collision pre-fix)
    assert r1["sealed_path"] != r2["sealed_path"]


def test_non_round_keyed_reseal_still_collides_negative_control(repo):
    """Non-vacuous proof: the ORIGINAL Bug A behavior — same reviewer_id+pass_id across
    rounds with DIFFERENT content — must STILL produce index_collision. Round-keying is
    precisely what gives round 2 a different pass_id so this path is never reached in
    practice; if round-keying silently regressed, round 2 would land here and FAIL to seal."""
    iteration_id = "iteration-emotion-engine-0001"
    rid = "kimi-iteration-emotion-engine-0001-converge-1"  # NOT round-keyed: same both rounds
    pid = f"{rid}-pass1"
    r1 = t6.handle(iteration_id, rid, pid, _packet(iteration_id, rid, pid, [{"round": 1}]))
    assert "error" not in r1, r1
    r2 = t6.handle(iteration_id, rid, pid,
                   _packet(iteration_id, rid, pid, [{"round": 2, "diff": True}]))
    assert r2.get("error") == "index_collision", r2
