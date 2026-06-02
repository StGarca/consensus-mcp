"""v1.19.0 result-logging tests (TDD: RED -> GREEN).

Covers `consensus_mcp._results_log` (per-iteration results record authoring +
JSONL upsert) and the additive `tools/audit_append_event.py` close hook.

The authored record MUST validate against
`consensus_mcp/schemas/results-v1.schema.json` (the shared contract). Findings
are derived from sealed review passes (claude/codex/gemini-review.yaml), the
converged-plan.yaml when present, and the audit events. Disposition per finding:

  validated_fixed   - finding id linked to an apply event / closure disposition
  dismissed_refuted - a closure disposition says so (carries evidence_ref)
  deferred / open    - otherwise

Conventions mirror test_iter_0018_cross_ai_invariant.py: build a fake iteration
dir under a tmp_path-rooted consensus-state/, point CONSENSUS_MCP_REPO_ROOT at
it, reload audit_append_event so its lazy resolvers pick up the env override.
"""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "results-v1.schema.json"
)

from consensus_mcp import _results_log  # noqa: E402
from consensus_mcp.tools import audit_append_event  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso(offset_seconds: int = 0) -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate(record: dict) -> None:
    jsonschema.validate(instance=record, schema=_load_schema())


def _make_iter_dir(state_root: Path, iter_id: str) -> Path:
    iter_dir = state_root / "active" / iter_id
    iter_dir.mkdir(parents=True)
    return iter_dir


def _write_review(
    iter_dir: Path,
    fname: str,
    reviewer_id: str,
    pass_id: str,
    findings: list[dict],
    model_family: str | None = None,
) -> None:
    review = {
        "iteration_id": iter_dir.name,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "sealed_at_utc": _now_iso(-200),
        "findings": findings,
        "goal_satisfied": False,
    }
    if model_family is not None:
        review["actor"] = {"id": reviewer_id, "model_family": model_family}
    (iter_dir / fname).write_text(yaml.safe_dump(review), encoding="utf-8")


def _finding(fid: str, severity: str = "medium", **extra) -> dict:
    f = {
        "id": fid,
        "severity": severity,
        "summary": f"summary for {fid}",
        "citation": "scripts/foo.py:1",
    }
    f.update(extra)
    return f


def _write_audit(iter_dir: Path, audit_log: list[dict]) -> None:
    (iter_dir / "independence-audit.yaml").write_text(
        yaml.safe_dump({"audit_log": audit_log}), encoding="utf-8"
    )


def _apply_event(finding_ids, files_touched, fix_summary="fixed it", patch_id="p-1"):
    return {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(-100),
        "event_id": f"{_now_iso(-100)}_apply_step_landed_codex",
        "effect": "applied patch",
        "actor": {"id": "codex-1", "model_family": "codex"},
        "patch_id": patch_id,
        "finding_ids": list(finding_ids),
        "files_touched": list(files_touched),
        "fix_summary": fix_summary,
    }


# ---------------------------------------------------------------------------
# _results_log: record building
# ---------------------------------------------------------------------------


def test_build_record_validates_against_schema(tmp_path):
    state_root = tmp_path / "consensus-state"
    iter_id = "iteration-results-basic"
    iter_dir = _make_iter_dir(state_root, iter_id)

    _write_review(
        iter_dir, "codex-review.yaml", "codex-1", "codex-1-pass1",
        [_finding("codex-rev-001", "high"), _finding("codex-rev-002", "low")],
        model_family="codex",
    )
    _write_audit(iter_dir, [])

    record = _results_log.build_results_record(iter_dir)
    _validate(record)
    assert record["consensus_results_schema_version"] == 1
    assert record["iteration_id"] == iter_id
    assert record["confidence"] == "authoritative"
    assert record["backfilled"] is False
    # Two distinct findings carried with provenance.
    ids = sorted(f["id"] for f in record["findings"])
    assert ids == ["codex-rev-001", "codex-rev-002"]
    by_src = {f["id"]: f for f in record["findings"]}
    assert by_src["codex-rev-001"]["source_reviewer"] == "codex-1"
    assert by_src["codex-rev-001"]["source_pass_id"] == "codex-1-pass1"
    assert by_src["codex-rev-001"]["severity"] == "high"


