"""Unit tests for consensus_mcp._engine_factory."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from consensus_mcp import _engine_factory as factory
from consensus_mcp import config as cfg
from consensus_mcp.contributors.claude import ClaudeAdapter
from consensus_mcp.contributors.codex import CodexAdapter
from consensus_mcp.contributors.gemini import GeminiAdapter


def _three_contributor_config():
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = ["claude", "codex", "gemini"]
    cfg.validate(c)
    return c


def test_build_adapters_returns_one_per_enabled_contributor(tmp_path):
    config = _three_contributor_config()
    def _cb(packet):
        return {"findings": [], "goal_satisfied": True, "blocking_objections": []}
    adapters = factory.build_adapters(config, claude_artifact_callback=_cb)
    assert set(adapters.keys()) == {"claude", "codex", "gemini"}
    assert isinstance(adapters["claude"], ClaudeAdapter)
    assert isinstance(adapters["codex"], CodexAdapter)
    assert isinstance(adapters["gemini"], GeminiAdapter)


def test_build_adapters_solo_claude(tmp_path):
    config = deepcopy(cfg.default_config())
    config["contributors"]["enabled"] = ["claude"]
    config["workflow"]["mode"] = cfg.WORKFLOW_POST_REVIEW
    config["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    config["convergence"]["rule"] = cfg.CONVERGE_UNANIMOUS
    cfg.validate(config)
    adapters = factory.build_adapters(config)
    assert list(adapters.keys()) == ["claude"]
    assert isinstance(adapters["claude"], ClaudeAdapter)


def test_build_adapters_unknown_key_raises(tmp_path):
    config = _three_contributor_config()
    config["contributors"]["enabled"] = ["claude", "fictional-ai"]
    # validate() rejects this — so bypass for the factory test.
    with pytest.raises(factory.EngineFactoryError, match="unknown contributor key"):
        factory.build_adapters(config)


def test_build_adapters_empty_enabled_raises(tmp_path):
    config = deepcopy(cfg.default_config())
    config["contributors"]["enabled"] = []
    with pytest.raises(factory.EngineFactoryError, match="empty"):
        factory.build_adapters(config)


def test_build_engine_validates_config(tmp_path):
    """build_engine() should validate the config before constructing adapters."""
    config = deepcopy(cfg.default_config())
    config["workflow"]["mode"] = "not-a-real-mode"
    with pytest.raises(cfg.ConfigValidationError):
        factory.build_engine(config, repo_root=tmp_path)


def test_build_engine_returns_workflow_engine(tmp_path):
    config = _three_contributor_config()
    def _cb(packet):
        return {"findings": [], "goal_satisfied": True, "blocking_objections": []}
    engine = factory.build_engine(
        config, repo_root=tmp_path, claude_artifact_callback=_cb,
    )
    from consensus_mcp.workflow_engine import WorkflowEngine
    assert isinstance(engine, WorkflowEngine)
    assert engine.repo_root == tmp_path
    assert set(engine.adapters.keys()) == {"claude", "codex", "gemini"}


def test_build_engine_claude_callback_threaded(tmp_path):
    """The claude_artifact_callback must reach ClaudeAdapter.artifact_callback."""
    config = _three_contributor_config()
    def _cb(packet):
        return {"findings": [], "goal_satisfied": True, "blocking_objections": []}
    engine = factory.build_engine(
        config, repo_root=tmp_path, claude_artifact_callback=_cb,
    )
    assert engine.adapters["claude"].artifact_callback is _cb


def test_build_adapters_reads_per_contributor_from_adapters_key():
    """1.17 review (codex-002): per-contributor config lives under
    `contributors.adapters` (what default_config + validate use). build_adapters
    previously read the never-populated `contributors.config` key, so adapter
    config (e.g. model) was silently always empty. Now it reaches the adapter."""
    config = _three_contributor_config()
    config["contributors"]["adapters"]["codex"] = {"model": "test-model-x"}
    adapters = factory.build_adapters(config, claude_artifact_callback=lambda p: {
        "findings": [], "goal_satisfied": True, "blocking_objections": []})
    assert adapters["codex"].adapter_config.get("model") == "test-model-x", (
        f"adapter_config not populated from contributors.adapters: "
        f"{adapters['codex'].adapter_config}")


def test_build_adapters_kimi_is_builtin_kimiadapter():
    """Default panel enables kimi with no profile; it must build a KimiAdapter
    (the hardened built-in), NOT a ProfileAdapter and NOT a build failure."""
    from consensus_mcp import _engine_factory
    from consensus_mcp.contributors.kimi import KimiAdapter
    # Use codex+gemini+kimi to avoid needing a claude_artifact_callback.
    # We exercise build_adapters directly (the factory is the unit under test);
    # kimi is a known contributor (config.KNOWN_CONTRIBUTORS) so cfg.validate()
    # would accept it too, but validation is covered elsewhere.
    config = deepcopy(cfg.default_config())
    config["contributors"]["enabled"] = ["codex", "gemini", "kimi"]
    adapters = _engine_factory.build_adapters(config)
    assert isinstance(adapters["kimi"], KimiAdapter)
