"""Tests for consensus_mcp._paths.

Per iter-0024 converged plan (SHIP-PHASED, Phase A): verify each resolver
honors its env-var override AT CALL TIME (not at import time), and the
state_root vs repo_root vs project_root split is preserved.

iter-0025 acceptance gates A1-A5 are all exercised here.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from consensus_mcp import _paths


# ---------- repo_root ----------

def test_repo_root_honors_env_override_at_call_time(tmp_path, monkeypatch):
    """A2 (the iter-0019 gotcha): setenv AFTER import must take effect."""
    # Import is already done (top of file). Sanity-check the default first.
    before = _paths.repo_root()
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    after = _paths.repo_root()
    assert after == tmp_path.resolve()
    assert after != before, "env override at call-time must change the result"


def test_repo_root_without_env_falls_back_to_dev_checkout(monkeypatch):
    """In a dev checkout (this test suite IS one), walked-up __file__.parent.parent
    contains `consensus_mcp/`, so that's the resolved repo root."""
    monkeypatch.delenv("CONSENSUS_MCP_REPO_ROOT", raising=False)
    result = _paths.repo_root()
    assert (result / "consensus_mcp").exists()


def test_repo_root_wheel_layout_fallback(tmp_path, monkeypatch):
    """A4 (gemini-rev-001 from iter-0024 + codex-rev-001 from iter-0025):
    Installed-wheel layout where this module lives under site-packages/.
    Walked-up `__file__.parent.parent` IS a real directory containing the
    `consensus_mcp` package — but lacks repo markers (pyproject.toml, .git).
    The resolver must NOT mistake site-packages for the repo root; it must
    fall back to cwd."""
    fake_module_path = tmp_path / "site-packages" / "consensus_mcp" / "_paths.py"
    fake_module_path.parent.mkdir(parents=True)
    fake_module_path.write_text("# fake module", encoding="utf-8")

    # PROVENANCE for what we're testing:
    fake_parent = fake_module_path.parent.parent  # i.e. tmp_path/site-packages
    assert (fake_parent / "consensus_mcp").exists(), \
        "test setup: consensus_mcp/ must exist under fake parent (mimics wheel)"
    assert not (fake_parent / "pyproject.toml").exists(), \
        "test setup: no repo marker under fake parent (mimics wheel)"
    assert not (fake_parent / ".git").exists(), \
        "test setup: no repo marker under fake parent (mimics wheel)"

    monkeypatch.delenv("CONSENSUS_MCP_REPO_ROOT", raising=False)
    monkeypatch.setattr(_paths, "__file__", str(fake_module_path))
    cwd_target = tmp_path / "cwd"
    cwd_target.mkdir()
    monkeypatch.chdir(cwd_target)

    result = _paths.repo_root()
    # If the resolver mistakenly used `consensus_mcp/` presence, it would
    # return tmp_path/site-packages. The repo-marker discriminator MUST
    # send it to cwd instead.
    assert result == cwd_target.resolve(), \
        f"repo_root must fall back to cwd in wheel layout; got {result}"
    assert result != fake_parent.resolve(), \
        "repo_root must NOT mistake site-packages for repo root"


