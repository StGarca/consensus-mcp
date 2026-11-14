"""Profile roster contracts; all dispatcher/model probes use fakes."""
from copy import deepcopy
import importlib
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from consensus_mcp import _contributor_profiles as profiles
from consensus_mcp import _init_wizard as wizard
from consensus_mcp import _tier_router as tiers
from consensus_mcp import config


@pytest.mark.parametrize("field,value", [
    ("model", None), ("model", " "), ("model", 1),
    ("fallback_models", "model"), ("fallback_models", None),
    ("fallback_models", [""]), ("fallback_models", [1]),
    ("default_enabled", "true"), ("default_enabled", 1),
    ("default_enabled", None),
])
def test_invalid_roster_fields(field, value):
    profile = deepcopy(profiles.load_builtin_profiles()["codex"])
    profile[field] = value
    with pytest.raises(ValueError, match=field):
        profiles.validate_profile("codex", profile)


def test_roster_defaults_and_enabled_panel(monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", raising=False)
    builtins = profiles.load_builtin_profiles()
    assert config._default_independent_enabled() == ["claude", "codex", "kimi", "grok", "gemini"]
    for name, profile in builtins.items():
        assert isinstance(profile["fallback_models"], list)
        assert isinstance(profile["default_enabled"], bool)
        profiles.validate_profile(name, profile)
    builtins["grok"]["default_enabled"] = False
    del builtins["kimi"]["default_enabled"]
    monkeypatch.setattr(profiles, "load_builtin_profiles", lambda: builtins)
    assert "grok" not in config._default_independent_enabled()
    assert "kimi" in config._default_independent_enabled()


def test_deepseek_default_requires_endpoint(monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", raising=False)
    assert "deepseek" not in config.default_config()["contributors"]["enabled"]
    monkeypatch.setenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", "http://localhost:8000")
    assert config.default_config()["contributors"]["enabled"][-1] == "deepseek"


def test_legacy_models_follow_profiles(tmp_path):
    builtins = profiles.load_builtin_profiles()
    for name, adapter in config.synthesize_legacy_config(tmp_path)["contributors"]["adapters"].items():
        assert adapter["model"] == builtins[name]["model"]


@pytest.mark.parametrize("tier", tiers._TIERS)
def test_tiers_keep_models_and_project_pins(tier):
    builtins = profiles.load_builtin_profiles()
    decision = tiers.effective_tier(tier)
    cfg = config.default_config()
    for name, settings in decision["model_settings"].items():
        assert settings["model"] == builtins[name]["model"]
        cfg["contributors"]["adapters"][name]["model"] = f"pinned-{name}"
    result = tiers.apply_tier_config(cfg, decision)
    for name in decision["model_settings"]:
        assert result["contributors"]["adapters"][name]["model"] == f"pinned-{name}"


@pytest.mark.parametrize("name", ["codex", "grok", "kimi", "gemini"])
def test_dispatcher_profile_project_and_explicit_model(tmp_path, monkeypatch, name):
    module = importlib.import_module(f"consensus_mcp._dispatch_{name}")
    profile = deepcopy(profiles.load_builtin_profiles()[name])
    assert getattr(module, f"_DEFAULT_{name.upper()}_MODEL") == profile["model"]
    assert config.resolve_reviewer_model(tmp_path, name) == profile["model"]
    profile["model"] = "profile-override"
    path = tmp_path / ".consensus" / "config.yaml"
    path.parent.mkdir()
    raw = {"contributors": {"profiles": {name: profile}}}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert config.resolve_reviewer_model(tmp_path, name) == profile["model"]
    raw["contributors"]["adapters"] = {name: {"model": "project-pin"}}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert config.resolve_reviewer_model(tmp_path, name) == "project-pin"
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(module, "_resolve_repo_root", lambda: tmp_path)
    events = []
    monkeypatch.setattr(module, "_log_dispatch", lambda path, event: events.append(event))
    # Missing goal packet stops before any CLI invocation.
    argv = ["--goal-packet", "missing.yaml", "--iteration-dir", "iteration-test"]
    module.main(argv)
    start = next(event for event in events if event["event"] == "dispatch_start")
    assert start["model"] == "project-pin"
    events.clear()
    module.main([*argv, "--model", "explicit-pin"])
    start = next(event for event in events if event["event"] == "dispatch_start")
    assert start["model"] == "explicit-pin"
    events.clear()
    raw["contributors"].pop("adapters")
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    module.main(argv)
    start = next(event for event in events if event["event"] == "dispatch_start")
    assert start["model"] == "profile-override"


@pytest.mark.parametrize("alias", ["Kimi K3.1", "kimi-k3.1"])
def test_kimi_model_alias(alias):
    from consensus_mcp._dispatch_kimi import _resolve_kimi_model

    assert _resolve_kimi_model(alias) == profiles.load_builtin_profiles()["kimi"]["model"]


@pytest.mark.parametrize("disabled", [None, "grok"])
def test_check_models_calls_enabled_models_once(tmp_path, monkeypatch, disabled):
    cfg = config.default_config()
    if disabled:
        cfg["contributors"]["enabled"].remove(disabled)
    cfg["contributors"]["adapters"]["codex"]["model"] = "project-pin"
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    path = tmp_path / ".consensus" / "config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setattr(wizard, "_detect_repo_root", lambda: tmp_path)
    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(wizard.subprocess, "run", run)
    assert wizard.main(["--check-models"]) == 0
    assert len(calls) == len(cfg["contributors"]["enabled"])
    assert {argv[argv.index("--model") + 1] for argv, _ in calls} == {
        cfg["contributors"]["adapters"][name]["model"] for name in cfg["contributors"]["enabled"]
    }
    assert all(argv[0].startswith("/fake/") for argv, _ in calls)
    gemini = next(argv for argv, _ in calls if argv[0] == "/fake/agy")
    assert gemini[gemini.index("--model") + 1] == profiles.load_builtin_profiles()["gemini"]["model"]
    assert all(kwargs["cwd"] == str(tmp_path) for _, kwargs in calls)


@pytest.mark.parametrize("failure", ["exit", "empty", "timeout", "missing"])
def test_check_models_failures_do_not_retry(tmp_path, monkeypatch, failure):
    cfg = config.default_config()
    cfg["contributors"]["enabled"] = ["codex"]
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)
        if failure == "missing":
            raise FileNotFoundError("fake missing CLI")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout="", stderr="")

    monkeypatch.setattr(wizard.subprocess, "run", run)
    assert wizard._check_models(tmp_path, cfg) == 2
    assert len(calls) == 1
