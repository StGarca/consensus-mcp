"""Unit tests for the closure-cross-verification-and-freshness invariant
(Task #28, codex 2026-05-10 v3+v4 directive).

Three independent enforcement layers are tested here:
  1. _closure_invariant module helpers (bundle_sha, last_mutation_from_audit,
     check_closure_invariant)
  2. _self_drive.cmd_check_stop_rules new `closure_cross_verification_failed`
     stop rule
  3. tools/loop_run_goal._detect_state_from_files transition guard
     (`blocked_closure_invariant_failed` state)
  4. tools/audit_append_event refusal to record `iteration_closed` when the
     invariant fails, and authoring of `closure-certificate.yaml` when it
     passes.

The 9 acceptance tests from the directive memory are the contract; the
helper-level unit tests catch lower-level bugs.

Tests use tmp_path for filesystem isolation and monkeypatch where needed.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest.mock as _mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _closure_invariant  # noqa: E402
from consensus_mcp import _self_drive  # noqa: E402
from consensus_mcp.tools import audit_append_event  # noqa: E402
from consensus_mcp.tools import loop_run_goal  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _now_iso(offset_seconds: int = 0) -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_file(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _make_apply_step_event(
    *,
    actor_id: str,
    model_family: str,
    role: str,
    pass_id: str,
    patch_id: str,
    files_touched: list[str],
    base_sha: str,
    post_sha: str,
    unified_diff_sha256: str,
    timestamp: str | None = None,
) -> dict:
    """Build a structured apply_step_landed event in the post-#28 shape."""
    return {
        "event": "apply_step_landed",
        "timestamp_utc": timestamp or _now_iso(),
        "event_id": f"{timestamp or _now_iso()}_apply_step_landed_{actor_id}",
        "effect": f"applied patch {patch_id}",
        "actor": {
            "id": actor_id,
            "model_family": model_family,
            "role": role,
            "pass_id": pass_id,
        },
        "patch_id": patch_id,
        "files_touched": files_touched,
        "base_sha": base_sha,
        "post_sha": post_sha,
        "unified_diff_sha256": unified_diff_sha256,
        "files_modified": files_touched,
    }


def _make_legacy_apply_step_event(timestamp: str | None = None) -> dict:
    """Pre-#28 shape: only `effect` + optional `files_modified`."""
    ts = timestamp or _now_iso()
    return {
        "event": "apply_step_landed",
        "timestamp_utc": ts,
        "event_id": f"{ts}_apply_step_landed_anon",
        "effect": "legacy effect text",
        "files_modified": ["foo.py"],
    }


# ---------------------------------------------------------------------------
# Helper unit tests: bundle_sha
# ---------------------------------------------------------------------------


def test_bundle_sha_deterministic(tmp_path):
    """Same files, same content -> same hash."""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("alpha\n", encoding="utf-8")
    b.write_text("bravo\n", encoding="utf-8")
    h1 = _closure_invariant.bundle_sha(tmp_path, ["a.py", "b.py"])
    h2 = _closure_invariant.bundle_sha(tmp_path, ["a.py", "b.py"])
    assert h1 == h2
    # Order-insensitivity: helper must sort internally.
    h3 = _closure_invariant.bundle_sha(tmp_path, ["b.py", "a.py"])
    assert h1 == h3


def test_bundle_sha_path_sensitive(tmp_path):
    """Different files (different paths) -> different hashes."""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("same\n", encoding="utf-8")
    b.write_text("same\n", encoding="utf-8")
    h_a = _closure_invariant.bundle_sha(tmp_path, ["a.py"])
    h_b = _closure_invariant.bundle_sha(tmp_path, ["b.py"])
    # Same content but different path -> different bundle hash because
    # the canonical form prefixes the path before hashing.
    assert h_a != h_b


def test_bundle_sha_missing_file_treated_as_empty(tmp_path):
    """Missing files contribute b'' content hash (allowed for delete patches)."""
    h_missing = _closure_invariant.bundle_sha(tmp_path, ["does_not_exist.py"])
    # Compute expected: sha256 of "does_not_exist.py\0" + sha256(b"")
    import hashlib
    empty_content_hash = hashlib.sha256(b"").hexdigest()
    canonical = f"does_not_exist.py\0{empty_content_hash}"
    expected = hashlib.sha256(canonical.encode()).hexdigest()
    assert h_missing == expected


