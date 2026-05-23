"""v1.24 install-workflow hardening (3-family init/install review, 2026-05-23).

Focused tests for the 12 reviewer-confirmed fixes, ALL confined to
consensus_mcp/_init_wizard.py. Mapping (fix -> test):

  1  IO error during install -> WARN + continue (never crash)
  2  relative CLAUDE_HOME override is normalized (expanduser/resolve)
  3  .mcp.json key-order difference is NOT a false conflict
  4  hook command is shell-safe even when the path has a double quote
  5  instruction-file path traversal refused (5a) + atomic write (5b)
  6  destination symlink is not written THROUGH (unlinked first)
  7  malformed settings.json -> hooks not activated -> rc 6 (incomplete)
  8  freshness below floor -> abort rc 6 (and --force proceeds) [see v123]
  9  per-project agent SKIP -> prominent warning + rc 7
 10  wizard repo-root detection is a reusable module-level helper
 11  config writes go through atomic tmp+os.replace (last-writer-wins)

Return-code convention: 0 ok; 5 managed-file SKIP; 6 freshness-abort /
settings-activation-failure (incomplete); 7 per-project agent SKIP.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from consensus_mcp import _init_wizard as wiz


# --------------------------------------------------------------------------- #
# Fix 1: an IO error on one file WARNs and continues (no crash mid-install).
# --------------------------------------------------------------------------- #

def test_install_extensions_io_error_warns_and_continues(tmp_path, monkeypatch):
    home = tmp_path / "claude_home"
    home.mkdir()

    real = wiz._atomic_write_bytes
    boom_target = home / "skills" / "consensus" / "SKILL.md"

    def flaky(path, data):
        # v1.26: writes route through the hardened _atomic_write_bytes primitive.
        if path == boom_target:
            raise OSError("simulated ENOSPC")
        return real(path, data)

    monkeypatch.setattr(wiz, "_atomic_write_bytes", flaky)
    statuses = wiz._install_claude_extensions(home, force=False)

    # The bad file is WARNed, not raised...
    assert any(s.startswith("WARN:") and "failed:" in s for s in statuses), statuses
    # ...and OTHER files still got written (loop continued past the failure).
    assert any(s.startswith("wrote:") for s in statuses), statuses


def test_install_project_agents_io_error_warns_and_continues(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    real = wiz._atomic_write_bytes
    first_agent = repo / ".claude" / "agents" / wiz._PROJECT_AGENT_FILES[0]

    def flaky(path, data):
        # v1.26: writes route through the hardened _atomic_write_bytes primitive.
        if path == first_agent:
            raise OSError("simulated EACCES")
        return real(path, data)

    monkeypatch.setattr(wiz, "_atomic_write_bytes", flaky)
    statuses = wiz._install_project_agents(repo, force=False)

    assert any(s.startswith("WARN:") and "failed:" in s for s in statuses), statuses
    # The second agent still wrote.
    assert any(s.startswith("wrote:") for s in statuses), statuses


# --------------------------------------------------------------------------- #
# Fix 2: a relative CLAUDE_HOME override is normalized to an absolute path.
# --------------------------------------------------------------------------- #

def test_resolve_claude_home_normalizes_relative_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", "rel_claude")  # relative
    resolved = wiz._resolve_claude_home()
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "rel_claude").resolve()


def test_resolve_claude_home_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HOME", "~/somewhere/claude")
    resolved = wiz._resolve_claude_home()
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "somewhere" / "claude").resolve()


# --------------------------------------------------------------------------- #
# Fix 3: an existing .mcp.json entry that differs ONLY in key order is current.
# --------------------------------------------------------------------------- #

def test_mcp_json_key_order_is_not_a_conflict(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = repo / "consensus-state"
    project_root = repo

    desired = wiz._build_consensus_mcp_entry(
        "consensus-mcp", [], state_root, project_root
    )
    # Write an existing entry with reversed key order at every level.
    reordered_env = dict(reversed(list(desired["env"].items())))
    reordered_entry = {"env": reordered_env, "command": desired["command"]}
    if "args" in desired:
        reordered_entry["args"] = desired["args"]
    mcp_path = wiz._resolve_mcp_json_path(repo)
    mcp_path.write_text(
        json.dumps({"mcpServers": {"consensus-mcp": reordered_entry}}, indent=2),
        encoding="utf-8",
    )

    status, _ = wiz._write_mcp_json(
        repo, state_root, project_root, "consensus-mcp", []
    )
    assert status == "already-current", status


def test_json_semantically_equal_helper():
    a = {"command": "x", "env": {"A": "1", "B": "2"}}
    b = {"env": {"B": "2", "A": "1"}, "command": "x"}
    assert wiz._json_semantically_equal(a, b)
    assert not wiz._json_semantically_equal(a, {"command": "y"})
    assert not wiz._json_semantically_equal(None, a)


# --------------------------------------------------------------------------- #
# Fix 4: the hook command is valid shell even when the path has a double quote.
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name == "nt",
                    reason="a '\"' in a path is POSIX-only; Windows quoting (list2cmdline) "
                           "is the canonical Windows convention and isn't shlex-splittable")
def test_hook_command_quote_safe(monkeypatch):
    weird = Path('/tmp/we"ird path/hook.py')
    cmd = wiz._build_consensus_hook_command(weird)
    # POSIX: shlex must round-trip the command back to [sys.executable, str(weird)].
    import shlex
    parts = shlex.split(cmd)
    assert parts == [sys.executable, str(weird)], (cmd, parts)


def test_hook_command_plain_path_unchanged():
    plain = Path("/usr/share/hooks/hook.py")
    cmd = wiz._build_consensus_hook_command(plain)
    # Platform-aware quoting (shlex.join on POSIX, list2cmdline on Windows). A
    # space-free path is bare on both, so both tokens appear verbatim in the command.
    assert sys.executable in cmd and str(plain) in cmd, cmd


# --------------------------------------------------------------------------- #
# Fix 5a: instruction-file path traversal is refused.
# --------------------------------------------------------------------------- #

def test_instruction_path_traversal_refused(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "evil.md"

    profiles = {
        "evil": {
            "kind": "cli_reviewer",
            "instructions": {"filename": "../evil.md"},
        }
    }
    written = wiz._provision_instruction_files(["evil"], profiles, repo)
    err = capsys.readouterr().err
    assert "refusing to write instruction file outside repo" in err
    assert written == []
    assert not outside.exists()


def test_path_is_within_helper(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert wiz._path_is_within(repo, repo / "AGENTS.md")
    assert wiz._path_is_within(repo, repo)  # repo itself
    assert not wiz._path_is_within(repo, tmp_path / "outside.md")
    assert not wiz._path_is_within(repo, repo / ".." / "escape.md")


# --------------------------------------------------------------------------- #
# Fix 5b: instruction-file writes are atomic (no .tmp left behind, content ok).
# --------------------------------------------------------------------------- #

def test_instruction_write_is_atomic(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    profiles = {
        "codex": {
            "kind": "cli_reviewer",
            "instructions": {"filename": "AGENTS.md"},
        }
    }
    written = wiz._provision_instruction_files(["codex"], profiles, repo)
    target = repo / "AGENTS.md"
    assert target in written
    assert target.exists()
    assert wiz.INSTRUCTION_BEGIN_MARKER in target.read_text(encoding="utf-8")
    # No leftover temp file from the atomic write.
    assert not (repo / "AGENTS.md.tmp").exists()


def test_atomic_write_text_replaces_atomically(tmp_path):
    target = tmp_path / "sub" / "f.txt"
    wiz._atomic_write_text(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert not (tmp_path / "sub" / "f.txt.tmp").exists()


# --------------------------------------------------------------------------- #
# Fix 6: a destination symlink is unlinked, not written THROUGH.
# --------------------------------------------------------------------------- #

def test_install_does_not_follow_destination_symlink(tmp_path):
    home = tmp_path / "claude_home"
    home.mkdir()
    # Point the SKILL.md destination at a symlink to a file OUTSIDE claude_home.
    outside = tmp_path / "outside_target.md"
    outside.write_text("ORIGINAL OUTSIDE CONTENT — MUST NOT BE CLOBBERED\n", encoding="utf-8")

    dst = home / "skills" / "consensus" / "SKILL.md"
    dst.parent.mkdir(parents=True)
    dst.symlink_to(outside)

    statuses = wiz._install_claude_extensions(home, force=True)

    # The symlink target is untouched (we did not write through the link).
    assert outside.read_text(encoding="utf-8") == "ORIGINAL OUTSIDE CONTENT — MUST NOT BE CLOBBERED\n"
    # The destination is now a regular file with the shipped content.
    assert not dst.is_symlink()
    assert "name: consensus" in dst.read_text(encoding="utf-8")
    assert any(s.startswith("wrote:") for s in statuses), statuses


# --------------------------------------------------------------------------- #
# Fix 7: malformed settings.json -> hooks not activated -> rc 6 (incomplete).
# --------------------------------------------------------------------------- #

def test_malformed_settings_json_yields_rc6(tmp_path, monkeypatch, capsys):
    home = tmp_path / "claude_home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    # Existing settings.json is malformed -> activation must fail soft + signal.
    (home / "settings.json").write_text("{ this is not json", encoding="utf-8")

    rc = wiz.main(["--install-claude-code"])
    err = capsys.readouterr().err
    assert rc == 6, err
    assert "enforcement is OFF" in err or "NOT activated" in err
    # The malformed file is left exactly as-is (never clobbered).
    assert (home / "settings.json").read_text(encoding="utf-8") == "{ this is not json"


def test_clean_settings_activation_is_rc0(tmp_path, monkeypatch, capsys):
    home = tmp_path / "claude_home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    rc = wiz.main(["--install-claude-code"])
    err = capsys.readouterr().err
    assert rc == 0, err
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert "hooks" in settings


# --------------------------------------------------------------------------- #
# Fix 9: a divergent per-project agent SKIP -> prominent warning + rc 7.
# --------------------------------------------------------------------------- #

def _no_prompt_args():
    # Build via main() so all dest paths derive from a fresh repo in tmp.
    return [
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-mcp-json",
    ]


def test_divergent_agent_skip_yields_rc7(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        wiz.shutil, "which",
        lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None,
    )
    # Pre-seed a DIVERGENT agent so the install SKIPs it (no --force).
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / wiz._PROJECT_AGENT_FILES[0]).write_text(
        "LOCAL DIVERGENT AGENT EDIT\n", encoding="utf-8"
    )

    rc = wiz.main(_no_prompt_args())
    err = capsys.readouterr().err
    assert rc == 7, err
    assert "SKIPPED" in err and "--force" in err
    # The divergent agent was preserved (not clobbered).
    assert "LOCAL DIVERGENT AGENT EDIT" in (
        agents_dir / wiz._PROJECT_AGENT_FILES[0]
    ).read_text(encoding="utf-8")
    # The config was still written (bootstrap usable despite the stale agent).
    assert (tmp_path / ".consensus" / "config.yaml").exists()


def test_divergent_agent_force_updates_and_rc0(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        wiz.shutil, "which",
        lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None,
    )
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / wiz._PROJECT_AGENT_FILES[0]).write_text(
        "STALE AGENT\n", encoding="utf-8"
    )

    rc = wiz.main(_no_prompt_args() + ["--force"])
    assert rc == 0
    updated = (agents_dir / wiz._PROJECT_AGENT_FILES[0]).read_text(encoding="utf-8")
    assert "STALE AGENT" not in updated


def test_clean_per_project_init_is_rc0(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        wiz.shutil, "which",
        lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None,
    )
    rc = wiz.main(_no_prompt_args())
    assert rc == 0
    assert (tmp_path / ".consensus" / "config.yaml").exists()
    for fname in wiz._PROJECT_AGENT_FILES:
        assert (tmp_path / ".claude" / "agents" / fname).exists()


# --------------------------------------------------------------------------- #
# Fix 10: wizard repo-root detection is a reusable module-level helper.
# --------------------------------------------------------------------------- #

def test_detect_repo_root_is_reusable_helper(tmp_path, monkeypatch):
    assert callable(wiz._detect_repo_root)
    # A directory with a strong marker is detected as the root.
    proj = tmp_path / "proj"
    (proj).mkdir()
    (proj / "pyproject.toml").write_text("[tool]\n", encoding="utf-8")
    # Disable git so we exercise the marker-walk branch deterministically.
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        wiz.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no git")),
    )
    root = wiz._detect_repo_root(proj)
    assert root == proj.resolve()


# --------------------------------------------------------------------------- #
# Fix 11: config writes are atomic (tmp + os.replace), no .tmp residue.
# --------------------------------------------------------------------------- #

def test_atomic_write_json_no_residue(tmp_path):
    p = tmp_path / "out" / ".mcp.json"
    wiz._atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
    assert not p.with_suffix(p.suffix + ".tmp").exists()


def test_write_config_is_atomic(tmp_path):
    from consensus_mcp import config as cfg
    p = tmp_path / ".consensus" / "config.yaml"
    wiz.write_config(cfg.default_config(), p)
    assert p.exists()
    assert not p.with_suffix(p.suffix + ".tmp").exists()


# --- v1.25 (convergence re-review fixes) ------------------------------------- #

def test_v125_install_replaces_dst_symlink_without_following(tmp_path, monkeypatch):
    """gemini BLOCKING: installing over a destination symlink must replace the LINK
    (atomically), never write through it to clobber the outside target."""
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    outside = tmp_path / "outside.txt"; outside.write_text("SECRET\n", encoding="utf-8")
    sk = home / "skills" / "consensus"; sk.mkdir(parents=True)
    (sk / "SKILL.md").symlink_to(outside)            # managed dst is a symlink

    wiz.main(["--install-claude-code", "--force"])    # --force overwrites divergent

    dstfile = home / "skills" / "consensus" / "SKILL.md"
    assert not dstfile.is_symlink()                   # link replaced with a real file
    assert dstfile.read_text(encoding="utf-8") != "SECRET\n"
    assert outside.read_text(encoding="utf-8") == "SECRET\n"   # target untouched


def test_v125_settings_write_oserror_warns_not_crashes(tmp_path, monkeypatch):
    """kimi: an IO failure writing settings.json fails SOFT (WARN), never crashes."""
    home = tmp_path / "home"; home.mkdir()
    def boom(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(wiz, "_atomic_write_json", boom)
    lines = wiz._install_claude_settings_json(home, force=False)
    assert any(l.startswith("WARN:") and "could not be written" in l for l in lines), lines


def test_v125_settings_write_failure_returns_rc6(tmp_path, monkeypatch):
    """kimi: settings.json activation failure surfaces as an incomplete install (rc 6)."""
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    real = wiz._atomic_write_json
    def selective(path, data):
        if str(path).endswith("settings.json"):
            raise OSError("boom")
        return real(path, data)
    monkeypatch.setattr(wiz, "_atomic_write_json", selective)
    rc = wiz.main(["--install-claude-code"])
    assert rc == 6


# --- v1.26 hardened atomic write primitive ---------------------------------- #

def test_v126_atomic_write_replaces_dst_symlink_not_target(tmp_path):
    outside = tmp_path / "outside.txt"; outside.write_text("SECRET", encoding="utf-8")
    dst = tmp_path / "dst.txt"; dst.symlink_to(outside)
    wiz._atomic_write_bytes(dst, b"NEW")
    assert not dst.is_symlink()                      # link replaced with a real file
    assert dst.read_text(encoding="utf-8") == "NEW"
    assert outside.read_text(encoding="utf-8") == "SECRET"   # target untouched


def test_v126_atomic_write_ignores_preplanted_predictable_tmp(tmp_path):
    # An attacker pre-plants the OLD predictable `<dst>.tmp` as a symlink to a decoy;
    # the random-name + O_EXCL primitive must not follow it.
    dst = tmp_path / "f.txt"
    decoy = tmp_path / "decoy.txt"; decoy.write_text("EVIL", encoding="utf-8")
    (tmp_path / "f.txt.tmp").symlink_to(decoy)
    wiz._atomic_write_bytes(dst, b"GOOD")
    assert dst.read_text(encoding="utf-8") == "GOOD"
    assert decoy.read_text(encoding="utf-8") == "EVIL"       # decoy untouched
