"""iter-0018 cross-AI invariant hardening tests (codex 2026-05-10 v5).

Tests the 5 findings from the v5 architectural review:

  Finding 1 (HIGH):   model_family-level cross-AI check (not just actor.id)
  Finding 2 (HIGH):   capstone happy-path is codex->apply->claude->close
  Finding 3 (MEDIUM): mutation-completeness check at close-attempt time
  Finding 4 (MEDIUM): strict patch_proposal mode in goal_packet
  Finding 5 (LOW/M):  T6 fails closed when invariant evaluator returns None

Each finding gets at least one dedicated test, with additional negative cases
where appropriate. RED -> GREEN.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _closure_invariant  # noqa: E402
from consensus_mcp import _dispatch_codex  # noqa: E402
from consensus_mcp import _self_drive  # noqa: E402
from consensus_mcp.tools import audit_append_event  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixture helpers (small re-use of existing test idioms; intentionally
# duplicated rather than imported to keep this test file self-contained per the
# Surgical Changes principle — touching the existing test_closure_invariant
# fixtures would couple modules that should evolve independently).
# ---------------------------------------------------------------------------


def _now_iso(offset_seconds: int = 0) -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_lm(*, actor_id, model_family, post_sha, ts_offset=0, files_touched=None):
    if files_touched is None:
        files_touched = ["foo.py"]
    return {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(ts_offset),
        "actor": {
            "id": actor_id,
            "model_family": model_family,
            "role": "fix_author",
            "pass_id": f"{actor_id}-pass1",
        },
        "patch_id": "patch-xxxxxxxxxxxx-yyyyyyyyyyyy",
        "files_touched": list(files_touched),
        "base_sha": "base-xxxxxxxxxxxx",
        "post_sha": post_sha,
        "unified_diff_sha256": "diff-yyyyyyyyyyyy",
        "timestamp": _now_iso(ts_offset),
    }


def _make_verdict(*, actor_id, model_family, target_hash, ts_offset=0):
    return {
        "actor": {
            "id": actor_id,
            "model_family": model_family,
            "pass_id": f"{actor_id}-pass1",
        },
        "review_target_hash": target_hash,
        "created_at_utc": _now_iso(ts_offset),
    }


# ===========================================================================
# Finding 1 (HIGH): closer.actor.model_family must differ from
#                   last_mutation.actor.model_family
# ===========================================================================


def test_same_model_family_different_actor_ids_fails():
    """Codex-A authors patch; codex-B (different id, SAME family=codex) closes.

    Pre-iter-0018: passes because actor.id differs.
    Post-iter-0018: FAILS because model_family is the same — that's same-family
    different-actor, NOT cross-AI.
    """
    lm = _make_lm(
        actor_id="codex-iter0018-1", model_family="codex",
        post_sha="POST", ts_offset=-100,
    )
    v = _make_verdict(
        actor_id="codex-iter0018-2", model_family="codex",
        target_hash="POST", ts_offset=0,
    )
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    # The check is now named cross_family (renamed from cross_actor) and must
    # fail in this case.
    assert res["checks"]["cross_family"] is False
    assert "cross_family" in res["reason"]


def test_different_model_family_passes():
    """Codex authors, claude closes -> different model_family, passes."""
    lm = _make_lm(
        actor_id="codex-iter0018-1", model_family="codex",
        post_sha="POST", ts_offset=-100,
    )
    v = _make_verdict(
        actor_id="claude-iter0018-2", model_family="claude",
        target_hash="POST", ts_offset=0,
    )
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is True
    assert res["checks"]["cross_family"] is True


def test_missing_model_family_on_closer_fails():
    """Closer actor missing model_family -> undeterminable cross-family -> FAIL."""
    lm = _make_lm(
        actor_id="codex-iter0018-1", model_family="codex",
        post_sha="POST", ts_offset=-100,
    )
    v = {
        "actor": {
            "id": "claude-iter0018-2",
            # model_family intentionally absent
            "pass_id": "claude-iter0018-2-pass1",
        },
        "review_target_hash": "POST",
        "created_at_utc": _now_iso(0),
    }
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["cross_family"] is False


def test_missing_model_family_on_last_mutation_fails():
    """last_mutation actor missing model_family -> undeterminable -> FAIL."""
    lm = _make_lm(
        actor_id="codex-iter0018-1", model_family="codex",
        post_sha="POST", ts_offset=-100,
    )
    # Strip model_family from the actor dict.
    lm["actor"] = {k: v for k, v in lm["actor"].items() if k != "model_family"}
    v = _make_verdict(
        actor_id="claude-iter0018-2", model_family="claude",
        target_hash="POST", ts_offset=0,
    )
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["cross_family"] is False


# ===========================================================================
# Finding 2 (HIGH): capstone happy-path codex->apply->claude->close
# ===========================================================================
# The capstone test itself is rewritten in test_capstone_full_fix_loop.py.
# Here we add unit-level coverage of the negative case: codex apply + codex
# post-mutation review = blocked at the invariant level.


def test_codex_apply_then_codex_post_mutation_review_blocked():
    """The closer must be the OPPOSITE family from the LAST MUTATOR.

    codex applies -> codex (different actor.id) post-correction reviews and
    closes -> BLOCKED (same model_family).
    """
    lm = _make_lm(
        actor_id="codex-iter0018-A", model_family="codex",
        post_sha="POST", ts_offset=-100,
    )
    v = _make_verdict(
        actor_id="codex-iter0018-B", model_family="codex",
        target_hash="POST", ts_offset=0,
    )
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["cross_family"] is False


# ===========================================================================
# Finding 3 (MEDIUM): mutation-completeness check at close-attempt
# ===========================================================================


def test_unaudited_working_tree_changes_block_close(tmp_path, monkeypatch):
    """If working-tree files changed outside apply.codex_patch (manual edit),
    refuse close with unaudited_mutation_detected.

    Setup: create a fake repo at tmp_path with an iteration dir + an
    apply_step_landed event covering ONE file. Drop a SECOND file with content
    that would show as unaudited mutation. T6 must refuse.
    """
    # Point repo_root + ACTIVE_DIR at tmp_path.
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    importlib.reload(audit_append_event)

    iter_id = "iteration-test-unaudited"
    iter_dir = tmp_path / "consensus-state" / "active" / iter_id
    iter_dir.mkdir(parents=True)

    # Audited file (covered by apply_step_landed).
    audited_rel = "audited_file.py"
    (tmp_path / audited_rel).write_text("audited content\n", encoding="utf-8")
    # UN-audited file: present in iteration_dir but never declared in any
    # apply_step_landed event. This is the smoking gun for unaudited mutation.
    unaudited_rel = "unaudited_file.py"
    (tmp_path / unaudited_rel).write_text("manual edit\n", encoding="utf-8")

    apply_event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(-100),
        "event_id": f"{_now_iso(-100)}_apply_step_landed_codex",
        "effect": "applied patch",
        "actor": {
            "id": "codex-iter0018-1",
            "model_family": "codex",
            "role": "fix_author",
            "pass_id": "codex-iter0018-1-pass1",
        },
        "patch_id": "codex-rev-001-patch",
        "files_touched": [audited_rel],
        "base_sha": "base",
        "post_sha": "POST",
        "unified_diff_sha256": "diff",
        "timestamp": _now_iso(-100),
        "files_modified": [audited_rel],
    }
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": [apply_event]}), encoding="utf-8"
    )

    # Cross-family closer review so the closer-invariant itself would PASS;
    # the new check should still block on unaudited mutation.
    closer = {
        "actor": {
            "id": "claude-iter0018-2",
            "model_family": "claude",
            "pass_id": "claude-iter0018-2-pass1",
        },
        "review_target_hash": "POST",
        "created_at_utc": _now_iso(0),
        "goal_satisfied": True,
    }
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump(closer), encoding="utf-8"
    )

    # Monkeypatch the unaudited-detection helper to report the unaudited file
    # as a working-tree change. The real implementation uses git; tests can't
    # rely on a git repo. The hardening adds a hook that the tool calls.
    from consensus_mcp.tools import audit_append_event as aae
    monkeypatch.setattr(
        aae,
        "_detect_working_tree_changes",
        lambda repo_root: [audited_rel, unaudited_rel],
    )

    result = aae.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="claude-iter0018-2",
        closing_state="quorum_close_passed",
    )
    assert "error" in result
    assert "unaudited_mutation_detected" in result["error"]
    # Both unaudited paths should be surfaced.
    assert unaudited_rel in result["error"]


def test_git_unavailable_blocks_close_fail_closed(tmp_path, monkeypatch):
    """H-5: the mutation-completeness gate must FAIL CLOSED when git is
    unavailable, not silently allow close (fail-open).

    Setup: a normal closeable iteration (apply event + cross-family closer
    review). Then monkeypatch subprocess.run to raise FileNotFoundError (git
    binary missing). Unlike the Finding-3/Finding-5 tests, this test does NOT
    stub _detect_working_tree_changes — it lets the real function body run so
    it raises GitUnavailableError, which handle() must surface as
    mutation_completeness_unverifiable.

    RED today: current code's _detect_working_tree_changes swallows the
    FileNotFoundError (per-cmd `continue`) and returns [], so handle() treats
    the empty set as "no unaudited mutation" and allows the close (success).
    """
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    importlib.reload(audit_append_event)

    iter_id = "iteration-test-git-unavailable"
    iter_dir = tmp_path / "consensus-state" / "active" / iter_id
    iter_dir.mkdir(parents=True)

    audited_rel = "audited_file.py"
    (tmp_path / audited_rel).write_text("audited content\n", encoding="utf-8")

    apply_event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(-100),
        "event_id": f"{_now_iso(-100)}_apply_step_landed_codex",
        "effect": "applied patch",
        "actor": {
            "id": "codex-iter0018-1",
            "model_family": "codex",
            "role": "fix_author",
            "pass_id": "codex-iter0018-1-pass1",
        },
        "patch_id": "codex-rev-001-patch",
        "files_touched": [audited_rel],
        "base_sha": "base",
        "post_sha": "POST",
        "unified_diff_sha256": "diff",
        "timestamp": _now_iso(-100),
        "files_modified": [audited_rel],
    }
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": [apply_event]}), encoding="utf-8"
    )

    closer = {
        "actor": {
            "id": "claude-iter0018-2",
            "model_family": "claude",
            "pass_id": "claude-iter0018-2-pass1",
        },
        "review_target_hash": "POST",
        "created_at_utc": _now_iso(0),
        "goal_satisfied": True,
    }
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump(closer), encoding="utf-8"
    )

    from consensus_mcp.tools import audit_append_event as aae

    # Simulate git missing: every subprocess.run raises FileNotFoundError.
    # The real _detect_working_tree_changes body must run and raise
    # GitUnavailableError, which handle() surfaces fail-closed.
    import subprocess as _subprocess

    def _no_git(*args, **kwargs):
        raise FileNotFoundError("git: command not found")

    monkeypatch.setattr(_subprocess, "run", _no_git)

    result = aae.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="claude-iter0018-2",
        closing_state="quorum_close_passed",
    )
    assert "error" in result, f"expected fail-closed error, got: {result}"
    assert "mutation_completeness_unverifiable" in result["error"]


def test_audited_working_tree_changes_allowed_to_close(tmp_path, monkeypatch):
    """When all working-tree changes are accounted for in apply_step_landed,
    no unaudited_mutation_detected refusal."""
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    importlib.reload(audit_append_event)

    iter_id = "iteration-test-audited"
    iter_dir = tmp_path / "consensus-state" / "active" / iter_id
    iter_dir.mkdir(parents=True)

    audited_rel = "audited_file.py"
    (tmp_path / audited_rel).write_text("audited content\n", encoding="utf-8")

    apply_event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(-100),
        "event_id": f"{_now_iso(-100)}_apply_step_landed_codex",
        "effect": "applied patch",
        "actor": {
            "id": "codex-iter0018-1",
            "model_family": "codex",
            "role": "fix_author",
            "pass_id": "codex-iter0018-1-pass1",
        },
        "patch_id": "codex-rev-001-patch",
        "files_touched": [audited_rel],
        "base_sha": "base",
        "post_sha": "POST",
        "unified_diff_sha256": "diff",
        "timestamp": _now_iso(-100),
        "files_modified": [audited_rel],
    }
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": [apply_event]}), encoding="utf-8"
    )

    closer = {
        "actor": {
            "id": "claude-iter0018-2",
            "model_family": "claude",
            "pass_id": "claude-iter0018-2-pass1",
        },
        "review_target_hash": "POST",
        "created_at_utc": _now_iso(0),
        "goal_satisfied": True,
    }
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump(closer), encoding="utf-8"
    )

    from consensus_mcp.tools import audit_append_event as aae
    monkeypatch.setattr(
        aae,
        "_detect_working_tree_changes",
        lambda repo_root: [audited_rel],  # ALL paths are audited
    )

    result = aae.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="claude-iter0018-2",
        closing_state="quorum_close_passed",
    )
    assert "error" not in result, f"unexpected error: {result.get('error')}"


# ===========================================================================
# Finding 4 (MEDIUM): strict patch_proposal mode
# ===========================================================================


def _strict_codex_output(findings: list[dict]) -> str:
    """Build a complete codex output JSON with the supplied findings list."""
    blocking = [f["id"] for f in findings if f["severity"] in ("blocking", "critical")]
    return json.dumps({
        "findings": findings,
        "goal_satisfied": False if findings else True,
        "blocking_objections": blocking,
        "goal_satisfied_rationale": "test",
    })


def _make_finding(
    *,
    fid: str = "codex-rev-001",
    severity: str = "medium",
    patch_proposal: dict | None = None,
    patch_not_proposed_reason: str | None = None,
):
    f = {
        "id": fid,
        "severity": severity,
        "summary": "test summary",
        "citation": "scripts/foo.py:1",
        "risk": "test risk",
        "recommendation": "test recommendation",
    }
    if patch_proposal is not None:
        f["patch_proposal"] = patch_proposal
    if patch_not_proposed_reason is not None:
        f["patch_not_proposed_reason"] = patch_not_proposed_reason
    return f


def _good_patch_proposal(finding_id: str = "codex-rev-001") -> dict:
    """A schema-valid patch_proposal with iter-0020 finding-id-derived patch_id.

    iter-0028 F4 update: diff body header paths now MUST match files_touched
    (body-vs-declaration consistency check). Prior fixture used 'foo' in the
    diff vs 'scripts/foo.py' in files_touched — that mismatch is now a
    validation error. Use the same path on both sides.
    """
    base_sha = "0" * 64
    diff = "--- a/scripts/foo.py\n+++ b/scripts/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    patch_id = f"{finding_id}-patch"
    return {
        "patch_id": patch_id,
        "applies_to_findings": [finding_id],
        "base_sha": base_sha,
        "unified_diff": diff,
        "files_touched": ["scripts/foo.py"],
        "expected_tests": ["pytest_smoke"],
    }


def _goal_packet_with_policy(policy: str | None) -> dict:
    gp: dict = {
        "schema_version": 1,
        "pilot_id": "test",
        "goal": {"summary": "x"},
        "allowed_files": ["scripts/foo.py"],
        "forbidden_files": [],
        "max_iterations": 5,
        "validators_required": [],
        "acceptance_gates": [],
        "stop_conditions": [],
        "authorization": {"authorized_by": "operator"},
    }
    if policy is not None:
        gp["fix_author_policy"] = policy
    return gp


def test_strict_mode_finding_with_patch_passes():
    """Strict mode: every finding has a patch_proposal -> accepted."""
    finding = _make_finding(patch_proposal=_good_patch_proposal())
    text = _strict_codex_output([finding])
    gp = _goal_packet_with_policy("strict")
    parsed = _dispatch_codex._parse_codex_output(text, goal_packet=gp)
    assert parsed["findings"][0]["patch_proposal"]["patch_id"]


def test_strict_mode_finding_with_reason_passes():
    """Strict mode: finding has patch_not_proposed_reason instead -> accepted."""
    finding = _make_finding(
        patch_not_proposed_reason="cannot author patch from sandbox without applying tools",
    )
    text = _strict_codex_output([finding])
    gp = _goal_packet_with_policy("strict")
    parsed = _dispatch_codex._parse_codex_output(text, goal_packet=gp)
    # The patch_not_proposed_reason field must be preserved in the output.
    assert parsed["findings"][0]["patch_not_proposed_reason"]


def test_strict_mode_finding_missing_both_rejected():
    """Strict mode: finding has neither patch_proposal nor patch_not_proposed_reason -> REJECT entire output."""
    finding = _make_finding()  # plain finding, no patch, no reason
    text = _strict_codex_output([finding])
    gp = _goal_packet_with_policy("strict")
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc_info:
        _dispatch_codex._parse_codex_output(text, goal_packet=gp)
    err = str(exc_info.value)
    assert "fix_author_policy" in err or "patch_not_proposed_reason" in err or "strict" in err


def test_strict_mode_finding_with_both_rejected():
    """Strict mode: mutually exclusive — finding can't have BOTH patch_proposal and patch_not_proposed_reason."""
    finding = _make_finding(
        patch_proposal=_good_patch_proposal(),
        patch_not_proposed_reason="contradiction",
    )
    text = _strict_codex_output([finding])
    gp = _goal_packet_with_policy("strict")
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc_info:
        _dispatch_codex._parse_codex_output(text, goal_packet=gp)
    err = str(exc_info.value)
    assert "mutually exclusive" in err or "both" in err.lower() or "patch_not_proposed_reason" in err