def test_bundle_sha_path_separator_normalised(tmp_path):
    """iter-0024 F3-004: Windows-style 'a\\b.py' and POSIX 'a/b.py' hash equal."""
    # Create the file at the POSIX path on disk.
    sub = tmp_path / "a"
    sub.mkdir()
    (sub / "b.py").write_text("hello\n", encoding="utf-8")
    h_posix = _closure_invariant.bundle_sha(tmp_path, ["a/b.py"])
    h_winsep = _closure_invariant.bundle_sha(tmp_path, ["a\\b.py"])
    assert h_posix == h_winsep, "backslash-separated paths must normalise to forward-slash form"


def test_bundle_sha_rejects_path_with_null_byte(tmp_path):
    """iter-0024 F3-004: paths containing \\0 (canonical field separator) raise ValueError."""
    with pytest.raises(ValueError, match="forbidden"):
        _closure_invariant.bundle_sha(tmp_path, ["a\0b.py"])


def test_bundle_sha_rejects_path_with_newline(tmp_path):
    """iter-0024 F3-004: paths containing \\n (canonical record separator) raise ValueError."""
    with pytest.raises(ValueError, match="forbidden"):
        _closure_invariant.bundle_sha(tmp_path, ["a\nb.py"])


# ---------------------------------------------------------------------------
# Helper unit tests: last_mutation_from_audit
# ---------------------------------------------------------------------------


def test_last_mutation_from_audit_returns_most_recent():
    """When multiple apply_step_landed events exist, the most recent wins."""
    older = _make_apply_step_event(
        actor_id="codex-iter0017-1",
        model_family="codex",
        role="fix_author",
        pass_id="codex-iter0017-1-pass1",
        patch_id="patch-aaaaaaaaaaaa-bbbbbbbbbbbb",
        files_touched=["foo.py"],
        base_sha="base1",
        post_sha="post1",
        unified_diff_sha256="diff1",
        timestamp=_now_iso(-1000),
    )
    newer = _make_apply_step_event(
        actor_id="claude-iter0017-2",
        model_family="claude",
        role="correction_author",
        pass_id="claude-iter0017-2-pass1",
        patch_id="patch-cccccccccccc-dddddddddddd",
        files_touched=["bar.py"],
        base_sha="base2",
        post_sha="post2",
        unified_diff_sha256="diff2",
        timestamp=_now_iso(),
    )
    audit_log = [older, newer]
    lm = _closure_invariant.last_mutation_from_audit(audit_log)
    assert lm is not None
    assert lm["actor"]["id"] == "claude-iter0017-2"
    assert lm["post_sha"] == "post2"
    assert lm["patch_id"] == "patch-cccccccccccc-dddddddddddd"


def test_last_mutation_from_audit_none_when_no_apply_step_landed():
    """No apply_step_landed events -> None (close paths without mutation aren't gated)."""
    audit_log = [
        {"event": "review_packet_built", "timestamp_utc": _now_iso()},
        {"event": "consensus_built", "timestamp_utc": _now_iso()},
    ]
    assert _closure_invariant.last_mutation_from_audit(audit_log) is None
    assert _closure_invariant.last_mutation_from_audit([]) is None


def test_last_mutation_from_audit_handles_legacy_events():
    """Pre-#28 apply_step_landed events without structured fields don't crash."""
    legacy = _make_legacy_apply_step_event(_now_iso())
    lm = _closure_invariant.last_mutation_from_audit([legacy])
    # Returns SOMETHING (best-effort) - invariant check downstream will fail
    # when the missing required fields are accessed, but the helper itself
    # doesn't crash.
    assert lm is not None
    assert lm.get("event") == "apply_step_landed" or lm.get("timestamp") is not None


# ---------------------------------------------------------------------------
# iter-0024 F1: merge-direction fix - nested structured form wins over
# top-level legacy fields when both are present on the same event.
# ---------------------------------------------------------------------------


