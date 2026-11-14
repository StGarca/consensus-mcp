"""Unit tests for consensus_mcp._snapshot_state.

Focused on label sanitization + tag construction. Full git-worktree integration
tests are heavier and gated behind an env var (CONSENSUS_MCP_SNAPSHOT_INTEGRATION=1)
in a separate test file. Here we verify the deterministic logic.
"""
from __future__ import annotations

import pytest

from consensus_mcp import _snapshot_state as ss


def test_iso_utc_now_format():
    iso = ss._iso_utc_now()
    assert iso.endswith("Z")
    assert "T" in iso
    # Format: YYYY-MM-DDTHHMMSSZ (no colons)
    assert ":" not in iso
    # 4+1+2+1+2 + T + 6 + Z = 18 chars
    assert len(iso) == 18


def test_sanitize_label_accepts_valid():
    assert ss._sanitize_label("hello-world_42") == "hello-world_42"
    assert ss._sanitize_label("a") == "a"
    assert ss._sanitize_label("a" * 64) == "a" * 64


def test_sanitize_label_rejects_invalid():
    with pytest.raises(ss.SnapshotError):
        ss._sanitize_label("has spaces")
    with pytest.raises(ss.SnapshotError):
        ss._sanitize_label("has.dots")
    with pytest.raises(ss.SnapshotError):
        ss._sanitize_label("has/slashes")
    with pytest.raises(ss.SnapshotError):
        ss._sanitize_label("a" * 65)
    with pytest.raises(ss.SnapshotError):
        ss._sanitize_label("emoji-" + chr(0x1F680) + "-no")


def test_sanitize_label_empty_returns_none():
    assert ss._sanitize_label("") is None
    assert ss._sanitize_label(None) is None


def test_build_tag_no_label():
    tag = ss._build_tag(None)
    assert tag.startswith("snapshot-")
    assert "-" in tag
    # Should be: snapshot-YYYY-MM-DDTHHMMSSZ (one suffix segment, no extra hyphens)
    parts = tag.split("-")
    # snapshot, YYYY, MM, DDTHHMMSSZ
    assert len(parts) == 4


def test_build_tag_with_label():
    tag = ss._build_tag("pre-restore-x")
    assert tag.startswith("snapshot-")
    assert tag.endswith("-pre-restore-x")


def test_build_tag_invalid_label_raises():
    with pytest.raises(ss.SnapshotError):
        ss._build_tag("invalid label with spaces")


def test_label_pattern_constant():
    """Pin the regex per iter-0012 F9c."""
    assert ss.LABEL_PATTERN.pattern == r"^[A-Za-z0-9_-]{1,64}$"


def test_snapshot_branch_constant():
    """Pin the branch name per iter-0012 design."""
    assert ss.SNAPSHOT_BRANCH == "consensus-state-snapshots"


def test_snapshotted_paths_includes_consensus_state():
    """Pin that consensus-state is the snapshotted tree (per iter-0012)."""
    assert "consensus-state" in ss.SNAPSHOTTED_PATHS


def test_next_unique_tag_returns_candidate_when_free(monkeypatch):
    """codex-rev-001 round-2 fix: no collision, return candidate unchanged."""
    calls = []
    def fake_run(args, cwd=None, check=True):
        calls.append(args)
        class R: returncode = 1; stdout = ""; stderr = ""  # tag doesn't exist
        return R()
    monkeypatch.setattr(ss, "_run_git", fake_run)
    from pathlib import Path as _P
    out = ss._next_unique_tag(_P("/fake"), "snapshot-X")
    assert out == "snapshot-X"


def test_next_unique_tag_appends_suffix_on_collision(monkeypatch):
    """codex-rev-001 round-2 fix: same-second collision -> -1, -2, ..."""
    seen = []
    def fake_run(args, cwd=None, check=True):
        # args: ["rev-parse", "--verify", "--quiet", "refs/tags/<tag>"]
        tag = args[-1].split("/")[-1]
        seen.append(tag)
        class R:
            returncode = 0 if tag in {"snapshot-X", "snapshot-X-1"} else 1
            stdout = ""; stderr = ""
        return R()
    monkeypatch.setattr(ss, "_run_git", fake_run)
    from pathlib import Path as _P
    out = ss._next_unique_tag(_P("/fake"), "snapshot-X")
    assert out == "snapshot-X-2"
    assert seen == ["snapshot-X", "snapshot-X-1", "snapshot-X-2"]


def test_validate_iteration_name_accepts_real_names():
    assert ss._validate_iteration_name("iteration-0001") == "iteration-0001"
    assert ss._validate_iteration_name("iteration-0042-some-slug") == "iteration-0042-some-slug"
    assert ss._validate_iteration_name("iteration-audit-2026-05-11-bare-except") == "iteration-audit-2026-05-11-bare-except"


def test_validate_iteration_name_rejects_path_traversal():
    """codex-rev-001 round-3 critical: --iteration ../.. must be refused."""
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name("..")
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name("../..")
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name("../archive")
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name("iter/../escape")


def test_validate_iteration_name_rejects_separators():
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name("with/slash")
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name("with\\backslash")
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name("/absolute")


def test_validate_iteration_name_rejects_empty_and_leading_dot():
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name("")
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name(".hidden")
    with pytest.raises(ss.SnapshotError):
        ss._validate_iteration_name(".")


def test_path_matches_subtree_exact():
    """codex-rev-003 regression: --iteration must not match siblings via prefix."""
    sub = "consensus-state/active/iteration-0001"
    # Exact match: OK
    assert ss._path_matches_subtree("consensus-state/active/iteration-0001", sub)
    # Child path: OK
    assert ss._path_matches_subtree("consensus-state/active/iteration-0001/goal_packet.yaml", sub)
    # Sibling with longer numeric suffix: REJECT (the bug we're guarding against)
    assert not ss._path_matches_subtree("consensus-state/active/iteration-00010", sub)
    # Sibling with hyphen suffix: REJECT
    assert not ss._path_matches_subtree("consensus-state/active/iteration-0001-foo", sub)
    # Unrelated path: REJECT
    assert not ss._path_matches_subtree("consensus-state/archive/imported-from-parent", sub)