def test_permissive_default_finding_without_patch_passes():
    """Permissive (default) mode: finding without patch_proposal -> accepted (current behavior)."""
    finding = _make_finding()
    text = _strict_codex_output([finding])
    # No fix_author_policy in goal_packet -> defaults to permissive.
    gp = _goal_packet_with_policy(None)
    parsed = _dispatch_codex._parse_codex_output(text, goal_packet=gp)
    assert parsed["findings"][0]["id"] == "codex-rev-001"


def test_permissive_explicit_finding_without_patch_passes():
    """Explicit permissive mode behaves same as default."""
    finding = _make_finding()
    text = _strict_codex_output([finding])
    gp = _goal_packet_with_policy("permissive")
    parsed = _dispatch_codex._parse_codex_output(text, goal_packet=gp)
    assert parsed["findings"][0]["id"] == "codex-rev-001"


def test_strict_mode_only_reason_field_validates_as_string():
    """patch_not_proposed_reason must be a non-empty string when present."""
    finding = _make_finding(patch_not_proposed_reason="")  # empty string
    text = _strict_codex_output([finding])
    gp = _goal_packet_with_policy("strict")
    with pytest.raises(_dispatch_codex.CodexOutputParseError):
        _dispatch_codex._parse_codex_output(text, goal_packet=gp)