def test_last_mutation_nested_actor_dict_beats_legacy_top_level_string():
    """F1: when event has top-level legacy actor string AND nested structured
    actor dict (apply_step_landed shape that audit_append_event writes), the
    nested dict wins.

    Reproduces the iter-0023 cross_family failure: legacy string actor
    overwrites the nested dict so _actor_model_family returns None.
    """
    event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(),
        # Top-level legacy form (flattened by audit_append_event from actor.id).
        "actor": "codex-iter0023-1",
        "extra_fields": {
            "last_mutation": {
                "actor": {
                    "id": "codex-iter0023-1",
                    "model_family": "codex",
                    "role": "fix_author",
                    "pass_id": "codex-iter0023-1-pass1",
                },
                "post_sha": "POST_NESTED",
                "patch_id": "patch-x",
                "files_touched": ["foo.py"],
                "base_sha": "BASE_NESTED",
                "unified_diff_sha256": "DIFF_NESTED",
            },
        },
    }
    lm = _closure_invariant.last_mutation_from_audit([event])
    assert lm is not None
    # The nested structured dict won the merge.
    assert isinstance(lm["actor"], dict), f"actor must be the nested dict, got {type(lm['actor'])!r}"
    assert lm["actor"]["model_family"] == "codex"
    assert lm["actor"]["id"] == "codex-iter0023-1"
    # post_sha hoisted from nested.
    assert lm["post_sha"] == "POST_NESTED"


def test_last_mutation_top_level_only_event_unchanged():
    """F1 regression guard: a pure top-level (no nested last_mutation) event
    is returned as-is. The merge only runs when nested form is present.
    """
    event = _make_apply_step_event(
        actor_id="codex-iter0024-1",
        model_family="codex",
        role="fix_author",
        pass_id="codex-iter0024-1-pass1",
        patch_id="patch-top",
        files_touched=["foo.py"],
        base_sha="BASE_TOP",
        post_sha="POST_TOP",
        unified_diff_sha256="DIFF_TOP",
        timestamp=_now_iso(),
    )
    lm = _closure_invariant.last_mutation_from_audit([event])
    assert lm is not None
    assert isinstance(lm["actor"], dict)
    assert lm["actor"]["model_family"] == "codex"
    assert lm["post_sha"] == "POST_TOP"


def test_last_mutation_nested_only_event_hoisted_cleanly():
    """F1: pure-nested form (no top-level actor/post_sha) - nested is hoisted."""
    event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(),
        "extra_fields": {
            "last_mutation": {
                "actor": {
                    "id": "claude-iter0024-1",
                    "model_family": "claude",
                    "role": "correction_author",
                    "pass_id": "claude-iter0024-1-pass1",
                },
                "post_sha": "POST_NESTED_ONLY",
                "patch_id": "patch-y",
                "files_touched": ["bar.py"],
                "base_sha": "BASE_NESTED_ONLY",
                "unified_diff_sha256": "DIFF_NESTED_ONLY",
            },
        },
    }
    lm = _closure_invariant.last_mutation_from_audit([event])
    assert lm is not None
    assert isinstance(lm["actor"], dict)
    assert lm["actor"]["model_family"] == "claude"
    assert lm["post_sha"] == "POST_NESTED_ONLY"


def test_last_mutation_both_present_identical_idempotent():
    """F1: top-level and nested both present with identical values - either
    wins (merge is idempotent on agreed values)."""
    actor_dict = {
        "id": "codex-iter0024-1",
        "model_family": "codex",
        "role": "fix_author",
        "pass_id": "codex-iter0024-1-pass1",
    }
    event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(),
        "actor": actor_dict,
        "post_sha": "SAME",
        "extra_fields": {
            "last_mutation": {
                "actor": actor_dict,
                "post_sha": "SAME",
            },
        },
    }
    lm = _closure_invariant.last_mutation_from_audit([event])
    assert lm["actor"] == actor_dict
    assert lm["post_sha"] == "SAME"


# ---------------------------------------------------------------------------
# iter-0024 F3-003: sort apply_events by timestamp_utc, not list order.
# ---------------------------------------------------------------------------


def test_last_mutation_sorted_by_timestamp_not_list_order():
    """Out-of-order list with two events: timestamp ordering wins."""
    newer_ts = _now_iso(0)
    older_ts = _now_iso(-3600)
    # Put the OLDER event last in the list - pre-F3-003 logic returned this.
    newer = _make_apply_step_event(
        actor_id="codex-newer",
        model_family="codex",
        role="fix_author",
        pass_id="codex-newer-pass1",
        patch_id="patch-newer",
        files_touched=["foo.py"],
        base_sha="b",
        post_sha="POST_NEWER",
        unified_diff_sha256="d",
        timestamp=newer_ts,
    )
    older = _make_apply_step_event(
        actor_id="codex-older",
        model_family="codex",
        role="fix_author",
        pass_id="codex-older-pass1",
        patch_id="patch-older",
        files_touched=["bar.py"],
        base_sha="b",
        post_sha="POST_OLDER",
        unified_diff_sha256="d",
        timestamp=older_ts,
    )
    audit_log = [newer, older]  # newer FIRST, older LAST in list
    lm = _closure_invariant.last_mutation_from_audit(audit_log)
    # Pre-F3-003 returned audit_log[-1] = older. Post-F3-003: timestamp wins.
    assert lm["post_sha"] == "POST_NEWER"


