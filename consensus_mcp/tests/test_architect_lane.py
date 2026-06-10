"""Tests for _architect_lane: worktree lifecycle + containment (consult Q1)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from consensus_mcp import _architect_lane as lane_mod
from consensus_mcp import _architect_paths as ap


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True
        )
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "init")
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def test_create_lane_makes_worktree_on_branch(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    base = _head(repo)
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", base)
    assert lane == ap.lane_dir(goal)
    assert (lane / "README.md").exists()
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=lane,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "arch-lane/g1"


def test_create_lane_is_idempotent(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    base = _head(repo)
    lane1 = lane_mod.create_lane(repo, goal, "arch-lane/g1", base)
    lane2 = lane_mod.create_lane(repo, goal, "arch-lane/g1", base)
    assert lane1 == lane2


def test_create_lane_rejects_branch_collision(tmp_path: Path):
    repo = _make_repo(tmp_path)
    base = _head(repo)
    subprocess.run(
        ["git", "branch", "arch-lane/g1"], cwd=repo, check=True,
        capture_output=True,
    )
    goal = ap.goal_dir(repo, "g1")
    with pytest.raises(lane_mod.LaneError, match="exists"):
        lane_mod.create_lane(repo, goal, "arch-lane/g1", base)


def test_commit_lane_returns_sha_and_keeps_main_clean(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    (lane / "new.py").write_text("x = 1\n", encoding="utf-8")
    sha = lane_mod.commit_lane(repo, lane, "builder cycle 1")
    assert len(sha) == 40
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    # main working tree untouched (the goal dir itself is expected dirt -
    # it must be gitignored by setup; here repo has no ignore so filter it)
    assert "new.py" not in status


def test_commit_lane_empty_diff_returns_head(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    sha1 = lane_mod.commit_lane(repo, lane, "noop")
    sha2 = lane_mod.commit_lane(repo, lane, "noop again")
    assert sha1 == sha2


def test_scan_lane_integrity_flags_symlink(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    try:
        (lane / "escape").symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    violations = lane_mod.scan_lane_integrity(lane)
    assert any("symlink" in v for v in violations)


def test_scan_lane_integrity_flags_outside_hardlink(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    outside = tmp_path / "secret.txt"
    outside.write_text("s\n", encoding="utf-8")
    try:
        os.link(outside, lane / "linked.txt")
    except OSError:
        pytest.skip("hardlinks unsupported on this filesystem")
    violations = lane_mod.scan_lane_integrity(lane)
    assert any("hardlink" in v for v in violations)


def test_scan_lane_integrity_allows_intra_lane_hardlink(tmp_path: Path):
    # A hardlink pair living ENTIRELY inside the lane is not an escape:
    # st_nlink equals the number of lane paths sharing the inode.
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    inside = lane / "a.txt"
    inside.write_text("a\n", encoding="utf-8")
    try:
        os.link(inside, lane / "b.txt")
    except OSError:
        pytest.skip("hardlinks unsupported on this filesystem")
    assert lane_mod.scan_lane_integrity(lane) == []


def test_clean_lane_scan_is_empty(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    (lane / "ok.py").write_text("y = 2\n", encoding="utf-8")
    assert lane_mod.scan_lane_integrity(lane) == []


def test_integrity_snapshot_detects_main_mutation(tmp_path: Path):
    repo = _make_repo(tmp_path)
    before = lane_mod.snapshot_main_integrity(repo)
    assert lane_mod.check_main_integrity(repo, before) == []
    (repo / "README.md").write_text("mutated\n", encoding="utf-8")
    violations = lane_mod.check_main_integrity(repo, before)
    assert any("working tree" in v for v in violations)


def test_integrity_snapshot_detects_file_in_untracked_dir(tmp_path: Path):
    # Plain `git status --porcelain` collapses an untracked dir to one
    # `?? dir/` line, so a NEW file inside a pre-existing untracked dir
    # would produce zero delta; --untracked-files=all itemizes paths.
    repo = _make_repo(tmp_path)
    (repo / "scratch").mkdir()
    (repo / "scratch" / "a.txt").write_text("a\n", encoding="utf-8")
    before = lane_mod.snapshot_main_integrity(repo)
    assert lane_mod.check_main_integrity(repo, before) == []
    (repo / "scratch" / "b.txt").write_text("b\n", encoding="utf-8")
    violations = lane_mod.check_main_integrity(repo, before)
    assert any("working tree" in v for v in violations)


def test_integrity_snapshot_detects_ref_change(tmp_path: Path):
    repo = _make_repo(tmp_path)
    before = lane_mod.snapshot_main_integrity(repo)
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "advance"], cwd=repo, check=True,
        capture_output=True,
    )
    violations = lane_mod.check_main_integrity(repo, before)
    assert any("ref" in v.lower() or "HEAD" in v for v in violations)


def test_check_main_integrity_lane_branch_carveout(tmp_path: Path):
    # The lane worktree shares the main ref store, so commit_lane moves
    # refs/heads/<lane-branch>: with the carve-out that EXPECTED delta is
    # clean; without it the same cycle reports a ref violation.
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    before = lane_mod.snapshot_main_integrity(repo)
    (lane / "new.py").write_text("x = 1\n", encoding="utf-8")
    lane_mod.commit_lane(repo, lane, "builder cycle 1")
    assert lane_mod.check_main_integrity(
        repo, before, lane_branch="arch-lane/g1"
    ) == []
    violations = lane_mod.check_main_integrity(repo, before)
    assert any("ref changed: refs/heads/arch-lane/g1" in v for v in violations)


def test_check_main_integrity_detects_hooks_and_config_change(tmp_path: Path):
    repo = _make_repo(tmp_path)
    before = lane_mod.snapshot_main_integrity(repo)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    (hooks_dir / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "core.fileMode", "false"], cwd=repo, check=True,
        capture_output=True,
    )
    violations = lane_mod.check_main_integrity(repo, before)
    assert any("hooks changed" in v for v in violations)
    assert any("repo config changed" in v for v in violations)


def test_lane_diff_shows_builder_change(tmp_path: Path):
    repo = _make_repo(tmp_path)
    base = _head(repo)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", base)
    (lane / "new.py").write_text("x = 1\n", encoding="utf-8")
    lane_mod.commit_lane(repo, lane, "c1")
    diff = lane_mod.lane_diff(repo, lane, base)
    assert "new.py" in diff and "+x = 1" in diff


def test_remove_lane(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    lane_mod.remove_lane(repo, goal)
    assert not lane.exists()
