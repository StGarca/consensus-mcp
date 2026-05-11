"""Unit tests for tools/apply_codex_patch.py — Task #26 (iter-0016).

Per codex 2026-05-10 v4 directive (memory/project_codex_fix_author_directive.md):
Staged-apply of verified codex patches with operator authorization.

Refuse-by-default. Requires BOTH:
  - goal_packet.authorization.codex_patch_apply_authorized: true
  - env CONSENSUS_MCP_CODEX_PATCH_APPLY=1

Refuses without claude verification approval. Detects base_sha drift between
codex review time and apply time. On success emits canonical apply_step_landed
audit event with the structured last_mutation event object.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp.tool_registry import ToolRegistry  # noqa: E402
from consensus_mcp.tools import apply_codex_patch  # noqa: E402
from consensus_mcp._closure_invariant import bundle_sha  # noqa: E402


# ---- helpers ---------------------------------------------------------------


@pytest.fixture
def repo_root_env(tmp_path, monkeypatch):
    """Point CONSENSUS_MCP_REPO_ROOT at tmp_path so audit + stage tools resolve there."""
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    # Reload modules that captured REPO_ROOT at import time so they pick up the
    # new env override. apply_codex_patch reads the env each call (no module-level
    # capture) but the patch_stage_and_dry_run + audit_append_event modules each
    # have a REPO_ROOT module-level constant.
    import importlib

    import consensus_mcp.tools.audit_append_event as _aae
    import consensus_mcp.tools.patch_stage_and_dry_run as _psd
    import consensus_mcp.tools.patch_apply_consensus_patch as _pac
    importlib.reload(_aae)
    importlib.reload(_psd)
    importlib.reload(_pac)
    importlib.reload(apply_codex_patch)
    return tmp_path


def _make_active_iter(repo_root: Path, iter_name: str = "iteration-0016") -> Path:
    """Create the consensus-state/active/<iter_name> dir layout the audit tool expects."""
    iter_dir = repo_root / "consensus-state" / "active" / iter_name
    iter_dir.mkdir(parents=True)
    # Seed empty independence-audit.yaml so audit_append_event has a target.
    (iter_dir / "independence-audit.yaml").write_text(
        yaml.safe_dump({"audit_log": []}), encoding="utf-8"
    )
    return iter_dir


def _write_target_file(repo_root: Path, rel: str, content: str) -> Path:
    full = repo_root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _build_patch_proposal(
    repo_root: Path,
    files_touched=("scripts/foo.py",),
    unified_diff: str = (
        "--- a/scripts/foo.py\n"
        "+++ b/scripts/foo.py\n"
        "@@ -1 +1,2 @@\n"
        " hello\n"
        "+world\n"
    ),
    base_sha: str | None = None,
    old_content: str = "hello\n",
    new_content: str = "hello\nworld\n",
) -> dict:
    """Construct a patch_proposal that targets a file under repo_root.

    base_sha is computed via bundle_sha against the current files-on-disk if not
    supplied. The "patch" we actually apply via stage/apply consumes
    full-file old/new strings (patch_apply_consensus_patch's contract); the
    unified_diff is recorded for traceability + diff_sha computation only.
    """
    if base_sha is None:
        base_sha = bundle_sha(repo_root, list(files_touched))
    # iter-0020: patch_id is finding-id-derived, not content-bound.
    patch_id = "codex-rev-001-patch"
    return {
        "patch_id": patch_id,
        "applies_to_findings": ["codex-rev-001"],
        "base_sha": base_sha,
        "unified_diff": unified_diff,
        "files_touched": list(files_touched),
        "_old_content": old_content,
        "_new_content": new_content,
    }


def _write_codex_review(iter_dir: Path, patch_proposal: dict, finding_id: str = "codex-rev-001") -> None:
    review = {
        "iteration_id": iter_dir.name,
        "reviewer_id": "codex-test-1",
        "pass_id": "codex-test-1-pass1",
        "findings": [
            {
                "id": finding_id,
                "severity": "medium",
                "summary": "Test finding for apply-codex-patch tests",
                "citation": "scripts/foo.py:1",
                "risk": "Low; test fixture",
                "recommendation": "Apply the patch_proposal as a fix",
                "patch_proposal": patch_proposal,
            }
        ],
        "goal_satisfied": False,
        "blocking_objections": [],
    }
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump(review), encoding="utf-8")


def _write_goal_packet(iter_dir: Path, codex_patch_apply_authorized: bool | None = True) -> None:
    """Write a goal_packet with the codex_patch_apply_authorized authorization flag.

    If codex_patch_apply_authorized=None, omit the field entirely; if False,
    set it to False. If True (default), set it to True.
    """
    auth: dict = {"authorized_by": "operator", "scope_signature": "test-sig"}
    if codex_patch_apply_authorized is True:
        auth["codex_patch_apply_authorized"] = True
    elif codex_patch_apply_authorized is False:
        auth["codex_patch_apply_authorized"] = False
    pkt = {
        "schema_version": 1,
        "pilot_id": "test-apply-codex-patch",
        "goal": {"summary": "test goal", "desired_end_state": "applied"},
        "allowed_files": ["scripts/foo.py"],
        "allowed_sections": [],
        "forbidden_files": [],
        "max_iterations": 10,
        "max_patch_size": None,
        "validators_required": [],
        "acceptance_gates": [],
        "stop_conditions": [],
        "operator_escalation_triggers": [],
        "authorization": auth,
    }
    (iter_dir / "goal_packet.yaml").write_text(yaml.safe_dump(pkt), encoding="utf-8")


def _write_claude_verification(
    iter_dir: Path,
    patch_id: str,
    verdict: str = "approved",
    review_scope_hash: str = "a" * 64,
) -> Path:
    out_dir = iter_dir / "codex-patch-verifications"
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "verifier": "claude",
        "verdict": verdict,
        "review_scope_hash": review_scope_hash,
        "rationale": "Test verification.",
        "approved_patch_id": patch_id,
        "codex_finding_id": "codex-rev-001",
    }
    out_path = out_dir / f"{patch_id}.yaml"
    out_path.write_text(yaml.safe_dump(record, sort_keys=True), encoding="utf-8")
    return out_path


def _make_actor() -> dict:
    return {
        "id": "codex-iter0016-1",
        "model_family": "codex",
        "role": "fix_author",
        "pass_id": "codex-iter0016-1-pass1",
    }


@pytest.fixture
def stub_clean_dry_run(monkeypatch):
    """Stub patch.stage_and_dry_run to always return APPROVED with no findings.

    The staged dry-run gate is exercised by patch_stage_and_dry_run's own
    tests. apply_codex_patch's responsibility is the authorization +
    verification + drift + audit flow on top of the gate. Stubbing keeps
    these tests focused on apply_codex_patch's logic and isolates them from
    spec-md / archive-index state in the source repo.
    """
    def _stub(iteration_id=None, proposed_patches=None, validators_to_run=None):
        return {
            "staging_dir_used": "/tmp/stub",
            "dry_run_findings": [],
            "gate_decision": "APPROVED",
            "dry_run_isolation_caveats": [],
        }
    import consensus_mcp.tools.apply_codex_patch as mod
    # Patch the stage_handle import inside the module's handle() at call site.
    # handle() does a late import; monkeypatch the module-level binding via
    # the patch_stage_and_dry_run module that handle() imports from.
    import consensus_mcp.tools.patch_stage_and_dry_run as _psd_mod
    monkeypatch.setattr(_psd_mod, "handle", _stub)
    return _stub


# ---- SCHEMA shape ----------------------------------------------------------


def test_schema_shape():
    schema = apply_codex_patch.SCHEMA
    assert schema["name"] == "apply.codex_patch"
    required = set(schema["input_schema"]["required"])
    assert required == {"iteration_dir", "patch_id", "actor"}
    assert schema["input_schema"]["additionalProperties"] is False
    out_required = set(schema["output_schema"]["required"])
    assert out_required == {"ok", "applied"}
    actor_props = schema["input_schema"]["properties"]["actor"]
    assert actor_props["required"] == ["id", "model_family", "role", "pass_id"]


def test_register_adds_tool():
    registry = ToolRegistry()
    apply_codex_patch.register(registry)
    names = [t["name"] for t in registry.list_tools()]
    assert "apply.codex_patch" in names
    handler = registry.get_handler("apply.codex_patch")
    assert callable(handler)


# ---- direct-call actor validation -----------------------------------------


def test_refuses_when_actor_missing_model_family(repo_root_env, monkeypatch):
    """Direct Python imports bypass MCP schema validation; handle() re-checks actor."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    actor = _make_actor()
    actor.pop("model_family")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id="codex-rev-001-patch",
        actor=actor,
    )
    assert result["ok"] is False
    assert result["applied"] is False
    assert "missing required actor key" in (result.get("error") or "")
    assert "model_family" in (result.get("error") or "")