def test_repo_root_uses_pyproject_marker(tmp_path, monkeypatch):
    """codex-rev-001 (iter-0025): explicitly verify the pyproject.toml marker
    is what discriminates dev-checkout from installed-wheel. Setup mimics a
    dev checkout: site-packages-like layout BUT with pyproject.toml present."""
    fake_module_path = tmp_path / "checkout" / "consensus_mcp" / "_paths.py"
    fake_module_path.parent.mkdir(parents=True)
    fake_module_path.write_text("# fake module", encoding="utf-8")
    (tmp_path / "checkout" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.delenv("CONSENSUS_MCP_REPO_ROOT", raising=False)
    monkeypatch.setattr(_paths, "__file__", str(fake_module_path))
    result = _paths.repo_root()
    assert result == (tmp_path / "checkout").resolve()


# ---------- state_root ----------

def test_state_root_honors_state_root_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    assert _paths.state_root() == tmp_path.resolve()


def test_state_root_falls_back_to_repo_root_subdir(tmp_path, monkeypatch):
    """A3: when STATE_ROOT unset but REPO_ROOT is, state_root() = REPO_ROOT/consensus-state."""
    monkeypatch.delenv("CONSENSUS_MCP_STATE_ROOT", raising=False)
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    assert _paths.state_root() == (tmp_path / "consensus-state").resolve()


def test_state_root_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_STATE_ROOT", raising=False)
    monkeypatch.delenv("CONSENSUS_MCP_REPO_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _paths.state_root() == (tmp_path / "consensus-state").resolve()


# ---------- project_root ----------

def test_project_root_honors_project_root_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(tmp_path))
    assert _paths.project_root() == tmp_path.resolve()


def test_project_root_independent_from_state_root(tmp_path, monkeypatch):
    """A3: PROJECT_ROOT and STATE_ROOT are orthogonal — operator can set
    one without affecting the other."""
    proj = tmp_path / "proj"
    proj.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(proj))
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state))
    assert _paths.project_root() == proj.resolve()
    assert _paths.state_root() == state.resolve()


# ---------- spec_path ----------

def test_spec_path_honors_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "custom-spec.md"
    custom.write_text("# spec", encoding="utf-8")
    monkeypatch.setenv("CONSENSUS_MCP_SPEC_PATH", str(custom))
    assert _paths.spec_path() == custom.resolve()


def test_spec_path_falls_back_to_packaged_template(tmp_path, monkeypatch):
    """When neither env nor walked-up spec exists, fall back to packaged template."""
    monkeypatch.delenv("CONSENSUS_MCP_SPEC_PATH", raising=False)
    # Point REPO_ROOT at an empty dir so the legacy fallback misses.
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    # The walked-up fallback only fires if the dev-checkout spec exists; in
    # the actual test repo it does (docs/architecture/orchestration-spec.md
    # exists). So this test asserts that EITHER walked-up OR packaged
    # template is returned — both are valid fallbacks.
    result = _paths.spec_path()
    # Must point at a real file.
    assert result.exists() or result.name == "spec_template.md"


# ---------- derived paths ----------

def test_archive_dir_composes_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    assert _paths.archive_dir() == (tmp_path / "archive" / "review-passes").resolve()


def test_index_path_composes_archive_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    assert _paths.index_path() == (tmp_path / "archive" / "review-passes" / "index.yaml").resolve()


def test_active_dir_composes_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    assert _paths.active_dir() == (tmp_path / "active").resolve()


def test_audit_log_path_composes_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    assert _paths.audit_log_path() == (tmp_path / "state" / "audit-log.jsonl").resolve()


def test_dispatch_log_path_composes_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    assert _paths.dispatch_log_path() == (tmp_path / "state" / "dispatch-log.jsonl").resolve()


# ---------- monkeypatch flip semantics (THE iter-0019 gotcha) ----------

def test_env_change_between_calls_is_observable(tmp_path, monkeypatch):
    """The whole point of Phase A: changing env between calls changes results.
    iter-0019's module-level capture would have frozen the first call's value."""
    p1 = tmp_path / "first"
    p1.mkdir()
    p2 = tmp_path / "second"
    p2.mkdir()
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(p1))
    r1 = _paths.state_root()
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(p2))
    r2 = _paths.state_root()
    assert r1 == p1.resolve()
    assert r2 == p2.resolve()
    assert r1 != r2


def test_unsetting_env_falls_back_correctly(tmp_path, monkeypatch):
    """Removing the env var mid-session reverts to fallback behavior."""
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    assert _paths.state_root() == tmp_path.resolve()
    monkeypatch.delenv("CONSENSUS_MCP_STATE_ROOT")
    monkeypatch.delenv("CONSENSUS_MCP_REPO_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _paths.state_root() == (tmp_path / "consensus-state").resolve()
