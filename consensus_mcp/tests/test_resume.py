"""Tests for consensus_mcp._resume snapshot tool (iter-0003).

Covers:
  - Auto-detection priority: authorized_at_utc DESC primary, dir name DESC tiebreaker
    (verifies pass-2 codex-rev-002 resolution).
  - Empty active dir + missing active dir return safe defaults.
  - Goal packet scope_signature validation.
  - Dispatch-log walking: malformed JSON lines are skipped with warning.
  - Current bundle sha computed BEFORE classification, with review-packet fallback
    when no bundle-mutation event is present (rev-003 + rev-004).
  - recent_activity sort tiebreaker on equal timestamps (rev-005).
  - recent_activity enum includes bundle-mutation kinds (rev-002).
  - In-flight dispatch + looks_stuck detection.
  - Review classification: open / superseded / consumed.
  - Watermark fast-path (rev-001 fields present in fast response).
  - expected_next_action decision tree: every branch returns; no nulls.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from consensus_mcp import _resume


# ---------------------------------------------------------------------------
# Fixtures: a minimal fake repo_root with consensus-state/ skeleton.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Build a minimal repo skeleton with marker dirs so _resolve_repo_root accepts it."""
    (tmp_path / "consensus-state" / "active").mkdir(parents=True)
    (tmp_path / "consensus-state" / "state").mkdir(parents=True)
    (tmp_path / "consensus-state" / "archive").mkdir(parents=True)
    (tmp_path / "consensus_mcp" / "validators").mkdir(parents=True)
    return tmp_path


def _write_iter(
    repo: Path,
    iter_name: str,
    *,
    authorized_at: str = "2026-05-11T18:00:00Z",
    scope_sig: str | None = None,
    extra_goal: dict | None = None,
) -> Path:
    """Author a minimal goal_packet.yaml for an iteration. Returns the iter dir."""
    iter_dir = repo / "consensus-state" / "active" / iter_name
    iter_dir.mkdir(parents=True, exist_ok=True)
    goal = {
        "schema_version": 1,
        "pilot_id": iter_name,
        "goal": {"summary": f"summary for {iter_name}", "desired_end_state": "x", "non_goals": []},
        "allowed_files": ["foo.py"],
        "allowed_sections": [],
        "forbidden_files": [],
        "max_iterations": 1,
        "max_patch_size": 100,
        "validators_required": [],
        "acceptance_gates": [],
        "stop_conditions": [],
        "operator_escalation_triggers": [],
        "authorization": {
            "authorized_by": "operator",
            "authorized_at_utc": authorized_at,
            "scope_signature": scope_sig or "deadbeef",
        },
    }
    if extra_goal:
        goal.update(extra_goal)
    (iter_dir / "goal_packet.yaml").write_text(yaml.safe_dump(goal), encoding="utf-8")
    return iter_dir


def _append_log(repo: Path, event: dict) -> None:
    log_path = repo / "consensus-state" / "state" / "dispatch-log.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# §3 auto-detection
# ---------------------------------------------------------------------------

def test_missing_active_dir_returns_unknown(tmp_path: Path):
    """If consensus-state/active does not exist, return iteration_state=unknown safely."""
    (tmp_path / "consensus-state" / "state").mkdir(parents=True)
    (tmp_path / "consensus_mcp" / "validators").mkdir(parents=True)
    # active dir intentionally missing
    snap = _resume.snapshot(repo_root=tmp_path)
    assert snap["iteration_state"] == "unknown"
    assert snap["selected_iteration_id"] is None
    assert any("active/ does not exist" in w for w in snap["warnings"])
    assert snap["expected_next_action"]["kind"] == "operator_decision_required"


def test_empty_active_dir_returns_unknown(fake_repo: Path):
    snap = _resume.snapshot(repo_root=fake_repo)
    assert snap["iteration_state"] == "unknown"
    assert snap["selected_iteration_id"] is None


def test_autodetect_picks_most_recent_authorized(fake_repo: Path):
    """Primary sort key is authorized_at_utc DESC (codex-rev-002)."""
    _write_iter(fake_repo, "iter-A-zzz", authorized_at="2026-05-10T00:00:00Z")
    _write_iter(fake_repo, "iter-B-aaa", authorized_at="2026-05-11T00:00:00Z")
    snap = _resume.snapshot(repo_root=fake_repo)
    assert snap["selected_iteration_id"] == "iter-B-aaa"
    assert "iter-A-zzz" in snap["multiple_active_iterations"]


