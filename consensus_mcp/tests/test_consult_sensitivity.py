"""Sensitive-data gate: seats marked sensitive_data_allowed: false (Kimi, Qwen)
never review a project the user says holds sensitive data, and the consult asks
when nobody has said."""
from __future__ import annotations

import contextlib
import io
import json

import pytest
import yaml

from consensus_mcp import _consult_panel as cp
from consensus_mcp import _start_consult as sc
from consensus_mcp._contributor_profiles import load_builtin_profiles
from consensus_mcp.tests.test_consult_panel import configure, dispatchers, project  # noqa: F401


def consult(project, **kwargs):
    return cp.consult_panel("iteration-test", repo_root=project, **kwargs)


def summary(project):
    path = project / "consensus-state" / "active" / "iteration-test" / "consult-summary.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_kimi_and_qwen_are_the_restricted_profiles():
    restricted = sorted(name for name, profile in load_builtin_profiles().items()
                        if profile.get("sensitive_data_allowed") is False)
    assert restricted == ["kimi", "qwen"]


def test_unknown_sensitivity_asks_before_launching_anything(project, dispatchers):
    _, launches, _ = dispatchers
    result = consult(project)
    assert result["ok"] is False and result["status"] == "needs_sensitivity_answer"
    assert result["restricted_seats"] == ["kimi"]
    assert "sensitive data" in result["question"] and "kimi" in result["question"]
    assert launches == []


def test_plan_pass_asks_before_scaffolding_or_launching(project, dispatchers):
    directory = project / "consensus-state" / "active" / "iteration-test"
    (directory / "consult-summary.yaml").write_text(yaml.safe_dump({
        "tier": "standard", "seats": [{"seat": "codex"}, {"seat": "kimi"}]}), encoding="utf-8")
    result = consult(project, pass_kind="plan")
    assert result["ok"] is False and result["status"] == "needs_sensitivity_answer"
    assert result["restricted_seats"] == ["kimi"] and dispatchers[1] == []
    assert not (directory.parent / "iteration-test-plan-1").exists()


@pytest.mark.parametrize("answer, expected", [("yes", ["codex", "gemini"]), ("no", ["codex", "kimi", "gemini"])])
def test_plan_pass_applies_sensitive_answer_to_same_panel(project, dispatchers, answer, expected):
    options, launches, _ = dispatchers
    assert consult(project, seats=["codex", "kimi", "gemini"], sensitive_data="no")["ok"]
    parent = project / "consensus-state" / "active" / "iteration-test"
    (parent / "converged-plan.yaml").write_text("steps: []\n", encoding="utf-8")
    (parent / "dispositions.yaml").write_text("dispositions: []\n", encoding="utf-8")
    for seat in ("codex", "kimi", "gemini"):
        options[seat] = {"packet": {"findings": [], "goal_satisfied": True}}
    launches.clear()
    result = consult(project, pass_kind="plan", sensitive_data=answer)
    assert result["ok"] is True and launches == expected
    assert result["sensitive_data"] == answer
    assert result["excluded_seats"] == (["kimi"] if answer == "yes" else [])


def test_no_question_when_no_restricted_seat_is_on_the_panel(project, dispatchers):
    _, launches, _ = dispatchers
    result = consult(project, seats=["claude", "codex"])
    assert result["ok"] is True and launches == ["claude", "codex"]
    assert summary(project)["sensitive_data"] is None


def test_yes_excludes_kimi_and_qwen_for_that_run(project, dispatchers, monkeypatch):
    _, launches, _ = dispatchers
    monkeypatch.setenv("CONSENSUS_MCP_QWEN_BASE_URL", "http://unused.invalid")
    result = consult(project, sensitive_data="yes")
    assert result["ok"] is True
    assert launches == ["claude", "codex", "grok", "gemini"]
    recorded = summary(project)
    assert recorded["sensitive_data"] == "yes" and recorded["sensitivity_source"] == "argument"
    assert recorded["excluded_seats"] == ["kimi", "qwen"]
    # Explicitly requested restricted seats are excluded too.
    launches.clear()
    result = consult(project, seats=["kimi", "codex"], sensitive_data="yes")
    assert result["ok"] is True and launches == ["codex"]