# ===========================================================================
# Finding 5 (LOW/MEDIUM): T6 fails closed when invariant evaluator returns
#                          None or raises
# ===========================================================================


def test_t6_fails_closed_when_invariant_evaluator_raises(tmp_path, monkeypatch):
    """If _evaluate_closure_invariant raises and apply_step_landed events exist,
    T6 must REFUSE iteration_closed with closure_invariant_evaluation_failed."""
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    importlib.reload(audit_append_event)

    iter_id = "iteration-test-eval-fail"
    iter_dir = tmp_path / "consensus-state" / "active" / iter_id
    iter_dir.mkdir(parents=True)

    apply_event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(-100),
        "event_id": f"{_now_iso(-100)}_apply_step_landed_codex",
        "effect": "applied patch",
        "actor": {
            "id": "codex-iter0018-1",
            "model_family": "codex",
            "role": "fix_author",
            "pass_id": "codex-iter0018-1-pass1",
        },
        "patch_id": "codex-rev-001-patch",
        "files_touched": ["foo.py"],
        "base_sha": "base",
        "post_sha": "POST",
        "unified_diff_sha256": "diff",
        "timestamp": _now_iso(-100),
        "files_modified": ["foo.py"],
    }
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": [apply_event]}), encoding="utf-8"
    )

    from consensus_mcp.tools import audit_append_event as aae

    def _raises(*args, **kwargs):
        raise RuntimeError("simulated evaluator failure")

    monkeypatch.setattr(aae, "_evaluate_closure_invariant", _raises)
    # Also stub the unaudited-mutation check so it doesn't interfere.
    monkeypatch.setattr(
        aae,
        "_detect_working_tree_changes",
        lambda repo_root: ["foo.py"],
    )

    result = aae.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="claude-iter0018-2",
        closing_state="quorum_close_passed",
    )
    assert "error" in result
    assert "closure_invariant_evaluation_failed" in result["error"]


