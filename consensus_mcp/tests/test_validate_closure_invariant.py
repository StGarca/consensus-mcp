"""Tests for _validate_closure_invariant - Phase 5 of Task #28.

Eight tests covering all verdict states and CLI exit codes.
Uses tmp_path for filesystem isolation; no real iteration dirs touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _validate_closure_invariant as vcl  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(offset_seconds: int = 0) -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_audit(iteration_dir: Path, audit_log: list) -> None:
    p = iteration_dir / "independence-audit.yaml"
    p.write_text(
        yaml.safe_dump({"schema_version": 1, "audit_log": audit_log}),
        encoding="utf-8",
    )


def _write_review(
    iteration_dir: Path,
    filename: str,
    *,
    reviewer_id: str,
    packet_sha256: str,
    sealed_at_utc: str,
) -> None:
    p = iteration_dir / filename
    p.write_text(
        yaml.safe_dump({
            "reviewer_id": reviewer_id,
            "packet_sha256": packet_sha256,
            "sealed_at_utc": sealed_at_utc,
            "goal_satisfied": True,
        }),
        encoding="utf-8",
    )


def _make_apply_event(
    *,
    actor_id: str,
    model_family: str,
    post_sha: str,
    ts: str,
) -> dict:
    return {
        "event": "apply_step_landed",
        "timestamp_utc": ts,
        "event_id": f"{ts}_apply_step_landed_{actor_id}",
        "actor": {
            "id": actor_id,
            "model_family": model_family,
            "role": "fix_author",
            "pass_id": f"{actor_id}-pass1",
        },
        "post_sha": post_sha,
        "base_sha": "base-aaa",
    }


def _make_iteration(tmp_path: Path, name: str = "iteration-test") -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_n_a_no_audit(tmp_path):
    """Iteration with no independence-audit.yaml -> n/a (no audit)."""
    d = _make_iteration(tmp_path)
    # No audit file written.
    result = vcl.scan_iteration(d)
    assert result["verdict"] == "n/a (no audit)"
    assert result["last_mutation"] is None
    assert result["closing_verdict_present"] is False


def test_n_a_no_mutation(tmp_path):
    """Audit exists but has no apply_step_landed events -> n/a (no mutation)."""
    d = _make_iteration(tmp_path)
    _write_audit(d, audit_log=[
        {"event": "review_returned_and_sealed", "timestamp_utc": _ts(), "actor": "codex-x-1"},
    ])
    result = vcl.scan_iteration(d)
    assert result["verdict"] == "n/a (no mutation)"
    assert result["last_mutation"] is None


def test_in_flight(tmp_path):
    """last_mutation present but no closing review with ts > mutation -> in_flight."""
    d = _make_iteration(tmp_path)
    mut_ts = _ts(-200)
    _write_audit(d, audit_log=[
        _make_apply_event(actor_id="codex-x-1", model_family="codex",
                          post_sha="POST-AAA", ts=mut_ts),
    ])
    # Write a review that is OLDER than the mutation (stale).
    _write_review(d, "codex-review.yaml",
                  reviewer_id="codex-x-1",
                  packet_sha256="POST-AAA",
                  sealed_at_utc=_ts(-300))
    result = vcl.scan_iteration(d)
    assert result["verdict"] == "in_flight"
    assert result["last_mutation"] is not None
    assert result["closing_verdict_present"] is False


def test_compliant(tmp_path):
    """last_mutation by codex + fresh claude review of same post_sha -> compliant."""
    d = _make_iteration(tmp_path)
    mut_ts = _ts(-100)
    _write_audit(d, audit_log=[
        _make_apply_event(actor_id="codex-x-1", model_family="codex",
                          post_sha="POST-BBB", ts=mut_ts),
    ])
    # Claude review is newer than mutation and covers the right sha.
    _write_review(d, "claude-review.yaml",
                  reviewer_id="claude-x-2",
                  packet_sha256="POST-BBB",
                  sealed_at_utc=_ts(0))
    result = vcl.scan_iteration(d)
    assert result["verdict"] == "compliant"
    assert result["invariant_check"]["ok"] is True


def test_non_compliant_self_close(tmp_path):
    """last_mutation by codex + closing review also by codex -> non_compliant (cross_family)."""
    d = _make_iteration(tmp_path)
    mut_ts = _ts(-100)
    _write_audit(d, audit_log=[
        _make_apply_event(actor_id="codex-x-1", model_family="codex",
                          post_sha="POST-CCC", ts=mut_ts),
    ])
    # Same actor codex closes - violates cross_family.
    _write_review(d, "codex-review.yaml",
                  reviewer_id="codex-x-1",
                  packet_sha256="POST-CCC",
                  sealed_at_utc=_ts(0))
    result = vcl.scan_iteration(d)
    assert result["verdict"] == "non_compliant"
    assert result["invariant_check"]["ok"] is False
    assert result["invariant_check"]["checks"]["cross_family"] is False


def test_summary_counts(tmp_path):
    """Multi-iteration scan returns correct summary tallies."""
    # iter-a: no audit -> n/a
    a = _make_iteration(tmp_path, "iteration-a")

    # iter-b: no mutation -> n/a
    b = _make_iteration(tmp_path, "iteration-b")
    _write_audit(b, audit_log=[])

    # iter-c: in_flight (mutation, no fresh review)
    c = _make_iteration(tmp_path, "iteration-c")
    mut_ts = _ts(-200)
    _write_audit(c, audit_log=[
        _make_apply_event(actor_id="codex-c-1", model_family="codex",
                          post_sha="POST-C", ts=mut_ts),
    ])

    # iter-d: compliant
    d = _make_iteration(tmp_path, "iteration-d")
    _write_audit(d, audit_log=[
        _make_apply_event(actor_id="codex-d-1", model_family="codex",
                          post_sha="POST-D", ts=_ts(-100)),
    ])
    _write_review(d, "claude-review.yaml",
                  reviewer_id="claude-d-2",
                  packet_sha256="POST-D",
                  sealed_at_utc=_ts(0))

    # iter-e: non_compliant
    e = _make_iteration(tmp_path, "iteration-e")
    _write_audit(e, audit_log=[
        _make_apply_event(actor_id="codex-e-1", model_family="codex",
                          post_sha="POST-E", ts=_ts(-100)),
    ])
    _write_review(e, "codex-review.yaml",
                  reviewer_id="codex-e-1",
                  packet_sha256="POST-E",
                  sealed_at_utc=_ts(0))

    output = vcl.scan_all([a, b, c, d, e])
    s = output["summary"]
    assert s["total"] == 5
    assert s["compliant"] == 1
    assert s["non_compliant"] == 1
    assert s["n/a"] == 2
    assert s["in_flight"] == 1


def test_exit_code_zero_when_all_compliant(tmp_path, monkeypatch):
    """CLI exits 0 when no non_compliant iterations found."""
    d = _make_iteration(tmp_path, "iteration-ok")
    _write_audit(d, audit_log=[
        _make_apply_event(actor_id="codex-ok-1", model_family="codex",
                          post_sha="POST-OK", ts=_ts(-100)),
    ])
    _write_review(d, "claude-review.yaml",
                  reviewer_id="claude-ok-2",
                  packet_sha256="POST-OK",
                  sealed_at_utc=_ts(0))

    monkeypatch.setattr(sys, "argv", ["_validate_closure_invariant",
                                      "--active-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        vcl.main()
    assert exc.value.code == 0


def test_exit_code_one_on_any_non_compliant(tmp_path, monkeypatch):
    """CLI exits 1 when any non_compliant iteration found."""
    e = _make_iteration(tmp_path, "iteration-bad")
    _write_audit(e, audit_log=[
        _make_apply_event(actor_id="codex-bad-1", model_family="codex",
                          post_sha="POST-BAD", ts=_ts(-100)),
    ])
    _write_review(e, "codex-review.yaml",
                  reviewer_id="codex-bad-1",
                  packet_sha256="POST-BAD",
                  sealed_at_utc=_ts(0))

    monkeypatch.setattr(sys, "argv", ["_validate_closure_invariant",
                                      "--active-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        vcl.main()
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# iter-0024 F3-005: reviewer_id prefix heuristic requires dash boundary.
# ---------------------------------------------------------------------------


def test_reviewer_id_codexica_does_not_match_codex_family(tmp_path):
    """F3-005: 'codexica-bot' must NOT route to codex family (no dash boundary)."""
    d = _make_iteration(tmp_path, "iteration-prefix-test")
    review_path = d / "claude-review.yaml"
    # File starts with 'claude-' but reviewer_id starts with 'codexica' - neither
    # the reviewer_id prefix nor the file-name prefix should yield 'codex'.
    review_path.write_text(
        yaml.safe_dump({
            "reviewer_id": "codexica-bot",
            "packet_sha256": "POST-X",
            "sealed_at_utc": _ts(0),
            "goal_satisfied": True,
        }),
        encoding="utf-8",
    )
    verdict = vcl._load_review_yaml(review_path)
    assert verdict is not None
    # Heuristic should fall through to filename, which IS 'claude-' (legitimate
    # dash boundary) -> family becomes 'claude' from filename, not from id.
    assert verdict["actor"]["model_family"] == "claude"


def test_reviewer_id_unrecognised_prefix_yields_no_family(tmp_path):
    """F3-005: reviewer_id 'orch-1' in a file without 'codex-' / 'claude-' prefix
    leaves family unset, which makes cross_family fail closed downstream.
    """
    d = _make_iteration(tmp_path, "iteration-orch")
    review_path = d / "supervisor-review.yaml"
    review_path.write_text(
        yaml.safe_dump({
            "reviewer_id": "orch-1",
            "packet_sha256": "POST-Y",
            "sealed_at_utc": _ts(0),
            "goal_satisfied": True,
        }),
        encoding="utf-8",
    )
    verdict = vcl._load_review_yaml(review_path)
    assert verdict is not None
    assert "model_family" not in verdict["actor"]


def test_reviewer_id_canonical_codex_prefix_matches(tmp_path):
    """F3-005 regression guard: canonical 'codex-iter0024-1' still maps to codex."""
    d = _make_iteration(tmp_path, "iteration-canonical-codex")
    review_path = d / "codex-review.yaml"
    review_path.write_text(
        yaml.safe_dump({
            "reviewer_id": "codex-iter0024-1",
            "packet_sha256": "POST-Z",
            "sealed_at_utc": _ts(0),
            "goal_satisfied": True,
        }),
        encoding="utf-8",
    )
    verdict = vcl._load_review_yaml(review_path)
    assert verdict["actor"]["model_family"] == "codex"


# ---------------------------------------------------------------------------
# iter-0024 F3-007: most-recent verdict picks BOTH-reviews case correctly.
# ---------------------------------------------------------------------------


def test_most_recent_verdict_picks_newer_when_both_present(tmp_path):
    """F3-007: both claude-review.yaml AND codex-review.yaml present; the
    newer sealed_at_utc wins as the closing_verdict.
    """
    d = _make_iteration(tmp_path, "iteration-both-reviews")
    mut_ts = _ts(-300)
    _write_audit(d, audit_log=[
        _make_apply_event(actor_id="orch-actor", model_family="orchestrator",
                          post_sha="POST-BOTH", ts=mut_ts),
    ])
    # claude review is older (sealed_at -100), codex review is newer (sealed_at 0).
    _write_review(d, "claude-review.yaml",
                  reviewer_id="claude-iter0024-1",
                  packet_sha256="POST-BOTH",
                  sealed_at_utc=_ts(-100))
    _write_review(d, "codex-review.yaml",
                  reviewer_id="codex-iter0024-2",
                  packet_sha256="POST-BOTH",
                  sealed_at_utc=_ts(0))
    result = vcl.scan_iteration(d)
    # codex review is newer -> it is the closing verdict.
    assert result["closing_verdict_present"] is True
    # cross_family pass: codex closer vs orchestrator mutation family.
    inv = result["invariant_check"]
    assert inv is not None