def test_no_includes_kimi_and_qwen(project, dispatchers, monkeypatch):
    _, launches, _ = dispatchers
    monkeypatch.setenv("CONSENSUS_MCP_QWEN_BASE_URL", "http://unused.invalid")
    result = consult(project, sensitive_data="no")
    assert result["ok"] is True
    assert launches == ["claude", "codex", "kimi", "grok", "gemini", "qwen"]
    recorded = summary(project)
    assert recorded["sensitive_data"] == "no" and recorded["excluded_seats"] == []


def test_children_receive_the_answer(project, dispatchers, monkeypatch):
    seen = []
    original = cp.subprocess.Popen

    def launch(argv, **kwargs):
        seen.append(kwargs["env"]["CONSENSUS_MCP_SENSITIVE_DATA"])
        return original(argv, **kwargs)

    monkeypatch.setattr(cp.subprocess, "Popen", launch)
    assert consult(project, seats=["codex"], sensitive_data="yes")["ok"] is True
    assert seen == ["yes"]


@pytest.mark.parametrize("declared, expected", [(True, "yes"), (False, "no"), ("yes", "yes"), ("no", "no")])
def test_project_config_declaration_answers_without_asking(project, dispatchers, declared, expected):
    _, launches, _ = dispatchers
    configure(project, {"schema_version": 1, "sensitive_data": declared})
    result = consult(project)
    assert result["ok"] is True
    assert ("kimi" in launches) is (expected == "no")
    recorded = summary(project)
    assert recorded["sensitive_data"] == expected and recorded["sensitivity_source"] == "config"


def test_argument_overrides_project_declaration(project, dispatchers):
    _, launches, _ = dispatchers
    configure(project, {"schema_version": 1, "sensitive_data": False})
    assert consult(project, sensitive_data="yes")["ok"] is True
    assert "kimi" not in launches


@pytest.mark.parametrize("kwargs", [
    {"sensitive_data": "maybe"},
    {"seats": ["kimi"], "sensitive_data": "yes"},
    {"tier": "quick", "seats": ["kimi", "codex"], "sensitive_data": "yes"},
])
def test_bad_answer_or_empty_panel_launches_nothing(project, dispatchers, kwargs):
    _, launches, _ = dispatchers
    result = consult(project, **kwargs)
    assert result["ok"] is False and launches == []


def test_cli_asks_with_exit_code_2(monkeypatch, capsys):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if kwargs["sensitive_data"] is None:
            return {"ok": False, "status": "needs_sensitivity_answer", "question": "Sensitive? yes or no"}
        return {"ok": True, "status": "success"}

    monkeypatch.setattr(cp, "consult_panel", fake)
    assert cp.main(["--iteration", "i"]) == 2
    assert "Sensitive?" in capsys.readouterr().err
    assert cp.main(["--iteration", "i", "--sensitive-data", "yes"]) == 0
    assert calls[-1]["sensitive_data"] == "yes"
    with pytest.raises(SystemExit):
        cp.main(["--iteration", "i", "--sensitive-data", "maybe"])


def test_mcp_tool_accepts_the_answer():
    from consensus_mcp import server

    descriptor = next(tool for tool in server.registry.list_tools() if tool["name"] == "consensus.consult_panel")
    assert descriptor["inputSchema"]["properties"]["sensitive_data"]["enum"] == ["yes", "no", None]
    assert "needs_sensitivity_answer" in descriptor["description"]


