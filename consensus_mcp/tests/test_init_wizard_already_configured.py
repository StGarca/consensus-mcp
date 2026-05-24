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
    ("2", "reconfigure"),
    ("3", "force"),
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
    monkeypatch.setattr(builtins, "input", _stub_input(["x", "9", "3"]))
    assert wiz._prompt_existing_config_action(tmp_path / "c.yaml") == "force"


def test_prompt_existing_config_action_ctrl_c_propagates(tmp_path, monkeypatch):
    def _kbi(prompt=""):
        raise KeyboardInterrupt
    monkeypatch.setattr(builtins, "input", _kbi)
    with pytest.raises(KeyboardInterrupt):
        wiz._prompt_existing_config_action(tmp_path / "c.yaml")