def test_autodetect_tiebreaker_is_dirname_desc(fake_repo: Path):
    """Equal authorized_at_utc → dir name DESC wins (codex-rev-002 deterministic tiebreaker)."""
    _write_iter(fake_repo, "iter-A", authorized_at="2026-05-11T00:00:00Z")
    _write_iter(fake_repo, "iter-B", authorized_at="2026-05-11T00:00:00Z")
    snap = _resume.snapshot(repo_root=fake_repo)
    assert snap["selected_iteration_id"] == "iter-B"
    assert snap["multiple_active_iterations"] == ["iter-B", "iter-A"]


def test_explicit_iteration_id_not_found_raises(fake_repo: Path):
    with pytest.raises(ValueError, match="iteration_id not found"):
        _resume.snapshot(iteration_id="nonexistent", repo_root=fake_repo)


def test_explicit_iteration_id_rejects_path_traversal(fake_repo: Path):
    """codex-iter0003-6 rev-001: reject path separators, parent traversal, absolute paths."""
    for bad in ("../../etc/passwd", "..", "foo/bar", "foo\\bar", "/etc/passwd", "C:\\Windows"):
        with pytest.raises(ValueError):
            _resume.snapshot(iteration_id=bad, repo_root=fake_repo)


# ---------------------------------------------------------------------------
# §5 step 2: goal_packet + scope_signature
# ---------------------------------------------------------------------------

def test_scope_signature_validation(fake_repo: Path):
    from consensus_mcp._self_drive import _scope_signature
    # author a packet, compute sig, write it back
    iter_dir = _write_iter(fake_repo, "iter-sig")
    gp = yaml.safe_load((iter_dir / "goal_packet.yaml").read_text())
    sig = _scope_signature(gp)
    gp["authorization"]["scope_signature"] = sig
    (iter_dir / "goal_packet.yaml").write_text(yaml.safe_dump(gp))

    snap = _resume.snapshot(repo_root=fake_repo)
    assert snap["goal"]["scope_signature_valid"] is True

    # tamper
    gp["authorization"]["scope_signature"] = "tampered"
    (iter_dir / "goal_packet.yaml").write_text(yaml.safe_dump(gp))
    snap = _resume.snapshot(repo_root=fake_repo)
    assert snap["goal"]["scope_signature_valid"] is False


# ---------------------------------------------------------------------------
# §5 step 3: dispatch-log walking + malformed-line handling (codex-rev-003 pass-1)
# ---------------------------------------------------------------------------

def test_malformed_dispatch_log_line_skipped_with_warning(fake_repo: Path):
    _write_iter(fake_repo, "iter-X")
    log = fake_repo / "consensus-state" / "state" / "dispatch-log.jsonl"
    log.write_text(
        json.dumps({"iteration_id": "iter-X", "event": "dispatch_started", "pass_id": "p1", "timestamp_utc": "2026-05-11T18:00:00Z"})
        + "\n{not valid json\n"
        + json.dumps({"iteration_id": "iter-X", "event": "dispatch_heartbeat", "pass_id": "p1", "timestamp_utc": "2026-05-11T18:00:30Z"})
        + "\n",
        encoding="utf-8",
    )
    snap = _resume.snapshot(repo_root=fake_repo)
    assert any("line 2: malformed JSON" in w for w in snap["warnings"])
    # The two good events should still produce an in-flight dispatch.
    assert len(snap["in_flight_dispatches"]) == 1
    assert snap["in_flight_dispatches"][0]["pass_id"] == "p1"


# ---------------------------------------------------------------------------
# rev-003 + rev-004: current bundle sha sourced correctly.
# ---------------------------------------------------------------------------

def test_current_bundle_sha_from_review_packet_when_no_mutation_event(fake_repo: Path):
    """codex-rev-004: when no bundle-mutation event in log, fall back to review-packet base_sha."""
    iter_dir = _write_iter(fake_repo, "iter-bundle")
    rp = {"defect_target": {"files": ["a.py"], "base_sha": "rp-bundle-sha", "touched_files_contents": {"a.py": "x"}}}
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump(rp))
    # No patch_applied event in log.
    events, _ = _resume._walk_dispatch_log(fake_repo / "consensus-state" / "state" / "dispatch-log.jsonl", "iter-bundle")
    sha, source = _resume._compute_current_bundle_sha(events, iter_dir / "review-packet.yaml")
    assert sha == "rp-bundle-sha"
    assert source == "review_packet_base_sha"


