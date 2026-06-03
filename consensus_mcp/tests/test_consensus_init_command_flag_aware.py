"""The Claude Code helper (slash command + skill) must HONOR the flag the user
typed - the README documents `consensus-init --install-claude-code` as the global
helper install, but the chat helper used to hardcode `--from-claude-code`,
substitute it for the user's flag, then circularly offer the command they typed.
These content guards lock in the flag-aware dispatch (cold-start UX bug fix).
"""
from __future__ import annotations

from pathlib import Path

from consensus_mcp import _init_wizard as wiz

_EXT = Path(wiz.__file__).parent / "claude_extensions"


def _command() -> str:
    return (_EXT / "commands" / "consensus-init.md").read_text(encoding="utf-8")


def _skill() -> str:
    return (_EXT / "skills" / "consensus" / "SKILL.md").read_text(encoding="utf-8")


def test_command_handles_install_claude_code_flag():
    text = _command()
    # It must have a branch that runs the GLOBAL install for --install-claude-code,
    # not silently substitute the per-project bootstrap.
    assert "--install-claude-code" in text
    assert "consensus-init --install-claude-code" in text
    # and it must NOT circularly offer the command the user just typed.
    assert ("never" in text.lower() and "already typed" in text.lower()) or \
           "you just ran it" in text.lower()


def test_command_still_does_project_bootstrap_by_default():
    text = _command()
    assert "consensus-init --from-claude-code" in text
    # the exit-code carve-outs for the project path are preserved.
    assert "already-configured" in text
    assert "looks-like-workspace-umbrella" in text


def test_skill_handles_install_claude_code_flag():
    text = _skill()
    assert "--install-claude-code" in text
    assert "consensus-init --install-claude-code" in text
    # default per-project bootstrap remains.
    assert "consensus-init --from-claude-code" in text


def test_helper_files_are_ascii_only():
    for text in (_command(), _skill()):
        text.encode("ascii")  # raises UnicodeEncodeError on any non-ASCII byte