# ---- authorization gate ----------------------------------------------------


def test_refuses_without_env_var(repo_root_env, monkeypatch):
    """env CONSENSUS_MCP_CODEX_PATCH_APPLY unset -> refuse."""
    monkeypatch.delenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", raising=False)
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = _build_patch_proposal(repo_root_env)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    assert result["applied"] is False
    assert "operator_authorization_missing" in (result.get("error") or "")


def test_refuses_without_goal_packet_authorization(repo_root_env, monkeypatch):
    """env set but goal_packet.authorization missing the flag -> refuse."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=None)  # field omitted
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = _build_patch_proposal(repo_root_env)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    assert result["applied"] is False
    assert "operator_authorization_missing" in (result.get("error") or "")


def test_refuses_with_authorization_missing_codex_patch_apply_field(repo_root_env, monkeypatch):
    """flag explicitly set to False -> refuse."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=False)
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = _build_patch_proposal(repo_root_env)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    assert result["applied"] is False
    assert "operator_authorization_missing" in (result.get("error") or "")


# ---- claude verification gate ---------------------------------------------


def test_refuses_when_claude_verification_missing(repo_root_env, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = _build_patch_proposal(repo_root_env)
    _write_codex_review(iter_dir, pp)
    # NO verification yaml written.

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    assert result["applied"] is False
    assert "claude_verification_missing" in (result.get("error") or "")


def test_refuses_when_claude_verdict_not_approved(repo_root_env, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = _build_patch_proposal(repo_root_env)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], verdict="corrected_resubmit")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    assert result["applied"] is False
    err = result.get("error") or ""
    assert "claude_verification_not_approved" in err
    assert "corrected_resubmit" in err