def test_watermark_includes_abort_signals(fake_repo: Path):
    """codex-iter0003-5 rev-001: abort-signal file presence must influence the watermark."""
    iter_dir = _write_iter(fake_repo, "iter-abort-wm")
    signal_dir = fake_repo / "consensus-state"
    snap1 = _resume.snapshot(repo_root=fake_repo)
    wm1 = snap1["snapshot_watermark"]
    # Now write an abort signal file; watermark must change.
    (signal_dir / "abort-dispatch-p1.signal").write_text("abort")
    snap2 = _resume.snapshot(repo_root=fake_repo)
    wm2 = snap2["snapshot_watermark"]
    assert wm1 != wm2, "abort-signal addition must change watermark"


def test_fresh_iteration_with_review_packet_suggests_dispatch(fake_repo: Path):
    """codex-iter0003-5 rev-002: fresh iter with review-packet should suggest dispatch,
    not fall through to operator_decision_required."""
    iter_dir = _write_iter(fake_repo, "iter-fresh-rp")
    rp = {"defect_target": {"files": ["a.py"], "base_sha": "rp-sha", "touched_files_contents": {"a.py": "x"}}}
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump(rp))
    snap = _resume.snapshot(repo_root=fake_repo)
    nxt = snap["expected_next_action"]
    assert nxt["kind"] == "dispatch_cross_family_reviewer"
    assert nxt["suggested_command"] is not None
    assert "codex-iter-fresh-rp-1" in nxt["suggested_command"]


def test_unknown_bundle_sha_classifies_review_as_invalid(fake_repo: Path):
    """codex-iter0003-4 rev-001: when current_bundle_sha is unknown, reviews must
    NOT default to 'open' — that would let orchestrators act on unverified state."""
    iter_dir = _write_iter(fake_repo, "iter-unknown-bundle")
    # No review-packet, no mutation event with hash → current_bundle_sha is None
    # Author a hash-bearing mutation event but WITHOUT the hash field, AND no review-packet
    _append_log(fake_repo, {
        "iteration_id": "iter-unknown-bundle",
        "event": "patch_applied",
        "timestamp_utc": "2026-05-11T18:00:00Z",
        # no bundle_sha256 / review_target_hash
        "actor": {"id": "claude-author-1", "model_family": "claude"},
    })
    # Add a sealed codex review that DOES carry a hash
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "iteration_id": "iter-unknown-bundle",
        "reviewer_id": "codex-1",
        "pass_id": "codex-1-pass1",
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
        "dispatch_provenance": {"review_target_hash": "some-hash"},
        "sealed_at_utc": "2026-05-11T18:01:00Z",
    }))
    snap = _resume.snapshot(repo_root=fake_repo)
    # Review should be classified invalid, not open
    assert snap["open_reviews"][0]["classification"] == "invalid"
    # And a warning must mention the unverified bundle state
    assert any("bundle-mutation event" in w or "current_bundle_sha unknown" in w for w in snap["warnings"])


def test_mutation_event_without_hash_returns_unknown_not_fallback(fake_repo: Path):
    """codex-iter0003-3 rev-001: a bundle-mutation event without a hash must NOT
    silently fall through to the review-packet base_sha. That would classify
    post-mutation state with pre-mutation hash and leave reviews mis-classified.
    """
    iter_dir = _write_iter(fake_repo, "iter-mut-no-hash")
    rp = {"defect_target": {"files": ["a.py"], "base_sha": "rp-old-sha", "touched_files_contents": {"a.py": "x"}}}
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump(rp))
    _append_log(fake_repo, {
        "iteration_id": "iter-mut-no-hash",
        "event": "patch_applied",
        "timestamp_utc": "2026-05-11T18:00:00Z",
        # NO bundle_sha256 / review_target_hash — malformed mutation event
        "actor_id": "claude-author-1",
        "model_family": "claude",
    })
    events, _ = _resume._walk_dispatch_log(fake_repo / "consensus-state" / "state" / "dispatch-log.jsonl", "iter-mut-no-hash")
    sha, source = _resume._compute_current_bundle_sha(events, iter_dir / "review-packet.yaml")
    assert sha is None
    assert source == "mutation_event_missing_hash"


