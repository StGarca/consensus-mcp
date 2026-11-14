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


import builtins
import consensus_mcp.config as cfg
import yaml


def _seed_umbrella(tmp_path):
    """tmp_path becomes a workspace umbrella with 2 child git repos."""
    _make_repo(tmp_path / "alpha")
    _make_repo(tmp_path / "beta")
    return tmp_path


def test_nontty_umbrella_emits_token_exit_8_no_write(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _seed_umbrella(tmp_path)
    rc = wiz.main([])
    assert rc == 8
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == wiz.WORKSPACE_UMBRELLA_TOKEN
    assert "alpha" in captured.err and "beta" in captured.err
    assert not (tmp_path / ".consensus").exists()  # nothing written


def test_here_flag_bypasses_guard(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _seed_umbrella(tmp_path)
    rc = wiz.main(["--here", "--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert wiz.WORKSPACE_UMBRELLA_TOKEN not in capsys.readouterr().out
    assert (tmp_path / ".consensus" / "config.yaml").exists()


def test_tty_umbrella_confirm_no_aborts_8(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: "n")
    _seed_umbrella(tmp_path)
    rc = wiz.main([])
    assert rc == 8
    assert not (tmp_path / ".consensus").exists()


def test_tty_umbrella_confirm_yes_proceeds(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    # Stub the environment to a dev box where every reviewer CLI is installed, so the
    # contributor multi-select's ">=2 required" default is satisfied on EMPTY input.
    # Without this the test depends on ambient PATH: on a clean runner (CI) only the
    # claude host is detected, the menu re-prompts forever for a 2nd reviewer, and the
    # fixed input iterator below is exhausted -> StopIteration. (CI failure, all platforms.)
    monkeypatch.setattr(wiz, "_profile_installed", lambda *_a, **_k: True)
    # "y" to the umbrella confirm, then defaults for the rest of the wizard
    answers = iter(["y"] + [""] * 12)
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: next(answers))
    _seed_umbrella(tmp_path)
    rc = wiz.main([])
    assert rc == 0
    assert (tmp_path / ".consensus" / "config.yaml").exists()


def test_guard_skipped_when_config_exists(tmp_path, capsys, monkeypatch):
    """An already-bootstrapped umbrella routes to the v1.29.0 already-configured
    path (so you can re-run to clean it up) - NOT the umbrella token."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _seed_umbrella(tmp_path)
    (tmp_path / ".consensus").mkdir()
    (tmp_path / ".consensus" / "config.yaml").write_text(
        yaml.safe_dump(cfg.default_config()), encoding="utf-8")
    rc = wiz.main([])
    out = capsys.readouterr().out
    assert wiz.WORKSPACE_UMBRELLA_TOKEN not in out
    assert out.splitlines()[0] == wiz.ALREADY_CONFIGURED_TOKEN  # already-configured wins
    assert rc == 4


def test_non_umbrella_fresh_init_unaffected(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    # no child repos -> not an umbrella -> normal non-TTY fresh init
    rc = wiz.main(["--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert wiz.WORKSPACE_UMBRELLA_TOKEN not in capsys.readouterr().out
    assert (tmp_path / ".consensus" / "config.yaml").exists()


def _ext_dir():
    return Path(wiz.__file__).parent / "claude_extensions"


def test_umbrella_token_documented_in_skill_and_command():
    skill = (_ext_dir() / "skills" / "consensus" / "SKILL.md").read_text(encoding="utf-8")
    command = (_ext_dir() / "commands" / "consensus-init.md").read_text(encoding="utf-8")
    assert wiz.WORKSPACE_UMBRELLA_TOKEN in skill
    assert wiz.WORKSPACE_UMBRELLA_TOKEN in command
    assert "--here" in skill and "--here" in command
    # exit 8 documented in the skill, distinct from already-configured (exit 4)
    assert "exit code 8" in skill.lower() or "exits with code 8" in skill.lower()
    # command doc is what Claude Code reads when dispatched - assert it symmetrically
    assert "exit code 8" in command.lower() or "exits with code 8" in command.lower()


def test_tty_umbrella_eof_aborts_1(tmp_path, capsys, monkeypatch):
    """EOF/Ctrl-D at the umbrella confirm -> 'aborted by user' exit 1 (distinct
    from the decline path, which is exit 8)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    def _eof(*_a, **_k):
        raise EOFError
    monkeypatch.setattr(builtins, "input", _eof)
    _seed_umbrella(tmp_path)
    rc = wiz.main([])
    assert rc == 1
    assert "aborted by user" in capsys.readouterr().err
    assert not (tmp_path / ".consensus").exists()


def test_reconfigure_skips_umbrella_guard(tmp_path, capsys, monkeypatch):
    """--reconfigure bypasses the umbrella guard (maintenance op). With no config
    yet + reconfigure, it must NOT emit the umbrella token."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _seed_umbrella(tmp_path)
    rc = wiz.main(["--reconfigure", "--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert wiz.WORKSPACE_UMBRELLA_TOKEN not in capsys.readouterr().out
    # reconfigure proceeded (did not refuse with exit 8)
    assert rc != 8


def test_config_path_override_does_not_bypass_guard(tmp_path, capsys, monkeypatch):
    """--config redirects the write target but does NOT bypass the umbrella guard
    (the guard keys on the detected root). Only --here bypasses."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _seed_umbrella(tmp_path)
    alt = tmp_path / "elsewhere.yaml"
    rc = wiz.main(["--config", str(alt)])
    assert rc == 8
    assert capsys.readouterr().out.splitlines()[0] == wiz.WORKSPACE_UMBRELLA_TOKEN
    assert not alt.exists()


def test_umbrella_child_list_capped_at_10(tmp_path, capsys, monkeypatch):
    """>10 child repos: guidance lists at most 10 names + a '(+N more)' suffix."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    for i in range(13):
        _make_repo(tmp_path / f"repo{i:02d}")
    rc = wiz.main([])
    assert rc == 8
    err = capsys.readouterr().err
    assert "more" in err  # the "(+N more)" overflow suffix
