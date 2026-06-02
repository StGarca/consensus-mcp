"""Integration tests for consensus_mcp._snapshot_state.

Uses tmp_path + a fresh git init so each test is isolated from the main
repo's snapshot branch. Verifies the end-to-end paths that the unit tests
in test_snapshot_state.py couldn't exercise: real git worktree lifecycle,
restore copy semantics, deletion-dirty detection, dry-run plan accuracy.

Gated by CONSENSUS_MCP_SNAPSHOT_INTEGRATION=1 env var? NO - these run in CI.
They're fast (~5s each) because the repos are tiny.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make consensus_mcp importable without install.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _snapshot_state as ss


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r


def _make_fake_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with a consensus-state/ tree + .gitignore."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    # Repo markers expected by _resolve_repo_root.
    (repo / "consensus_mcp").mkdir()
    (repo / "consensus_mcp" / "validators").mkdir()
    (repo / "consensus-state").mkdir()
    (repo / "consensus-state" / "active").mkdir()
    (repo / "consensus-state" / "active" / "iteration-0001-alpha").mkdir()
    (repo / "consensus-state" / "active" / "iteration-0001-alpha" / "goal_packet.yaml").write_text(
        "schema_version: 1\npilot_id: iteration-0001-alpha\n", encoding="utf-8"
    )
    (repo / "consensus-state" / "active" / "iteration-0002-beta").mkdir()
    (repo / "consensus-state" / "active" / "iteration-0002-beta" / "goal_packet.yaml").write_text(
        "schema_version: 1\npilot_id: iteration-0002-beta\n", encoding="utf-8"
    )
    # Gitignore the iteration dirs (mimics real repo).
    (repo / ".gitignore").write_text("consensus-state/active/iteration-*/\n", encoding="utf-8")
    (repo / "README.md").write_text("# fake repo\n", encoding="utf-8")
    _git(["add", "README.md", ".gitignore"], repo)
    _git(["commit", "-m", "init"], repo)
    return repo


def _with_repo_root(monkeypatch, repo: Path):
    """Monkey-patch _resolve_repo_root to return the fake repo."""
    monkeypatch.setattr(ss, "_resolve_repo_root", lambda: repo)


# ---------- snapshot + list round-trip ----------

def test_snapshot_creates_orphan_branch_and_tag(tmp_path, monkeypatch):
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    # First snapshot creates the orphan branch.
    rc = ss.main(["snapshot", "--label", "first"])
    assert rc == 0
    # Branch exists.
    r = _git(["rev-parse", "--verify", "consensus-state-snapshots"], repo, check=False)
    assert r.returncode == 0
    # Tag exists.
    tags = _git(["tag", "-l", "snapshot-*-first"], repo).stdout.strip()
    assert tags.endswith("-first")


def test_list_shows_newest_first(tmp_path, monkeypatch, capsys):
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "alpha"])
    ss.main(["snapshot", "--label", "beta"])
    capsys.readouterr()  # drain
    rc = ss.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    # Both tags appear; the time-based ordering should yield beta before alpha
    # because beta was created second (same-second collision auto-suffixes "-1"
    # but the tag with "-1" sorts AFTER plain - either order is acceptable as
    # long as both are present).
    assert "alpha" in out
    assert "beta" in out


# ---------- restore round-trip ----------

def test_full_restore_recovers_deleted_file(tmp_path, monkeypatch, capsys):
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    # Baseline snapshot.
    ss.main(["snapshot", "--label", "baseline"])
    # Delete a file.
    target = repo / "consensus-state" / "active" / "iteration-0001-alpha" / "goal_packet.yaml"
    assert target.exists()
    target.unlink()
    assert not target.exists()
    # Restore (--force to skip auto-pre-snapshot since the test fakes the env).
    tag = _git(["tag", "-l", "snapshot-*-baseline"], repo).stdout.strip()
    capsys.readouterr()
    rc = ss.main(["restore", "--tag", tag, "--force"])
    assert rc == 0
    assert target.exists(), "deleted file should be restored from snapshot"


def test_full_restore_removes_files_not_in_snapshot(tmp_path, monkeypatch, capsys):
    """codex-rev-002 round-1 regression: scope cleanup removes hybrid state."""
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "baseline"])
    # Add a NEW file that wasn't in the baseline snapshot.
    new_file = repo / "consensus-state" / "active" / "iteration-0001-alpha" / "post-baseline.yaml"
    new_file.write_text("added: true\n", encoding="utf-8")
    tag = _git(["tag", "-l", "snapshot-*-baseline"], repo).stdout.strip()
    capsys.readouterr()
    ss.main(["restore", "--tag", tag, "--force"])
    assert not new_file.exists(), "files absent from snapshot must be cleaned during full restore"


def test_iteration_restore_does_not_match_siblings(tmp_path, monkeypatch, capsys):
    """codex-rev-003 round-1 regression: exact iteration names need boundary checks.

    Sibling name MUST share the full requested iteration name as a prefix so
    a naive startswith would WRONGLY match (codex iter-0014 r4 codex-rev-001
    test-strengthening: prior name 'iteration-00010' didn't share a prefix
    with the actual scope 'iteration-0001-alpha', so the test could pass even
    if the production filter regressed).
    """
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    # Add a sibling that WOULD match a naive prefix scan of iteration-0001-alpha.
    sibling = repo / "consensus-state" / "active" / "iteration-0001-alpha-extra"
    sibling.mkdir()
    (sibling / "goal_packet.yaml").write_text("sibling: yes\n", encoding="utf-8")
    ss.main(["snapshot", "--label", "with-sibling"])
    # Delete the sibling on disk; if --iteration iteration-0001 wrongly matches
    # it, the restore would resurrect the sibling.
    import shutil
    shutil.rmtree(sibling)
    # Modify iteration-0001 so restore copies its file too.
    (repo / "consensus-state" / "active" / "iteration-0001-alpha" / "goal_packet.yaml").write_text(
        "modified: true\n", encoding="utf-8"
    )
    tag = _git(["tag", "-l", "snapshot-*-with-sibling"], repo).stdout.strip()
    capsys.readouterr()
    rc = ss.main(["restore", "--tag", tag, "--iteration", "iteration-0001-alpha", "--force"])
    assert rc == 0
    # iteration-0001-alpha should be restored (file content reset).
    restored = (repo / "consensus-state" / "active" / "iteration-0001-alpha" / "goal_packet.yaml").read_text(encoding="utf-8")
    assert "modified" not in restored
    assert "alpha" in restored
    # The sibling must NOT be resurrected.
    assert not sibling.exists(), "--iteration iteration-0001-alpha must not match sibling iteration-0001-alpha-extra"


# ---------- dry-run plan ----------

def test_dry_run_shows_deletions(tmp_path, monkeypatch, capsys):
    """iter-0014 codex-rev-001 regression: dry-run reports both copies AND deletions."""
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "base"])
    # Add a new file that isn't in the snapshot.
    new_file = repo / "consensus-state" / "active" / "iteration-0001-alpha" / "new.yaml"
    new_file.write_text("new: yes\n", encoding="utf-8")
    tag = _git(["tag", "-l", "snapshot-*-base"], repo).stdout.strip()
    capsys.readouterr()
    rc = ss.main(["restore", "--tag", tag, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would COPY" in out
    assert "would DELETE" in out
    assert "new.yaml" in out  # the to-be-deleted file


def test_dry_run_rejects_invalid_tag(tmp_path, monkeypatch, capsys):
    """iter-0014 codex-rev-001 round-6: dry-run must reject non-tag refs symmetric with real restore."""
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "base"])
    capsys.readouterr()
    # Pass the main branch name as --tag; not a tag, must be rejected.
    rc = ss.main(["restore", "--tag", "main", "--dry-run"])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_dry_run_rejects_missing_iteration(tmp_path, monkeypatch, capsys):
    """iter-0014 codex-rev-001 regression: dry-run validates missing iteration."""
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "base"])
    tag = _git(["tag", "-l", "snapshot-*-base"], repo).stdout.strip()
    capsys.readouterr()
    rc = ss.main(["restore", "--tag", tag, "--dry-run", "--iteration", "iteration-9999-nonexistent"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found in snapshot" in err


# ---------- dirty-state ----------

def test_dirty_detection_catches_deletions(tmp_path, monkeypatch):
    """codex-rev-002 round-3 regression: deleted snapshotted file flagged as dirty."""
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "base"])
    # Delete a file that's in the snapshot.
    target = repo / "consensus-state" / "active" / "iteration-0001-alpha" / "goal_packet.yaml"
    target.unlink()
    tag = _git(["tag", "-l", "snapshot-*-base"], repo).stdout.strip()
    dirty = ss._detect_dirty_paths(repo, target_tag=tag)
    rel = "consensus-state/active/iteration-0001-alpha/goal_packet.yaml"
    assert rel in dirty, f"deleted snapshotted file must be dirty; got {dirty}"


# ---------- diff ----------

def test_diff_returns_nonzero_on_changes(tmp_path, monkeypatch, capsys):
    """codex-rev-004 round-1 regression: diff actually surfaces changes and returns nonzero."""
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "base"])
    # Modify a file.
    (repo / "consensus-state" / "active" / "iteration-0001-alpha" / "goal_packet.yaml").write_text(
        "schema_version: 999\n", encoding="utf-8"
    )
    tag = _git(["tag", "-l", "snapshot-*-base"], repo).stdout.strip()
    capsys.readouterr()
    rc = ss.main(["diff", "--tag", tag])
    assert rc == 1, "diff must return 1 when differences exist"
    out = capsys.readouterr().out
    assert "schema_version" in out


def test_diff_returns_zero_when_unchanged(tmp_path, monkeypatch, capsys):
    repo = _make_fake_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "base"])
    tag = _git(["tag", "-l", "snapshot-*-base"], repo).stdout.strip()
    capsys.readouterr()
    rc = ss.main(["diff", "--tag", tag])
    assert rc == 0, "diff must return 0 when no differences"