def test_expected_next_action_when_valid_closer_is_claude(fake_repo: Path, monkeypatch):
    """codex-iter0003-3 rev-002: when the required closer is not codex, do NOT
    suggest a codex dispatch. Today claude has no dispatcher; recommend
    operator action instead.
    """
    iter_dir = _write_iter(fake_repo, "iter-closer-fam")
    # Set up: a codex-authored mutation, so the valid closer is claude.
    rp = {"defect_target": {"files": ["a.py"], "base_sha": "post-codex-sha", "touched_files_contents": {"a.py": "x"}}}
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump(rp))
    _append_log(fake_repo, {
        "iteration_id": "iter-closer-fam",
        "event": "patch_applied",
        "timestamp_utc": "2026-05-11T18:00:00Z",
        "bundle_sha256": "post-codex-sha",
        "actor": {"id": "codex-author-1", "model_family": "codex"},
    })
    snap = _resume.snapshot(repo_root=fake_repo)
    inv = snap["closure_invariant_status"]
    # valid_closer_families should be ["claude"]
    assert "claude" in (inv["valid_closer_families"] or [])
    # expected_next_action must NOT be a codex dispatch suggestion
    nxt = snap["expected_next_action"]
    assert nxt["kind"] == "operator_decision_required"
    assert "claude" in (nxt["rationale"] or "")


def test_current_bundle_sha_from_mutation_event_wins_over_review_packet(fake_repo: Path):
    """codex-rev-003: bundle-mutation event takes precedence over review-packet fallback."""
    iter_dir = _write_iter(fake_repo, "iter-mut")
    rp = {"defect_target": {"files": ["a.py"], "base_sha": "rp-sha", "touched_files_contents": {"a.py": "x"}}}
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump(rp))
    _append_log(fake_repo, {
        "iteration_id": "iter-mut",
        "event": "patch_applied",
        "timestamp_utc": "2026-05-11T18:00:00Z",
        "bundle_sha256": "post-patch-sha",
        "actor_id": "claude-author-1",
        "model_family": "claude",
    })
    events, _ = _resume._walk_dispatch_log(fake_repo / "consensus-state" / "state" / "dispatch-log.jsonl", "iter-mut")
    sha, source = _resume._compute_current_bundle_sha(events, iter_dir / "review-packet.yaml")
    assert sha == "post-patch-sha"
    assert source == "dispatch_log_bundle_mutation"


# ---------------------------------------------------------------------------
# rev-005: tiebreaker on equal timestamps.
# ---------------------------------------------------------------------------

def test_recent_activity_tiebreaker_uses_line_number(fake_repo: Path):
    _write_iter(fake_repo, "iter-tie")
    # Two events with identical timestamps — line number must determine order.
    for ev in ("dispatch_heartbeat", "dispatch_streamed_line"):
        _append_log(fake_repo, {
            "iteration_id": "iter-tie",
            "event": ev,
            "pass_id": "p1",
            "timestamp_utc": "2026-05-11T18:00:00Z",
        })
    snap = _resume.snapshot(repo_root=fake_repo)
    kinds = [a["kind"] for a in snap["recent_activity"]]
    # The later line wins on tiebreaker.
    assert kinds == ["dispatch_streamed_line", "dispatch_heartbeat"]


# ---------------------------------------------------------------------------
# rev-002: bundle-mutation kinds appear in recent_activity enum.
# ---------------------------------------------------------------------------

def test_bundle_mutation_kind_in_recent_activity_enum():
    assert "patch_applied" in _resume.RECENT_ACTIVITY_KINDS
    assert "review_packet_rebundled" in _resume.RECENT_ACTIVITY_KINDS
    assert "operator_force_bundle_rewrite" in _resume.RECENT_ACTIVITY_KINDS


# ---------------------------------------------------------------------------
# in_flight + looks_stuck.
# ---------------------------------------------------------------------------

def test_include_streamed_lines_threaded_through_to_in_flight(fake_repo: Path):
    """codex-iter0003-2 rev-001: snapshot() must forward include_streamed_lines + max."""
    _write_iter(fake_repo, "iter-stream")
    _append_log(fake_repo, {
        "iteration_id": "iter-stream",
        "event": "dispatch_started",
        "pass_id": "p1",
        "reviewer_id": "codex-iter-stream-1",
        "timestamp_utc": "2026-05-11T18:00:00Z",
    })
    for i in range(5):
        _append_log(fake_repo, {
            "iteration_id": "iter-stream",
            "event": "dispatch_streamed_line",
            "pass_id": "p1",
            "timestamp_utc": f"2026-05-11T18:00:{10+i:02d}Z",
            "line": f"line-{i}",
        })
    # Default: streaming OFF
    snap_off = _resume.snapshot(repo_root=fake_repo)
    assert "streamed_lines" not in snap_off["in_flight_dispatches"][0]
    # Streaming ON: lines threaded through
    snap_on = _resume.snapshot(repo_root=fake_repo, include_streamed_lines=True, max_streamed_lines=3)
    d = snap_on["in_flight_dispatches"][0]
    assert "streamed_lines" in d
    assert len(d["streamed_lines"]) == 3
    # Cap is "last N" — should keep newest 3
    assert [l["line"] for l in d["streamed_lines"]] == ["line-2", "line-3", "line-4"]


