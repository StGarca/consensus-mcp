"""Tests for architect.approve_spec + architect.cleanup (consult Q5/Q7)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from consensus_mcp import _architect_paths as ap
from consensus_mcp.tools import architect_gates as gates


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    for args in (
        ["init", "-b", "main"], ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text(f"x {name}\n", encoding="utf-8")
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


def test_approve_spec_refuses_tampered_spec_seal(tmp_path: Path):
    # The spec seal must reproduce before approval binds spec_sha256: a body
    # edited after sealing (stale payload_sha256) would bind spec-approval
    # to a hash that does not match the on-disk content the human read.
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    sealed = yaml.safe_load(ap.spec_path(goal).read_text(encoding="utf-8"))
    tampered = dict(sealed, body="build something ELSE")
    ap.spec_path(goal).write_text(
        yaml.safe_dump(tampered, sort_keys=False), encoding="utf-8"
    )
    result = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo)
    )
    assert result["ok"] is False
    assert "seal" in result["error"]
    assert not (goal / ap.SPEC_APPROVAL_FILENAME).exists()


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


def test_approve_spec_binds_latest_spec_rev(tmp_path: Path):
    # Rev-binding is the point of the gate ('the architect owns spec
    # evolution between gates'): approval must seal the HIGHEST spec-rev-N,
    # never fall back to spec.yaml when revisions exist.
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    ap.seal_artifact(
        goal / "spec-rev-2.yaml", {"kind": "spec", "body": "build it, revised"}
    )
    result = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo)
    )
    assert result["ok"] is True
    approval = yaml.safe_load(
        (goal / ap.SPEC_APPROVAL_FILENAME).read_text(encoding="utf-8")
    )
    rev = yaml.safe_load(
        (goal / "spec-rev-2.yaml").read_text(encoding="utf-8")
    )
    base = yaml.safe_load(ap.spec_path(goal).read_text(encoding="utf-8"))
    assert approval["spec_file"] == "spec-rev-2.yaml"
    assert approval["spec_sha256"] == rev["payload_sha256"]
    assert approval["spec_sha256"] != base["payload_sha256"]


def test_approve_spec_ignores_git_dir_env(tmp_path: Path, monkeypatch):
    # GIT_DIR in the server env (e.g. a hook context) must never redirect
    # rev-parse to a different repository's HEAD: base_sha resolution goes
    # through the hardened lane git (_scrubbed_env pops GIT_DIR/GIT_WORK_TREE).
    repo = _make_repo(tmp_path)
    other = _make_repo(tmp_path, name="other")
    goal = _goal_with_spec(repo)
    repo_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    other_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=other, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert repo_head != other_head
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    result = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo)
    )
    assert result["ok"] is True
    assert result["base_sha"] == repo_head


def test_approve_spec_derives_root_from_goal_layout(tmp_path: Path):
    # repo_root omitted: the root comes from the VALIDATED inversion of the
    # <root>/.consensus/architect/<id> layout, not blind parent-hopping.
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    repo_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    result = gates.handle_approve_spec(goal_dir=str(goal), approver="operator")
    assert result["ok"] is True
    assert result["base_sha"] == repo_head


def test_approve_spec_refuses_misshaped_goal_dir_without_root(tmp_path: Path):
    # A goal dir NOT shaped <root>/.consensus/architect/<id> must fail loud
    # when repo_root is omitted - never seal the enclosing repo's HEAD.
    repo = _make_repo(tmp_path)
    goal = repo / "not-consensus" / "architect" / "g1"
    goal.mkdir(parents=True)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "build it"})
    result = gates.handle_approve_spec(goal_dir=str(goal), approver="operator")
    assert result["ok"] is False
    assert "repo root" in result["error"]
    assert not (goal / ap.SPEC_APPROVAL_FILENAME).exists()


def test_cleanup_prunes_lane_branch_and_unblocks_recreation(tmp_path: Path):
    # The schema promises 'prunes the lane worktree + branch': the branch is
    # what makes a goal-id collision sticky (create_lane refuses while it
    # exists), so cleanup must clear it or the collision advice is a dead end.
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
    branches = subprocess.run(
        ["git", "branch", "--list", "arch-lane/g1"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert branches == ""
    # The collision is actually resolved: the lane can be re-created.
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", head)
    assert lane.exists()


def test_cleanup_refuses_unknown_closing_state(tmp_path: Path):
    # Fail-closed allowlist (PRUNE_ELIGIBLE_CLOSING_STATES): a typo, casing
    # drift, or future state must refuse the DESTRUCTIVE prune, not permit it.
    from consensus_mcp import _architect_lane as lane_mod
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    lane_mod.create_lane(repo, goal, "arch-lane/g1", head)
    ap.seal_artifact(goal / ap.OUTCOME_FILENAME, {"closing_state": "Delivered"})
    result = gates.handle_cleanup(
        goal_dir=str(goal), repo_root=str(repo), prune_lane=True
    )
    assert result["ok"] is False
    assert "prune-eligible" in result["error"]
    assert ap.lane_dir(goal).exists()


def test_cleanup_refuses_tampered_outcome_seal(tmp_path: Path):
    # The outcome seal must reproduce before cleanup acts on closing_state;
    # a payload edited after sealing is refused, lane retained.
    from consensus_mcp import _architect_lane as lane_mod
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    lane_mod.create_lane(repo, goal, "arch-lane/g1", head)
    sealed = ap.seal_artifact(
        goal / ap.OUTCOME_FILENAME, {"closing_state": "killed"}
    )
    tampered = dict(sealed, closing_state="delivered")
    (goal / ap.OUTCOME_FILENAME).write_text(
        yaml.safe_dump(tampered, sort_keys=False), encoding="utf-8"
    )
    result = gates.handle_cleanup(
        goal_dir=str(goal), repo_root=str(repo), prune_lane=True
    )
    assert result["ok"] is False
    assert "seal" in result["error"]
    assert ap.lane_dir(goal).exists()


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


# ---- Q1 hardening (consult iteration-architect-hardening-2026-06-11) ----
# Re-approval is legal EXACTLY when the binding would fail: true duplicate
# refused; evolved spec archives the prior approval and re-binds; base_sha
# carried forward when a lane exists; refused while a dispatch is in flight.

from consensus_mcp import _architect_lane as lane_mod


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", name], cwd=repo, check=True,
                   capture_output=True)


def test_reapprove_evolved_spec_supersedes_and_rebinds(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    first = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))
    assert first["ok"]
    ap.seal_artifact(goal / "spec-rev-2.yaml",
                     {"kind": "spec", "body": "build it, revised"})
    second = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))
    assert second["ok"] is True
    archived = goal / "spec-approval-superseded-1.yaml"
    assert archived.exists()
    old = yaml.safe_load(archived.read_text(encoding="utf-8"))
    assert ap.seal_is_intact(old)
    assert old["spec_sha256"] == first["spec_sha256"]
    fresh = yaml.safe_load(
        (goal / ap.SPEC_APPROVAL_FILENAME).read_text(encoding="utf-8"))
    rev = yaml.safe_load((goal / "spec-rev-2.yaml").read_text(encoding="utf-8"))
    assert fresh["spec_sha256"] == rev["payload_sha256"]


def test_reapprove_carries_base_sha_when_lane_exists(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    first = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))
    lane_mod.create_lane(repo, goal, "arch-lane/g1", first["base_sha"])
    _commit(repo, "advance.txt")   # main HEAD moves past the approved base
    ap.seal_artifact(goal / "spec-rev-2.yaml",
                     {"kind": "spec", "body": "revised"})
    second = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))
    assert second["ok"] is True
    # lane exists: carry forward - re-approval must NOT un-stick the
    # head-moved stop rule through a side door
    assert second["base_sha"] == first["base_sha"]


def test_reapprove_fresh_head_when_no_lane(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    first = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))
    _commit(repo, "advance.txt")
    ap.seal_artifact(goal / "spec-rev-2.yaml",
                     {"kind": "spec", "body": "revised"})
    second = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))
    assert second["ok"] is True
    assert second["base_sha"] != first["base_sha"]


def test_approve_refused_while_dispatch_in_flight(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    assert gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))["ok"]
    ap.seal_artifact(goal / "spec-rev-2.yaml",
                     {"kind": "spec", "body": "revised"})
    ap.acquire_lock_artifact(goal / ap.IN_FLIGHT_FILENAME,
                             {"role": "builder", "cycle": 1})
    result = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))
    assert result["ok"] is False
    assert "in flight" in result["error"]


def test_reapprove_refused_when_existing_approval_tampered(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = _goal_with_spec(repo)
    assert gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))["ok"]
    path = goal / ap.SPEC_APPROVAL_FILENAME
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["approver"] = "evil"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    ap.seal_artifact(goal / "spec-rev-2.yaml",
                     {"kind": "spec", "body": "revised"})
    result = gates.handle_approve_spec(
        goal_dir=str(goal), approver="operator", repo_root=str(repo))
    assert result["ok"] is False
    assert "tamper" in result["error"]
