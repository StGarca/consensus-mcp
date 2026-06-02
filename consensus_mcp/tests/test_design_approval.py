"""Tests for the design-approval marker mint/verify (FIX TRACK 1, decision B1/B3).

The `.consensus/design-approved` marker is a POINTER/CACHE, not the trust root.
It holds {schema_version, design_consensus_ref, converged_plan_sha256, scope_glob,
repo_root_id}. `verify_design_approval` ALWAYS re-validates against the live
consensus-state/ seal via `_delivery_readiness.resolve_consensus_ref`: the
referenced iteration must resolve as a real CLOSED/SEALED consensus iteration
with >=2 NON-CLAUDE reviewer artifacts, and the converged-plan hash must match.

FORGE invariant (load-bearing): a hand-written marker pointing at a non-sealed or
non-existent ref is REJECTED. Forging an accepted approval would require forging
the sealed cross-family artifacts the T6 seal already protects. Fail-closed on
every error path.
"""
from pathlib import Path

import yaml

from consensus_mcp import _design_approval as da
from consensus_mcp import _delivery_readiness as dr


# --------------------------------------------------------------------------- #
# Fixtures: build a fake sealed cross-family iteration on disk.
# --------------------------------------------------------------------------- #

def _make_sealed_iteration(
    repo_root: Path,
    ref: str = "iteration-fix-impl",
    *,
    closing_state: str = "quorum_close_passed",
    reviewers=("codex", "gemini"),
    plan_body: str = "decision:\n  do: the thing\n",
) -> str:
    """Create consensus-state/active/<ref> with an iteration-outcome.yaml carrying
    a sealed closing_state, a converged-plan.yaml, and one <family>-review.yaml per
    reviewer. Returns the converged-plan sha256 (so callers can mint a marker)."""
    iter_dir = repo_root / "consensus-state" / "active" / ref
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "iteration-outcome.yaml").write_text(
        f"closing_state: {closing_state}\n", encoding="utf-8")
    plan = iter_dir / "converged-plan.yaml"
    plan.write_text(plan_body, encoding="utf-8")
    for fam in reviewers:
        (iter_dir / f"{fam}-review.yaml").write_text(
            f"iteration_id: {ref}\nreviewer_id: {fam}-1\n", encoding="utf-8")
    return dr.compute_artifact_hash(plan)


