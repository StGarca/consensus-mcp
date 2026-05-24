"""Engine-factory routing tests for v1.18.0 ProfileAdapter (B-routing).

Per the converged plan (iteration-v1180-contributor-design-2026-05-22)
decision.engine_factory, `build_adapters` resolves each enabled contributor in
this order:
  (a) _REGISTERED_ADAPTERS
  (b) _BUILTIN_ADAPTERS (claude/codex/gemini/kimi → existing classes)
  (c) merged profiles (load_builtin_profiles + config contributors.profiles via
      merge_profiles); kind:cli_reviewer → ProfileAdapter(profile); kind:host is
      reserved (claude) and must NOT be turned into a subprocess adapter.
  Unknown name → EngineFactoryError.

R8 REGRESSION GATE: enabled=[claude,codex,gemini] must build the SAME adapter
classes as v1.17.5 (the existing test_engine_factory.py asserts this; this file
re-asserts it alongside the new profile routing to keep both observable in one
suite).
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _engine_factory as factory  # noqa: E402
from consensus_mcp import config as cfg  # noqa: E402
from consensus_mcp.contributors.claude import ClaudeAdapter  # noqa: E402
from consensus_mcp.contributors.codex import CodexAdapter  # noqa: E402
from consensus_mcp.contributors.gemini import GeminiAdapter  # noqa: E402
from consensus_mcp.contributors.kimi import KimiAdapter  # noqa: E402
from consensus_mcp.contributors.profile_adapter import ProfileAdapter  # noqa: E402


def _claude_cb(_packet):
    return {"findings": [], "goal_satisfied": True, "blocking_objections": []}


def _base_config(enabled):
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = list(enabled)
    return c


# --------------------------------------------------------------------------- #
# R8: built-in trio resolves to the existing classes (regression)
# --------------------------------------------------------------------------- #

def test_r8_builtin_trio_unchanged():
    config = _base_config(["claude", "codex", "gemini"])
    cfg.validate(config)
    adapters = factory.build_adapters(config, claude_artifact_callback=_claude_cb)
    assert set(adapters.keys()) == {"claude", "codex", "gemini"}
    assert isinstance(adapters["claude"], ClaudeAdapter)
    assert isinstance(adapters["codex"], CodexAdapter)
    assert isinstance(adapters["gemini"], GeminiAdapter)
    # Specifically NOT ProfileAdapter — built-in classes win over their profiles.
    assert not isinstance(adapters["codex"], ProfileAdapter)
    assert not isinstance(adapters["gemini"], ProfileAdapter)


# --------------------------------------------------------------------------- #
# kimi: now a built-in adapter class (KimiAdapter), not a profile fallback
# --------------------------------------------------------------------------- #

def test_kimi_builtin_resolves_to_kimiadapter():
    """kimi is now a built-in (_BUILTIN_ADAPTERS), so it resolves to KimiAdapter,
    NOT ProfileAdapter.  This replaced the old test that expected ProfileAdapter
    when kimi had no Python class (pre-v1.29.2)."""
    config = _base_config(["claude", "kimi"])
    adapters = factory.build_adapters(config, claude_artifact_callback=_claude_cb)
    assert set(adapters.keys()) == {"claude", "kimi"}
    assert isinstance(adapters["claude"], ClaudeAdapter)
    assert isinstance(adapters["kimi"], KimiAdapter)
    assert adapters["kimi"].name == "kimi"


def test_kimi_alongside_builtins():
    """kimi + the full built-in trio: trio keeps classes, kimi is KimiAdapter."""
    config = _base_config(["claude", "codex", "gemini", "kimi"])
    adapters = factory.build_adapters(config, claude_artifact_callback=_claude_cb)
    assert isinstance(adapters["codex"], CodexAdapter)
    assert isinstance(adapters["gemini"], GeminiAdapter)
    assert isinstance(adapters["kimi"], KimiAdapter)


# --------------------------------------------------------------------------- #
# config contributors.profiles overlay: add a custom + override a built-in
# --------------------------------------------------------------------------- #

def _custom_cli_profile(name="acme"):
    return {
        "name": name,
        "kind": "cli_reviewer",
        "model": f"{name}-model",
        "detect": {"command": f"{name}-cli"},
        "invoke": {
            "transport": "stdin",
            "base_args": [],
            "prompt_flag": None,
            "workdir_flag": None,
            "model_flag": None,
        },
        "env": {},
        "output": {"strip_patterns": [], "schema_enforced": False},
        "sealed_filename": f"{name}-review.yaml",
        "id_prefix": f"{name}-rev",
        "timeout_seconds": 1800,
    }


def test_config_profile_adds_new_contributor():
    config = _base_config(["claude", "acme"])
    config["contributors"]["profiles"] = {"acme": _custom_cli_profile("acme")}
    adapters = factory.build_adapters(config, claude_artifact_callback=_claude_cb)
    assert isinstance(adapters["acme"], ProfileAdapter)
    assert adapters["acme"].profile["model"] == "acme-model"


def test_config_profile_cannot_override_builtin_kimi():
    """kimi is now in _BUILTIN_ADAPTERS, so it resolves to KimiAdapter (step b)
    BEFORE profile lookup (step c).  A config profile named 'kimi' is silently
    ignored — same behaviour as claude.  To swap the impl a host must call
    register_contributor('kimi', CustomAdapter) which shadows the built-in."""
    config = _base_config(["claude", "kimi"])
    override = _custom_cli_profile("kimi")
    override["model"] = "kimi-overridden-model"
    config["contributors"]["profiles"] = {"kimi": override}
    adapters = factory.build_adapters(config, claude_artifact_callback=_claude_cb)
    # Built-in wins — KimiAdapter, NOT ProfileAdapter.
    assert isinstance(adapters["kimi"], KimiAdapter)
    assert not isinstance(adapters["kimi"], ProfileAdapter)


# --------------------------------------------------------------------------- #
# host profile never becomes a subprocess; unknown name fails closed
# --------------------------------------------------------------------------- #

def test_unknown_name_raises():
    config = _base_config(["claude", "totally-unknown-ai"])
    with pytest.raises(factory.EngineFactoryError, match="unknown contributor"):
        factory.build_adapters(config, claude_artifact_callback=_claude_cb)


def test_host_profile_not_instantiated_as_subprocess():
    """A bare `kind: host` profile name (other than the built-in claude class)
    must NOT be turned into a ProfileAdapter subprocess. claude itself resolves
    via _BUILTIN_ADAPTERS, so to exercise the host branch we register a host
    profile under a fresh name via config and assert it is rejected (host is
    reserved; only claude is a host and it routes to ClaudeAdapter)."""
    config = _base_config(["claude", "ghost"])
    config["contributors"]["profiles"] = {
        "ghost": {"name": "ghost", "kind": "host", "model": "ghost (host)"}
    }
    with pytest.raises(factory.EngineFactoryError, match="host"):
        factory.build_adapters(config, claude_artifact_callback=_claude_cb)


def test_claude_still_routes_to_claude_adapter_not_profile():
    """claude has kind:host AND a built-in class; it must resolve to
    ClaudeAdapter (built-in wins), never ProfileAdapter."""
    config = _base_config(["claude", "kimi"])
    adapters = factory.build_adapters(config, claude_artifact_callback=_claude_cb)
    assert isinstance(adapters["claude"], ClaudeAdapter)
    assert not isinstance(adapters["claude"], ProfileAdapter)