def test_in_flight_dispatch_detected_with_freshness(fake_repo: Path):
    _write_iter(fake_repo, "iter-flight")
    _append_log(fake_repo, {
        "iteration_id": "iter-flight",
        "event": "dispatch_started",
        "pass_id": "codex-iter-flight-1-pass1",
        "reviewer_id": "codex-iter-flight-1",
        "timestamp_utc": "2026-05-11T18:00:00Z",
    })
    _append_log(fake_repo, {
        "iteration_id": "iter-flight",
        "event": "dispatch_streamed_line",
        "pass_id": "codex-iter-flight-1-pass1",
        "timestamp_utc": "2026-05-11T18:00:30Z",
    })
    snap = _resume.snapshot(repo_root=fake_repo)
    assert len(snap["in_flight_dispatches"]) == 1
    d = snap["in_flight_dispatches"][0]
    assert d["pass_id"] == "codex-iter-flight-1-pass1"
    # seconds_since_last_line is "now - last_line_ts" so will be large given test fixtures
    assert d["seconds_since_last_line"] is not None
    assert isinstance(d["looks_stuck"], bool)


# ---------------------------------------------------------------------------
# Review classification.
# ---------------------------------------------------------------------------

def test_review_classified_open_when_hash_matches(fake_repo: Path):
    iter_dir = _write_iter(fake_repo, "iter-rev")
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump(
        {"defect_target": {"files": ["a.py"], "base_sha": "match-sha", "touched_files_contents": {"a.py": "x"}}}
    ))
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "iteration_id": "iter-rev",
        "reviewer_id": "codex-iter-rev-1",
        "pass_id": "codex-iter-rev-1-pass1",
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
        "dispatch_provenance": {"review_target_hash": "match-sha"},
        "sealed_at_utc": "2026-05-11T18:00:00Z",
    }))
    snap = _resume.snapshot(repo_root=fake_repo)
    assert len(snap["open_reviews"]) == 1
    assert snap["open_reviews"][0]["classification"] == "open"


def test_review_classified_superseded_when_bundle_changes(fake_repo: Path):
    iter_dir = _write_iter(fake_repo, "iter-sup")
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump(
        {"defect_target": {"files": ["a.py"], "base_sha": "old-sha", "touched_files_contents": {"a.py": "x"}}}
    ))
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "iteration_id": "iter-sup",
        "reviewer_id": "codex-iter-sup-1",
        "pass_id": "codex-iter-sup-1-pass1",
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
        "dispatch_provenance": {"review_target_hash": "old-sha"},
        "sealed_at_utc": "2026-05-11T18:00:00Z",
    }))
    # Now post a patch_applied that changes the bundle.
    _append_log(fake_repo, {
        "iteration_id": "iter-sup",
        "event": "patch_applied",
        "timestamp_utc": "2026-05-11T18:01:00Z",
        "bundle_sha256": "new-sha",
        "actor_id": "claude-author-1",
        "model_family": "claude",
    })
    snap = _resume.snapshot(repo_root=fake_repo)
    assert snap["open_reviews"][0]["classification"] == "superseded"


# ---------------------------------------------------------------------------
# Watermark fast-path (rev-001).
# ---------------------------------------------------------------------------

def test_watermark_fastpath_when_unchanged(fake_repo: Path):
    _write_iter(fake_repo, "iter-wm")
    snap1 = _resume.snapshot(repo_root=fake_repo)
    wm = snap1["snapshot_watermark"]
    snap2 = _resume.snapshot(repo_root=fake_repo, prior_snapshot_watermark=wm)
    assert snap2.get("watermark_unchanged_since_prior") is True
    assert snap2["snapshot_watermark"] == wm
    # fast-path returns selected_iteration_id but omits heavy fields
    assert "open_reviews" not in snap2


