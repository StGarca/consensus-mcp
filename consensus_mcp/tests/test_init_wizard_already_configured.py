import builtins
import pytest

import consensus_mcp._init_wizard as wiz


def _stub_input(values):
    it = iter(values)
    def _fake(prompt=""):
        return next(it)
    return _fake


def test_token_constant_value():
    # The contract token is a fixed string with no variable data.
    assert wiz.ALREADY_CONFIGURED_TOKEN == "STATUS: already-configured"


@pytest.mark.parametrize("raw,expected", [
    ("1", "leave"),
    ("", "leave"),
    ("2", "repair"),
    ("3", "reconfigure"),
    ("4", "force"),
])
def test_prompt_existing_config_action_choices(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setattr(builtins, "input", _stub_input([raw]))
    assert wiz._prompt_existing_config_action(tmp_path / ".consensus" / "config.yaml") == expected


def test_prompt_existing_config_action_eof_defaults_to_leave(tmp_path, monkeypatch):
    def _eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", _eof)
    assert wiz._prompt_existing_config_action(tmp_path / "c.yaml") == "leave"


def test_prompt_existing_config_action_reprompts_on_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(builtins, "input", _stub_input(["x", "9", "4"]))
    assert wiz._prompt_existing_config_action(tmp_path / "c.yaml") == "force"


def test_prompt_existing_config_action_ctrl_c_propagates(tmp_path, monkeypatch):
    def _kbi(prompt=""):
        raise KeyboardInterrupt
    monkeypatch.setattr(builtins, "input", _kbi)
    with pytest.raises(KeyboardInterrupt):
        wiz._prompt_existing_config_action(tmp_path / "c.yaml")


import yaml
import consensus_mcp.config as cfg


def _write_existing_config(tmp_path):
    d = tmp_path / ".consensus"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump(cfg.default_config()), encoding="utf-8")
    return d / "config.yaml"


