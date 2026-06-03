"""Tests for consensus.get_iteration_outcome MCP tool."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from consensus_mcp.tools import consensus_get_iteration_outcome as tool


def test_returns_converged_plan_when_present(tmp_path):
    iter_dir = tmp_path / "iter-x"
    iter_dir.mkdir()
    plan = {"iteration_id": "iter-x", "converged_at_round": 1, "rule": "unanimous"}
    (iter_dir / "converged-plan.yaml").write_text(yaml.safe_dump(plan), encoding="utf-8")
    result = tool.handle(iteration_dir=str(iter_dir))
    assert result["ok"] is True
    assert result["iteration_id"] == "iter-x"
    assert result["converged_plan"]["converged_at_round"] == 1


def test_returns_contributor_artifacts_when_present(tmp_path):
    iter_dir = tmp_path / "iter-y"
    iter_dir.mkdir()
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({
            "pass_id": "codex-iter-y-1-pass1",
            "goal_satisfied": True,
            "sealed_at_utc": "2026-05-13T07:00:00Z",
            "findings": [],
            "blocking_objections": [],
        }), encoding="utf-8",
    )
    (iter_dir / "gemini-review.yaml").write_text(
        yaml.safe_dump({
            "pass_id": "gemini-iter-y-1-pass1",
            "goal_satisfied": False,
            "findings": [{"id": "g1"}],
            "blocking_objections": ["g1"],
        }), encoding="utf-8",
    )
    result = tool.handle(iteration_dir=str(iter_dir))
    assert result["ok"] is True
    keys = {a["contributor"] for a in result["contributor_artifacts"]}
    assert keys == {"codex", "gemini"}
    codex_entry = next(a for a in result["contributor_artifacts"] if a["contributor"] == "codex")
    assert codex_entry["goal_satisfied"] is True
    assert codex_entry["sealed_at"] == "2026-05-13T07:00:00Z"


def test_discovers_all_reviewer_families_not_just_hardcoded(tmp_path):
    """P0.2: the inspector must surface EVERY sealed reviewer family (grok, kimi,
    incl. hash/round-keyed mirror names), not only the hardcoded codex/claude/
    gemini - else a cold AI reading back a 4-AI panel silently loses families."""
    iter_dir = tmp_path / "iter-panel"
    iter_dir.mkdir()
    for fname, fam in [
        ("codex-review.yaml", "codex"),
        ("grok-review.yaml", "grok"),
        ("kimi-review-kimi-0c532a488f1cbfd5.yaml", "kimi"),  # hash-keyed mirror
        ("claude-proposal.yaml", "claude"),
    ]:
        (iter_dir / fname).write_text(
            yaml.safe_dump({"goal_satisfied": True, "pass_id": fam}), encoding="utf-8")
    # noise that must NOT be picked up as a reviewer artifact
    (iter_dir / "review-packet.yaml").write_text("x: 1", encoding="utf-8")
    (iter_dir / "converged-plan.yaml").write_text("decision: RATIFIED", encoding="utf-8")
    result = tool.handle(iteration_dir=str(iter_dir))
    assert result["ok"] is True
    keys = {a["contributor"] for a in result["contributor_artifacts"]}
    assert keys == {"codex", "grok", "kimi", "claude"}


def test_returns_effective_config_path_when_present(tmp_path):
    iter_dir = tmp_path / "iter-z"
    iter_dir.mkdir()
    cfg_path = iter_dir / "effective-config.yaml"
    cfg_path.write_text("schema_version: 1\n", encoding="utf-8")
    result = tool.handle(iteration_dir=str(iter_dir))
    assert result["ok"] is True
    assert result["effective_config_path"] == str(cfg_path)


def test_missing_iteration_dir(tmp_path):
    result = tool.handle(iteration_dir=str(tmp_path / "does-not-exist"))
    assert result["ok"] is False
    assert "does not exist" in result["error"]


def test_iteration_dir_is_file(tmp_path):
    file_not_dir = tmp_path / "file.yaml"
    file_not_dir.write_text("x: y", encoding="utf-8")
    result = tool.handle(iteration_dir=str(file_not_dir))
    assert result["ok"] is False
    assert "not a directory" in result["error"]


def test_empty_dir_returns_empty_outcome(tmp_path):
    iter_dir = tmp_path / "iter-empty"
    iter_dir.mkdir()
    result = tool.handle(iteration_dir=str(iter_dir))
    assert result["ok"] is True
    assert result["converged_plan"] is None
    assert result["contributor_artifacts"] == []
    assert result["effective_config_path"] is None


def test_malformed_converged_plan_yaml(tmp_path):
    iter_dir = tmp_path / "iter-bad"
    iter_dir.mkdir()
    (iter_dir / "converged-plan.yaml").write_text(
        "key: value\n  bad: indent\n", encoding="utf-8"
    )
    result = tool.handle(iteration_dir=str(iter_dir))
    assert result["ok"] is False
    assert "YAML" in result["error"]


def test_register_attaches_tool():
    from consensus_mcp.tool_registry import ToolRegistry
    registry = ToolRegistry()
    tool.register(registry)
    listed = registry.list_tools()
    names = {t["name"] for t in listed}
    assert "consensus.get_iteration_outcome" in names


def test_relative_iteration_dir_resolves_against_repo_root(tmp_path, monkeypatch):
    """codex iter-0022 pass-2 rev-001: relative iteration_dir must join repo_root
    not process cwd."""
    iter_dir = tmp_path / "iter-rel"
    iter_dir.mkdir()
    (iter_dir / "converged-plan.yaml").write_text(yaml.safe_dump({"x": 1}), encoding="utf-8")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    result = tool.handle(
        iteration_dir=str(iter_dir.relative_to(tmp_path)),
        repo_root=str(tmp_path),
    )
    assert result["ok"] is True
    assert result["iteration_id"] == "iter-rel"


def test_mcp_wire_format(tmp_path):
    """Tool must surface flat {name, description, inputSchema} per iter-0008."""
    from consensus_mcp.tool_registry import ToolRegistry
    from consensus_mcp.tools import consensus_run_iteration
    registry = ToolRegistry()
    consensus_run_iteration.register(registry)
    tool.register(registry)
    for entry in registry.list_tools():
        assert set(entry.keys()) >= {"name", "description", "inputSchema"}
        # Snake-case must not leak through.
        assert "input_schema" not in entry
        assert isinstance(entry["inputSchema"], dict)