def test_last_mutation_no_timestamps_falls_back_to_list_order():
    """All events lacking timestamps - stable sort preserves list order."""
    e1 = {"event": "apply_step_landed", "post_sha": "FIRST"}
    e2 = {"event": "apply_step_landed", "post_sha": "SECOND"}
    e3 = {"event": "apply_step_landed", "post_sha": "THIRD"}
    lm = _closure_invariant.last_mutation_from_audit([e1, e2, e3])
    assert lm["post_sha"] == "THIRD"


# ---------------------------------------------------------------------------
# iter-0024 F3-006: test gaps - hash_match None==None, legacy string actor.
# ---------------------------------------------------------------------------


def test_hash_match_none_equals_none_fails_closed():
    """F3-006: both post_sha and review_target_hash being None must NOT match.

    Pins fail-closed semantics: None==None is not a hash match.
    """
    lm = {
        "actor": {"id": "codex-iter0024-1", "model_family": "codex"},
        "post_sha": None,
        "timestamp": _now_iso(-100),
    }
    v = {
        "actor": {"id": "claude-iter0024-2", "model_family": "claude"},
        "review_target_hash": None,
        "created_at_utc": _now_iso(0),
    }
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["hash_match"] is False


def test_legacy_string_actor_on_last_mutation_fails_cross_family():
    """F3-006: when last_mutation.actor is a string (legacy), model_family is
    None and cross_family fails closed regardless of closer's family.
    """
    lm = {
        "actor": "codex-legacy-1",  # legacy string, not dict
        "post_sha": "POST",
        "timestamp": _now_iso(-100),
    }
    v = _make_verdict(
        actor_id="claude-iter0024-1",
        model_family="claude",
        target_hash="POST",
        ts_offset=0,
    )
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["cross_family"] is False


# ---------------------------------------------------------------------------
# 9 acceptance tests - helper-level invariant check
# (driven through check_closure_invariant directly; no filesystem dependency)
# ---------------------------------------------------------------------------


def _make_lm(*, actor_id, model_family, post_sha, ts_offset=0):
    """Build a last_mutation event-object."""
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
        "files_touched": ["foo.py"],
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


def test_codex_patch_then_codex_close_blocked():
    """#1 Same-actor close - invariant fires cross_family (same id, same family)."""
    lm = _make_lm(actor_id="codex-iter0017-1", model_family="codex", post_sha="POST", ts_offset=-100)
    v = _make_verdict(actor_id="codex-iter0017-1", model_family="codex",
                      target_hash="POST", ts_offset=0)
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["cross_family"] is False
    assert "cross_family" in res["reason"]


def test_codex_patch_then_claude_approve_allowed():
    """#2 Happy path: codex authors, claude closes. All three pass."""
    lm = _make_lm(actor_id="codex-iter0017-1", model_family="codex", post_sha="POST", ts_offset=-100)
    v = _make_verdict(actor_id="claude-iter0017-2", model_family="claude",
                      target_hash="POST", ts_offset=0)
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is True
    assert res["checks"] == {"cross_family": True, "hash_match": True, "freshness": True}


def test_codex_patch_claude_correction_claude_close_blocked():
    """#3 claude is last_mutation; claude tries to close -> cross_family fail (same family)."""
    lm = _make_lm(actor_id="claude-iter0017-2", model_family="claude", post_sha="POST", ts_offset=-100)
    v = _make_verdict(actor_id="claude-iter0017-2", model_family="claude",
                      target_hash="POST", ts_offset=0)
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["cross_family"] is False


def test_codex_patch_claude_correction_codex_post_correction_review_allowed():
    """#4 After claude correction, codex reviews POST hash with newer ts -> allowed."""
    lm = _make_lm(actor_id="claude-iter0017-2", model_family="claude", post_sha="POST", ts_offset=-100)
    v = _make_verdict(actor_id="codex-iter0017-3", model_family="codex",
                      target_hash="POST", ts_offset=0)
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is True