def _write_marker(repo_root: Path, **fields) -> Path:
    """Write a raw .consensus/design-approved (simulates a hand-forged marker)."""
    (repo_root / ".consensus").mkdir(parents=True, exist_ok=True)
    path = repo_root / ".consensus" / "design-approved"
    path.write_text(yaml.safe_dump(fields, sort_keys=True), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Basic fail-closed behavior.
# --------------------------------------------------------------------------- #

def test_verify_rejects_missing_marker(tmp_path):
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False
    assert res.reason


def test_verify_fail_closed_on_unparseable_marker(tmp_path):
    (tmp_path / ".consensus").mkdir(parents=True)
    (tmp_path / ".consensus" / "design-approved").write_text(
        "::: not yaml :::\n  - [unbalanced", encoding="utf-8")
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False


def test_verify_fail_closed_on_non_mapping_marker(tmp_path):
    (tmp_path / ".consensus").mkdir(parents=True)
    (tmp_path / ".consensus" / "design-approved").write_text(
        "- just\n- a\n- list\n", encoding="utf-8")
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False


# --------------------------------------------------------------------------- #
# Mint -> verify happy path (genuine sealed cross-family iteration).
# --------------------------------------------------------------------------- #

def test_mint_then_verify_in_scope(tmp_path):
    plan_sha = _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    da.mint_design_approval(
        repo_root=tmp_path,
        design_consensus_ref="iteration-fix-impl",
        scope_glob="src/**",
        converged_plan_sha256=plan_sha,
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is True, res.reason


def test_verify_rejects_out_of_scope(tmp_path):
    plan_sha = _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    da.mint_design_approval(
        repo_root=tmp_path,
        design_consensus_ref="iteration-fix-impl",
        scope_glob="src/**",
        converged_plan_sha256=plan_sha,
    )
    res = da.verify_design_approval(tmp_path / "docs/y.md", repo_root=tmp_path)
    assert res.ok is False


def test_verify_accepts_absolute_target_under_repo(tmp_path):
    plan_sha = _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    da.mint_design_approval(
        repo_root=tmp_path,
        design_consensus_ref="iteration-fix-impl",
        scope_glob="src/*.py",
        converged_plan_sha256=plan_sha,
    )
    abs_target = (tmp_path / "src" / "x.py").resolve()
    res = da.verify_design_approval(abs_target, repo_root=tmp_path)
    assert res.ok is True, res.reason


def test_mint_writes_pointer_contract_fields(tmp_path):
    plan_sha = _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    da.mint_design_approval(
        repo_root=tmp_path,
        design_consensus_ref="iteration-fix-impl",
        scope_glob="src/**",
        converged_plan_sha256=plan_sha,
    )
    marker = tmp_path / ".consensus" / "design-approved"
    assert marker.exists()
    data = yaml.safe_load(marker.read_text(encoding="utf-8"))
    assert set(data) >= {
        "schema_version",
        "design_consensus_ref",
        "converged_plan_sha256",
        "scope_glob",
        "repo_root_id",
    }
    # The trusted boolean is GONE - no self-asserted approval field.
    assert "cross_family_sealed" not in data
    assert data["design_consensus_ref"] == "iteration-fix-impl"
    assert data["scope_glob"] == "src/**"
    assert data["converged_plan_sha256"] == plan_sha


# --------------------------------------------------------------------------- #
# FORGE TEST (load-bearing) - a marker is only as good as the live seal it points
# at. A hand-written marker pointing at an unsealed / nonexistent / single-claude
# iteration is REJECTED; only a pointer to a real sealed cross-family iteration
# with a matching converged-plan hash is ACCEPTED.
# --------------------------------------------------------------------------- #

def test_forge_marker_pointing_at_nonexistent_ref_rejected(tmp_path):
    # Hand-forge a fully-formed marker pointing at an iteration that does not exist.
    _write_marker(
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-does-not-exist",
        converged_plan_sha256="deadbeef",
        scope_glob="src/**",
        repo_root_id="x",
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False, "a marker pointing at a non-existent iteration must be REJECTED"


def test_forge_marker_pointing_at_unsealed_ref_rejected(tmp_path):
    # The iteration exists with a converged-plan, but its closing_state is NOT a
    # sealed state - resolve_consensus_ref must reject it.
    plan_sha = _make_sealed_iteration(
        tmp_path, "iteration-open", closing_state="in_progress")
    _write_marker(
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-open",
        converged_plan_sha256=plan_sha,
        scope_glob="src/**",
        repo_root_id="x",
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False, "an UNSEALED iteration must not authorize implementation"


def test_forge_marker_single_claude_only_iteration_rejected(tmp_path):
    # Sealed, but only a single CLAUDE reviewer artifact -> fewer than 2 non-claude
    # reviewers -> rejected (no single-family self-approval).
    plan_sha = _make_sealed_iteration(
        tmp_path, "iteration-claude-only", reviewers=("claude",))
    _write_marker(
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-claude-only",
        converged_plan_sha256=plan_sha,
        scope_glob="src/**",
        repo_root_id="x",
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False, "a single-claude-only sealed iteration must be REJECTED"


def test_forge_marker_one_nonclaude_reviewer_rejected(tmp_path):
    # Sealed cross family but only ONE non-claude reviewer -> < 2 -> rejected.
    plan_sha = _make_sealed_iteration(
        tmp_path, "iteration-one-rev", reviewers=("claude", "codex"))
    _write_marker(
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-one-rev",
        converged_plan_sha256=plan_sha,
        scope_glob="src/**",
        repo_root_id="x",
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False, "need >=2 non-claude reviewers; one is not enough"


def test_forge_marker_wrong_plan_hash_rejected(tmp_path):
    # Genuinely sealed cross-family iteration, but the marker's converged_plan_sha256
    # does NOT match the iteration's converged-plan.yaml -> rejected (tamper guard).
    _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    _write_marker(
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-fix-impl",
        converged_plan_sha256="0" * 64,  # wrong
        scope_glob="src/**",
        repo_root_id="x",
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False, "a converged_plan_sha256 mismatch must be REJECTED"


def test_genuine_sealed_cross_family_marker_accepted(tmp_path):
    # The accept counterpart of the forge test: real sealed cross-family iteration,
    # >=2 non-claude reviewers, matching converged-plan hash -> ACCEPTED.
    plan_sha = _make_sealed_iteration(
        tmp_path, "iteration-fix-impl", reviewers=("codex", "gemini", "kimi"))
    _write_marker(
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-fix-impl",
        converged_plan_sha256=plan_sha,
        scope_glob="src/**",
        repo_root_id="x",
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is True, res.reason


# --------------------------------------------------------------------------- #
# Scope confinement (decision B3) - out-of-repo / unsafe targets rejected.
# --------------------------------------------------------------------------- #

def test_verify_rejects_out_of_repo_target(tmp_path):
    plan_sha = _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    da.mint_design_approval(
        repo_root=tmp_path,
        design_consensus_ref="iteration-fix-impl",
        scope_glob="**",  # even with a broad glob, out-of-repo must fail
        converged_plan_sha256=plan_sha,
    ) if False else _write_marker(  # avoid mint's '**' rejection; forge instead
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-fix-impl",
        converged_plan_sha256=plan_sha,
        scope_glob="src/**",
        repo_root_id="x",
    )
    outside = tmp_path.parent / "elsewhere" / "evil.py"
    res = da.verify_design_approval(outside, repo_root=tmp_path)
    assert res.ok is False, "a target resolving OUTSIDE the repo must be REJECTED"


def test_verify_rejects_dotdot_escape_target(tmp_path):
    plan_sha = _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    _write_marker(
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-fix-impl",
        converged_plan_sha256=plan_sha,
        scope_glob="src/**",
        repo_root_id="x",
    )
    res = da.verify_design_approval(Path("../../etc/passwd"), repo_root=tmp_path)
    assert res.ok is False, "a ../ escape must be REJECTED"


# --------------------------------------------------------------------------- #
# Mint-time scope_glob validation (decision B3, kimi) - reject '*'/'**'.
# --------------------------------------------------------------------------- #

def test_mint_rejects_overbroad_scope_glob_star(tmp_path):
    _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    import pytest
    with pytest.raises(Exception):
        da.mint_design_approval(
            repo_root=tmp_path,
            design_consensus_ref="iteration-fix-impl",
            scope_glob="*",
            converged_plan_sha256="abc",
        )


def test_mint_rejects_overbroad_scope_glob_doublestar(tmp_path):
    _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    import pytest
    with pytest.raises(Exception):
        da.mint_design_approval(
            repo_root=tmp_path,
            design_consensus_ref="iteration-fix-impl",
            scope_glob="**",
            converged_plan_sha256="abc",
        )


# --------------------------------------------------------------------------- #
# marker_is_sealed helper (path-agnostic; used by the Bash branch).
# --------------------------------------------------------------------------- #

def test_marker_is_sealed_true_for_genuine_sealed_marker(tmp_path):
    plan_sha = _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    da.mint_design_approval(
        repo_root=tmp_path,
        design_consensus_ref="iteration-fix-impl",
        scope_glob="src/**",
        converged_plan_sha256=plan_sha,
    )
    res = da.marker_is_sealed(tmp_path)
    assert res.ok is True, res.reason


def test_marker_is_sealed_false_when_missing(tmp_path):
    res = da.marker_is_sealed(tmp_path)
    assert res.ok is False


def test_marker_is_sealed_false_for_forged_unsealed_pointer(tmp_path):
    _write_marker(
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-nope",
        converged_plan_sha256="x",
        scope_glob="src/**",
        repo_root_id="x",
    )
    res = da.marker_is_sealed(tmp_path)
    assert res.ok is False, "a pointer to a non-sealed iteration is not 'sealed'"


def test_marker_is_sealed_false_for_broad_scope(tmp_path):
    # A valid sealed iteration but a broad scope_glob in the marker -> not 'tight'.
    plan_sha = _make_sealed_iteration(tmp_path, "iteration-fix-impl")
    _write_marker(
        tmp_path,
        schema_version=1,
        design_consensus_ref="iteration-fix-impl",
        converged_plan_sha256=plan_sha,
        scope_glob="**",  # broad
        repo_root_id="x",
    )
    res = da.marker_is_sealed(tmp_path)
    assert res.ok is False, "marker_is_sealed requires a TIGHT scope_glob"