def _write_host_peer_review(
    iter_dir: Path,
    reviewer_id: str,
    pass_id: str,
    findings: list[dict],
    model_family: str = "claude",
) -> None:
    """Write a v1.20.0 host_peer (same-family supplementary) sealed review file."""
    review = {
        "iteration_id": iter_dir.name,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "sealed_at_utc": _now_iso(-150),
        "findings": findings,
        "goal_satisfied": not findings,
        "actor": {"id": reviewer_id, "model_family": model_family, "role": "swe_reviewer"},
        "gate_eligible": False,
        "weight": "supplementary",
        "role": "swe_reviewer",
        "independence_attestation": {
            "method": "host_peer_callback",
            "fresh_context": True,
            "no_peer_review_visible_at_dispatch": True,
        },
        "dispatch_provenance": {"adapter": "host_peer", "family": model_family,
                                "gate_eligible": False, "weight": "supplementary"},
    }
    (iter_dir / "host-peer-review.yaml").write_text(yaml.safe_dump(review), encoding="utf-8")


def test_host_peer_reviewer_tagged_supplementary(tmp_path):
    """v1.20.0: a host_peer (same-family) reviewer is collected and tagged
    supplementary in reviewers[]; its findings carry source_reviewer provenance.
    The record still validates against the schema."""
    state_root = tmp_path / "consensus-state"
    iter_dir = _make_iter_dir(state_root, "iteration-host-peer-results")

    # A genuine cross-family reviewer plus the supplementary host_peer.
    _write_review(
        iter_dir, "codex-review.yaml", "codex-1", "codex-1-pass1",
        [_finding("codex-rev-001", "high")], model_family="codex",
    )
    _write_host_peer_review(
        iter_dir, "claude-swe-reviewer-1", "claude-swe-reviewer-1-pass1",
        [_finding("claude-swe-rev-001", "medium")], model_family="claude",
    )
    _write_audit(iter_dir, [])

    record = _results_log.build_results_record(iter_dir)
    _validate(record)

    revs = {r["name"]: r for r in record["reviewers"]}
    assert revs["codex-1"].get("supplementary") in (None, False)
    assert revs["claude-swe-reviewer-1"]["supplementary"] is True
    assert revs["claude-swe-reviewer-1"]["family"] == "claude"

    by_id = {f["id"]: f for f in record["findings"]}
    assert by_id["claude-swe-rev-001"]["source_reviewer"] == "claude-swe-reviewer-1"
    # The cross-family reviewer is NOT tagged supplementary.
    assert "supplementary" not in revs["codex-1"]


def test_disposition_validated_fixed_when_apply_links_finding(tmp_path):
    state_root = tmp_path / "consensus-state"
    iter_dir = _make_iter_dir(state_root, "iteration-results-fixed")

    _write_review(
        iter_dir, "codex-review.yaml", "codex-1", "codex-1-pass1",
        [_finding("codex-rev-001", "high"), _finding("codex-rev-002", "medium")],
        model_family="codex",
    )
    # codex-rev-001 is fixed by an apply event; codex-rev-002 is untouched.
    _write_audit(iter_dir, [
        _apply_event(["codex-rev-001"], ["scripts/foo.py"], "off-by-one fix"),
    ])

    record = _results_log.build_results_record(iter_dir)
    _validate(record)
    by_id = {f["id"]: f for f in record["findings"]}
    assert by_id["codex-rev-001"]["disposition"] == "validated_fixed"
    assert by_id["codex-rev-001"]["fix"]["patch_id"] == "p-1"
    assert by_id["codex-rev-001"]["fix"]["files"] == ["scripts/foo.py"]
    # Unlinked finding falls to deferred/open (not validated).
    assert by_id["codex-rev-002"]["disposition"] in ("deferred", "open")
    assert record["counts"]["validated"] == 1
    assert record["counts"]["fixes_applied"] >= 1
    assert record["counts"]["by_severity"]["high"] == 1
    assert record["counts"]["by_severity"]["medium"] == 1


def test_disposition_dismissed_refuted_carries_evidence(tmp_path):
    state_root = tmp_path / "consensus-state"
    iter_dir = _make_iter_dir(state_root, "iteration-results-dismissed")

    _write_review(
        iter_dir, "gemini-review.yaml", "gemini-1", "gemini-1-pass1",
        [_finding("gemini-rev-001", "medium")],
        model_family="gemini",
    )
    _write_audit(iter_dir, [])

    dispositions = [
        {
            "id": "gemini-rev-001",
            "disposition": "dismissed_refuted",
            "evidence_ref": "commit:abc123 - finding refuted by grep, file content absent",
        }
    ]
    record = _results_log.build_results_record(
        iter_dir, finding_dispositions=dispositions
    )
    _validate(record)
    by_id = {f["id"]: f for f in record["findings"]}
    assert by_id["gemini-rev-001"]["disposition"] == "dismissed_refuted"
    assert "abc123" in by_id["gemini-rev-001"]["evidence_ref"]
    assert record["counts"]["dismissed"] == 1


