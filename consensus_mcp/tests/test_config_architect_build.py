"""Tests for the architect-build (workflow D) config contract."""
from __future__ import annotations

import pytest

import consensus_mcp.config as cfg
from consensus_mcp import _contributor_profiles as profiles_mod


def _abd_config(**overrides):
    """Minimal valid architect-build config for tests."""
    c = cfg.default_config()
    c["workflow"]["mode"] = cfg.WORKFLOW_ARCHITECT_BUILD
    c["contributors"]["enabled"] = ["claude", "codex"]
    c["roles"] = {"architect": "claude", "builder": "codex", "reviewer": "codex"}
    for k, v in overrides.items():
        c[k] = v
    return c


def test_constant_and_valid_workflows():
    assert cfg.WORKFLOW_ARCHITECT_BUILD == "architect-build"
    assert cfg.WORKFLOW_ARCHITECT_BUILD in cfg.VALID_WORKFLOWS


def test_letter_alias_d_resolves():
    c = cfg.default_config()
    c["workflow"]["mode"] = "D"
    n = cfg.normalize(c)
    assert n["workflow"]["mode"] == cfg.WORKFLOW_ARCHITECT_BUILD


def test_letter_alias_lower_d_resolves():
    c = cfg.default_config()
    c["workflow"]["mode"] = "d"
    n = cfg.normalize(c)
    assert n["workflow"]["mode"] == cfg.WORKFLOW_ARCHITECT_BUILD


def test_default_config_has_architect_loop_block():
    c = cfg.default_config()
    assert c["architect_loop"] == {
        "max_cycles": 8,
        "verification": "",
        "lane_branch_prefix": "arch-lane/",
        "max_wall_clock_minutes": 0,
    }


def test_validate_accepts_minimal_architect_build():
    cfg.validate(cfg.normalize(_abd_config()))  # must not raise


def test_validate_rejects_missing_roles_block():
    c = _abd_config()
    del c["roles"]
    with pytest.raises(cfg.ConfigValidationError, match="requires a top-level roles"):
        cfg.validate(cfg.normalize(c))


def test_validate_rejects_roles_block_outside_architect_build():
    c = cfg.default_config()
    c["contributors"]["enabled"] = ["claude", "codex"]
    c["roles"] = {"architect": "claude", "builder": "codex", "reviewer": "codex"}
    with pytest.raises(cfg.ConfigValidationError, match="only legal when"):
        cfg.validate(cfg.normalize(c))


def test_validate_rejects_missing_reviewer_role():
    c = _abd_config()
    del c["roles"]["reviewer"]
    with pytest.raises(cfg.ConfigValidationError, match="reviewer"):
        cfg.validate(cfg.normalize(c))


def test_validate_rejects_role_not_in_enabled():
    c = _abd_config()
    c["roles"]["reviewer"] = "gemini"  # not in enabled
    with pytest.raises(cfg.ConfigValidationError, match="enabled"):
        cfg.validate(cfg.normalize(c))


def test_validate_rejects_same_family_everywhere():
    # builder=codex, architect=codex, reviewer=codex: no cross-family signer.
    c = _abd_config()
    c["contributors"]["enabled"] = ["codex"]
    c["roles"] = {"architect": "codex", "builder": "codex", "reviewer": "codex"}
    with pytest.raises(cfg.ConfigValidationError, match="cross-family"):
        cfg.validate(cfg.normalize(c))


def test_validate_rejects_non_builder_capable_builder():
    c = _abd_config()
    c["contributors"]["enabled"] = ["claude", "codex", "gemini"]
    c["roles"]["builder"] = "gemini"  # gemini profile lacks builder_capable
    with pytest.raises(cfg.ConfigValidationError, match="builder_capable"):
        cfg.validate(cfg.normalize(c))


def test_validate_rejects_bad_max_cycles():
    c = _abd_config()
    c["architect_loop"] = dict(cfg.default_config()["architect_loop"], max_cycles=0)
    with pytest.raises(cfg.ConfigValidationError, match="max_cycles"):
        cfg.validate(cfg.normalize(c))


def test_validate_rejects_bad_lane_prefix():
    c = _abd_config()
    c["architect_loop"] = dict(
        cfg.default_config()["architect_loop"], lane_branch_prefix="a/b/"
    )
    with pytest.raises(cfg.ConfigValidationError, match="lane_branch_prefix"):
        cfg.validate(cfg.normalize(c))


def test_validate_rejects_unknown_role_key():
    c = _abd_config()
    c["roles"]["observer"] = "claude"
    with pytest.raises(cfg.ConfigValidationError, match="unknown keys"):
        cfg.validate(cfg.normalize(c))


def test_codex_profile_is_builder_capable():
    builtin = profiles_mod.load_builtin_profiles()
    assert profiles_mod.resolve_builder_capable("codex", builtin) is True


def test_other_profiles_default_not_builder_capable():
    builtin = profiles_mod.load_builtin_profiles()
    for name in ("claude", "gemini", "grok", "kimi"):
        assert profiles_mod.resolve_builder_capable(name, builtin) is False


def test_unknown_profile_not_builder_capable():
    assert profiles_mod.resolve_builder_capable("nope", {}) is False
