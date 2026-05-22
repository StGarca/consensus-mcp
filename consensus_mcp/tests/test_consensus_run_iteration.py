"""Tests for consensus.run_iteration MCP tool."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from consensus_mcp import _engine_factory as factory
from consensus_mcp import config as cfg
from consensus_mcp.tools import consensus_run_iteration as tool


def _make_iter_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    iter_dir = tmp_path / "iteration-test"
    iter_dir.mkdir()
    goal = iter_dir / "goal_packet.yaml"
    goal.write_text("pilot: iter-test\nschema_version: 1\n", encoding="utf-8")
    target = iter_dir / "problem.yaml"
    target.write_text("question: test\n", encoding="utf-8")
    return iter_dir, goal, target


def _write_config(tmp_path: Path, contributors=None, mode=None):
    config = deepcopy(cfg.default_config())
    if contributors is not None:
        config["contributors"]["enabled"] = contributors
    if mode is not None:
        config["workflow"]["mode"] = mode
        if mode == cfg.WORKFLOW_POST_REVIEW:
            config["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
            if len(config["contributors"]["enabled"]) == 1:
                config["convergence"]["rule"] = cfg.CONVERGE_UNANIMOUS
    cfg.validate(config)
    cfg_dir = tmp_path / ".consensus"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return cfg_path


# ---------- happy paths ----------

def test_run_iteration_workflow_4_with_fakes(tmp_path, monkeypatch):
    """End-to-end: workflow #4 with Fake adapters via factory monkeypatch."""
    _write_config(tmp_path, contributors=["claude", "codex", "gemini"])
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)

    # Swap in Fake adapters so we don't need real codex/gemini subprocesses.
    from consensus_mcp.contributors.base import (
        FakeAlwaysApprove, FakeAlwaysBlock, FakeRaisesDispatchError,
    )

    def _fake_build_adapters(config, *, claude_artifact_callback=None, **_kwargs):
        # **_kwargs absorbs additive callbacks (e.g. v1.20.0
        # host_peer_review_callback) so the fake stays forward-compatible with
        # build_engine threading new optional callbacks through build_adapters.
        return {
            "claude": FakeAlwaysApprove(),
            "codex": FakeAlwaysApprove(),
            "gemini": FakeAlwaysApprove(),
        }
    monkeypatch.setattr(factory, "build_adapters", _fake_build_adapters)

    claude_yaml = yaml.safe_dump({
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
    })

    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        claude_proposal_yaml=claude_yaml,
        repo_root=str(tmp_path),
    )
    assert result["ok"] is True
    assert result["workflow_mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE
    assert result["converged"] is True
    # All three contributors approved (Fake adapters share a name so the
    # engine's _artifact_contributor_key collapses them under one key when
    # artifact values are identical — counting >=1 approve_vote is enough
    # to assert convergence happened).
    assert len(result["approve_votes"]) >= 1
    assert result["final_artifact_path"] is not None
    assert "converged-plan.yaml" in result["final_artifact_path"]


def test_run_iteration_workflow_3_with_fakes(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        contributors=["claude", "codex", "gemini"],
        mode=cfg.WORKFLOW_POST_REVIEW,
    )
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    from consensus_mcp.contributors.base import FakeAlwaysApprove
    monkeypatch.setattr(factory, "build_adapters", lambda cfg, **k: {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysApprove(),
        "gemini": FakeAlwaysApprove(),
    })
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        repo_root=str(tmp_path),
    )
    assert result["ok"] is True
    assert result["workflow_mode"] == cfg.WORKFLOW_POST_REVIEW
    assert result["converged"] is True


def test_run_iteration_block_vote_fails_convergence(tmp_path, monkeypatch):
    """Workflow #4 with one blocking contributor → converged False."""
    _write_config(tmp_path, contributors=["claude", "codex", "gemini"])
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    from consensus_mcp.contributors.base import FakeAlwaysApprove, FakeAlwaysBlock
    monkeypatch.setattr(factory, "build_adapters", lambda cfg, **k: {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysBlock(),
        "gemini": FakeAlwaysApprove(),
    })
    claude_yaml = yaml.safe_dump({
        "findings": [], "goal_satisfied": True, "blocking_objections": [],
    })
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        claude_proposal_yaml=claude_yaml,
        repo_root=str(tmp_path),
    )
    assert result["ok"] is True
    assert result["converged"] is False
    assert "codex" in result["block_votes"]


# ---------- error paths ----------

def test_run_iteration_missing_config_uses_legacy(tmp_path, monkeypatch):
    """No .consensus/config.yaml → falls back to legacy-mode synthesis.

    Legacy synthesis returns contributors.enabled=[claude, codex], so the
    engine factory needs both adapters available.
    """
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    from consensus_mcp.contributors.base import FakeAlwaysApprove
    monkeypatch.setattr(factory, "build_adapters", lambda cfg, **k: {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysApprove(),
    })
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        repo_root=str(tmp_path),
    )
    assert result["ok"] is True
    # Legacy synthesizes workflow #3 (post-review).
    assert result["workflow_mode"] == cfg.WORKFLOW_POST_REVIEW


