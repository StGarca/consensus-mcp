"""Capstone end-to-end integration test for the full codex-fix-loop — Task #27 (iter-0017).

Per codex 2026-05-10 v4 directive (memory/project_codex_fix_author_directive.md):
This test walks through the COMPLETE codex-fix-loop in a single happy-path run,
using synthetic codex output (no real codex CLI). Proves the end-to-end
architecture works: codex emits patch_proposal -> claude verifies -> apply
under operator authorization -> codex post-correction re-review -> close
with closure-certificate.yaml authored.

Plus a forbidden-transition test that proves apply.codex_patch refuses on
verdict=corrected_resubmit and that audit.append_event refuses iteration_closed
when the closure invariant fails.

This is the capstone — when this test lands and passes, the entire
codex-fix-loop is mechanically proven end-to-end via integration test.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _dispatch_codex  # noqa: E402
from consensus_mcp import _self_drive  # noqa: E402
from consensus_mcp._closure_invariant import bundle_sha  # noqa: E402
from consensus_mcp.tools import apply_codex_patch  # noqa: E402
from consensus_mcp.tools import audit_append_event  # noqa: E402
from consensus_mcp.tools import loop_run_goal  # noqa: E402
from consensus_mcp.tools import loop_verify_codex_patch  # noqa: E402


# ---- helpers --------------------------------------------------------------


# A 3-line target source file with a known off-by-one defect; the synthesized
# codex patch_proposal will fix the off-by-one. Trivial enough for the
# minimal unified-diff applier in apply_codex_patch._apply_unified_diff.
TARGET_REL = "scripts/sample_off_by_one.py"
OLD_CONTENT = "def first_n(xs, n):\n    return xs[:n - 1]\n"
NEW_CONTENT = "def first_n(xs, n):\n    return xs[:n]\n"


@pytest.fixture
def repo_root_env(tmp_path, monkeypatch):
    """Point CONSENSUS_MCP_REPO_ROOT at tmp_path so audit + apply tools resolve there.

    Reload modules that captured REPO_ROOT at import time so they pick up the
    env override (mirrors the pattern in test_apply_codex_patch.py).
    """
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    import consensus_mcp.tools.audit_append_event as _aae
    import consensus_mcp.tools.patch_stage_and_dry_run as _psd
    import consensus_mcp.tools.patch_apply_consensus_patch as _pac
    importlib.reload(_aae)
    importlib.reload(_psd)
    importlib.reload(_pac)
    importlib.reload(apply_codex_patch)
    return tmp_path


@pytest.fixture
def stub_clean_dry_run(monkeypatch):
    """Stub patch.stage_and_dry_run to return APPROVED with no findings.

    Matches the stubbing pattern from test_apply_codex_patch.py — the staged
    dry-run gate has its own tests; this capstone focuses on the end-to-end
    fix-loop wiring, not validator gating.
    """
    def _stub(iteration_id=None, proposed_patches=None, validators_to_run=None):
        return {
            "staging_dir_used": "/tmp/stub",
            "dry_run_findings": [],
            "gate_decision": "APPROVED",
            "dry_run_isolation_caveats": [],
        }
    import consensus_mcp.tools.patch_stage_and_dry_run as _psd_mod
    monkeypatch.setattr(_psd_mod, "handle", _stub)
    return _stub


@pytest.fixture
def stub_clean_working_tree(repo_root_env, monkeypatch):
    """Stub audit's working-tree detection to a clean tree (no changes).

    H-5 (v1.17.5) made the iteration_closed mutation-completeness gate fail
    CLOSED when git is unavailable. These capstone tests run in a non-git
    tmp_path, so the real _detect_working_tree_changes now (correctly) raises
    GitUnavailableError and pre-empts the closure-invariant gate these tests
    actually exercise. Stubbing to [] supplies the legitimate "git ran, no
    changes" signal — matching the Finding-3/Finding-5 idiom in
    test_iter_0018_cross_ai_invariant.py — so the mutation gate passes and the
    closure-invariant path is reached. Depends on repo_root_env so it patches
    the post-reload module object.
    """
    import consensus_mcp.tools.audit_append_event as _aae
    monkeypatch.setattr(_aae, "_detect_working_tree_changes", lambda repo_root: [])


def _make_iter_dir(repo_root: Path, iter_name: str = "iteration-0017-capstone") -> Path:
    """Create the consensus-state/active/<iter_name> dir layout audit_append_event expects."""
    iter_dir = repo_root / "consensus-state" / "active" / iter_name
    iter_dir.mkdir(parents=True)
    (iter_dir / "independence-audit.yaml").write_text(
        yaml.safe_dump({"audit_log": []}), encoding="utf-8"
    )
    return iter_dir


def _write_target_file(repo_root: Path, content: str = OLD_CONTENT) -> Path:
    full = repo_root / TARGET_REL
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _write_goal_packet(iter_dir: Path, codex_patch_apply_authorized: bool = True) -> Path:
    """Goal packet with codex_patch_apply_authorized + scope_signature so cmd_validate accepts."""
    packet = {
        "schema_version": 1,
        "pilot_id": "test-capstone-fix-loop",
        "goal": {"summary": "fix off-by-one defect in sample"},
        "allowed_files": [TARGET_REL],
        "allowed_sections": [],
        "forbidden_files": [],
        "max_iterations": 10,
        "max_patch_size": None,
        "validators_required": [],
        "acceptance_gates": [],
        "stop_conditions": [],
        "operator_escalation_triggers": [],
        "authorization": {
            "authorized_by": "operator",
            "codex_patch_apply_authorized": codex_patch_apply_authorized,
        },
    }
    sig = _self_drive._scope_signature(packet)
    packet["authorization"]["scope_signature"] = sig
    p = iter_dir / "goal_packet.yaml"
    p.write_text(yaml.safe_dump(packet), encoding="utf-8")
    return p


def _build_synthetic_codex_output_with_patch(
    repo_root: Path,
    finding_id: str = "codex-rev-001",
) -> tuple[str, dict]:
    """Build a finding-id-derived patch_proposal + return (json_text, patch_proposal_dict).

    iter-0020: patch_id MUST equal f"{finding_id}-patch" (codex-producible form,
    replaces the old content-bound formula). unified_diff is a real applicable
    diff; apply_codex_patch's minimal applier handles it.
    """
    base_sha = bundle_sha(repo_root, [TARGET_REL])
    unified_diff = (
        f"--- a/{TARGET_REL}\n"
        f"+++ b/{TARGET_REL}\n"
        "@@ -1,2 +1,2 @@\n"
        " def first_n(xs, n):\n"
        "-    return xs[:n - 1]\n"
        "+    return xs[:n]\n"
    )
    patch_id = f"{finding_id}-patch"

    patch_proposal = {
        "patch_id": patch_id,
        "applies_to_findings": [finding_id],
        "base_sha": base_sha,
        "unified_diff": unified_diff,
        "files_touched": [TARGET_REL],
        "expected_tests": ["pytest_smoke"],
    }

    codex_output = {
        "findings": [
            {
                "id": finding_id,
                "severity": "medium",
                "summary": "Off-by-one in first_n: returns n-1 elements instead of n.",
                "citation": f"{TARGET_REL}:2",
                "risk": "Returns one element fewer than the caller asked for; quiet bug.",
                "recommendation": "Replace xs[:n - 1] with xs[:n].",
                "patch_proposal": patch_proposal,
            }
        ],
        "goal_satisfied": False,
        "blocking_objections": [],
        "goal_satisfied_rationale": (
            "Off-by-one defect identified with proposed patch; awaiting verification + apply."
        ),
    }
    return json.dumps(codex_output), patch_proposal


def _author_codex_review_with_patch(
    iter_dir: Path,
    codex_output_json: str,
    reviewer_id: str = "codex-iter0017-1",
    pass_id: str = "codex-iter0017-1-pass1",
) -> Path:
    """Parse codex output JSON via _parse_codex_output (real validator) and write
    codex-review.yaml in T6's sealed shape. Also adds the 'side-channel'
    _old_content/_new_content keys to the patch_proposal so apply_codex_patch's
    structured-fast-path is exercised (mirrors the existing iter-0016 test
    fixture pattern; the unified_diff is also valid for the minimal applier
    fallback)."""
    parsed = _dispatch_codex._parse_codex_output(codex_output_json)
    # Append the structured _old_content/_new_content side-channel for the
    # fast-path (apply_codex_patch checks it before falling back to diff applier).
    for finding in parsed["findings"]:
        pp = finding.get("patch_proposal")
        if isinstance(pp, dict):
            pp["_old_content"] = OLD_CONTENT
            pp["_new_content"] = NEW_CONTENT
    review = {
        "iteration_id": iter_dir.name,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "findings": parsed["findings"],
        "goal_satisfied": parsed["goal_satisfied"],
        "goal_satisfied_rationale": parsed["goal_satisfied_rationale"],
        "blocking_objections": parsed["blocking_objections"],
    }
    p = iter_dir / "codex-review.yaml"
    p.write_text(yaml.safe_dump(review), encoding="utf-8")
    return p


def _make_codex_actor() -> dict:
    return {
        "id": "codex-iter0017-1",
        "model_family": "codex",
        "role": "fix_author",
        "pass_id": "codex-iter0017-1-pass1",
    }


def _now_iso(offset_seconds: int = 0) -> str:
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---- happy-path capstone --------------------------------------------------


def test_capstone_full_codex_fix_loop_end_to_end(
    repo_root_env, monkeypatch, stub_clean_dry_run, stub_clean_working_tree
):
    """The 14-step full cycle from codex 2026-05-10 v4 directive (revised v5).

    Setup: iteration-0017-capstone with goal_packet, target source file with
    a known off-by-one. Codex emits patch_proposal -> claude builds verifier
    inputs + records approved verdict -> apply.codex_patch under operator
    authorization mutates the file + emits apply_step_landed -> CLAUDE
    post-mutation review (cross-FAMILY closer per CLAUDE.md verification
    duty) -> audit.append_event iteration_closed authors
    closure-certificate.yaml with all PASS.

    Per iter-0018 Finding 2 (codex 2026-05-10 v5): the closer MUST be the
    OPPOSITE family from the LAST MUTATOR. Codex applied; claude must verify
    + close. Codex closing its own mutation is BLOCKED at the cross_family
    check (regardless of actor.id difference).
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_iter_dir(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, OLD_CONTENT)

    # Pre-conditions for loop.run_goal to reach codex_patch_proposed:
    # review-packet.yaml + claude-review.yaml + codex-review.yaml (the last
    # one carries the patch_proposal). claude-review.yaml from earlier
    # claude-rev pass; the patch verification is a separate flow.
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )

    # ------------------------------------------------------------------
    # 1. Synthesize codex output JSON with a content-bound patch_proposal,
    #    parse it via _parse_codex_output (the real validator) so the
    #    schema/binding invariants are exercised end-to-end.
    # ------------------------------------------------------------------
    codex_output_json, patch_proposal = _build_synthetic_codex_output_with_patch(repo_root_env)
    parsed = _dispatch_codex._parse_codex_output(codex_output_json)
    assert len(parsed["findings"]) == 1
    assert parsed["findings"][0]["id"] == "codex-rev-001"
    assert parsed["findings"][0]["patch_proposal"]["patch_id"] == patch_proposal["patch_id"]

    # ------------------------------------------------------------------
    # 2. Author the codex-review.yaml (with _old_content/_new_content
    #    side-channel for the apply fast-path).
    # ------------------------------------------------------------------
    _author_codex_review_with_patch(iter_dir, codex_output_json)

    # ------------------------------------------------------------------
    # 3. Supervisor state detection: should report codex_patch_proposed.
    # ------------------------------------------------------------------
    pkt_path = str(iter_dir / "goal_packet.yaml")
    sup_result = loop_run_goal.handle(
        goal_packet_path=pkt_path,
        iteration_dir=str(iter_dir),
        auto_dispatch_codex=False,
    )
    assert sup_result["ok"] is True, f"supervisor errored: {sup_result}"
    assert sup_result["state"] == "codex_patch_proposed", (
        f"expected codex_patch_proposed; got {sup_result['state']}: {sup_result}"
    )

    # ------------------------------------------------------------------
    # 4. Claude verification: build_inputs -> returns review_scope_hash.
    # ------------------------------------------------------------------
    bi_result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        repo_root=str(repo_root_env),
        claude_md_path=str(repo_root_env / "CLAUDE.md"),  # absent; tool tolerates
    )
    assert bi_result["verdict"] == "blocked"  # placeholder until subagent runs
    review_scope_hash = bi_result["review_scope_hash"]
    assert review_scope_hash and len(review_scope_hash) == 64
    assert "verifier_inputs" in bi_result
    bundle = bi_result["verifier_inputs"]
    assert bundle["patch_proposal"]["patch_id"] == patch_proposal["patch_id"]

    # ------------------------------------------------------------------
    # 5. Claude verification: record_verdict approved.
    # ------------------------------------------------------------------
    rv_result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="record_verdict",
        verdict="approved",
        rationale="Patch correctly fixes the off-by-one per CLAUDE.md.",
        review_scope_hash=review_scope_hash,
        approved_patch_id=patch_proposal["patch_id"],
    )
    assert rv_result["verdict"] == "approved"
    verif_path = iter_dir / "codex-patch-verifications" / f"{patch_proposal['patch_id']}.yaml"
    assert verif_path.exists()

    # ------------------------------------------------------------------
    # 6. Apply with operator authorization: env + goal_packet flag both set.
    # ------------------------------------------------------------------
    actor = _make_codex_actor()
    apply_result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=patch_proposal["patch_id"],
        actor=actor,
    )
    assert apply_result["ok"] is True, f"expected ok=True, got: {apply_result}"
    assert apply_result["applied"] is True
    last_mutation = apply_result["last_mutation"]
    assert last_mutation is not None
    assert last_mutation["actor"]["id"] == "codex-iter0017-1"
    assert last_mutation["actor"]["model_family"] == "codex"
    assert last_mutation["patch_id"] == patch_proposal["patch_id"]
    assert last_mutation["base_sha"] == patch_proposal["base_sha"]
    assert last_mutation["post_sha"] != patch_proposal["base_sha"]
    assert last_mutation["files_touched"] == [TARGET_REL]
    apply_post_sha = last_mutation["post_sha"]
    apply_timestamp = last_mutation["timestamp"]

    # ------------------------------------------------------------------
    # 7. File mutation observable on disk.
    # ------------------------------------------------------------------
    on_disk = (repo_root_env / TARGET_REL).read_text(encoding="utf-8")
    assert on_disk == NEW_CONTENT, f"file content not mutated: {on_disk!r}"

    # ------------------------------------------------------------------
    # 8. Audit log carries apply_step_landed with structured last_mutation.
    # ------------------------------------------------------------------
    audit = yaml.safe_load((iter_dir / "independence-audit.yaml").read_text(encoding="utf-8"))
    apply_events = [e for e in audit["audit_log"] if e.get("event") == "apply_step_landed"]
    assert len(apply_events) == 1
    audit_lm = apply_events[0]["last_mutation"]
    assert audit_lm["patch_id"] == patch_proposal["patch_id"]
    assert audit_lm["post_sha"] == apply_post_sha
    assert audit_lm["actor"]["id"] == "codex-iter0017-1"

    # iter-0026 F3: the test-only hoist bridge that used to live here was
    # made obsolete by iter-0024 F1 (last_mutation_from_audit merges nested
    # last_mutation onto the event with nested-wins semantics) PLUS
    # iter-0025 codex-rev-002 (apply_step_landed emitter now puts canonical
    # mutation fields at extra_fields top-level alongside the nested
    # last_mutation). Closure invariant reads the post_sha + structured
    # actor directly without test-side hoisting now.

    # ------------------------------------------------------------------
    # 9. Post-mutation CLAUDE review (per iter-0018 Finding 2, the closer
    #    MUST be the opposite family from the last mutator). Codex applied,
    #    so claude verifies post-mutation per CLAUDE.md.
    #
    #    Build a claude-review.yaml with the structured closer fields T6
    #    inspects: actor (with model_family=claude), review_target_hash, and
    #    created_at_utc strictly after apply_timestamp.
    # ------------------------------------------------------------------
    closer_created = _now_iso(offset_seconds=5)
    assert closer_created > apply_timestamp, (
        f"closer ts {closer_created!r} not after apply ts {apply_timestamp!r}"
    )
    closer_actor = {
        "id": "claude-iter0017-verifier",
        "model_family": "claude",
        "pass_id": "claude-iter0017-verifier-pass1",
    }
    claude_post_review = {
        "iteration_id": iter_dir.name,
        "reviewer_id": "claude-iter0017-verifier",
        "pass_id": "claude-iter0017-verifier-pass1",
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": (
            "Patch applied; current file content matches the proposed fix; "
            "off-by-one no longer present per CLAUDE.md correctness verification."
        ),
        "blocking_objections": [],
        # T6 closer-shape fields:
        "actor": closer_actor,
        "review_target_hash": apply_post_sha,
        "created_at_utc": closer_created,
    }
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump(claude_post_review), encoding="utf-8"
    )

    # ------------------------------------------------------------------
    # 10. Close: audit.append_event iteration_closed -> closure-certificate
    #     authored with all 3 invariant_checks=PASS (codex apply + claude
    #     close = cross-family pass).
    # ------------------------------------------------------------------
    close_result = audit_append_event.handle(
        iteration_id=iter_dir.name,
        event_type="iteration_closed",
        actor="claude-iter0017-verifier",
        closing_state="quorum_close_passed",
        extra_fields={
            "closing_verdict": {
                "actor": closer_actor,
                "review_target_hash": apply_post_sha,
                "created_at_utc": closer_created,
            },
        },
    )
    assert "error" not in close_result, f"close blocked: {close_result}"
    assert close_result.get("event_id")

    cert_path = iter_dir / "closure-certificate.yaml"
    assert cert_path.exists(), "closure-certificate.yaml not authored on PASS"
    cert = yaml.safe_load(cert_path.read_text(encoding="utf-8"))
    assert cert["overall"] == "PASS", f"cert not PASS: {cert}"
    assert cert["invariant_checks"]["cross_family"] == "PASS"
    assert cert["invariant_checks"]["hash_match"] == "PASS"
    assert cert["invariant_checks"]["freshness"] == "PASS"
    # Cert references the right last_mutation + claude closer.
    assert cert["last_mutation"]["actor"]["id"] == "codex-iter0017-1"
    assert cert["last_mutation"]["actor"]["model_family"] == "codex"
    assert cert["closing_verdict"]["actor"]["id"] == "claude-iter0017-verifier"
    assert cert["closing_verdict"]["actor"]["model_family"] == "claude"
    assert cert["closing_verdict"]["review_target_hash"] == apply_post_sha


