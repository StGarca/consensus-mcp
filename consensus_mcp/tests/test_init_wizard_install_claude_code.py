"""iter-0040 - tests for consensus-init --install-claude-code + --from-claude-code.

Per iter-0039 converged-plan.yaml acceptance gates A1-A7.

A1: package ships consensus_mcp/claude_extensions/{skills,commands}/
A2: --install-claude-code copies into HOME idempotently
A3: CLAUDE_HOME env override redirects install destination
A4: --from-claude-code prints Claude-Code-specific restart message
A5: `consensus init` (space form) routes to the same entry point
A6: skill/command content avoids consensus-mcp-internal jargon
A7: README/CHANGELOG covered by separate manual review
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from consensus_mcp import _init_wizard as wiz


# ---------- A1: package-data presence ----------

def test_claude_extensions_skill_md_ships_in_package():
    """A1: SKILL.md must exist next to the module so package_data picks it up."""
    pkg_root = Path(wiz.__file__).resolve().parent
    skill = pkg_root / "claude_extensions" / "skills" / "consensus" / "SKILL.md"
    assert skill.exists(), f"missing packaged SKILL.md at {skill}"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md must start with YAML frontmatter"
    assert "name: consensus" in text
    assert "description:" in text


def test_claude_extensions_command_md_ships_in_package():
    """A1: consensus-init.md command file must ship with the package."""
    pkg_root = Path(wiz.__file__).resolve().parent
    cmd = pkg_root / "claude_extensions" / "commands" / "consensus-init.md"
    assert cmd.exists(), f"missing packaged command at {cmd}"
    text = cmd.read_text(encoding="utf-8")
    assert text.startswith("---"), "command must start with YAML frontmatter"
    assert "description:" in text


def test_consensus_workflow_skill_ships_in_package():
    """iter-0041: operating-procedure skill must ship next to the bootstrap skill."""
    pkg_root = Path(wiz.__file__).resolve().parent
    skill = pkg_root / "claude_extensions" / "skills" / "consensus-workflow" / "SKILL.md"
    assert skill.exists(), f"missing packaged consensus-workflow SKILL.md at {skill}"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: consensus-workflow" in text
    # Sanity check that the skill carries the load-bearing rules.
    assert "workflow #4" in text.lower() or "propose-converge" in text.lower()
    assert "review-packet" in text.lower() or "review_target" in text.lower()


def test_install_claude_code_copies_workflow_skill(tmp_path, monkeypatch):
    """iter-0041: --install-claude-code also installs the consensus-workflow skill."""
    fake_home = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    rc = wiz.main(["--install-claude-code"])
    assert rc == 0
    assert (fake_home / "skills" / "consensus" / "SKILL.md").exists()
    assert (fake_home / "skills" / "consensus-workflow" / "SKILL.md").exists()
    assert (fake_home / "commands" / "consensus-init.md").exists()


# ---------- A2: --install-claude-code copies idempotently ----------

def test_install_claude_code_copies_skill_and_command(tmp_path, monkeypatch):
    """A2: --install-claude-code copies SKILL.md + command MD into CLAUDE_HOME.

    iter-0040 hot-fix: --install-claude-code is a STANDALONE global install
    action - it does NOT trigger per-project bootstrap (config.yaml, .mcp.json).
    Test runs in a fresh tmp_path with no prior config; expects only the
    extension files to land.
    """
    fake_home = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    rc = wiz.main(["--install-claude-code"])
    assert rc == 0
    assert (fake_home / "skills" / "consensus" / "SKILL.md").exists()
    assert (fake_home / "commands" / "consensus-init.md").exists()
    # Per-project artifacts must NOT be written by the global install.
    assert not (tmp_path / ".consensus" / "config.yaml").exists()
    assert not (tmp_path / ".mcp.json").exists()


def test_install_claude_code_idempotent_on_rerun(tmp_path, monkeypatch):
    """A2: running --install-claude-code twice is a no-op the second time."""
    fake_home = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    first = wiz.main(["--install-claude-code"])
    assert first == 0
    skill = fake_home / "skills" / "consensus" / "SKILL.md"
    first_text = skill.read_text(encoding="utf-8")

    # Rerun. Skill content is byte-identical -> no rewrite.
    second = wiz.main(["--install-claude-code"])
    assert second == 0
    assert skill.read_text(encoding="utf-8") == first_text


def test_install_claude_code_refuses_overwrite_on_divergent_existing(
    tmp_path, monkeypatch, capsys
):
    """A2: if user-edited skill diverges, refuse without --force."""
    fake_home = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)
    skill_dir = fake_home / "skills" / "consensus"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    skill.write_text("# user-edited skill content do not clobber\n", encoding="utf-8")

    rc = wiz.main(["--install-claude-code"])
    captured = capsys.readouterr()
    # v1.23 (finding 4): a divergent managed file is PRESERVED (not clobbered), but
    # the skip is now surfaced LOUDLY and returns a distinct nonzero (was silently
    # rc=0 - a silent stale skill is the failure mode codex flagged).
    assert rc == 5
    assert "user-edited skill content" in skill.read_text(encoding="utf-8")
    assert "SKIPPED" in captured.err and "--force" in captured.err


def test_install_claude_code_force_replaces_divergent_existing(
    tmp_path, monkeypatch
):
    """A2: --force replaces a divergent SKILL.md."""
    fake_home = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)
    skill_dir = fake_home / "skills" / "consensus"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    skill.write_text("# stale content\n", encoding="utf-8")

    rc = wiz.main(["--install-claude-code", "--force"])
    assert rc == 0
    new = skill.read_text(encoding="utf-8")
    assert "# stale content" not in new
    assert "name: consensus" in new


# ---------- A3: CLAUDE_HOME env override ----------

def test_install_claude_code_honors_claude_home_env(tmp_path, monkeypatch):
    """A3: CLAUDE_HOME env redirects install to a non-default location."""
    custom = tmp_path / "weird_location" / "my_claude"
    monkeypatch.setenv("CLAUDE_HOME", str(custom))
    monkeypatch.chdir(tmp_path)

    rc = wiz.main(["--install-claude-code"])
    assert rc == 0
    assert (custom / "skills" / "consensus" / "SKILL.md").exists()
    assert (custom / "commands" / "consensus-init.md").exists()


def test_resolve_claude_home_defaults_to_dot_claude(monkeypatch, tmp_path):
    """A3: when CLAUDE_HOME unset, _resolve_claude_home() returns ~/.claude."""
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))     # POSIX
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    resolved = wiz._resolve_claude_home()
    assert resolved == tmp_path / ".claude"


# ---------- A4: --from-claude-code prints contextual restart message ----------

def test_from_claude_code_prints_reload_message(tmp_path, monkeypatch, capsys):
    """A4: --from-claude-code prints the Claude-Code-specific restart text."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which",
                        lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--from-claude-code",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Must mention reload/restart explicitly.
    assert ("reload" in out.lower()) or ("restart" in out.lower())
    # Should mention Claude Code or `/mcp` so the user knows what to do.
    assert ("claude code" in out.lower()) or ("/mcp" in out.lower())


def test_default_does_not_print_claude_code_specific_message(
    tmp_path, monkeypatch, capsys
):
    """A4 negative: without --from-claude-code, the Claude-specific text is NOT printed."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which",
                        lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # The Claude-Code-specific sentinel should not appear in default output.
    assert "Detected --from-claude-code" not in out


# ---------- A5: `consensus init` (space form) routes to same entry point ----------

def test_consensus_space_init_routes_to_init_wizard(tmp_path, monkeypatch):
    """A5: argv[0] == "init" is stripped and the wizard runs normally."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which",
                        lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    # Simulate `consensus init --non-interactive --accept-defaults ...`
    rc = wiz.main([
        "init",  # the subcommand
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    # Config and .mcp.json should be written exactly as if --init was absent.
    assert (tmp_path / ".consensus" / "config.yaml").exists()
    assert (tmp_path / ".mcp.json").exists()


def test_consensus_space_no_args_runs_init(tmp_path, monkeypatch):
    """A5: bare `consensus init` with no extra args still works (interactive
    would prompt; non-interactive accept-defaults explicit here)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which",
                        lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    # Subcommand by itself + the defaults flags.
    rc = wiz.main(["init", "--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert rc == 0
