"""Tests for _architect_lane: worktree lifecycle + containment (consult Q1)."""
from __future__ import annotations

import os
import stat
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


def _rewrite_lane_git_pointer(lane: Path, content: str) -> None:
    """Replace a worktree's .git pointer file cross-platform.

    Git for Windows creates the linked-worktree .git pointer file read-only,
    so a plain write_text() onto it raises PermissionError [Errno 13]
    (Windows reports a read-only-file write as EACCES). Clear the attribute
    and unlink before writing; harmless on POSIX."""
    pointer = lane / ".git"
    try:
        os.chmod(pointer, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    try:
        pointer.unlink()
    except OSError:
        pass
    pointer.write_text(content, encoding="utf-8")


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


def test_remove_lane_refuses_symlinked_goal_or_lane(tmp_path: Path):
    # remove_lane is the one DESTRUCTIVE lane op: a goal/lane that is itself
    # a symlink would land 'worktree remove --force' + 'branch -D' on
    # whatever registered worktree it points at (the symlink-cousin-at-a-
    # destructive-site class).
    repo = _make_repo(tmp_path)
    victim_goal = ap.goal_dir(repo, "victim")
    victim_lane = lane_mod.create_lane(
        repo, victim_goal, "arch-lane/victim", _head(repo)
    )
    lane_goal = ap.goal_dir(repo, "g1")
    lane_goal.mkdir(parents=True)
    try:
        ap.lane_dir(lane_goal).symlink_to(victim_lane, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    with pytest.raises(lane_mod.LaneError, match="symlink"):
        lane_mod.remove_lane(repo, lane_goal)
    goal_goal = ap.goal_dir(repo, "g2")
    goal_goal.symlink_to(victim_goal, target_is_directory=True)
    with pytest.raises(lane_mod.LaneError, match="symlink"):
        lane_mod.remove_lane(repo, goal_goal)
    assert victim_lane.exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "arch-lane/victim"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert branches != ""


def test_remove_lane_refuses_lane_resolving_outside_architect_root(tmp_path: Path):
    # A symlinked path COMPONENT (here: the architect dir itself) lands the
    # resolved lane outside repo_root's architect root; the prune must anchor
    # on the RESOLVED path, never the unresolved argument handed to git.
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    (outside / "g1" / ap.LANE_DIRNAME).mkdir(parents=True)
    architect_parent = repo / ap.GOAL_ROOT_PARTS[0]
    architect_parent.mkdir()
    try:
        (architect_parent / ap.GOAL_ROOT_PARTS[1]).symlink_to(
            outside, target_is_directory=True
        )
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    goal = repo.joinpath(*ap.GOAL_ROOT_PARTS, "g1")
    with pytest.raises(lane_mod.LaneError, match="architect root"):
        lane_mod.remove_lane(repo, goal)
    assert (outside / "g1" / ap.LANE_DIRNAME).exists()


def test_remove_lane_path_only_check_keeps_tampered_lane_removable(tmp_path: Path):
    # By DESIGN the removal anchor is path-only (lstat + resolve), NOT the
    # full .git pointer check: a tampered-but-delivered lane must STAY
    # removable or cleanup of a closed goal would deadlock. A symlinked
    # pointer with intact content is exactly the tamper the full check
    # refuses but git itself removes.
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    pointer = lane / ".git"
    stash = goal / "stash-git-pointer"
    stash.write_text(pointer.read_text(encoding="utf-8"), encoding="utf-8")
    pointer.unlink()
    try:
        pointer.symlink_to(stash)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    with pytest.raises(lane_mod.LaneError, match="git pointer"):
        lane_mod._require_lane_contained(repo, lane)
    lane_mod.remove_lane(repo, goal)
    assert not lane.exists()


def test_scan_lane_integrity_flags_symlinked_git_pointer(tmp_path: Path):
    # The .git pointer is the ONE path that redirects all supervisor-owned
    # (L3) git; it gets no scan exemption (design spec: any symlink in the
    # lane fires lane_integrity_violation).
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    pointer = lane / ".git"
    stash = goal / "stash-git-pointer"
    stash.write_text(pointer.read_text(encoding="utf-8"), encoding="utf-8")
    pointer.unlink()
    try:
        pointer.symlink_to(stash)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    violations = lane_mod.scan_lane_integrity(lane)
    assert any(".git pointer" in v for v in violations)


def test_scan_lane_integrity_flags_rewritten_gitdir_line(tmp_path: Path):
    # A rewritten gitdir: line redirects commit_lane's add/commit to an
    # attacker-chosen repo. Mimic the legitimate .../worktrees/<name> shape
    # so a format-only check would pass; containment must anchor on the
    # REAL main gitdir.
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    evil = tmp_path / "evil"
    subprocess.run(
        ["git", "init", "-b", "main", str(evil)], check=True, capture_output=True
    )
    fake = evil / ".git" / "worktrees" / "lane"
    fake.mkdir(parents=True)
    _rewrite_lane_git_pointer(lane, f"gitdir: {fake}\n")
    violations = lane_mod.scan_lane_integrity(lane)
    assert any(".git pointer" in v for v in violations)


class _ReparseStat:
    """Wrap a real lstat result, adding the Windows reparse attribute."""

    def __init__(self, real):
        self._real = real
        self.st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_scan_lane_integrity_flags_reparse_point_and_prunes(
    tmp_path: Path, monkeypatch
):
    # NTFS junctions are reparse points that Path.is_symlink() misses and
    # rglob descends through; the scan must flag them AND not descend into
    # them (simulated via lstat - the scan's only OS-specific input).
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    junction = lane / "junction"
    junction.mkdir()
    (junction / "inside-the-junction.txt").write_text("m\n", encoding="utf-8")
    junction_resolved = junction.resolve()
    real_lstat = os.lstat
    seen: list[str] = []

    def fake_lstat(path, *args, **kwargs):
        seen.append(os.fspath(path))
        st = real_lstat(path, *args, **kwargs)
        if Path(os.fspath(path)) == junction_resolved:
            return _ReparseStat(st)
        return st

    monkeypatch.setattr(lane_mod.os, "lstat", fake_lstat)
    violations = lane_mod.scan_lane_integrity(lane)
    assert any("reparse" in v or "junction" in v for v in violations)
    # pruned, not descended: the junction's contents are never scanned
    assert not any(s.endswith("inside-the-junction.txt") for s in seen)


def test_commit_lane_and_lane_diff_reject_foreign_lane(tmp_path: Path):
    # repo_root anchors containment: a lane that does not resolve under
    # repo_root's architect root is refused BEFORE any git op runs.
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    other = tmp_path / "other-root"
    other.mkdir()
    with pytest.raises(lane_mod.LaneError, match="architect root"):
        lane_mod.commit_lane(other, lane, "msg")
    with pytest.raises(lane_mod.LaneError, match="architect root"):
        lane_mod.lane_diff(other, lane, "HEAD")


def test_commit_lane_and_lane_diff_refuse_redirected_git_pointer(tmp_path: Path):
    # Even if a scan were skipped, supervisor git must never run against an
    # attacker-chosen gitdir.
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    evil = tmp_path / "evil"
    subprocess.run(
        ["git", "init", "-b", "main", str(evil)], check=True, capture_output=True
    )
    _rewrite_lane_git_pointer(lane, f"gitdir: {evil / '.git'}\n")
    with pytest.raises(lane_mod.LaneError, match="git pointer"):
        lane_mod.commit_lane(repo, lane, "msg")
    with pytest.raises(lane_mod.LaneError, match="git pointer"):
        lane_mod.lane_diff(repo, lane, "HEAD")


def test_create_lane_resume_rejects_tampered_git_pointer(tmp_path: Path):
    # Resume-path containment (quality review): an existing lane whose .git
    # pointer was rewritten must be refused BEFORE any supervisor git op
    # runs against it.
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    evil = tmp_path / "evil-gitdir"
    evil.mkdir()
    _rewrite_lane_git_pointer(lane, f"gitdir: {evil}\n")
    with pytest.raises(lane_mod.LaneError):
        lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))


