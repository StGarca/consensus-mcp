from pathlib import Path

import consensus_mcp._init_wizard as wiz


def _make_repo(d: Path):
    """A directory that looks like a git repo (has a .git DIRECTORY)."""
    d.mkdir(parents=True, exist_ok=True)
    (d / ".git").mkdir()
    return d


def test_token_constant_value():
    assert wiz.WORKSPACE_UMBRELLA_TOKEN == "STATUS: looks-like-workspace-umbrella"
    assert wiz.WORKSPACE_UMBRELLA_TOKEN != wiz.ALREADY_CONFIGURED_TOKEN


def test_umbrella_with_child_repos_detected(tmp_path):
    _make_repo(tmp_path / "proj-a")
    _make_repo(tmp_path / "proj-b")
    (tmp_path / "not-a-repo").mkdir()
    children = wiz._looks_like_workspace_umbrella(tmp_path)
    names = sorted(c.name for c in children)
    assert names == ["proj-a", "proj-b"]


def test_root_is_a_repo_is_not_umbrella(tmp_path):
    (tmp_path / ".git").mkdir()          # root itself is a repo (monorepo case)
    _make_repo(tmp_path / "sub")
    assert wiz._looks_like_workspace_umbrella(tmp_path) == []


def test_child_dot_git_FILE_does_not_count(tmp_path):
    # a .git FILE = submodule/worktree gitlink, NOT a repo dir
    (tmp_path / "with-submodule").mkdir()
    (tmp_path / "with-submodule" / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    assert wiz._looks_like_workspace_umbrella(tmp_path) == []


def test_zero_child_repos_is_not_umbrella(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
    assert wiz._looks_like_workspace_umbrella(tmp_path) == []


def test_symlinked_child_not_followed(tmp_path):
    real = _make_repo(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    children = wiz._looks_like_workspace_umbrella(tmp_path)
    # only the real dir counts; the symlink is skipped
    assert [c.name for c in children] == ["real"]
