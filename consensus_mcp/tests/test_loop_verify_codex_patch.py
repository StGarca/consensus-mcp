"""Unit tests for tools/loop_verify_codex_patch.py - Task #25 (iter-0015).

Per codex 2026-05-10 v4 directive (memory/project_codex_fix_author_directive.md):
  - Claude verifies codex-emitted patches per CLAUDE.md.
  - The MCP tool BUILDS the verifier-input bundle (reproducibility-bounded);
    it does NOT itself dispatch a subagent - that's the orchestrator's job.
  - The tool also RECORDS the eventual subagent verdict back into the
    iteration_dir.

Two modes:
  * mode=build_inputs (default) - read codex-review.yaml, find patch_proposal,
    assemble verifier_inputs (goal_packet + acceptance_gates + touched files
    full content + CLAUDE.md + codex finding TEXT only - NOT codex's
    reasoning trail), compute review_scope_hash. Returns blocked verdict
    placeholder for orchestrator to follow up.
  * mode=record_verdict - validate subagent's structured verdict and write
    to iteration_dir/codex-patch-verifications/<patch_id>.yaml.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp.tool_registry import ToolRegistry  # noqa: E402
from consensus_mcp.tools import loop_verify_codex_patch  # noqa: E402


# ---- helpers ---------------------------------------------------------------


def _build_patch_proposal(
    base_sha: str = "abc123def456abc123def456abc123def456abc123def456abc123def456abcd",
    unified_diff: str = (
        "--- a/scripts/foo.py\n"
        "+++ b/scripts/foo.py\n"
        "@@ -1 +1,2 @@\n"
        " hello\n"
        "+world\n"
    ),
    applies_to_findings=("codex-rev-001",),
    files_touched=("scripts/foo.py",),
    expected_tests=None,
) -> dict:
    # iter-0020: patch_id is finding-id-derived, not content-bound.
    finding_id = list(applies_to_findings)[0] if applies_to_findings else "codex-rev-001"
    patch_id = f"{finding_id}-patch"
    pp: dict = {
        "patch_id": patch_id,
        "applies_to_findings": list(applies_to_findings),
        "base_sha": base_sha,
        "unified_diff": unified_diff,
        "files_touched": list(files_touched),
    }
    if expected_tests is not None:
        pp["expected_tests"] = list(expected_tests)
    return pp


def _write_codex_review(
    iter_dir: Path,
    patch_proposal: dict | None = None,
    finding_id: str = "codex-rev-001",
    extra_review_fields: dict | None = None,
) -> Path:
    finding = {
        "id": finding_id,
        "severity": "medium",
        "summary": "Test finding for verify-codex-patch tests",
        "citation": "scripts/foo.py:1",
        "risk": "Low; test fixture",
        "recommendation": "Apply the patch_proposal as a fix",
    }
    if patch_proposal is not None:
        finding["patch_proposal"] = patch_proposal
    review = {
        "iteration_id": iter_dir.name,
        "reviewer_id": "codex-test-1",
        "pass_id": "codex-test-1-pass1",
        "findings": [finding],
        "goal_satisfied": False,
        "blocking_objections": [],
        "goal_satisfied_rationale": "Codex's private reasoning trail; verifier MUST NOT see this.",
    }
    if extra_review_fields:
        review.update(extra_review_fields)
    p = iter_dir / "codex-review.yaml"
    p.write_text(yaml.safe_dump(review), encoding="utf-8")
    return p


def _write_goal_packet(iter_dir: Path) -> Path:
    pkt = {
        "schema_version": 1,
        "pilot_id": "test-verify-codex-patch",
        "goal": {"summary": "test goal", "desired_end_state": "verified"},
        "allowed_files": ["scripts/foo.py"],
        "allowed_sections": [],
        "forbidden_files": [],
        "max_iterations": 10,
        "max_patch_size": None,
        "validators_required": [],
        "acceptance_gates": [
            {"id": "gate1", "description": "tests pass", "check": "pytest -q"}
        ],
        "stop_conditions": [],
        "operator_escalation_triggers": [],
        "authorization": {"authorized_by": "operator", "scope_signature": "sig"},
    }
    p = iter_dir / "goal_packet.yaml"
    p.write_text(yaml.safe_dump(pkt), encoding="utf-8")
    return p


def _write_touched_file(repo_root: Path, rel: str, content: str) -> Path:
    full = repo_root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _write_claude_md(repo_root: Path, content: str = "# CLAUDE.md\nKarpathy four principles.\n") -> Path:
    p = repo_root / "CLAUDE.md"
    p.write_text(content, encoding="utf-8")
    return p


def _make_iter_dir(tmp_path: Path) -> Path:
    iter_dir = tmp_path / "iteration-0015"
    iter_dir.mkdir()
    return iter_dir


# ---- SCHEMA shape ----------------------------------------------------------


def test_schema_name_is_loop_verify_codex_patch():
    assert loop_verify_codex_patch.SCHEMA["name"] == "loop.verify_codex_patch"


def test_schema_required_fields():
    required = set(loop_verify_codex_patch.SCHEMA["input_schema"]["required"])
    assert required == {"iteration_dir", "codex_finding_id"}


def test_schema_supports_mode_property():
    props = loop_verify_codex_patch.SCHEMA["input_schema"]["properties"]
    assert "mode" in props


def test_schema_disallows_additional_properties():
    assert (
        loop_verify_codex_patch.SCHEMA["input_schema"]["additionalProperties"] is False
    )


def test_schema_output_required_fields():
    required = set(loop_verify_codex_patch.SCHEMA["output_schema"]["required"])
    assert required == {"verdict", "rationale"}


def test_schema_verdict_enum_includes_all_three_values():
    verdict_def = loop_verify_codex_patch.SCHEMA["output_schema"]["properties"]["verdict"]
    enum = set(verdict_def["enum"])
    assert enum == {"approved", "corrected_resubmit", "blocked"}


# ---- register() integration ------------------------------------------------


def test_register_adds_tool_to_registry():
    registry = ToolRegistry()
    loop_verify_codex_patch.register(registry)
    names = [t["name"] for t in registry.list_tools()]
    assert "loop.verify_codex_patch" in names


def test_register_handler_is_callable():
    registry = ToolRegistry()
    loop_verify_codex_patch.register(registry)
    handler = registry.get_handler("loop.verify_codex_patch")
    assert callable(handler)


# ---- mode=build_inputs -----------------------------------------------------


def test_build_inputs_assembles_bundle(tmp_path):
    """Given fake codex-review with patch_proposal + goal_packet + touched files,
    returns input bundle with review_scope_hash."""
    iter_dir = _make_iter_dir(tmp_path)
    _write_goal_packet(iter_dir)
    _write_touched_file(tmp_path, "scripts/foo.py", "hello\nworld\n")
    _write_claude_md(tmp_path, "# CLAUDE.md\nKarpathy four principles.\n")
    pp = _build_patch_proposal()
    _write_codex_review(iter_dir, patch_proposal=pp)

    result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        claude_md_path=str(tmp_path / "CLAUDE.md"),
        repo_root=str(tmp_path),
    )

    assert result["verdict"] == "blocked"
    assert "review_scope_hash" in result
    assert result["review_scope_hash"]
    assert isinstance(result["review_scope_hash"], str)
    assert len(result["review_scope_hash"]) == 64  # sha256 hex
    assert "verifier_inputs" in result
    bundle = result["verifier_inputs"]
    # Required keys per spec
    assert "goal_packet" in bundle
    assert "acceptance_gates" in bundle
    assert "patch_proposal" in bundle
    assert "touched_files" in bundle
    assert "claude_md_excerpt" in bundle
    assert "codex_finding_text" in bundle
    assert "base_sha" in bundle
    assert "post_sha_hint" in bundle


def test_build_inputs_excludes_codex_reasoning_trail(tmp_path):
    """verifier_inputs MUST NOT include codex's goal_satisfied_rationale or
    other reasoning fields. Independence by source-segregating reasoning."""
    iter_dir = _make_iter_dir(tmp_path)
    _write_goal_packet(iter_dir)
    _write_touched_file(tmp_path, "scripts/foo.py", "hello\nworld\n")
    _write_claude_md(tmp_path)
    pp = _build_patch_proposal()
    _write_codex_review(iter_dir, patch_proposal=pp)

    result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        claude_md_path=str(tmp_path / "CLAUDE.md"),
        repo_root=str(tmp_path),
    )

    bundle = result["verifier_inputs"]
    # Walk the entire bundle JSON-string and assert the reasoning-trail
    # marker text never appears anywhere.
    bundle_serialized = json.dumps(bundle, default=str)
    assert "Codex's private reasoning trail" not in bundle_serialized
    assert "goal_satisfied_rationale" not in bundle
    # The finding text MUST be present (the WHAT).
    finding_text = bundle["codex_finding_text"]
    assert "Test finding for verify-codex-patch tests" in finding_text
    assert "Apply the patch_proposal as a fix" in finding_text


def test_build_inputs_includes_full_touched_file_contents(tmp_path):
    """touched_files maps path -> FULL content (not snippets)."""
    iter_dir = _make_iter_dir(tmp_path)
    _write_goal_packet(iter_dir)
    full_content = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n"
    _write_touched_file(tmp_path, "scripts/foo.py", full_content)
    _write_claude_md(tmp_path)
    pp = _build_patch_proposal(
        files_touched=("scripts/foo.py",),
    )
    _write_codex_review(iter_dir, patch_proposal=pp)

    result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        claude_md_path=str(tmp_path / "CLAUDE.md"),
        repo_root=str(tmp_path),
    )
    touched = result["verifier_inputs"]["touched_files"]
    assert "scripts/foo.py" in touched
    assert touched["scripts/foo.py"] == full_content


def test_build_inputs_no_patch_proposal_returns_blocked_with_error(tmp_path):
    """If codex-review.yaml has the named finding but no patch_proposal,
    return verdict=blocked with explanatory error."""
    iter_dir = _make_iter_dir(tmp_path)
    _write_goal_packet(iter_dir)
    _write_claude_md(tmp_path)
    _write_codex_review(iter_dir, patch_proposal=None)

    result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        claude_md_path=str(tmp_path / "CLAUDE.md"),
        repo_root=str(tmp_path),
    )
    assert result["verdict"] == "blocked"
    assert result.get("error")
    assert "patch_proposal" in result["error"]


def test_build_inputs_unknown_finding_id_returns_blocked(tmp_path):
    iter_dir = _make_iter_dir(tmp_path)
    _write_goal_packet(iter_dir)
    _write_claude_md(tmp_path)
    pp = _build_patch_proposal()
    _write_codex_review(iter_dir, patch_proposal=pp)

    result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-999",  # not present
        mode="build_inputs",
        claude_md_path=str(tmp_path / "CLAUDE.md"),
        repo_root=str(tmp_path),
    )
    assert result["verdict"] == "blocked"
    assert result.get("error")
    assert "codex-rev-999" in result["error"]


def test_review_scope_hash_deterministic(tmp_path):
    """Same inputs produce the same review_scope_hash on repeat calls."""
    iter_dir = _make_iter_dir(tmp_path)
    _write_goal_packet(iter_dir)
    _write_touched_file(tmp_path, "scripts/foo.py", "hello\nworld\n")
    _write_claude_md(tmp_path)
    pp = _build_patch_proposal()
    _write_codex_review(iter_dir, patch_proposal=pp)

    r1 = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        claude_md_path=str(tmp_path / "CLAUDE.md"),
        repo_root=str(tmp_path),
    )
    r2 = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        claude_md_path=str(tmp_path / "CLAUDE.md"),
        repo_root=str(tmp_path),
    )
    assert r1["review_scope_hash"] == r2["review_scope_hash"]


def test_review_scope_hash_input_sensitive(tmp_path):
    """Different file content produces a different review_scope_hash."""
    iter_dir = _make_iter_dir(tmp_path)
    _write_goal_packet(iter_dir)
    _write_touched_file(tmp_path, "scripts/foo.py", "hello\nworld\n")
    _write_claude_md(tmp_path)
    pp = _build_patch_proposal()
    _write_codex_review(iter_dir, patch_proposal=pp)

    r1 = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        claude_md_path=str(tmp_path / "CLAUDE.md"),
        repo_root=str(tmp_path),
    )

    # Mutate the touched file content
    _write_touched_file(tmp_path, "scripts/foo.py", "hello\nworld\nDIFFERENT\n")

    r2 = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="build_inputs",
        claude_md_path=str(tmp_path / "CLAUDE.md"),
        repo_root=str(tmp_path),
    )
    assert r1["review_scope_hash"] != r2["review_scope_hash"]


# ---- mode=record_verdict ---------------------------------------------------


def test_record_verdict_approved_writes_yaml(tmp_path):
    iter_dir = _make_iter_dir(tmp_path)
    pp = _build_patch_proposal()
    patch_id = pp["patch_id"]

    result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="record_verdict",
        verdict="approved",
        rationale="Patch matches finding; tests pass; CLAUDE.md compliant.",
        review_scope_hash="a" * 64,
        approved_patch_id=patch_id,
    )
    assert result["verdict"] == "approved"
    out_path = iter_dir / "codex-patch-verifications" / f"{patch_id}.yaml"
    assert out_path.exists()
    data = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert data["verdict"] == "approved"
    assert data["review_scope_hash"] == "a" * 64
    assert data["approved_patch_id"] == patch_id
    assert data["schema_version"] == 1
    assert data["verifier"] == "claude"


def test_record_verdict_corrected_resubmit_writes_corrections(tmp_path):
    iter_dir = _make_iter_dir(tmp_path)
    pp = _build_patch_proposal()
    patch_id = pp["patch_id"]
    corrections = "--- a/scripts/foo.py\n+++ b/scripts/foo.py\n@@ -1 +1 @@\n-hello\n+hello there\n"

    result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="record_verdict",
        verdict="corrected_resubmit",
        rationale="Patch incomplete; claude added missing case.",
        review_scope_hash="b" * 64,
        corrections=corrections,
        approved_patch_id=patch_id,
    )
    assert result["verdict"] == "corrected_resubmit"
    out_path = iter_dir / "codex-patch-verifications" / f"{patch_id}.yaml"
    assert out_path.exists()
    data = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert data["verdict"] == "corrected_resubmit"
    assert data["corrections"] == corrections


def test_record_verdict_invalid_verdict_rejected(tmp_path):
    iter_dir = _make_iter_dir(tmp_path)
    pp = _build_patch_proposal()
    patch_id = pp["patch_id"]

    result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="record_verdict",
        verdict="approveddddd",  # invalid; not in enum
        rationale="x",
        review_scope_hash="c" * 64,
        approved_patch_id=patch_id,
    )
    assert result["verdict"] == "blocked"
    assert result.get("error")
    assert "verdict" in result["error"].lower()


def test_record_verdict_missing_patch_id_rejected(tmp_path):
    """record_verdict requires approved_patch_id (so the file path is well-defined)."""
    iter_dir = _make_iter_dir(tmp_path)

    result = loop_verify_codex_patch.handle(
        iteration_dir=str(iter_dir),
        codex_finding_id="codex-rev-001",
        mode="record_verdict",
        verdict="approved",
        rationale="x",
        review_scope_hash="d" * 64,
        approved_patch_id=None,
    )
    assert result["verdict"] == "blocked"
    assert result.get("error")
