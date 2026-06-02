"""v1.21 (converged-plan B5) - tests for settings.json hook ACTIVATION.

The old installer copied a bare hooks.json into ~/.claude, which Claude Code
does NOT read - so enforcement never fired. The fix merges consensus hook
entries into <claude_home>/settings.json (the config Claude Code actually reads)
in a merge-safe, idempotent way, with an uninstall path that removes ONLY
consensus-tagged entries.

Verified settings.json hooks schema (read from a live ~/.claude/settings.json
AND superpowers v5.1.0 hooks/hooks.json before writing):

  {"hooks": {"<Event>": [{"matcher": "...",            # OPTIONAL
                          "hooks": [{"type": "command",
                                     "command": "...",
                                     "async": false}]}]}}
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from consensus_mcp import _init_wizard as wiz


def _read_settings(claude_home: Path) -> dict:
    return json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))


def _all_groups(settings: dict) -> list[dict]:
    groups: list[dict] = []
    for ev_groups in settings.get("hooks", {}).values():
        groups.extend(ev_groups)
    return groups


def _consensus_commands(settings: dict) -> list[str]:
    cmds: list[str] = []
    for group in _all_groups(settings):
        for h in group.get("hooks", []):
            if h.get(wiz.CONSENSUS_HOOK_MARKER) is True:
                cmds.append(h["command"])
    return cmds


# --------------------------------------------------------------------------- #
# Merge writes consensus hook entries (the core activation fix).
# --------------------------------------------------------------------------- #

def test_merge_writes_consensus_hook_entries(tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()

    statuses = wiz._install_claude_settings_json(claude_home)
    assert any("activated" in s for s in statuses), statuses

    settings = _read_settings(claude_home)
    hooks = settings["hooks"]
    # All four enforcement events registered.
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"):
        assert event in hooks, event
        groups = hooks[event]
        assert isinstance(groups, list) and groups
        # Each consensus group: a command-type hook carrying the marker.
        for grp in groups:
            assert isinstance(grp["hooks"], list) and grp["hooks"]
            for h in grp["hooks"]:
                assert h["type"] == "command"
                assert h["command"]
                assert h[wiz.CONSENSUS_HOOK_MARKER] is True

    # PreToolUse matcher covers the gated tools.
    pre_matcher = hooks["PreToolUse"][0]["matcher"]
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
        assert tool in pre_matcher, (tool, pre_matcher)

    # Commands reference the installed hook scripts via ABSOLUTE paths.
    cmds = _consensus_commands(settings)
    assert any("consensus_sessionstart.py" in c for c in cmds)
    assert any("consensus_pretooluse_gate.py" in c for c in cmds)
    assert any("consensus_stop_gate.py" in c for c in cmds)
    for c in cmds:
        # The hook script path embedded in the command is absolute.
        # command shape (v1.30.7): shlex.join([<python>, <abs script path>]).
        # Parse with shlex.split - the cross-platform single-strategy contract
        # admits any-character paths (incl. spaces in 'C:\Program Files\...'),
        # which a naive rsplit-on-whitespace would garble.
        assert ".py" in c
        script_part = shlex.split(c)[-1]
        assert Path(script_part).is_absolute(), c


# --------------------------------------------------------------------------- #
# Idempotency: a re-run produces NO duplicate consensus entries.
# --------------------------------------------------------------------------- #

def test_merge_is_idempotent_no_dup(tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()

    wiz._install_claude_settings_json(claude_home)
    first = _read_settings(claude_home)
    first_cmds = sorted(_consensus_commands(first))

    statuses = wiz._install_claude_settings_json(claude_home)
    second = _read_settings(claude_home)
    second_cmds = sorted(_consensus_commands(second))

    # Same set of consensus commands; no duplicates introduced.
    assert first_cmds == second_cmds
    # One consensus command per event spec (no growth on re-run).
    assert len(second_cmds) == len(wiz._CONSENSUS_HOOK_SPECS)
    # And the second run reports "already current" (true idempotency).
    assert any("already current" in s for s in statuses), statuses
    # Full settings dict unchanged across re-run.
    assert first == second


# --------------------------------------------------------------------------- #
# A pre-existing UNRELATED user hook is preserved.
# --------------------------------------------------------------------------- #

def test_merge_preserves_unrelated_user_hook(tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    # Seed a realistic existing settings.json (mirrors the live shape: a
    # SessionStart hook with NO matcher + unrelated top-level keys).
    existing = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command",
                            "command": '"/home/u/.claude/hooks/other.mjs"'}]}
            ],
            "PostToolUse": [
                {"matcher": "Edit", "hooks": [
                    {"type": "command", "command": "echo user-post"}]}
            ],
        },
        "theme": "dark-ansi",
        "enabledPlugins": {"some-plugin": True},
    }
    (claude_home / "settings.json").write_text(json.dumps(existing), encoding="utf-8")

    wiz._install_claude_settings_json(claude_home)
    settings = _read_settings(claude_home)

    # Unrelated top-level keys preserved verbatim.
    assert settings["theme"] == "dark-ansi"
    assert settings["enabledPlugins"] == {"some-plugin": True}

    # Unrelated user SessionStart hook still present.
    ss_cmds = [
        h["command"]
        for grp in settings["hooks"]["SessionStart"]
        for h in grp["hooks"]
    ]
    assert '"/home/u/.claude/hooks/other.mjs"' in ss_cmds
    # Consensus SessionStart hook ADDED alongside it.
    assert any("consensus_sessionstart.py" in c for c in ss_cmds)

    # Unrelated PostToolUse event preserved untouched.
    assert settings["hooks"]["PostToolUse"] == existing["hooks"]["PostToolUse"]


# --------------------------------------------------------------------------- #
# Uninstall removes ONLY consensus entries.
# --------------------------------------------------------------------------- #

def test_uninstall_removes_only_consensus_entries(tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    existing = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command",
                            "command": '"/home/u/.claude/hooks/other.mjs"'}]}
            ],
        },
        "theme": "dark-ansi",
    }
    (claude_home / "settings.json").write_text(json.dumps(existing), encoding="utf-8")

    wiz._install_claude_settings_json(claude_home)
    # Consensus entries now present.
    assert _consensus_commands(_read_settings(claude_home))

    statuses = wiz._uninstall_claude_settings_json(claude_home)
    assert any("removed consensus hooks" in s for s in statuses), statuses

    after = _read_settings(claude_home)
    # No consensus entries remain.
    assert _consensus_commands(after) == []
    # The consensus-only events (UserPromptSubmit/PreToolUse/Stop) are gone...
    assert "PreToolUse" not in after["hooks"]
    assert "Stop" not in after["hooks"]
    assert "UserPromptSubmit" not in after["hooks"]
    # ...but the user's unrelated SessionStart hook survives.
    ss_cmds = [
        h["command"]
        for grp in after["hooks"]["SessionStart"]
        for h in grp["hooks"]
    ]
    assert '"/home/u/.claude/hooks/other.mjs"' in ss_cmds
    # Unrelated top-level key preserved.
    assert after["theme"] == "dark-ansi"


def test_uninstall_when_absent_is_noop(tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    statuses = wiz._uninstall_claude_settings_json(claude_home)
    assert statuses  # returns a status line, no crash
    assert not (claude_home / "settings.json").exists()


# --------------------------------------------------------------------------- #
# Wired into --install-claude-code (settings.json activation alongside copy).
# --------------------------------------------------------------------------- #

def test_install_claude_code_activates_hooks_in_settings(tmp_path, monkeypatch):
    fake_home = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    rc = wiz.main(["--install-claude-code"])
    assert rc == 0
    # settings.json now exists and carries consensus hook entries.
    settings = _read_settings(fake_home)
    assert _consensus_commands(settings)
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"):
        assert event in settings["hooks"], event


def test_install_claude_code_settings_idempotent_via_cli(tmp_path, monkeypatch):
    fake_home = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    assert wiz.main(["--install-claude-code"]) == 0
    first = _read_settings(fake_home)
    assert wiz.main(["--install-claude-code"]) == 0
    second = _read_settings(fake_home)
    assert first == second


def test_uninstall_claude_code_cli_removes_hooks(tmp_path, monkeypatch):
    fake_home = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    assert wiz.main(["--install-claude-code"]) == 0
    assert _consensus_commands(_read_settings(fake_home))
    assert wiz.main(["--uninstall-claude-code"]) == 0
    assert _consensus_commands(_read_settings(fake_home)) == []


# --------------------------------------------------------------------------- #
# Fail-soft: an unparseable existing settings.json is left untouched.
# --------------------------------------------------------------------------- #

def test_merge_failsoft_on_unparseable_settings(tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    bad = claude_home / "settings.json"
    bad.write_text("{ not json", encoding="utf-8")

    statuses = wiz._install_claude_settings_json(claude_home)
    assert any("WARN" in s for s in statuses), statuses
    # File left exactly as-is (not clobbered).
    assert bad.read_text(encoding="utf-8") == "{ not json"