def test_run_iteration_invalid_claude_proposal_yaml(tmp_path, monkeypatch):
    _write_config(tmp_path, contributors=["claude", "codex"])
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        claude_proposal_yaml="not a mapping",
        repo_root=str(tmp_path),
    )
    assert result["ok"] is False
    assert "mapping" in result["error"]


def test_run_iteration_claude_proposal_missing_field(tmp_path, monkeypatch):
    _write_config(tmp_path, contributors=["claude", "codex"])
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    # missing blocking_objections
    bad = yaml.safe_dump({"findings": [], "goal_satisfied": True})
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        claude_proposal_yaml=bad,
        repo_root=str(tmp_path),
    )
    assert result["ok"] is False
    assert "blocking_objections" in result["error"]


def test_run_iteration_invalid_yaml_in_proposal(tmp_path, monkeypatch):
    _write_config(tmp_path, contributors=["claude", "codex"])
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        claude_proposal_yaml="key: value\n  bad-indent: this",
        repo_root=str(tmp_path),
    )
    assert result["ok"] is False


def test_claude_proposal_findings_wrong_type(tmp_path, monkeypatch):
    """codex pass-1 rev-003: findings must be a list, not a mapping."""
    _write_config(tmp_path, contributors=["claude", "codex"])
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    bad = yaml.safe_dump({
        "findings": {"id": "f1"},  # mapping, not list
        "goal_satisfied": True,
        "blocking_objections": [],
    })
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        claude_proposal_yaml=bad,
        repo_root=str(tmp_path),
    )
    assert result["ok"] is False
    assert "findings must be a list" in result["error"]


def test_claude_proposal_goal_satisfied_wrong_type(tmp_path, monkeypatch):
    """codex pass-1 rev-003: goal_satisfied must be a bool."""
    _write_config(tmp_path, contributors=["claude", "codex"])
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    bad = yaml.safe_dump({
        "findings": [],
        "goal_satisfied": "yes",  # string, not bool
        "blocking_objections": [],
    })
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        claude_proposal_yaml=bad,
        repo_root=str(tmp_path),
    )
    assert result["ok"] is False
    assert "goal_satisfied must be a bool" in result["error"]


def test_relative_paths_resolve_against_repo_root(tmp_path, monkeypatch):
    """codex pass-1 rev-002: relative iteration_dir/goal_packet/target paths
    must join repo_root, not the process cwd."""
    _write_config(tmp_path, contributors=["claude", "codex"])
    iter_dir, goal, target = _make_iter_dir(tmp_path)

    # Move cwd somewhere ELSE so process-cwd resolution would fail.
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)

    from consensus_mcp.contributors.base import FakeAlwaysApprove
    monkeypatch.setattr(factory, "build_adapters", lambda cfg, **k: {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysApprove(),
    })

    # Pass repo-RELATIVE paths (not absolute). Engine must still find them
    # because repo_root override redirects resolution.
    rel_iter = iter_dir.relative_to(tmp_path)
    rel_goal = goal.relative_to(tmp_path)
    rel_target = target.relative_to(tmp_path)
    claude_yaml = yaml.safe_dump({
        "findings": [], "goal_satisfied": True, "blocking_objections": [],
    })
    result = tool.handle(
        iteration_dir=str(rel_iter),
        goal_packet_path=str(rel_goal),
        target_path=str(rel_target),
        claude_proposal_yaml=claude_yaml,
        repo_root=str(tmp_path),
    )
    assert result["ok"] is True, result.get("error")


def test_missing_claude_proposal_yaml_rejected_in_workflow_4(tmp_path, monkeypatch):
    """codex pass-3 rev-001: workflow #4 with claude enabled MUST require
    claude_proposal_yaml; without it, fail fast with MissingClaudeProposalError
    rather than waiting for ClaudeAdapter to raise DispatchError mid-run."""
    _write_config(tmp_path, contributors=["claude", "codex", "gemini"])  # default = #4
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        # claude_proposal_yaml deliberately omitted
        repo_root=str(tmp_path),
    )
    assert result["ok"] is False
    assert result["error_type"] == "MissingClaudeProposalError"
    assert "claude_proposal_yaml is required" in result["error"]
    assert "propose-converge" in result["error"]


def test_missing_claude_proposal_yaml_ok_in_workflow_3(tmp_path, monkeypatch):
    """Workflow #3 doesn't dispatch ClaudeAdapter — claude_proposal_yaml stays
    optional."""
    _write_config(
        tmp_path,
        contributors=["claude", "codex", "gemini"],
        mode=cfg.WORKFLOW_POST_REVIEW,
    )
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    from consensus_mcp.contributors.base import FakeAlwaysApprove
    monkeypatch.setattr(factory, "build_adapters", lambda cfg, **k: {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysApprove(),
        "gemini": FakeAlwaysApprove(),
    })
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        repo_root=str(tmp_path),
    )
    assert result["ok"] is True


def test_register_attaches_tool(tmp_path):
    """register() must add the tool to a registry."""
    from consensus_mcp.tool_registry import ToolRegistry
    registry = ToolRegistry()
    tool.register(registry)
    listed = registry.list_tools()
    names = {t["name"] for t in listed}
    assert "consensus.run_iteration" in names
