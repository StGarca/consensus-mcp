"""Security-cluster regression tests - consensus review 2026-05-22.

Closes CR-1..CR-5, H-2, M-6 from CODE_REVIEW_2026-05-22.md, validated by the
codex+gemini consensus audit (iteration-codereview-audit-2026-05-22):

  CR-1 apply_codex_patch: arbitrary write via files_touched path traversal
  CR-2 apply_codex_patch: files_touched not constrained to goal_packet.allowed_files
  CR-3 build_review_packet: arbitrary read/exfil via target_files traversal
  CR-4 scope_check: empty/missing/non-list allowed_files fails OPEN (allow-all)
  CR-5 apply_codex_patch: non-canonical iteration_dir self-authorizes (auth bypass)
  H-2  patch_stage_and_dry_run: file_rel traversal (silent staging-write escape)
  M-6  patch_stage_and_dry_run: gate_decision does not block on "critical"

Each test asserts the FAIL-CLOSED behavior the fix introduces; it FAILS against
the pre-fix code (demonstrating the vulnerability) and PASSES after.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from consensus_mcp import _paths
from consensus_mcp._closure_invariant import bundle_sha
from consensus_mcp.tests.test_apply_codex_patch import (
    _make_active_iter,
    _build_patch_proposal,
    _write_codex_review,
    _write_goal_packet,
    _write_claude_verification,
    _write_target_file,
    _make_actor,
)


def _reload_apply():
    import consensus_mcp.tools.apply_codex_patch as acp
    importlib.reload(acp)
    return acp


# --------------------------------------------------------------------------
# _paths.resolve_contained - shared containment helper (basis for CR-1/3/5, H-2)
# --------------------------------------------------------------------------

def test_resolve_contained_inside_returns_resolved(tmp_path):
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "ok.txt"
    f.write_text("ok", encoding="utf-8")
    assert _paths.resolve_contained(tmp_path, "sub/ok.txt") == f.resolve()


def test_resolve_contained_absolute_outside_raises(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}_secret.txt"
    with pytest.raises(_paths.PathTraversalError):
        _paths.resolve_contained(tmp_path, str(outside))


def test_resolve_contained_dotdot_traversal_raises(tmp_path):
    with pytest.raises(_paths.PathTraversalError):
        _paths.resolve_contained(tmp_path, "../escape.txt")


# --------------------------------------------------------------------------
# CR-1 - apply_codex_patch refuses a path-traversal files_touched (no outside write)
# --------------------------------------------------------------------------

def test_apply_codex_patch_refuses_traversal_write(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    acp = _reload_apply()
    iter_dir = _make_active_iter(tmp_path, "iteration-sec-cr1")
    _write_goal_packet(iter_dir)
    pp = _build_patch_proposal(
        tmp_path, files_touched=["../escape.txt"], base_sha="0" * 64,
        old_content="", new_content="PWNED\n",
    )
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"])

    res = acp.handle(iteration_dir=str(iter_dir), patch_id=pp["patch_id"], actor=_make_actor())

    assert res["ok"] is False and res["applied"] is False
    err = res.get("error", "").lower()
    assert "traversal" in err or "outside" in err
    assert not (tmp_path.parent / "escape.txt").exists(), \
        "traversal write must not land outside repo_root"


# --------------------------------------------------------------------------
# CR-2 - apply_codex_patch refuses a files_touched not in goal_packet.allowed_files
# --------------------------------------------------------------------------

def test_apply_codex_patch_refuses_out_of_scope_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    acp = _reload_apply()
    iter_dir = _make_active_iter(tmp_path, "iteration-sec-cr2")
    _write_goal_packet(iter_dir, allowed_files=["scripts/foo.py"])  # evil.py NOT in scope
    evil = tmp_path / "scripts" / "evil.py"
    evil.parent.mkdir(parents=True, exist_ok=True)
    evil.write_text("original\n", encoding="utf-8")
    pp = _build_patch_proposal(
        tmp_path, files_touched=["scripts/evil.py"], base_sha="0" * 64,
        old_content="original\n", new_content="HACKED\n",
    )
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"])

    res = acp.handle(iteration_dir=str(iter_dir), patch_id=pp["patch_id"], actor=_make_actor())

    assert res["ok"] is False and res["applied"] is False
    assert "scope" in res.get("error", "").lower()
    assert evil.read_text(encoding="utf-8") == "original\n", \
        "out-of-scope file must be left untouched"


# --------------------------------------------------------------------------
# CR-5 - apply_codex_patch refuses a non-canonical iteration_dir (auth bypass)
# --------------------------------------------------------------------------

def test_apply_codex_patch_refuses_noncanonical_iteration_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    acp = _reload_apply()
    # A crafted dir OUTSIDE consensus-state/active/ carrying a self-authorizing
    # goal_packet - the exact attack codex-rev-003 describes.
    rogue = tmp_path / "rogue-iter"
    rogue.mkdir(parents=True, exist_ok=True)
    (rogue / "independence-audit.yaml").write_text(
        yaml.safe_dump({"audit_log": []}), encoding="utf-8")
    _write_goal_packet(rogue)
    pp = _build_patch_proposal(
        tmp_path, files_touched=["scripts/foo.py"], base_sha="0" * 64,
        old_content="", new_content="x\n",
    )
    _write_codex_review(rogue, pp)
    _write_claude_verification(rogue, pp["patch_id"])

    res = acp.handle(iteration_dir=str(rogue), patch_id=pp["patch_id"], actor=_make_actor())

    assert res["ok"] is False and res["applied"] is False
    err = res.get("error", "").lower()
    assert "iteration_dir" in err or "canonical" in err or "active" in err


# --------------------------------------------------------------------------
# CR-3 - build_review_packet does not read a traversal target_file into the packet
# --------------------------------------------------------------------------

def test_build_review_packet_skips_traversal_target_file(tmp_path, monkeypatch):
    import consensus_mcp.validators.build_review_packet as brp
    monkeypatch.setattr(brp, "REPO_ROOT", tmp_path, raising=True)
    secret = tmp_path.parent / f"{tmp_path.name}_id_rsa"
    secret.write_text("PRIVATE_KEY_MATERIAL\n", encoding="utf-8")
    inp = tmp_path / "input.yaml"
    inp.write_text(yaml.safe_dump({
        "iteration_id": "iteration-sec-cr3",
        "objective": "x",
        "mode": "review",
        "gate_state": "open",
        "target_files": [f"../{secret.name}"],
    }), encoding="utf-8")
    try:
        packet = brp.build_review_packet(inp)
        blob = yaml.safe_dump(packet)
        assert "PRIVATE_KEY_MATERIAL" not in blob, \
            "traversal target_file content must not be read into the review packet"
    finally:
        secret.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# CR-4 - scope_check fails CLOSED when allowed_files is empty (was allow-all)
# --------------------------------------------------------------------------

def _write_consensus(tmp_path: Path, allowed_files) -> Path:
    impl_scope: dict = {"forbidden_files": [], "forbidden_actions": []}
    if allowed_files is not _OMIT:
        impl_scope["allowed_files"] = allowed_files
    cpath = tmp_path / "consensus.yaml"
    cpath.write_text(yaml.safe_dump({"implementation_scope": impl_scope}), encoding="utf-8")
    return cpath


_OMIT = object()


@pytest.mark.parametrize("allowed_files", [[], _OMIT, "scripts/foo.py", [123, None]])
def test_scope_check_fails_closed_without_valid_allowed_files(tmp_path, allowed_files):
    from consensus_mcp.validators import scope_check
    cpath = _write_consensus(tmp_path, allowed_files)
    report = scope_check.scope_check(
        cpath, "a", "b", _touched_files_override=["scripts/foo.py"])
    assert any(f.get("id") == "FILE_OUTSIDE_ALLOWED_SCOPE" for f in report["findings"]), \
        "a touched file must be out-of-scope when no valid allowed_files patterns exist"
    assert report["scope_check_block"]["passed"] is False


# --------------------------------------------------------------------------
# H-2 - patch_stage_and_dry_run refuses a path-traversal file_rel
# --------------------------------------------------------------------------

def test_patch_stage_refuses_traversal_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(tmp_path))
    import consensus_mcp.tools.patch_stage_and_dry_run as psd
    importlib.reload(psd)
    secret = tmp_path.parent / f"{tmp_path.name}_psd_secret.txt"
    secret.write_text("SECRET\n", encoding="utf-8")
    try:
        res = psd.handle(
            iteration_id=None,
            proposed_patches=[{
                "file": f"../{secret.name}",
                "old_string": "SECRET\n",
                "new_string": "OWNED\n",
            }],
            validators_to_run=["validate_disposition_index"],
        )
        assert "error" in res
        assert "traversal" in res["error"].lower() or "outside" in res["error"].lower()
    finally:
        secret.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# M-6 - gate_decision blocks on "critical" severity findings
# --------------------------------------------------------------------------

def test_gate_decision_blocks_on_critical():
    import consensus_mcp.tools.patch_stage_and_dry_run as psd
    assert psd._gate_decision([{"severity": "critical"}]) == "BLOCKED"
    assert psd._gate_decision([{"severity": "high"}]) == "BLOCKED"
    assert psd._gate_decision([{"severity": "blocking"}]) == "BLOCKED"
    assert psd._gate_decision([{"severity": "low"}]) == "APPROVED"
    assert psd._gate_decision([]) == "APPROVED"


# --------------------------------------------------------------------------
# Invariant guard (secfix audit, codex-rev-001 DISMISSED) - _apply_unified_diff
# keys new_contents strictly by files_touched (apply_codex_patch.py:539-547:
# `for rel in files_touched: out[rel] = ...`), so a unified_diff `+++ b/<path>`
# header outside files_touched is built into file_segments but never consumed.
# codex-rev-001 claimed this was a bypass (diff header becomes the write key);
# verified false. This test guards the invariant against future regression.
# --------------------------------------------------------------------------

def test_apply_codex_patch_diff_header_cannot_escape_files_touched(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    acp = _reload_apply()
    iter_dir = _make_active_iter(tmp_path, "iteration-sec-cr1-diff")
    _write_goal_packet(iter_dir)  # allowed_files = ["scripts/*.py"]
    _write_target_file(tmp_path, "scripts/foo.py", "hello\n")
    # files_touched is in-scope, but the unified_diff header names a traversal
    # path. No _old_content/_new_content -> forces the _apply_unified_diff path.
    pp = {
        "patch_id": "codex-rev-001-patch",
        "applies_to_findings": ["codex-rev-001"],
        "base_sha": bundle_sha(tmp_path, ["scripts/foo.py"]),
        "unified_diff": "--- /dev/null\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+pwned\n",
        "files_touched": ["scripts/foo.py"],
    }
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"])

    res = acp.handle(iteration_dir=str(iter_dir), patch_id=pp["patch_id"], actor=_make_actor())

    # Refused, and crucially nothing is written outside repo_root.
    assert res["ok"] is False and res["applied"] is False
    assert not (tmp_path.parent / "escape.txt").exists(), \
        "a diff-header path outside files_touched must never be written"