# ---- drift detection -------------------------------------------------------


def test_refuses_on_base_sha_drift(repo_root_env, monkeypatch):
    """Files modified between codex review and apply -> refuse with drift error."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = _build_patch_proposal(repo_root_env)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    # Mutate the touched file AFTER codex's base_sha was captured.
    _write_target_file(repo_root_env, "scripts/foo.py", "DIFFERENT CONTENT\n")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    assert result["applied"] is False
    err = result.get("error") or ""
    assert "base_sha_drift" in err


# ---- happy path ------------------------------------------------------------


def test_authorized_verified_no_drift_applies_successfully(repo_root_env, monkeypatch, stub_clean_dry_run):
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = _build_patch_proposal(repo_root_env)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"
    assert result["applied"] is True
    assert result.get("audit_event_id")
    lm = result.get("last_mutation")
    assert lm is not None
    # Structured actor preserved
    assert lm["actor"]["id"] == "codex-iter0016-1"
    assert lm["actor"]["model_family"] == "codex"
    assert lm["actor"]["role"] == "fix_author"
    assert lm["patch_id"] == pp["patch_id"]
    assert lm["files_touched"] == ["scripts/foo.py"]
    assert lm["base_sha"] == pp["base_sha"]
    # post_sha is the bundle_sha of the post-apply state and differs from base.
    assert lm["post_sha"] != pp["base_sha"]
    # post_apply file content reflects new_string
    on_disk = (repo_root_env / "scripts/foo.py").read_text(encoding="utf-8")
    assert on_disk == "hello\nworld\n"


def test_apply_step_landed_event_emitted_to_audit(repo_root_env, monkeypatch, stub_clean_dry_run):
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = _build_patch_proposal(repo_root_env)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True

    audit = yaml.safe_load((iter_dir / "independence-audit.yaml").read_text(encoding="utf-8"))
    log = audit.get("audit_log", [])
    apply_events = [e for e in log if e.get("event") == "apply_step_landed"]
    assert len(apply_events) >= 1, f"expected apply_step_landed event, got: {log}"
    ev = apply_events[-1]
    assert ev.get("effect") == "codex_patch_applied"
    assert ev.get("files_modified") == ["scripts/foo.py"]
    # last_mutation embedded as extra_field
    lm = ev.get("last_mutation")
    assert lm is not None
    assert lm["patch_id"] == pp["patch_id"]
    assert lm["actor"]["id"] == "codex-iter0016-1"
    assert lm["base_sha"] == pp["base_sha"]
    assert "post_sha" in lm
    assert "timestamp" in lm
    assert "unified_diff_sha256" in lm


def test_post_sha_computed_from_post_apply_state(repo_root_env, monkeypatch, stub_clean_dry_run):
    """post_sha == bundle_sha of files AFTER apply; differs from pre-apply base_sha."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = _build_patch_proposal(repo_root_env)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    pre_bundle = bundle_sha(repo_root_env, pp["files_touched"])
    assert pre_bundle == pp["base_sha"]

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True
    post_bundle = bundle_sha(repo_root_env, pp["files_touched"])
    lm = result["last_mutation"]
    assert lm["post_sha"] == post_bundle
    assert lm["post_sha"] != pre_bundle
