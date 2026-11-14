from __future__ import annotations

import importlib
import json

import yaml

from consensus_mcp import _contributor_profiles as profiles
from consensus_mcp import _engine_factory, _init_wizard, _start_consult, config
from consensus_mcp.contributors.claude_cli import ClaudeCliAdapter
from consensus_mcp.contributors.openai_compatible import OpenAICompatibleAdapter
from consensus_mcp.contributors.opencode import OpencodeAdapter
from consensus_mcp.tools import (
    reviewer_dispatch_claude,
    reviewer_dispatch_codex,
    reviewer_dispatch_openai_compat,
    reviewer_dispatch_opencode,
)


def test_builtin_http_and_opencode_seats_are_wired(monkeypatch):
    builtins = profiles.load_builtin_profiles()
    monkeypatch.delenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", raising=False)
    defaults = config.default_config()["contributors"]["enabled"]
    assert "deepseek" not in defaults
    assert "opencode-deepseek" not in defaults
    assert builtins["deepseek"]["model"] == "DeepSeek-V4.1-Flash-1017"
    assert builtins["deepseek"]["reasoning_effort"] == "low"
    assert builtins["deepseek"]["max_concurrency"] == 4

    monkeypatch.delenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", raising=False)
    assert not _init_wizard._profile_installed(builtins["deepseek"])
    monkeypatch.setenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", "http://127.0.0.1:8888/v1")
    assert "deepseek" in config.default_config()["contributors"]["enabled"]
    assert _init_wizard._profile_installed(builtins["deepseek"])

    adapters = _engine_factory.build_adapters({"contributors": {"enabled": [
        "deepseek", "opencode-deepseek",
    ]}})
    assert isinstance(adapters["deepseek"], OpenAICompatibleAdapter)
    assert isinstance(adapters["opencode-deepseek"], OpencodeAdapter)


def test_claude_callback_and_cli_routes_are_preserved():
    cfg = {"contributors": {"enabled": ["claude"]}}
    assert isinstance(_engine_factory.build_adapters(cfg)["claude"], ClaudeCliAdapter)
    callback = lambda packet: {}
    from consensus_mcp.contributors.claude import ClaudeAdapter
    assert isinstance(_engine_factory.build_adapters(
        cfg, claude_artifact_callback=callback)["claude"], ClaudeAdapter)


def test_start_consult_emits_provider_specific_dispatch_commands(tmp_path):
    config_path = tmp_path / ".consensus" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(yaml.safe_dump({
        "contributors": {"enabled": ["claude", "deepseek", "opencode-deepseek"]},
    }), encoding="utf-8")
    result = _start_consult.start_consult("review seats", "src/**", repo_root=tmp_path)
    command = result["next_steps"]["1_run_consult_panel"]
    assert command == (
        f"consensus-mcp-consult --iteration {result['iteration']} "
        "--seats claude,deepseek,opencode-deepseek"
    )


def test_new_dispatch_tools_register_from_existing_bootstrap():
    registered = {}

    class Registry:
        def register(self, name, schema, handler):
            registered[name] = (schema, handler)

    reviewer_dispatch_codex.register(Registry())
    assert {
        "reviewer.dispatch_codex",
        "reviewer.dispatch_claude",
        "reviewer.dispatch_openai_compat",
        "reviewer.dispatch_opencode",
    } <= registered.keys()
    from consensus_mcp._jobs import ALLOWED_TOOLS
    assert {
        "reviewer.dispatch_claude",
        "reviewer.dispatch_openai_compat",
        "reviewer.dispatch_opencode",
    } <= set(ALLOWED_TOOLS)
    assert "failure_class" in registered["reviewer.dispatch_claude"][0]["output_schema"]["properties"]
    for schema, handler in registered.values():
        resolved = getattr(importlib.import_module(handler.__module__), handler.__name__)
        assert resolved is handler


def test_new_dispatch_tool_handlers_use_importable_fake_dispatchers(monkeypatch):
    cases = (
        (reviewer_dispatch_claude, _dispatch_module("consensus_mcp._dispatch_claude"), {}),
        (reviewer_dispatch_openai_compat,
         _dispatch_module("consensus_mcp._dispatch_openai_compat"), {"profile": "deepseek"}),
        (reviewer_dispatch_opencode,
         _dispatch_module("consensus_mcp.contributors.opencode"),
         {"profile": "opencode-deepseek"}),
    )
    for wrapper, dispatcher, extra in cases:
        monkeypatch.setattr(dispatcher, "main", lambda argv: print(json.dumps({"ok": True})) or 0)
        result = wrapper.handle(
            goal_packet_path="goal.yaml", iteration_dir="iteration",
            **extra,
        )
        assert result["ok"] is True


def _dispatch_module(name):
    return importlib.import_module(name)