def test_codex_refix_then_codex_rereview_blocked():
    """#5 Codex re-fix then codex re-review -> cross_family fail (same model_family)."""
    lm = _make_lm(actor_id="codex-iter0017-3", model_family="codex", post_sha="POST", ts_offset=-100)
    v = _make_verdict(actor_id="codex-iter0017-3", model_family="codex",
                      target_hash="POST", ts_offset=0)
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["cross_family"] is False


def test_stale_claude_review_before_codex_refix_blocked():
    """#6 Claude review timestamp BEFORE last_mutation timestamp -> freshness fail."""
    # last_mutation is "now"; claude verdict is from 1 hour ago.
    lm = _make_lm(actor_id="codex-iter0017-1", model_family="codex", post_sha="POST", ts_offset=0)
    v = _make_verdict(actor_id="claude-iter0017-2", model_family="claude",
                      target_hash="POST", ts_offset=-3600)
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["freshness"] is False


def test_review_target_hash_mismatch_blocked():
    """#7 closer's review_target_hash != last_mutation.post_sha -> hash_match fail."""
    lm = _make_lm(actor_id="codex-iter0017-1", model_family="codex", post_sha="POST_REAL", ts_offset=-100)
    v = _make_verdict(actor_id="claude-iter0017-2", model_family="claude",
                      target_hash="POST_FAKE", ts_offset=0)
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["hash_match"] is False


def test_no_mutation_yet_close_not_gated():
    """#8 last_mutation is None -> invariant trivially passes."""
    v = _make_verdict(actor_id="claude-iter0017-1", model_family="claude",
                      target_hash="ANY", ts_offset=0)
    res = _closure_invariant.check_closure_invariant(None, v)
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# v1.20.0 host_peer LOAD-BEARING REGRESSION:
# the cross-family closure invariant must NOT be weakened. A same-family blind
# SWE-reviewer (host_peer) carries gate_eligible=false in its closing verdict
# and can NEVER be the different-family signer that closes a mutation -
# regardless of whether its family matches the mutator's. A genuinely
# different, gate-eligible family is still required.
# ---------------------------------------------------------------------------


def _make_host_peer_verdict(*, actor_id, model_family, target_hash, ts_offset=0):
    """A host_peer closing verdict: cross-family-DIFFERENT actor but tagged
    gate_eligible=false + weight=supplementary (a same-family blind reviewer)."""
    v = _make_verdict(
        actor_id=actor_id, model_family=model_family,
        target_hash=target_hash, ts_offset=ts_offset,
    )
    v["gate_eligible"] = False
    v["weight"] = "supplementary"
    v["independence_attestation"] = {
        "method": "host_peer_callback",
        "fresh_context": True,
        "no_peer_review_visible_at_dispatch": True,
    }
    return v


def test_claude_mutator_claude_host_peer_cannot_close():
    """host_peer is SAME family as the mutator (claude+claude) - must NOT close.

    This already fails on the existing cross_family check (same family); the
    gate_eligible=false tag is belt-and-suspenders. Pins that [host + host_peer]
    cannot close a host-authored change.
    """
    lm = _make_lm(actor_id="claude-iter1200-1", model_family="claude",
                  post_sha="POST", ts_offset=-100)
    v = _make_host_peer_verdict(actor_id="claude-swe-reviewer-iter1200",
                                model_family="claude",
                                target_hash="POST", ts_offset=0)
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False
    assert res["checks"]["cross_family"] is False


def test_codex_mutator_claude_host_peer_cannot_close_despite_different_family():
    """THE load-bearing case: a claude host_peer reviewing a CODEX-authored
    change is cross-family-DIFFERENT, hash-matched, and fresh - yet because it
    is gate_eligible=false it STILL must NOT satisfy cross-family signoff.

    Without the gate_eligible exclusion this would WRONGLY pass (different
    families). It must fail: the host is not independent of its own
    orchestration, so a genuinely external gate-eligible family is required.
    """
    lm = _make_lm(actor_id="codex-iter1200-1", model_family="codex",
                  post_sha="POST", ts_offset=-100)
    v = _make_host_peer_verdict(actor_id="claude-swe-reviewer-iter1200",
                                model_family="claude",
                                target_hash="POST", ts_offset=0)
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is False, (
        "a gate_eligible=false host_peer must NOT satisfy cross-family signoff "
        "even when its family differs from the mutator"
    )
    assert res["checks"]["cross_family"] is False