def test_findings_deduped_by_id_within_iteration(tmp_path):
    state_root = tmp_path / "consensus-state"
    iter_dir = _make_iter_dir(state_root, "iteration-results-dedup")

    # Same finding id appears in two passes (codex pass1 + claude pass1).
    _write_review(
        iter_dir, "codex-review.yaml", "codex-1", "codex-1-pass1",
        [_finding("shared-001", "high")], model_family="codex",
    )
    _write_review(
        iter_dir, "claude-review.yaml", "claude-1", "claude-1-pass1",
        [_finding("shared-001", "high"), _finding("claude-001", "low")],
        model_family="claude",
    )
    _write_audit(iter_dir, [])

    record = _results_log.build_results_record(iter_dir)
    _validate(record)
    ids = [f["id"] for f in record["findings"]]
    assert ids.count("shared-001") == 1, f"finding not deduped: {ids}"
    assert sorted(set(ids)) == ["claude-001", "shared-001"]


# ---------------------------------------------------------------------------
# _results_log: JSONL upsert (one snapshot per iteration_id)
# ---------------------------------------------------------------------------


def test_jsonl_upsert_keeps_one_snapshot_per_iteration(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path / "consensus-state"))
    state_root = tmp_path / "consensus-state"
    iter_dir = _make_iter_dir(state_root, "iteration-results-upsert")
    _write_review(
        iter_dir, "codex-review.yaml", "codex-1", "codex-1-pass1",
        [_finding("codex-rev-001", "high")], model_family="codex",
    )
    _write_audit(iter_dir, [])

    ledger = state_root / "state" / "results-v1.jsonl"

    # First write.
    _results_log.write_results_record(iter_dir)
    lines1 = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines1) == 1
    rec1 = json.loads(lines1[0])
    assert rec1["iteration_id"] == "iteration-results-upsert"
    _validate(rec1)

    # Second write (re-author) - must REPLACE, not append.
    _results_log.write_results_record(iter_dir)
    lines2 = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines2) == 1, f"upsert appended a duplicate: {lines2}"

    # A DIFFERENT iteration appends a second line.
    iter_dir2 = _make_iter_dir(state_root, "iteration-results-upsert-2")
    _write_review(
        iter_dir2, "codex-review.yaml", "codex-2", "codex-2-pass1",
        [_finding("codex-rev-009", "low")], model_family="codex",
    )
    _write_audit(iter_dir2, [])
    _results_log.write_results_record(iter_dir2)
    lines3 = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines3) == 2
    ids = sorted(json.loads(l)["iteration_id"] for l in lines3)
    assert ids == ["iteration-results-upsert", "iteration-results-upsert-2"]


def test_write_authors_human_readable_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path / "consensus-state"))
    state_root = tmp_path / "consensus-state"
    iter_dir = _make_iter_dir(state_root, "iteration-results-yaml")
    _write_review(
        iter_dir, "codex-review.yaml", "codex-1", "codex-1-pass1",
        [_finding("codex-rev-001", "high")], model_family="codex",
    )
    _write_audit(iter_dir, [])

    _results_log.write_results_record(iter_dir)
    yaml_path = iter_dir / "iteration-results.yaml"
    assert yaml_path.exists()
    loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    _validate(loaded)
    assert loaded["iteration_id"] == "iteration-results-yaml"


# ---------------------------------------------------------------------------
# audit_append_event close hook: non-fatal-but-warns + new optional fields
# ---------------------------------------------------------------------------


def _seed_closeable_iteration(state_root: Path, iter_id: str):
    """Build an iteration that the close-invariant + mutation gate will pass.

    Mirrors the capstone happy-path: a codex apply_step_landed (with the new
    optional finding_ids/files_touched/fix_summary) + a fresh cross-family
    claude closer review.
    """
    iter_dir = _make_iter_dir(state_root, iter_id)
    audited_rel = "scripts/foo.py"
    (state_root.parent / audited_rel).parent.mkdir(parents=True, exist_ok=True)
    (state_root.parent / audited_rel).write_text("content\n", encoding="utf-8")

    apply_event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(-100),
        "event_id": f"{_now_iso(-100)}_apply_step_landed_codex",
        "effect": "applied patch",
        "actor": {
            "id": "codex-1", "model_family": "codex",
            "role": "fix_author", "pass_id": "codex-1-pass1",
        },
        "patch_id": "p-1",
        "files_touched": [audited_rel],
        "finding_ids": ["codex-rev-001"],
        "fix_summary": "off-by-one fix",
        "base_sha": "base",
        "post_sha": "POST",
        "unified_diff_sha256": "diff",
        "timestamp": _now_iso(-100),
        "files_modified": [audited_rel],
    }
    _write_audit(iter_dir, [apply_event])

    # Sealed codex review carries the finding so the results record has content.
    _write_review(
        iter_dir, "codex-review.yaml", "codex-1", "codex-1-pass1",
        [_finding("codex-rev-001", "high")], model_family="codex",
    )

    closer = {
        "actor": {"id": "claude-2", "model_family": "claude", "pass_id": "claude-2-pass1"},
        "review_target_hash": "POST",
        "created_at_utc": _now_iso(0),
        "goal_satisfied": True,
    }
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump(closer), encoding="utf-8")
    return iter_dir