def test_t6_fails_closed_when_invariant_evaluator_returns_none(tmp_path, monkeypatch):
    """If _evaluate_closure_invariant returns None and apply_step_landed events
    exist, T6 must REFUSE with closure_invariant_evaluation_failed."""
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    importlib.reload(audit_append_event)

    iter_id = "iteration-test-eval-none"
    iter_dir = tmp_path / "consensus-state" / "active" / iter_id
    iter_dir.mkdir(parents=True)

    apply_event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(-100),
        "event_id": f"{_now_iso(-100)}_apply_step_landed_codex",
        "effect": "applied patch",
        "actor": {
            "id": "codex-iter0018-1",
            "model_family": "codex",
            "role": "fix_author",
            "pass_id": "codex-iter0018-1-pass1",
        },
        "patch_id": "codex-rev-001-patch",
        "files_touched": ["foo.py"],
        "base_sha": "base",
        "post_sha": "POST",
        "unified_diff_sha256": "diff",
        "timestamp": _now_iso(-100),
        "files_modified": ["foo.py"],
    }
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": [apply_event]}), encoding="utf-8"
    )

    from consensus_mcp.tools import audit_append_event as aae
    monkeypatch.setattr(
        aae, "_evaluate_closure_invariant", lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        aae, "_detect_working_tree_changes", lambda repo_root: ["foo.py"],
    )

    result = aae.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="claude-iter0018-2",
        closing_state="quorum_close_passed",
    )
    assert "error" in result
    assert "closure_invariant_evaluation_failed" in result["error"]


