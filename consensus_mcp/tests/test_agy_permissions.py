"""The agy (Antigravity) Gemini seat must be able to read the repo.

Bare --sandbox made agy deny the reviewer's own read_file on platforms that
enforce it, and the gemini approval_mode config key was silently ignored.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from consensus_mcp import _dispatch_gemini as gem
from consensus_mcp import config as cfg
from consensus_mcp._contributor_profiles import load_builtin_profiles
from consensus_mcp.contributors.gemini import GeminiAdapter


def _argv(monkeypatch, tmp_path, binary="C:/tools/agy.exe", **kwargs):
    seen = {}
    monkeypatch.setattr(gem, "_resolve_gemini_bin", lambda _b: binary)

    def fake_run_process(argv, **kw):
        seen["argv"] = argv
        return "{}"

    monkeypatch.setattr(gem, "run_process", fake_run_process)
    gem._invoke_gemini(prompt="x", gemini_bin="agy", model="gemini-4-argon-high",
                       timeout_seconds=600, repo_root=tmp_path, **kwargs)
    return seen["argv"]


def test_default_agy_argv_can_read_but_plan_mode_blocks_writes(monkeypatch, tmp_path):
    argv = _argv(monkeypatch, tmp_path)
    assert argv[argv.index("--mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" in argv
    assert "--sandbox" in argv


@pytest.mark.parametrize("mode", ["auto", "accept-edits"])
def test_prompts_are_never_skipped_outside_plan_mode(monkeypatch, tmp_path, mode):
    argv = _argv(monkeypatch, tmp_path, approval_mode=mode, skip_permissions=True)
    assert argv[argv.index("--mode") + 1] == mode
    assert "--dangerously-skip-permissions" not in argv


def test_skip_permissions_can_be_turned_off(monkeypatch, tmp_path):
    argv = _argv(monkeypatch, tmp_path, approval_mode="plan", skip_permissions=False)
    assert "--dangerously-skip-permissions" not in argv


def test_invalid_mode_is_refused(monkeypatch, tmp_path):
    with pytest.raises(gem.GeminiInvocationError):
        _argv(monkeypatch, tmp_path, approval_mode="yolo")


def test_legacy_gemini_cli_branch_unchanged(monkeypatch, tmp_path):
    argv = _argv(monkeypatch, tmp_path, binary="C:/tools/gemini.exe")
    assert argv[argv.index("--approval-mode") + 1] == "plan"
    assert "--mode" not in argv and "--dangerously-skip-permissions" not in argv


def test_permissions_resolve_cli_then_profile_then_default():
    profile = load_builtin_profiles()["gemini"]
    assert profile["approval_mode"] == "plan" and profile["skip_permissions"] is True
    ns = SimpleNamespace(profile_data={"approval_mode": "auto", "skip_permissions": False},
                         approval_mode=None, skip_permissions=None)
    assert gem._agy_permissions(ns) == ("auto", False)
    ns.approval_mode, ns.skip_permissions = "plan", True
    assert gem._agy_permissions(ns) == ("plan", True)
    assert gem._agy_permissions(SimpleNamespace(profile_data=None)) == ("plan", True)


def test_cli_flags_parse():
    import argparse
    parser = argparse.ArgumentParser()
    gem._configure_parser(parser)
    assert parser.parse_args([]).skip_permissions is None
    ns = parser.parse_args(["--approval-mode", "auto", "--no-skip-permissions"])
    assert ns.approval_mode == "auto" and ns.skip_permissions is False


def test_adapter_forwards_approval_mode_and_skip_permissions():
    argv = GeminiAdapter.__new__(GeminiAdapter)._extra_argv(
        {"command": "agy", "model": "m", "approval_mode": "auto", "skip_permissions": False})
    assert argv[argv.index("--approval-mode") + 1] == "auto"
    assert "--no-skip-permissions" in argv


def test_config_keeps_gemini_approval_mode():
    raw = cfg.default_config()
    raw["contributors"]["adapters"]["gemini"]["approval_mode"] = "auto"
    assert cfg.normalize(raw)["contributors"]["adapters"]["gemini"]["approval_mode"] == "auto"


# ---------- model names checked against agy's roster ----------

_AGY_MODELS_OUTPUT = (
    "Fetching available models...\n"
    "gemini-4-argon-medium\tGemini 4 Argon (Medium)\n"
    "gemini-4-argon-high\tGemini 4 Argon (High)\n"
    "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
)


def test_agy_roster_parses_the_models_listing(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=_AGY_MODELS_OUTPUT, stderr="")

    monkeypatch.setattr(gem.subprocess, "run", fake_run)
    roster = gem._agy_roster("agy")
    assert calls == [["agy", "models"]]
    assert roster == {"gemini-4-argon-medium": "Gemini 4 Argon (Medium)",
                      "gemini-4-argon-high": "Gemini 4 Argon (High)",
                      "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)"}


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=1, stdout=_AGY_MODELS_OUTPUT, stderr="boom"),
    SimpleNamespace(returncode=0, stdout="no tabbed rows here\n", stderr=""),
])
def test_unreadable_roster_is_none(monkeypatch, result):
    monkeypatch.setattr(gem.subprocess, "run", lambda argv, **kw: result)
    assert gem._agy_roster("agy") is None


def test_roster_lookup_failure_is_none(monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(gem.subprocess, "run", boom)
    assert gem._agy_roster("agy") is None


def _roster(_binary):
    return {"gemini-4-argon-high": "Gemini 4 Argon (High)", "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)"}


@pytest.mark.parametrize("configured, expected", [
    ("gemini-4-argon-high", "gemini-4-argon-high"),
    ("Gemini 4 Argon (High)", "gemini-4-argon-high"),
    ("gemini 3.8 flash (medium)", "gemini-3.8-flash-medium"),
])
def test_model_ids_and_display_names_resolve(configured, expected):
    assert gem._resolve_agy_model("agy", configured, roster_fn=_roster) == expected


def test_retired_model_fails_up_front_with_the_setting_to_change():
    with pytest.raises(gem.GeminiInvocationError) as err:
        gem._resolve_agy_model("agy", "Gemini 3.5 Flash (Medium)", roster_fn=_roster)
    message = str(err.value)
    assert "Gemini 3.5 Flash (Medium)" in message and "gemini-4-argon-high" in message
    assert "contributors.profiles.gemini.model" in message


def test_unknown_roster_passes_the_model_through():
    assert gem._resolve_agy_model("agy", "anything", roster_fn=lambda _b: None) == "anything"


def test_dispatch_path_resolves_agy_display_names(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(gem, "_resolve_gemini_bin", lambda _b: "C:/tools/agy.exe")
    monkeypatch.setattr(gem, "_agy_roster", _roster)

    def fake_retry(**kwargs):
        seen.update(kwargs)
        return "{}", {}

    monkeypatch.setattr(gem, "_invoke_gemini_with_retry", fake_retry)
    ns = SimpleNamespace(profile_data={}, approval_mode=None, skip_permissions=None, gemini_bin="agy",
                         timeout_seconds=60, stall_silence_seconds=180, goal_packet_data={}, mode="review")
    gem._provider_invoke(prompt="x", model="Gemini 4 Argon (High)", repo_root=tmp_path,
                         schema_path=None, ns=ns, log_path=None, anchors=None)
    assert seen["model"] == "gemini-4-argon-high"
    assert seen["approval_mode"] == "plan" and seen["skip_permissions"] is True


def test_non_agy_binary_skips_the_roster(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(gem, "_resolve_gemini_bin", lambda _b: "/usr/bin/gemini")
    monkeypatch.setattr(gem, "_agy_roster", lambda _b: pytest.fail("roster must not be read for gemini-cli"))
    monkeypatch.setattr(gem, "_invoke_gemini_with_retry", lambda **kw: (seen.update(kw), ("{}", {}))[1])
    ns = SimpleNamespace(profile_data={}, approval_mode=None, skip_permissions=None, gemini_bin="gemini",
                         timeout_seconds=60, stall_silence_seconds=180, goal_packet_data={}, mode="review")
    gem._provider_invoke(prompt="x", model="Gemini 4 Argon (High)", repo_root=tmp_path,
                         schema_path=None, ns=ns, log_path=None, anchors=None)
    assert seen["model"] == "Gemini 4 Argon (High)"