def test_integrity_filter_is_root_anchored(tmp_path: Path):
    # A write under vendor/.../.consensus/architect/ in the MAIN tree must
    # remain visible to the L5 detector (the old substring filter hid it).
    repo = _make_repo(tmp_path)
    before = lane_mod.snapshot_main_integrity(repo)
    nested = repo / "vendor" / "foo" / ".consensus" / "architect"
    nested.mkdir(parents=True)
    (nested / "evil.sh").write_text("boom\n", encoding="utf-8")
    violations = lane_mod.check_main_integrity(repo, before)
    assert any("working tree" in v for v in violations)


def test_integrity_filter_still_excludes_goal_dir(tmp_path: Path):
    repo = _make_repo(tmp_path)
    before = lane_mod.snapshot_main_integrity(repo)
    goal = ap.goal_dir(repo, "g1")
    goal.mkdir(parents=True)
    (goal / "spec.yaml").write_text("kind: spec\n", encoding="utf-8")
    assert lane_mod.check_main_integrity(repo, before) == []


def test_architect_tree_catches_sibling_goal_and_root_writes(tmp_path: Path):
    # Decisive-experiment regression (no real codex needed): a write into a
    # SIBLING goal's dir or the architect root - which snapshot_main_integrity
    # blanket-excludes and snapshot_goal_artifacts misses - must be caught by
    # the architect-tree guard. Only the active lane is exempt.
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    before = lane_mod.snapshot_architect_tree(repo, lane)
    # legitimate lane write: exempt
    (lane / "work.py").write_text("x = 1\n", encoding="utf-8")
    # escapes the experiment proved possible:
    (goal / "escape1.txt").write_text("e1\n", encoding="utf-8")          # goal dir
    sibling = ap.goal_dir(repo, "g2"); sibling.mkdir(parents=True)
    (sibling / "stolen.yaml").write_text("forged\n", encoding="utf-8")   # sibling goal
    (goal.parent / "escape2.txt").write_text("e2\n", encoding="utf-8")   # architect root
    violations = lane_mod.check_architect_tree(repo, before, lane)
    assert any("escape1.txt" in v for v in violations)
    assert any("stolen.yaml" in v for v in violations)
    assert any("escape2.txt" in v for v in violations)
    assert not any("work.py" in v for v in violations)  # lane is exempt


def test_architect_tree_clean_window_is_empty(tmp_path: Path):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    lane = lane_mod.create_lane(repo, goal, "arch-lane/g1", _head(repo))
    before = lane_mod.snapshot_architect_tree(repo, lane)
    (lane / "only-lane.py").write_text("y = 2\n", encoding="utf-8")
    assert lane_mod.check_architect_tree(repo, before, lane) == []