def test_non_tty_existing_config_emits_token_and_exit_4(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _write_existing_config(tmp_path)
    rc = wiz.main([])
    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == wiz.ALREADY_CONFIGURED_TOKEN
    assert "already configured" in captured.err.lower()


def test_dry_run_existing_non_tty_emits_token(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _write_existing_config(tmp_path)
    rc = wiz.main(["--dry-run"])
    assert rc == 4
    assert capsys.readouterr().out.splitlines()[0] == wiz.ALREADY_CONFIGURED_TOKEN


def test_token_absent_when_reconfigure_flag(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _write_existing_config(tmp_path)
    rc = wiz.main(["--reconfigure", "--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert wiz.ALREADY_CONFIGURED_TOKEN not in capsys.readouterr().out


def test_token_absent_when_force_flag(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _write_existing_config(tmp_path)
    rc = wiz.main(["--force", "--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert wiz.ALREADY_CONFIGURED_TOKEN not in capsys.readouterr().out


def test_tty_menu_leave_returns_0_without_writing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(wiz, "_prompt_existing_config_action", lambda _p: "leave")
    cfg_path = _write_existing_config(tmp_path)
    original = cfg_path.read_text(encoding="utf-8")
    rc = wiz.main([])
    assert rc == 0
    assert cfg_path.read_text(encoding="utf-8") == original  # untouched
    assert wiz.ALREADY_CONFIGURED_TOKEN not in capsys.readouterr().out


def test_tty_menu_force_overwrites(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(wiz, "_prompt_existing_config_action", lambda _p: "force")
    cfg_path = tmp_path / ".consensus" / "config.yaml"
    cfg_path.parent.mkdir()
    cfg_path.write_text("schema_version: 1\n# user edit\n", encoding="utf-8")
    rc = wiz.main(["--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert "# user edit" not in cfg_path.read_text(encoding="utf-8")  # overwritten


def test_tty_menu_ctrl_c_returns_1(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    def _kbi(_p):
        raise KeyboardInterrupt
    monkeypatch.setattr(wiz, "_prompt_existing_config_action", _kbi)
    _write_existing_config(tmp_path)
    rc = wiz.main([])
    assert rc == 1
    assert "aborted by user" in capsys.readouterr().err


def test_tty_menu_reconfigure_stays_interactive(tmp_path, monkeypatch):
    """Menu 'reconfigure' must re-prompt (interactive), not silently accept
    defaults — guards against the accept_defaults regression."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(wiz, "_prompt_existing_config_action", lambda _p: "reconfigure")
    _write_existing_config(tmp_path)
    calls = {"n": 0}
    def _counting_input(prompt=""):
        calls["n"] += 1
        return ""  # accept the default for every re-prompt
    monkeypatch.setattr(builtins, "input", _counting_input)
    rc = wiz.main([])
    assert rc == 0
    assert calls["n"] > 0  # would be 0 if reconfigure were forced non-interactive


def test_force_beats_reconfigure(tmp_path, capsys, monkeypatch):
    """Both flags together: --force wins (overwrite, no reconfigure diff)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    cfg_path = tmp_path / ".consensus" / "config.yaml"
    cfg_path.parent.mkdir()
    cfg_path.write_text("schema_version: 1\n# user edit\n", encoding="utf-8")
    rc = wiz.main(["--force", "--reconfigure", "--non-interactive",
                   "--accept-defaults", "--contributors", "claude,codex,gemini"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# user edit" not in cfg_path.read_text(encoding="utf-8")  # overwritten
    assert "reconfigure diff" not in out  # reconfigure path suppressed


def test_repair_flag_runs_engine_and_exits_0(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    _write_existing_config(tmp_path)
    rc = wiz.main(["--repair"])
    assert rc == 0
    out = capsys.readouterr().out
    assert any(line.startswith(("OK:", "REPAIRED:")) for line in out.splitlines())


def test_repair_does_not_emit_already_configured_token(tmp_path, capsys, monkeypatch):
    """Gate carve-out: --repair must NOT trip the v1.29.0 existing-config gate."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    _write_existing_config(tmp_path)
    rc = wiz.main(["--repair"])
    out = capsys.readouterr().out
    assert wiz.ALREADY_CONFIGURED_TOKEN not in out
    assert rc != 4


def test_repair_missing_config_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    rc = wiz.main(["--repair"])
    assert rc == 2


def test_repair_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    _write_existing_config(tmp_path)
    rc = wiz.main(["--repair", "--dry-run"])
    assert not (tmp_path / ".mcp.json").exists()  # previewed, not written


def test_tty_menu_repair_runs_repair(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(wiz, "_prompt_existing_config_action", lambda _p: "repair")
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    _write_existing_config(tmp_path)
    rc = wiz.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert any(l.startswith(("OK:", "REPAIRED:")) for l in out.splitlines())


from pathlib import Path


def _ext_dir():
    return Path(wiz.__file__).parent / "claude_extensions"


def test_contract_token_present_in_skill_and_command():
    """The skill/command matchers MUST stay in sync with the binary token.
    If the token string changes, these docs must change too — this test fails
    on drift, which is the whole point of the binary<->skill contract."""
    token = wiz.ALREADY_CONFIGURED_TOKEN
    skill = (_ext_dir() / "skills" / "consensus" / "SKILL.md").read_text(encoding="utf-8")
    command = (_ext_dir() / "commands" / "consensus-init.md").read_text(encoding="utf-8")
    assert token in skill, "SKILL.md must reference the exact already-configured token"
    assert token in command, "consensus-init.md must reference the exact token"
    # exit code 4 is the paired half of the contract — keep it documented too.
    assert "exit code 4" in skill.lower() or "exits with code 4" in skill.lower()
    # consensus-init.md is what Claude Code reads when dispatched — test it symmetrically.
    assert "exit code 4" in command.lower() or "exits with code 4" in command.lower()


def test_repair_option_documented_in_skill_and_command():
    skill = (_ext_dir() / "skills" / "consensus" / "SKILL.md").read_text(encoding="utf-8")
    command = (_ext_dir() / "commands" / "consensus-init.md").read_text(encoding="utf-8")
    assert "--repair" in skill and "Verify / repair" in skill
    assert "--repair" in command
