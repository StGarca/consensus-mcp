"""v1.21 smooth-init UX polish (recon items #4-#7).

Covers four post-recon polish behaviors in `consensus_mcp/_init_wizard.py`:

  #4 POST-INIT STATUS SUMMARY — a fresh per-project bootstrap prints a concise
     friendly summary (config path, panel composition, present/missing CLIs,
     MCP-command resolvability, first concrete next step). Suppressed for
     --check / --print-defaults / --install-claude-code.

  #5 MCP-COMMAND RESOLVABILITY — when the resolved .mcp.json command does not
     resolve on PATH, a clear WARNING is printed (does not fail). No warning
     when it resolves.

  #6 DEGENERATE-PANEL GUARD — non-interactive bootstrap with <2 independent
     contributors prints an explicit single-reviewer (degraded) WARNING. No
     warning at >=2.

  #7 ORPHANED hooks.json removed — the pre-v1.21 inert manifest is gone from
     the package and nothing load-bearing references it.
"""
from __future__ import annotations

from pathlib import Path

from consensus_mcp import _init_wizard as wiz


# --------------------------------------------------------------------------- #
# #4 — POST-INIT STATUS / NEXT-STEPS SUMMARY
# --------------------------------------------------------------------------- #

def test_fresh_init_prints_status_summary(tmp_path, capsys, monkeypatch):
    """A fresh non-interactive bootstrap prints the next-steps summary block
    naming the config path, the panel, and the first concrete next step."""
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-instructions",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Next steps" in out
    # config path surfaced in the summary
    assert str(tmp_path / ".consensus" / "config.yaml") in out
    # panel composition / enabled contributors named
    assert "Panel:" in out
    assert "claude" in out and "codex" in out and "gemini" in out
    # first concrete next step: how to run a consult
    assert "consult" in out.lower()


def test_status_summary_lists_present_and_missing_clis(tmp_path, capsys, monkeypatch):
    """The summary distinguishes contributor CLIs present vs missing on PATH."""
    monkeypatch.chdir(tmp_path)

    # Make ONLY 'codex' resolve on PATH; gemini absent. host (claude) is always
    # available and has no CLI to detect.
    real_which = wiz.shutil.which

    def fake_which(cmd, *a, **k):
        if cmd == "codex":
            return "/usr/bin/codex"
        if cmd == "consensus-mcp":
            return "/usr/bin/consensus-mcp"
        if cmd in ("gemini",):
            return None
        return real_which(cmd, *a, **k)

    monkeypatch.setattr(wiz.shutil, "which", fake_which)

    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-instructions",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # present vs missing surfaced
    assert "present" in out.lower()
    assert "missing" in out.lower()
    assert "gemini" in out


def test_status_summary_suppressed_for_check(tmp_path, capsys, monkeypatch):
    """--check must NOT print the bootstrap status summary."""
    monkeypatch.chdir(tmp_path)
    # Seed a valid config first.
    wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-instructions",
    ])
    capsys.readouterr()  # discard bootstrap output

    rc = wiz.main(["--check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Next steps" not in out


def test_status_summary_suppressed_for_print_defaults(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main(["--print-defaults"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Next steps" not in out


def test_status_summary_suppressed_for_install_claude_code(tmp_path, capsys, monkeypatch):
    """--install-claude-code is a global op; no per-project status summary."""
    fake_home = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)
    rc = wiz.main(["--install-claude-code"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Next steps" not in out


def test_status_summary_from_claude_code_mentions_restart(tmp_path, capsys, monkeypatch):
    """--from-claude-code: the first next step references reloading Claude Code."""
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-instructions", "--from-claude-code",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Next steps" in out
    low = out.lower()
    assert "restart" in low or "/mcp" in low or "reload" in low


# --------------------------------------------------------------------------- #
# #5 — MCP-COMMAND RESOLVABILITY CHECK
# --------------------------------------------------------------------------- #

def test_mcp_resolvability_warns_when_command_absent(tmp_path, capsys, monkeypatch):
    """When the resolved .mcp.json command does not resolve on PATH, warn."""
    monkeypatch.chdir(tmp_path)

    # Force an explicit command that is not on PATH so resolution fails.
    def fake_which(cmd, *a, **k):
        if cmd == "definitely-not-on-path":
            return None
        if cmd == "consensus-mcp":
            return None  # fall back path also irrelevant; explicit overrides
        return "/usr/bin/" + cmd

    monkeypatch.setattr(wiz.shutil, "which", fake_which)

    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-instructions",
        "--mcp-command", "definitely-not-on-path",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "WARNING" in combined or "WARN" in combined
    assert "definitely-not-on-path" in combined
    assert "PATH" in combined or "resolve" in combined.lower()


def test_mcp_resolvability_no_warn_when_present(tmp_path, capsys, monkeypatch):
    """When the resolved command IS on PATH, no resolvability warning fires."""
    monkeypatch.chdir(tmp_path)

    def fake_which(cmd, *a, **k):
        return "/usr/bin/" + cmd  # everything resolves

    monkeypatch.setattr(wiz.shutil, "which", fake_which)

    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-instructions",
        "--mcp-command", "consensus-mcp",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "may not resolve" not in combined.lower()
    # The summary's MCP line should report it resolves.
    assert "consensus-mcp" in combined


def test_mcp_resolvability_helper_detects_absent(monkeypatch):
    """Unit-level: the resolvability helper reports False when which() is None."""
    monkeypatch.setattr(wiz.shutil, "which", lambda *a, **k: None)
    assert wiz._mcp_command_resolves("consensus-mcp") is False


def test_mcp_resolvability_helper_detects_present(monkeypatch):
    monkeypatch.setattr(wiz.shutil, "which", lambda *a, **k: "/usr/bin/consensus-mcp")
    assert wiz._mcp_command_resolves("consensus-mcp") is True


# --------------------------------------------------------------------------- #
# #6 — DEGENERATE-PANEL GUARD (non-interactive only)
# --------------------------------------------------------------------------- #

def test_degenerate_panel_warns_single_contributor(tmp_path, capsys, monkeypatch):
    """Non-interactive bootstrap with a single contributor warns about the
    single-reviewer (degraded) configuration."""
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude",
        "--no-instructions",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    low = combined.lower()
    assert "warn" in low
    assert "2" in combined  # references the >=2 requirement
    assert "single" in low or "degrad" in low


def test_degenerate_panel_no_warn_when_two_or_more(tmp_path, capsys, monkeypatch):
    """No degenerate-panel warning when >=2 independent contributors enabled."""
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex",
        "--no-instructions",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "single-reviewer" not in combined
    assert "cross-family review" not in combined


# --------------------------------------------------------------------------- #
# #7 — ORPHANED hooks.json REMOVED
# --------------------------------------------------------------------------- #

def test_orphaned_hooks_json_removed_from_package():
    """The pre-v1.21 inert hooks.json manifest must no longer ship."""
    pkg_root = Path(wiz.__file__).resolve().parent
    orphan = pkg_root / "claude_extensions" / "hooks" / "hooks.json"
    assert not orphan.exists(), f"orphaned manifest still present: {orphan}"


def test_real_hook_scripts_still_present():
    """Deleting hooks.json must NOT remove the real hook scripts."""
    pkg_root = Path(wiz.__file__).resolve().parent
    hooks_dir = pkg_root / "claude_extensions" / "hooks"
    for name in (
        "consensus_sessionstart.py",
        "consensus_pretooluse_gate.py",
        "consensus_stop_gate.py",
    ):
        assert (hooks_dir / name).exists(), name