def test_codex_mutator_genuine_claude_closer_still_passes():
    """Invariant NOT weakened: a REAL (gate-eligible) claude closer over a codex
    mutation still passes. The gate_eligible exclusion only blocks the
    supplementary host_peer, never the genuine cross-family signer."""
    lm = _make_lm(actor_id="codex-iter1200-1", model_family="codex",
                  post_sha="POST", ts_offset=-100)
    v = _make_verdict(actor_id="claude-iter1200-2", model_family="claude",
                      target_hash="POST", ts_offset=0)
    # No gate_eligible key (absent) means gate-eligible by default.
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is True
    assert res["checks"]["cross_family"] is True


def test_gate_eligible_true_closer_unaffected():
    """An explicit gate_eligible=true closer is treated exactly like a normal
    closer (the exclusion only triggers on the literal false)."""
    lm = _make_lm(actor_id="codex-iter1200-1", model_family="codex",
                  post_sha="POST", ts_offset=-100)
    v = _make_verdict(actor_id="claude-iter1200-2", model_family="claude",
                      target_hash="POST", ts_offset=0)
    v["gate_eligible"] = True
    res = _closure_invariant.check_closure_invariant(lm, v)
    assert res["ok"] is True
    assert res["checks"]["cross_family"] is True


def test_closure_certificate_authored_on_pass(tmp_path, monkeypatch):
    """#9 When invariant passes and iteration_closed is recorded, closure-certificate.yaml is authored.

    Drive through the audit_append_event T6 layer. iter-0036: redirect state
    root via env var (NOT monkeypatch.setattr on the tool module - pytest
    teardown leaks __getattr__-synthesized values into __dict__ and poisons
    subsequent tests). Also stub _detect_working_tree_changes so T7's
    unaudited-mutation check is deterministic.
    """
    state_root = tmp_path / "state"
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state_root))
    monkeypatch.setattr(
        audit_append_event, "_detect_working_tree_changes",
        lambda repo_root: ["foo.py"],
    )

    iter_id = "iteration-test-cert"
    iter_dir = state_root / "active" / iter_id
    iter_dir.mkdir(parents=True)

    # Pre-populate an audit log with a structured apply_step_landed event.
    apply_event = _make_apply_step_event(
        actor_id="codex-iter0017-1",
        model_family="codex",
        role="fix_author",
        pass_id="codex-iter0017-1-pass1",
        patch_id="patch-aaaaaaaaaaaa-bbbbbbbbbbbb",
        files_touched=["foo.py"],
        base_sha="base-aaaaaaaaaaaa",
        post_sha="POST_HASH",
        unified_diff_sha256="diff-bbbbbbbbbbbb",
        timestamp=_now_iso(-1000),
    )
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": [apply_event]}, sort_keys=False),
        encoding="utf-8",
    )

    # Author a fresh claude-review.yaml that satisfies the invariant.
    claude_review = {
        "actor": {
            "id": "claude-iter0017-2",
            "model_family": "claude",
            "pass_id": "claude-iter0017-2-pass1",
        },
        "review_target_hash": "POST_HASH",
        "created_at_utc": _now_iso(),
        "goal_satisfied": True,
    }
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump(claude_review, sort_keys=False),
        encoding="utf-8",
    )

    result = audit_append_event.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="claude-iter0017-2",
        closing_state="quorum_close_passed",
    )

    # Refuse-on-fail OR pass: this case should pass; certificate must exist.
    assert "error" not in result, f"unexpected error: {result.get('error')}"
    cert_path = iter_dir / "closure-certificate.yaml"
    assert cert_path.exists(), "closure-certificate.yaml not authored on PASS"
    cert = yaml.safe_load(cert_path.read_text(encoding="utf-8"))
    assert cert["overall"] == "PASS"
    assert cert["invariant_checks"]["cross_family"] == "PASS"
    assert cert["invariant_checks"]["hash_match"] == "PASS"
    assert cert["invariant_checks"]["freshness"] == "PASS"


# ---------------------------------------------------------------------------
# T6 audit_append_event refusal tests
# ---------------------------------------------------------------------------