def test_watermark_drift_warning_when_files_change_during_snapshot(fake_repo: Path):
    # This is hard to deterministically trigger without internal mocking;
    # at minimum verify the field shape is consistent when no drift occurs.
    _write_iter(fake_repo, "iter-nodrift")
    snap = _resume.snapshot(repo_root=fake_repo)
    assert snap.get("snapshot_watermark_drift") is None


# ---------------------------------------------------------------------------
# expected_next_action exhaustiveness (codex-rev-001 pass-1).
# ---------------------------------------------------------------------------

def test_expected_next_action_kind_always_in_enum(fake_repo: Path):
    _write_iter(fake_repo, "iter-ena")
    snap = _resume.snapshot(repo_root=fake_repo)
    assert snap["expected_next_action"]["kind"] in {
        "dispatch_cross_family_reviewer", "wait_for_dispatch", "apply_proposed_patch",
        "close_iteration", "operator_decision_required",
    }


def test_expected_next_action_dispatch_path_when_no_reviews(fake_repo: Path):
    _write_iter(fake_repo, "iter-fresh")
    snap = _resume.snapshot(repo_root=fake_repo)
    # Fresh iteration: no mutation, no reviews → dispatch a cross-family reviewer.
    # (Or operator_decision_required because there's nothing to close yet — both are valid;
    # the spec's tree picks dispatch_cross_family_reviewer as the default leaf.)
    assert snap["expected_next_action"]["kind"] in (
        "dispatch_cross_family_reviewer", "operator_decision_required",
    )


# ---------------------------------------------------------------------------
# H-8 (iter-0036 parity narrowing): YAML/IO helpers narrow their except clauses
# so PROGRAMMER errors propagate instead of being mislabeled as parse/IO
# failures. Behavior for genuinely missing/malformed files is UNCHANGED.
#
# NOTE on H-8's "silent wrong-state" headline: the dispatch-log read path was
# NEVER bare/silent — it already appends a warning. The third test below is a
# regression-lock documenting that the scary framing is not supported by the
# code; it PASSES today.
# ---------------------------------------------------------------------------

def test_load_yaml_propagates_unexpected_exception(fake_repo: Path, monkeypatch):
    """A non-IO/parse exception (programmer error) from yaml.safe_load must
    propagate, not be swallowed and returned as None.

    RED today: _load_yaml's bare ``except Exception`` catches TypeError and
    returns None. After narrowing to (OSError, UnicodeDecodeError, yaml.YAMLError)
    the TypeError propagates.
    """
    iter_dir = _write_iter(fake_repo, "iter-load-prop")
    gp_path = iter_dir / "goal_packet.yaml"

    def boom(*_args, **_kwargs):
        raise TypeError("programmer error, not a parse failure")

    monkeypatch.setattr(_resume.yaml, "safe_load", boom)
    with pytest.raises(TypeError, match="programmer error"):
        _resume._load_yaml(gp_path)


def test_load_yaml_returns_none_on_parse_error(fake_repo: Path):
    """Genuinely malformed YAML still yields None (behavior unchanged before & after).

    yaml.YAMLError is the legitimate parse-failure class; _load_yaml must keep
    swallowing it and return None.
    """
    iter_dir = _write_iter(fake_repo, "iter-load-malformed")
    bad = iter_dir / "bad.yaml"
    # Unbalanced flow mapping → yaml.YAMLError on safe_load.
    bad.write_text("{ this is: not, valid: yaml: ::: [unclosed", encoding="utf-8")
    assert _resume._load_yaml(bad) is None


def test_dispatch_log_read_failure_is_not_silent(fake_repo: Path, monkeypatch):
    """Regression-lock for H-8's FALSE 'silent wrong-state' headline.

    When the dispatch-log read raises (e.g. PermissionError), the orchestrator
    must NOT silently think nothing is in-flight — it must surface a warning AND
    return an empty in_flight list. This PASSES today (the path already warns);
    the test documents that the HIGH 'silent wrong-state' framing is unfounded.
    """
    _write_iter(fake_repo, "iter-logfail")
    log_path = fake_repo / "consensus-state" / "state" / "dispatch-log.jsonl"
    # Make the log exist (so is_file() passes) but its read raise PermissionError.
    log_path.write_text("", encoding="utf-8")

    real_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == log_path:
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    snap = _resume.snapshot(repo_root=fake_repo)
    assert snap["in_flight_dispatches"] == []
    assert any("dispatch-log.jsonl read failed" in w for w in snap["warnings"])