def test_t6_no_mutation_evaluator_none_still_allowed(tmp_path, monkeypatch):
    """When NO apply_step_landed events exist, evaluator returning None is
    fine (no mutation = no gate). Refusal only fires when the audit log has
    apply_step_landed events but the evaluator can't give a verdict."""
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    importlib.reload(audit_append_event)

    iter_id = "iteration-test-no-mutation-eval"
    iter_dir = tmp_path / "consensus-state" / "active" / iter_id
    iter_dir.mkdir(parents=True)

    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": []}), encoding="utf-8"
    )

    from consensus_mcp.tools import audit_append_event as aae
    # Even if the evaluator returns None, with no apply_step_landed events the
    # close should be allowed.
    monkeypatch.setattr(
        aae, "_evaluate_closure_invariant", lambda *a, **kw: None,
    )
    # H-5: the mutation-completeness gate now FAILS CLOSED when git is
    # unavailable. The pytest tmp_path is not a git repo, so the real helper
    # would (correctly) raise GitUnavailableError. Stub it to return [] — the
    # legitimate "git ran, no working-tree changes" signal — so this test
    # exercises ONLY the no-mutation/evaluator-None path it was written for.
    monkeypatch.setattr(
        aae, "_detect_working_tree_changes", lambda repo_root: [],
    )

    result = aae.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="orchestrator",
        closing_state="blocked_needs_operator",
    )
    assert "error" not in result, f"unexpected error: {result.get('error')}"