def test_t6_refuses_iteration_closed_when_invariant_fails(tmp_path, monkeypatch):
    """T6 refuses to write iteration_closed when invariant fails.

    Set up: codex authored last mutation; codex tries to close (cross_family fail).
    iter-0036: env-var redirection instead of unsafe setattr.
    """
    state_root = tmp_path / "state"
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state_root))
    monkeypatch.setattr(
        audit_append_event, "_detect_working_tree_changes",
        lambda repo_root: ["foo.py"],
    )
    iter_id = "iteration-test-refuse"
    iter_dir = state_root / "active" / iter_id
    iter_dir.mkdir(parents=True)

    apply_event = _make_apply_step_event(
        actor_id="codex-iter0017-1",
        model_family="codex",
        role="fix_author",
        pass_id="codex-iter0017-1-pass1",
        patch_id="patch-aaaaaaaaaaaa-bbbbbbbbbbbb",
        files_touched=["foo.py"],
        base_sha="base-x",
        post_sha="POST",
        unified_diff_sha256="diff-y",
        timestamp=_now_iso(-1000),
    )
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": [apply_event]}, sort_keys=False),
        encoding="utf-8",
    )

    # Codex tries to close: same actor as last_mutation -> blocked.
    codex_review = {
        "actor": {
            "id": "codex-iter0017-1",
            "model_family": "codex",
            "pass_id": "codex-iter0017-1-pass2",
        },
        "review_target_hash": "POST",
        "created_at_utc": _now_iso(),
        "goal_satisfied": True,
    }
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump(codex_review, sort_keys=False),
        encoding="utf-8",
    )

    result = audit_append_event.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="codex-iter0017-1",
        closing_state="quorum_close_passed",
    )
    assert "error" in result
    assert "closure_cross_verification_failed" in result["error"]
    # No certificate authored on refusal.
    assert not (iter_dir / "closure-certificate.yaml").exists()


def test_t6_no_mutation_yet_iteration_closed_allowed(tmp_path, monkeypatch):
    """When no apply_step_landed events exist, T6 does NOT block iteration_closed.

    iter-0036: env-var redirection + working-tree stub.
    """
    state_root = tmp_path / "state"
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state_root))
    monkeypatch.setattr(
        audit_append_event, "_detect_working_tree_changes",
        lambda repo_root: [],
    )
    iter_id = "iteration-test-no-mutation"
    iter_dir = state_root / "active" / iter_id
    iter_dir.mkdir(parents=True)
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": []}, sort_keys=False),
        encoding="utf-8",
    )

    result = audit_append_event.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="orchestrator",
        closing_state="blocked_needs_operator",
    )
    assert "error" not in result, f"unexpected error: {result.get('error')}"


# ---------------------------------------------------------------------------
# _self_drive stop-rule tests
# ---------------------------------------------------------------------------


def _write_goal_packet_for_self_drive(tmp_path: Path) -> Path:
    """Minimal goal_packet for cmd_check_stop_rules invocation."""
    packet = {
        "schema_version": 1,
        "pilot_id": "test-pilot",
        "goal": {"summary": "x"},
        "allowed_files": ["scripts/foo.py"],
        "forbidden_files": [],
        "max_iterations": 10,
        "max_patch_size": None,
        "validators_required": [],
        "acceptance_gates": [],
        "stop_conditions": [],
        "authorization": {"authorized_by": "operator"},
    }
    p = tmp_path / "goal_packet.yaml"
    p.write_text(yaml.safe_dump(packet), encoding="utf-8")
    return p


def _run_check_stop_rules(packet_path: Path, iter_dir: Path):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _self_drive.cmd_check_stop_rules(
            argparse.Namespace(goal_packet=str(packet_path), iteration_dir=str(iter_dir))
        )
    out = buf.getvalue()
    return rc, json.loads(out)


def test_self_drive_stop_rule_no_mutation_no_fire(tmp_path):
    """No apply_step_landed events -> stop rule does NOT fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(yaml.safe_dump({"audit_log": []}), encoding="utf-8")

    packet = _write_goal_packet_for_self_drive(tmp_path)
    rc, parsed = _run_check_stop_rules(packet, iter_dir)
    fired = [r["rule"] for r in parsed["stop_rules_fired"]]
    assert "closure_cross_verification_failed" not in fired


def test_self_drive_stop_rule_fires_on_self_close(tmp_path):
    """Codex authored last mutation; codex review is the closer -> rule fires."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()

    apply_event = _make_apply_step_event(
        actor_id="codex-iter0017-1",
        model_family="codex",
        role="fix_author",
        pass_id="codex-iter0017-1-pass1",
        patch_id="patch-x-y",
        files_touched=["foo.py"],
        base_sha="base",
        post_sha="POST",
        unified_diff_sha256="diff",
        timestamp=_now_iso(-100),
    )
    audit_path = iter_dir / "independence-audit.yaml"
    audit_path.write_text(
        yaml.safe_dump({"audit_log": [apply_event]}, sort_keys=False),
        encoding="utf-8",
    )

    codex_review = {
        "actor": {"id": "codex-iter0017-1", "model_family": "codex",
                  "pass_id": "codex-iter0017-1-pass2"},
        "review_target_hash": "POST",
        "created_at_utc": _now_iso(),
        "goal_satisfied": True,
    }
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump(codex_review), encoding="utf-8"
    )

    packet = _write_goal_packet_for_self_drive(tmp_path)
    rc, parsed = _run_check_stop_rules(packet, iter_dir)
    fired = [r["rule"] for r in parsed["stop_rules_fired"]]
    assert "closure_cross_verification_failed" in fired


