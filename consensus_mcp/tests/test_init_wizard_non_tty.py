"""Regression tests for the non-TTY init-wizard crash (v1.28.1).

Bug: `consensus-init --from-claude-code` (and any bare `consensus init`) was run
without --non-interactive/--accept-defaults under a non-TTY stdin (Claude Code's
Bash tool, CI runners, pipes). The wizard entered the interactive path and the
first input() prompt hit EOF:

  - contributor multi-select EOF -> KeyboardInterrupt -> "aborted by user" (exit 1)
  - the host-peer follow-up prompt did NOT catch EOFError -> uncaught EOFError
    traceback (hard crash)

Fix:
  1. `_stdin_is_interactive()` reports whether stdin is a real interactive TTY.
  2. cmd_init downgrades interactive -> non-interactive when stdin is not a TTY,
     so the wizard auto-detects reviewers + defaults instead of crashing, and
     prints a guidance note.
  3. `_prompt_host_peer_followup` catches EOFError -> KeyboardInterrupt, matching
     its sibling `_select_contributors_interactive` (no more uncaught traceback).
"""
from __future__ import annotations

import builtins

import pytest

from consensus_mcp import _init_wizard as wiz


# ---------- input stub (mirrors test_init_wizard_contributors._stub_input) ----------

def _stub_input(responses):
    queue = list(responses)

    def _fake(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return _fake


class _FakeStdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# ============================================================
# 1. _stdin_is_interactive() helper
# ============================================================

def test_stdin_is_interactive_true_for_tty(monkeypatch):
    # A TTY is interactive only with no agent/CI marker env (v1.33.4): under
    # CLAUDECODE/AI_AGENT/CI there is no human to answer, so those are cleared.
    for var in ("CLAUDECODE", "AI_AGENT", "CI"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(wiz.sys, "stdin", _FakeStdin(tty=True))
    assert wiz._stdin_is_interactive() is True


def test_stdin_is_interactive_false_for_non_tty(monkeypatch):
    monkeypatch.setattr(wiz.sys, "stdin", _FakeStdin(tty=False))
    assert wiz._stdin_is_interactive() is False


def test_stdin_is_interactive_false_when_stdin_none(monkeypatch):
    monkeypatch.setattr(wiz.sys, "stdin", None)
    assert wiz._stdin_is_interactive() is False


# ============================================================
# 2. host-peer follow-up no longer crashes on EOF
# ============================================================

def test_prompt_host_peer_followup_eof_raises_keyboardinterrupt(monkeypatch):
    """EOF during the host-peer prompt must surface as KeyboardInterrupt (clean
    abort, mapped to exit 1) - NOT an uncaught EOFError traceback."""
    profiles = wiz._load_merged_profiles(None)
    monkeypatch.setattr(builtins, "input", _stub_input([]))  # immediate EOF
    with pytest.raises(KeyboardInterrupt):
        wiz._prompt_host_peer_followup(["claude", "codex"], profiles, default_yes=False)


# ============================================================
# 3. cmd_init gates interactivity on a real TTY
# ============================================================

def test_cmd_init_downgrades_to_noninteractive_on_non_tty(tmp_path, monkeypatch):
    """Under pytest, stdin is not a TTY; cmd_init must build the config
    non-interactively (interactive=False) rather than prompting."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: f"/usr/bin/{name}")
    captured = {}
    real = wiz.build_config_from_flags

    def spy(args, repo_root, interactive=False):
        captured["interactive"] = interactive
        return real(args, repo_root, interactive=False)

    monkeypatch.setattr(wiz, "build_config_from_flags", spy)
    rc = wiz.main([])  # no --non-interactive / --accept-defaults
    assert rc == 0
    assert captured["interactive"] is False


def test_cmd_init_keeps_interactive_on_tty(tmp_path, monkeypatch):
    """When stdin IS a TTY, the gate must NOT suppress the interactive path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True, raising=False)
    captured = {}
    real = wiz.build_config_from_flags

    def spy(args, repo_root, interactive=False):
        captured["interactive"] = interactive
        # avoid actually prompting; build a valid config non-interactively
        return real(args, repo_root, interactive=False)

    monkeypatch.setattr(wiz, "build_config_from_flags", spy)
    rc = wiz.main([])
    assert rc == 0
    assert captured["interactive"] is True


# ============================================================
# 4. end-to-end: the exact --from-claude-code scenario must not crash
# ============================================================

def test_from_claude_code_non_tty_initializes_without_crashing(
    tmp_path, monkeypatch, capsys
):
    """The reported bug: `consensus-init --from-claude-code` with no interactive
    flags under a non-TTY stdin must initialize cleanly (auto-detected reviewers
    + defaults) instead of aborting/crashing, and print a guidance note."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: f"/usr/bin/{name}")
    rc = wiz.main(["--from-claude-code"])
    assert rc == 0
    assert (tmp_path / ".consensus" / "config.yaml").exists()
    err = capsys.readouterr().err.lower()
    assert "auto-detected" in err
    assert "terminal" in err


def test_bare_non_tty_initializes_without_crashing(tmp_path, monkeypatch, capsys):
    """Same fallback for a plain non-TTY run (no --from-claude-code)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: f"/usr/bin/{name}")
    rc = wiz.main([])
    assert rc == 0
    assert (tmp_path / ".consensus" / "config.yaml").exists()
    err = capsys.readouterr().err.lower()
    assert "auto-detected" in err
    assert "tty" in err