# ---- forbidden-transition test --------------------------------------------


def test_capstone_corrected_resubmit_refuses_apply_and_close(
    repo_root_env, monkeypatch, stub_clean_dry_run, stub_clean_working_tree,
):
    """Forbidden transition: verdict=corrected_resubmit blocks apply, and
    iteration_closed without an approved+applied patch + cross-actor closer
    is refused at the T6 layer.

    Two assertions:
      1. apply.codex_patch refuses when verdict != approved.
      2. audit.append_event(iteration_closed) refuses with
         closure_cross_verification_failed when the closure invariant fails
         (here: claude is the closer but no cross-actor closer-verdict was
         authored after the apply — so T6's no-fresh-review branch fires).
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_iter_dir(repo_root_env, "iteration-0017-capstone-forbid")
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, OLD_CONTENT)

    codex_output_json, patch_proposal = _build_synthetic_codex_output_with_patch(repo_root_env)
    _author_codex_review_with_patch(iter_dir, codex_output_json)

    # Record verdict=corrected_resubmit (claude says the patch is wrong).
    rv_result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="record_verdict",
        verdict="corrected_resubmit",
        rationale="Off-by-one not the right fix; missing edge case.",
        review_scope_hash="d" * 64,
        corrections="--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
        approved_patch_id=patch_proposal["patch_id"],
    )
    assert rv_result["verdict"] == "corrected_resubmit"

    # Apply must refuse on verdict != approved.
    apply_result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=patch_proposal["patch_id"],
        actor=_make_codex_actor(),
    )
    assert apply_result["ok"] is False
    assert apply_result["applied"] is False
    assert "claude_verification_not_approved" in (apply_result.get("error") or "")

    # File NOT mutated.
    assert (repo_root_env / TARGET_REL).read_text(encoding="utf-8") == OLD_CONTENT

    # To test T6's iteration_closed refusal under invariant-fail, we need
    # an apply_step_landed event in the audit log. Hand-craft one (the
    # apply.codex_patch path refused, so the audit log is empty otherwise).
    audit = yaml.safe_load((iter_dir / "independence-audit.yaml").read_text(encoding="utf-8"))
    fake_apply_event = {
        "event": "apply_step_landed",
        "timestamp_utc": _now_iso(-100),
        "event_id": f"{_now_iso(-100)}_apply_step_landed_codex-iter0017-1",
        "effect": "codex_patch_applied",
        "actor": {
            "id": "codex-iter0017-1",
            "model_family": "codex",
            "role": "fix_author",
            "pass_id": "codex-iter0017-1-pass1",
        },
        "patch_id": patch_proposal["patch_id"],
        "files_touched": [TARGET_REL],
        "base_sha": patch_proposal["base_sha"],
        "post_sha": "POST_HASH_TEST",
        "unified_diff_sha256": hashlib.sha256(
            patch_proposal["unified_diff"].encode("utf-8")
        ).hexdigest(),
        "files_modified": [TARGET_REL],
        "last_mutation": {
            "actor": {
                "id": "codex-iter0017-1",
                "model_family": "codex",
                "role": "fix_author",
                "pass_id": "codex-iter0017-1-pass1",
            },
            "patch_id": patch_proposal["patch_id"],
            "files_touched": [TARGET_REL],
            "base_sha": patch_proposal["base_sha"],
            "post_sha": "POST_HASH_TEST",
            "unified_diff_sha256": hashlib.sha256(
                patch_proposal["unified_diff"].encode("utf-8")
            ).hexdigest(),
            "timestamp": _now_iso(-100),
        },
    }
    audit["audit_log"].append(fake_apply_event)
    (iter_dir / "independence-audit.yaml").write_text(
        yaml.safe_dump(audit), encoding="utf-8"
    )

    # Author a SAME-actor closer review (codex-iter0017-1 closes its own
    # mutation) -> cross_family invariant fails -> T6 refuses.
    same_actor_closer = {
        "iteration_id": iter_dir.name,
        "reviewer_id": "codex-iter0017-1",
        "pass_id": "codex-iter0017-1-pass2",
        "actor": {
            "id": "codex-iter0017-1",  # SAME id as last_mutation -> cross_family fail
            "model_family": "codex",
            "pass_id": "codex-iter0017-1-pass2",
        },
        "review_target_hash": "POST_HASH_TEST",
        "created_at_utc": _now_iso(),
        "goal_satisfied": True,
    }
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump(same_actor_closer), encoding="utf-8"
    )

    close_result = audit_append_event.handle(
        iteration_id=iter_dir.name,
        event_type="iteration_closed",
        actor="codex-iter0017-1",
        closing_state="quorum_close_passed",
    )
    assert "error" in close_result
    assert "closure_cross_verification_failed" in close_result["error"]
    # No certificate authored on refusal.
    assert not (iter_dir / "closure-certificate.yaml").exists()


# ---- iter-0018 Finding 2: codex post-correction re-review is blocked ----


def test_capstone_codex_apply_then_codex_post_review_blocked(
    repo_root_env, monkeypatch, stub_clean_dry_run, stub_clean_working_tree,
):
    """Per iter-0018 Finding 2 (codex 2026-05-10 v5): the closer MUST be the
    OPPOSITE family from the LAST MUTATOR. Codex applies + codex (different
    actor.id) post-correction reviews and tries to close = BLOCKED at
    cross_family.

    Pre-iter-0018 this passed because actor.id differed; post-iter-0018 the
    family check catches it.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_iter_dir(repo_root_env, "iteration-0018-codex-codex-blocked")
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, OLD_CONTENT)

    # Pre-conditions for the loop to reach codex_patch_proposed.
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )

    codex_output_json, patch_proposal = _build_synthetic_codex_output_with_patch(repo_root_env)
    _author_codex_review_with_patch(iter_dir, codex_output_json)

    # Claude verification approved (so apply will proceed).
    bi_result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        repo_root=str(repo_root_env),
        claude_md_path=str(repo_root_env / "CLAUDE.md"),
    )
    review_scope_hash = bi_result["review_scope_hash"]
    loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="record_verdict",
        verdict="approved",
        rationale="approved",
        review_scope_hash=review_scope_hash,
        approved_patch_id=patch_proposal["patch_id"],
    )

    # Codex applies.
    actor = _make_codex_actor()
    apply_result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=patch_proposal["patch_id"],
        actor=actor,
    )
    assert apply_result["ok"] is True
    last_mutation = apply_result["last_mutation"]
    apply_post_sha = last_mutation["post_sha"]
    apply_timestamp = last_mutation["timestamp"]

    # iter-0026 F3: hoist bridge removed (see happy-path test for rationale).
    # iter-0024 F1 + iter-0025 codex-rev-002 make production code emit and
    # consume the canonical fields directly.

    # CODEX post-correction re-review (different actor.id, SAME family=codex).
    closer_created = _now_iso(offset_seconds=5)
    assert closer_created > apply_timestamp
    codex_closer_actor = {
        "id": "codex-iter0018-2",     # different from the applier (codex-iter0017-1)
        "model_family": "codex",       # but SAME family
        "pass_id": "codex-iter0018-2-pass1",
    }
    codex_post_review = {
        "iteration_id": iter_dir.name,
        "reviewer_id": "codex-iter0018-2",
        "pass_id": "codex-iter0018-2-pass1",
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "post-correction codex review",
        "blocking_objections": [],
        # T6 closer-shape fields:
        "actor": codex_closer_actor,
        "review_target_hash": apply_post_sha,
        "created_at_utc": closer_created,
    }
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump(codex_post_review), encoding="utf-8"
    )

    # T6 must refuse close: cross_family fails (codex applied, codex closes).
    close_result = audit_append_event.handle(
        iteration_id=iter_dir.name,
        event_type="iteration_closed",
        actor="codex-iter0018-2",
        closing_state="quorum_close_passed",
        extra_fields={
            "closing_verdict": {
                "actor": codex_closer_actor,
                "review_target_hash": apply_post_sha,
                "created_at_utc": closer_created,
            },
        },
    )
    assert "error" in close_result
    assert "closure_cross_verification_failed" in close_result["error"]
    assert "cross_family" in close_result["error"]
    # No certificate authored on refusal.
    assert not (iter_dir / "closure-certificate.yaml").exists()