def test_self_drive_stop_rules_required_includes_new_rule():
    """Contract enumeration includes the new rule (9 of 9)."""
    assert "closure_cross_verification_failed" in _self_drive.STOP_RULES_REQUIRED_BY_CONTRACT
    assert "closure_cross_verification_failed" in _self_drive.STOP_RULES_IMPLEMENTED


# ---------------------------------------------------------------------------
# loop.run_goal transition guard
# ---------------------------------------------------------------------------


def _write_loop_goal_packet(tmp_path: Path) -> Path:
    packet = {
        "schema_version": 1,
        "pilot_id": "test-supervisor-cert",
        "goal": {"summary": "x"},
        "allowed_files": ["scripts/foo.py"],
        "allowed_sections": [],
        "forbidden_files": [],
        "max_iterations": 10,
        "max_patch_size": None,
        "validators_required": [],
        "acceptance_gates": [],
        "stop_conditions": [],
        "operator_escalation_triggers": [],
        "authorization": {"authorized_by": "operator"},
    }
    sig = _self_drive._scope_signature(packet)
    packet["authorization"]["scope_signature"] = sig
    p = tmp_path / "goal_packet.yaml"
    p.write_text(yaml.safe_dump(packet), encoding="utf-8")
    return p


def test_loop_run_goal_blocks_close_on_invariant_failure(tmp_path):
    """When invariant fails on a 'would be ready_to_close' iteration, returns blocked_closure_invariant_failed."""
    iter_dir = tmp_path / "iteration-0001"
    iter_dir.mkdir()

    # Set up files that would otherwise trigger ready_to_close.
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )

    apply_event = _make_apply_step_event(
        actor_id="codex-iter0017-1",
        model_family="codex",
        role="fix_author",
        pass_id="codex-iter0017-1-pass1",
        patch_id="patch-x-y",
        files_touched=["foo.py"],
        base_sha="base",
        post_sha="POST",
        unified_diff_sha256="diff",
        timestamp=_now_iso(-100),
    )
    (iter_dir / "independence-audit.yaml").write_text(
        yaml.safe_dump({"audit_log": [apply_event]}, sort_keys=False),
        encoding="utf-8",
    )

    # Both reviews say goal_satisfied=True with same actor (codex) -> consensus-shape OK,
    # but codex is BOTH last_mutation actor and the most-recent close-target review.
    codex_close_review = {
        "actor": {"id": "codex-iter0017-1", "model_family": "codex",
                  "pass_id": "codex-iter0017-1-pass2"},
        "review_target_hash": "POST",
        "created_at_utc": _now_iso(),
        "goal_satisfied": True,
    }
    claude_pre_review = {
        "actor": {"id": "claude-iter0017-pre", "model_family": "claude",
                  "pass_id": "claude-iter0017-pre"},
        "review_target_hash": "PRE",
        # claude review BEFORE last_mutation timestamp -> stale
        "created_at_utc": _now_iso(-200),
        "goal_satisfied": True,
    }
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump(claude_pre_review), encoding="utf-8"
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump(codex_close_review), encoding="utf-8"
    )
    (iter_dir / "consensus.yaml").write_text(yaml.safe_dump({"state": "ok"}), encoding="utf-8")

    packet = _write_loop_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(packet),
        iteration_dir=str(iter_dir),
        auto_dispatch_codex=False,
    )
    # Should be flagged as blocked, either via stop_rule or new state.
    assert result["state"] in (
        "blocked_closure_invariant_failed",
        "blocked_stop_rule_fired",
    )