def test_iteration_closed_writes_results_record(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    importlib.reload(audit_append_event)
    from consensus_mcp.tools import audit_append_event as aae

    state_root = tmp_path / "consensus-state"
    iter_id = "iteration-close-writes-results"
    _seed_closeable_iteration(state_root, iter_id)

    # All working-tree changes are audited (single file, in files_touched).
    monkeypatch.setattr(aae, "_detect_working_tree_changes", lambda repo_root: ["scripts/foo.py"])

    result = aae.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="claude-2",
        closing_state="quorum_close_passed",
        finding_dispositions=[
            {"id": "codex-rev-001", "disposition": "validated_fixed"},
        ],
    )
    assert "error" not in result, f"close blocked: {result}"

    # Co-located YAML authored.
    yaml_path = state_root / "active" / iter_id / "iteration-results.yaml"
    assert yaml_path.exists(), "iteration-results.yaml not authored on close"
    loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    _validate(loaded)
    by_id = {f["id"]: f for f in loaded["findings"]}
    assert by_id["codex-rev-001"]["disposition"] == "validated_fixed"
    assert loaded["counts"]["fixes_applied"] >= 1

    # JSONL ledger upserted.
    ledger = state_root / "state" / "results-v1.jsonl"
    assert ledger.exists()
    lines = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    _validate(json.loads(lines[0]))


def test_results_write_failure_is_nonfatal_to_close(tmp_path, monkeypatch, capsys):
    """A results-logging failure must NOT break a valid iteration_closed -
    non-fatal-but-warns, mirroring the closure-certificate author."""
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    importlib.reload(audit_append_event)
    from consensus_mcp.tools import audit_append_event as aae

    state_root = tmp_path / "consensus-state"
    iter_id = "iteration-close-results-fails"
    _seed_closeable_iteration(state_root, iter_id)

    monkeypatch.setattr(aae, "_detect_working_tree_changes", lambda repo_root: ["scripts/foo.py"])

    # Force the results writer to blow up.
    from consensus_mcp import _results_log as rl

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated results-log failure")

    monkeypatch.setattr(rl, "write_results_record", _boom)

    result = aae.handle(
        iteration_id=iter_id,
        event_type="iteration_closed",
        actor="claude-2",
        closing_state="quorum_close_passed",
    )
    # Close still succeeds.
    assert "error" not in result, f"results failure broke a valid close: {result}"
    assert result.get("event_id")
    # Certificate (the prior happy-path artifact) still authored.
    assert (state_root / "active" / iter_id / "closure-certificate.yaml").exists()
    # Warning surfaced (stderr or a result field), never silently skipped.
    err = capsys.readouterr().err
    surfaced = "simulated results-log failure" in err or "results" in err.lower() \
        or "results_log_warning" in result or "warning" in result
    assert surfaced, f"results failure was silently skipped (no warning): err={err!r} result={result}"


def test_existing_apply_event_without_new_fields_still_works(tmp_path):
    """Backward-compat: an apply_step_landed with NO finding_ids/fix_summary
    must still build a valid record (deferred/open dispositions)."""
    state_root = tmp_path / "consensus-state"
    iter_dir = _make_iter_dir(state_root, "iteration-results-legacy-apply")
    _write_review(
        iter_dir, "codex-review.yaml", "codex-1", "codex-1-pass1",
        [_finding("codex-rev-001", "high")], model_family="codex",
    )
    # Legacy apply event: only files_modified, no finding_ids/fix_summary.
    _write_audit(iter_dir, [{
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(-100),
        "effect": "applied patch",
        "files_modified": ["scripts/foo.py"],
    }])

    record = _results_log.build_results_record(iter_dir)
    _validate(record)
    # No finding_ids link -> finding is not validated_fixed.
    by_id = {f["id"]: f for f in record["findings"]}
    assert by_id["codex-rev-001"]["disposition"] in ("deferred", "open")