def _runner_project(tmp_path, config):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".consensus").mkdir()
    (tmp_path / ".consensus" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    iter_dir = tmp_path / "consensus-state" / "active" / "iter-sensitive-gate"
    iter_dir.mkdir(parents=True)
    goal = iter_dir / "goal_packet.yaml"
    goal.write_text(
        "schema_version: 1\npilot_id: sensitive-gate\n"
        "goal:\n  summary: t\n  desired_end_state: t\n  non_goals: []\n"
        "allowed_files: []\nacceptance_gates:\n  - id: A\n    description: x\n    check: 'true'\n"
        "stop_conditions: []\noperator_escalation_triggers: []\n"
        "authorization:\n  authorized_by: t\n  authorized_at_utc: '2026-05-26T00:00:00Z'\n",
        encoding="utf-8")
    return goal, iter_dir


@pytest.mark.parametrize("config, env, reason", [
    ({"sensitive_data": True}, None, "marked sensitive_data: yes"),
    ({}, "yes", "marked sensitive_data: yes"),
    ({}, None, "nobody has said"),
    ({"sensitive_data": "maybe"}, None, "nobody has said"),
])
def test_restricted_dispatcher_refuses_unless_explicitly_not_sensitive(tmp_path, monkeypatch, config, env, reason):
    from consensus_mcp import _dispatch_kimi

    goal, iter_dir = _runner_project(tmp_path, config)
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    if env is None:
        monkeypatch.delenv("CONSENSUS_MCP_SENSITIVE_DATA", raising=False)
    else:
        monkeypatch.setenv("CONSENSUS_MCP_SENSITIVE_DATA", env)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _dispatch_kimi.main(["--goal-packet", str(goal), "--iteration-dir", str(iter_dir)])
    assert rc == 3
    result = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert result["error_type"] == "SensitiveDataPolicy" and reason in result["error"]


def test_scaffold_tells_the_host_to_ask_unless_declared(tmp_path):
    cfg = tmp_path / ".consensus" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(yaml.safe_dump({"schema_version": 1}), encoding="utf-8")
    res = sc.start_consult("Q?", scope_glob="src/x.py", reviewers=["codex", "kimi"], repo_root=tmp_path)
    assert res["ok"] is True
    step = res["next_steps"]["0_ask_about_sensitive_data"]
    assert "--sensitive-data yes" in step and "kimi" in step
    assert list(res["next_steps"])[0] == "0_ask_about_sensitive_data"
    cfg.write_text(yaml.safe_dump({"schema_version": 1, "sensitive_data": False}), encoding="utf-8")
    res = sc.start_consult("Q2?", scope_glob="src/x.py", reviewers=["codex", "kimi"], repo_root=tmp_path)
    assert "0_ask_about_sensitive_data" not in res["next_steps"]
    res = sc.start_consult("Q3?", scope_glob="src/x.py", reviewers=["codex", "gemini"], repo_root=tmp_path)
    assert "0_ask_about_sensitive_data" not in res["next_steps"]


def test_default_config_panel_adds_qwen_only_when_configured(monkeypatch):
    from consensus_mcp import config

    monkeypatch.delenv("CONSENSUS_MCP_QWEN_BASE_URL", raising=False)
    monkeypatch.delenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", raising=False)
    assert "qwen" not in config.default_config()["contributors"]["enabled"]
    monkeypatch.setenv("CONSENSUS_MCP_QWEN_BASE_URL", "http://unused.invalid")
    assert config.default_config()["contributors"]["enabled"][-1] == "qwen"


# ---------- engine path (consensus.run_iteration) ----------

def _engine_config(**extra):
    return {"contributors": {"enabled": ["codex", "kimi"]}, **extra}


def test_engine_asks_when_restricted_seat_is_enabled_and_unanswered(monkeypatch):
    from consensus_mcp import _engine_factory as factory

    monkeypatch.delenv("CONSENSUS_MCP_SENSITIVE_DATA", raising=False)
    with pytest.raises(factory.SensitivityAnswerRequired) as err:
        factory.build_adapters(_engine_config())
    assert err.value.seats == ["kimi"] and "sensitive data" in err.value.question
    assert str(err.value).startswith("needs_sensitivity_answer")


@pytest.mark.parametrize("extra, env, built", [
    ({"sensitive_data": True}, None, ["codex"]),
    ({"sensitive_data": False}, None, ["codex", "kimi"]),
    ({}, "yes", ["codex"]),
    ({}, "no", ["codex", "kimi"]),
])
def test_engine_applies_the_answer(monkeypatch, extra, env, built):
    from consensus_mcp import _engine_factory as factory

    if env is None:
        monkeypatch.delenv("CONSENSUS_MCP_SENSITIVE_DATA", raising=False)
    else:
        monkeypatch.setenv("CONSENSUS_MCP_SENSITIVE_DATA", env)
    assert sorted(factory.build_adapters(_engine_config(**extra))) == built


def test_engine_with_only_restricted_seats_and_yes_fails(monkeypatch):
    from consensus_mcp import _engine_factory as factory

    monkeypatch.setenv("CONSENSUS_MCP_SENSITIVE_DATA", "yes")
    with pytest.raises(factory.EngineFactoryError, match="no contributors remain"):
        factory.build_adapters({"contributors": {"enabled": ["kimi"]}})


def test_project_config_cannot_relax_the_builtin_restriction(project, dispatchers, monkeypatch):
    from consensus_mcp import _engine_factory as factory

    _, launches, _ = dispatchers
    relaxed = {"schema_version": 1, "contributors": {
        "enabled": ["codex", "kimi"],
        "profiles": {"kimi": {"sensitive_data_allowed": True}},
        "adapters": {"kimi": {"sensitive_data_allowed": True}}}}
    configure(project, relaxed)
    result = consult(project)
    assert result["status"] == "needs_sensitivity_answer" and result["restricted_seats"] == ["kimi"]
    assert consult(project, sensitive_data="yes")["ok"] is True and launches == ["codex"]
    monkeypatch.setenv("CONSENSUS_MCP_SENSITIVE_DATA", "yes")
    assert sorted(factory.build_adapters(relaxed)) == ["codex"]


# ---------- endpoint-based restriction (DeepSeek trusted only off China/Singapore hosts) ----------

@pytest.mark.parametrize("base_url, restricted", [
    ("http://192.168.0.10:8000/v1", False),           # local server
    ("http://localhost:8000/v1", False),
    ("https://api.together.example/v1", False),        # other-region provider
    ("https://api.deepseek.com/v1", True),             # DeepSeek's own API
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", True),  # Alibaba Singapore
    ("https://example.qwencloudapi.com/compatible-mode/v1", True),
    ("https://api.moonshot.cn/v1", True),
    ("https://llm.example.cn/v1", True),
])
def test_deepseek_restricted_only_on_china_or_singapore_endpoints(monkeypatch, base_url, restricted):
    from consensus_mcp import _sensitivity

    monkeypatch.setenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", base_url)
    profile = load_builtin_profiles()["deepseek"]
    assert _sensitivity.is_restricted(profile, "deepseek") is restricted


def test_panel_asks_when_deepseek_points_at_its_own_api(project, dispatchers, monkeypatch):
    _, launches, _ = dispatchers
    monkeypatch.setenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    result = consult(project, seats=["deepseek", "codex"])
    assert result["status"] == "needs_sensitivity_answer" and result["restricted_seats"] == ["deepseek"]
    assert consult(project, seats=["deepseek", "codex"], sensitive_data="yes")["ok"] is True
    assert launches == ["codex"]
    launches.clear()
    monkeypatch.setenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", "http://192.168.0.10:8000/v1")
    assert consult(project, seats=["deepseek", "codex"])["ok"] is True
    assert launches == ["deepseek", "codex"]


# --- fail-closed parsing (found by the live v2.5 plan pass) -----------------

import pytest as _pytest

from consensus_mcp import _sensitivity as _policy


@_pytest.mark.parametrize("base", [
    "api.deepseek.com/v1",
    "dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "  https://api.moonshot.cn/v1  ",
    "API.DEEPSEEK.COM.",
])
def test_endpoint_without_scheme_is_still_restricted(base):
    assert _policy.endpoint_restricted({"base_url": base})
    assert _policy.is_restricted({"base_url": base}, "custom-http-seat")


@_pytest.mark.parametrize("base", ["http://10.0.0.5:8000/v1", "localhost:8000/v1",
                                    "https://example.org/v1"])
def test_trusted_endpoints_stay_allowed(base):
    assert not _policy.is_restricted({"base_url": base}, "custom-http-seat")


def test_unparseable_endpoint_fails_closed():
    assert _policy.endpoint_restricted({"base_url": "http://[::1"})


@_pytest.mark.parametrize("value", [False, "false", "False", "no", " NO ", 0, "0", None, "maybe"])
def test_allowed_flag_restricts_unless_clearly_yes(value):
    assert _policy.is_restricted({"sensitive_data_allowed": value}, "custom-seat")


@_pytest.mark.parametrize("value", [True, "true", "yes", " Yes "])
def test_allowed_flag_yes_allows(value):
    assert not _policy.is_restricted({"sensitive_data_allowed": value}, "custom-seat")


def test_missing_flag_and_missing_profile_allow():
    assert not _policy.is_restricted({}, "custom-seat")
    assert not _policy.is_restricted(None, "custom-seat")


@_pytest.mark.parametrize("profile", ["kimi", ["x"], 42])
def test_malformed_profile_fails_closed(profile):
    assert _policy.is_restricted(profile, "custom-seat")


def test_builtin_restricted_seats_unchanged():
    assert {"kimi", "qwen"} <= _policy.builtin_restricted()
    assert "deepseek" not in _policy.builtin_restricted()
