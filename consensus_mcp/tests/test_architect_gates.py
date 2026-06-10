"""Tests for architect.approve_spec + architect.cleanup (consult Q5/Q7)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from consensus_mcp import _architect_paths as ap
from consensus_mcp.tools import architect_gates as gates


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"], ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    return repo


def _goal_with_spec(repo: Path) -> Path:
    goal = ap.goal_dir(repo, "g1")
    goal.mkdir(parents=True)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "build it"})
    return goal


def test_approve_spec_seals_with_sha_and_base(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    result = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo)
    )
    assert result["ok"] is True
    approval = yaml.safe_load(
        (goal / ap.SPEC_APPROVAL_FILENAME).read_text(encoding="utf-8")
    )
    spec = yaml.safe_load(ap.spec_path(goal).read_text(encoding="utf-8"))
    assert approval["spec_sha256"] == spec["payload_sha256"]
    assert len(approval["base_sha"]) == 40
    assert approval["approver"] == "operator"


def test_approve_spec_refuses_missing_spec(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    goal.mkdir(parents=True)
    result = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo)
    )
    assert result["ok"] is False
    assert "spec" in result["error"]


def test_approve_spec_refuses_double_approval(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    assert gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo)
    )["ok"]
    second = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo)
    )
    assert second["ok"] is False
    assert "already" in second["error"]


def test_cleanup_refuses_open_goal(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    result = gates.handle_cleanup(goal_dir=str(goal), repo_root=str(repo))
    assert result["ok"] is False
    assert "outcome" in result["error"]


def test_cleanup_prunes_closed_goal_lane(tmp_path: Path):
    from consensus_mcp import _architect_lane as lane_mod
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    lane_mod.create_lane(repo, goal, "arch-lane/g1", head)
    ap.seal_artifact(goal / ap.OUTCOME_FILENAME, {"closing_state": "delivered"})
    result = gates.handle_cleanup(
        goal_dir=str(goal), repo_root=str(repo), prune_lane=True
    )
    assert result["ok"] is True
    assert not ap.lane_dir(goal).exists()


def test_cleanup_retains_lane_on_killed(tmp_path: Path):
    from consensus_mcp import _architect_lane as lane_mod
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    lane_mod.create_lane(repo, goal, "arch-lane/g1", head)
    ap.seal_artifact(goal / ap.OUTCOME_FILENAME, {"closing_state": "killed"})
    result = gates.handle_cleanup(
        goal_dir=str(goal), repo_root=str(repo), prune_lane=True
    )
    assert result["ok"] is False
    assert "killed" in result["error"]
    assert ap.lane_dir(goal).exists()
