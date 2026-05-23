"""Tests for the design-approval marker mint/verify (Track B, Task B1).

The `.consensus/design-approved` marker is the cross-family-sealed gate that the
PreToolUse hook validates before allowing implementation tool calls. A marker is
VALID iff: it parses, `cross_family_sealed` is true, and the edited repo-relative
path matches `scope_glob` (fnmatch). A `cross_family_sealed=False` marker is
ADVISORY ONLY (a single-Claude step cannot self-approve) -> treated as NOT
approved. Fail-closed on any error.
"""
from pathlib import Path

import yaml

from consensus_mcp import _design_approval as da


def test_verify_rejects_missing_marker(tmp_path):
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False
    assert res.reason  # a non-empty reason is always given


def test_mint_then_verify_in_scope(tmp_path):
    (tmp_path / ".consensus").mkdir(parents=True)
    da.mint_design_approval(
        repo_root=tmp_path,
        iteration_id="it1",
        scope_glob="src/**",
        converged_plan_sha256="abc",
        cross_family_sealed=True,
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is True, res


def test_verify_rejects_out_of_scope(tmp_path):
    (tmp_path / ".consensus").mkdir(parents=True)
    da.mint_design_approval(
        repo_root=tmp_path,
        iteration_id="it1",
        scope_glob="src/**",
        converged_plan_sha256="abc",
        cross_family_sealed=True,
    )
    res = da.verify_design_approval(tmp_path / "docs/y.md", repo_root=tmp_path)
    assert res.ok is False


def test_single_claude_marker_is_advisory_only(tmp_path):
    (tmp_path / ".consensus").mkdir(parents=True)
    da.mint_design_approval(
        repo_root=tmp_path,
        iteration_id="it1",
        scope_glob="src/**",
        converged_plan_sha256="abc",
        cross_family_sealed=False,
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False


def test_mint_writes_contract_fields(tmp_path):
    (tmp_path / ".consensus").mkdir(parents=True)
    da.mint_design_approval(
        repo_root=tmp_path,
        iteration_id="it1",
        scope_glob="src/**",
        converged_plan_sha256="abc",
        cross_family_sealed=True,
    )
    marker = tmp_path / ".consensus" / "design-approved"
    assert marker.exists()
    data = yaml.safe_load(marker.read_text(encoding="utf-8"))
    assert set(data) >= {
        "iteration_id",
        "scope_glob",
        "converged_plan_sha256",
        "sealed_at_utc",
        "cross_family_sealed",
    }
    assert data["iteration_id"] == "it1"
    assert data["scope_glob"] == "src/**"
    assert data["converged_plan_sha256"] == "abc"
    assert data["cross_family_sealed"] is True


def test_verify_fail_closed_on_unparseable_marker(tmp_path):
    (tmp_path / ".consensus").mkdir(parents=True)
    (tmp_path / ".consensus" / "design-approved").write_text(
        "::: not yaml :::\n  - [unbalanced", encoding="utf-8"
    )
    res = da.verify_design_approval(tmp_path / "src/x.py", repo_root=tmp_path)
    assert res.ok is False


def test_verify_accepts_absolute_target_under_repo(tmp_path):
    (tmp_path / ".consensus").mkdir(parents=True)
    da.mint_design_approval(
        repo_root=tmp_path,
        iteration_id="it1",
        scope_glob="src/*.py",
        converged_plan_sha256="abc",
        cross_family_sealed=True,
    )
    abs_target = (tmp_path / "src" / "x.py").resolve()
    res = da.verify_design_approval(abs_target, repo_root=tmp_path)
    assert res.ok is True, res
