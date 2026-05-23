"""v1.23 install-workflow fixes (codex install-workflow review 2026-05-23).

Finding 4: a divergent managed skill/hook silently SKIPPED on upgrade is now
surfaced loudly + returns a distinct nonzero.
Finding 5: --install-claude-code warns when the package ships fewer than the
expected vendored-skill floor (a stale/partial package).
"""
from consensus_mcp import _init_wizard as wiz


def test_install_skip_is_surfaced_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    home = tmp_path / "claude_home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    # Pre-seed a DIVERGENT managed skill so the install SKIPs it (not --force).
    sk = home / "skills" / "consensus-workflow"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("LOCAL DIVERGENT EDIT\n", encoding="utf-8")

    rc = wiz.main(["--install-claude-code"])
    err = capsys.readouterr().err
    assert rc == 5, err
    assert "SKIPPED" in err
    assert "--force" in err
    assert "consensus-workflow" in err


def test_install_freshness_warns_when_below_floor(tmp_path, monkeypatch, capsys):
    home = tmp_path / "claude_home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    # Raise the floor so the real (complete) package trips the staleness warning.
    monkeypatch.setattr(wiz, "_EXPECTED_VENDORED_SKILLS", 999)

    # v1.24 (fix 8): below-floor now ABORTS before copying any asset and returns a
    # distinct nonzero (6, incomplete) instead of warning-then-installing-stale.
    rc = wiz.main(["--install-claude-code"])
    err = capsys.readouterr().err
    assert rc == 6, err
    assert "STALE or partial" in err
    assert "ABORTING" in err
    # Nothing was copied — the install aborted before touching CLAUDE_HOME.
    assert not (home / "skills" / "consensus" / "SKILL.md").exists()


def test_install_freshness_force_proceeds_despite_below_floor(tmp_path, monkeypatch, capsys):
    home = tmp_path / "claude_home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setattr(wiz, "_EXPECTED_VENDORED_SKILLS", 999)

    # v1.24 (fix 8): --force overrides the staleness abort and installs anyway,
    # with the warning still printed.
    rc = wiz.main(["--install-claude-code", "--force"])
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "STALE or partial" in err
    assert (home / "skills" / "consensus" / "SKILL.md").exists()


def test_install_clean_install_is_rc0_no_freshness_warning(tmp_path, monkeypatch, capsys):
    # The real package on a fresh home: all files written, no SKIP, no staleness.
    home = tmp_path / "claude_home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    rc = wiz.main(["--install-claude-code"])
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "STALE or partial" not in err
