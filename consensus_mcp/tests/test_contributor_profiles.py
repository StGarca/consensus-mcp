"""Unit tests for consensus_mcp._contributor_profiles - the v1.18.0 contributor
profile data foundation (loader / merger / validator).

Per converged-plan.yaml (iteration-v1180-contributor-design-2026-05-22):
B-ROUTING + UNIVERSAL PROFILES. This module supplies the wizard list, detect
status, install/auth guidance, model/provenance labels, and forward-compat
schema. It does NOT dispatch - _engine_factory + ProfileAdapter own that.

This test file pins:
  * load_builtin_profiles() returns the 4 built-in AIs (claude/codex/gemini/kimi)
  * every built-in profile validates clean
  * validate_profile rejects each malformation class
  * merge_profiles overrides by name + adds new names
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from consensus_mcp import _contributor_profiles as cp


# v1.20.0: claude-swe-reviewer (kind=host_peer) joins the built-in set.
BUILTIN_NAMES = {"claude", "codex", "gemini", "grok", "kimi", "claude-swe-reviewer"}


# ---------- load_builtin_profiles ----------

def test_load_builtin_returns_the_builtins():
    profiles = cp.load_builtin_profiles()
    assert isinstance(profiles, dict)
    assert set(profiles) == BUILTIN_NAMES


def test_load_builtin_keyed_by_name_field():
    """Each profile's `name` field matches its dict key (the yaml stem)."""
    profiles = cp.load_builtin_profiles()
    for key, prof in profiles.items():
        assert prof["name"] == key


def test_each_builtin_validates_clean():
    profiles = cp.load_builtin_profiles()
    for name, prof in profiles.items():
        # must not raise
        cp.validate_profile(name, prof)


def test_claude_is_host_kind():
    profiles = cp.load_builtin_profiles()
    assert profiles["claude"]["kind"] == "host"


def test_cli_reviewers_are_cli_reviewer_kind():
    profiles = cp.load_builtin_profiles()
    for name in ("codex", "gemini", "kimi"):
        assert profiles[name]["kind"] == "cli_reviewer"


def test_kimi_provenance_model_is_not_gemini():
    """Regression: the parent kimi wrapper mislabeled model='gemini-2.5-pro'.
    The kimi profile must seal an ACCURATE model label.
    """
    profiles = cp.load_builtin_profiles()
    assert profiles["kimi"]["model"] != "gemini-2.5-pro"
    assert profiles["kimi"]["model"] == "kimi (CLI-configured default)"


def test_kimi_transport_is_stdin_with_null_prompt_flag():
    profiles = cp.load_builtin_profiles()
    kimi = profiles["kimi"]
    assert kimi["invoke"]["transport"] == "stdin"
    assert kimi["invoke"].get("prompt_flag") is None


def test_gemini_env_trust_workspace():
    profiles = cp.load_builtin_profiles()
    assert profiles["gemini"]["env"]["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def test_codex_schema_enforced_true():
    profiles = cp.load_builtin_profiles()
    assert profiles["codex"]["output"]["schema_enforced"] is True


def test_instruction_filename_map():
    """claude->CLAUDE.md, codex->AGENTS.md, gemini->GEMINI.md, kimi->AGENTS.md."""
    profiles = cp.load_builtin_profiles()
    assert profiles["claude"]["instructions"]["filename"] == "CLAUDE.md"
    assert profiles["codex"]["instructions"]["filename"] == "AGENTS.md"
    assert profiles["gemini"]["instructions"]["filename"] == "GEMINI.md"
    assert profiles["kimi"]["instructions"]["filename"] == "AGENTS.md"


# ---------- validate_profile (rejections) ----------

def _minimal_cli_reviewer() -> dict:
    """A minimal VALID cli_reviewer profile to mutate in rejection tests."""
    return {
        "name": "fake",
        "kind": "cli_reviewer",
        "detect": {"command": "fake"},
        "invoke": {"transport": "stdin", "prompt_flag": None},
        "output": {"schema_enforced": False},
    }


def test_minimal_cli_reviewer_validates_clean():
    cp.validate_profile("fake", _minimal_cli_reviewer())


def test_reject_missing_name():
    d = _minimal_cli_reviewer()
    del d["name"]
    with pytest.raises(ValueError):
        cp.validate_profile("fake", d)


def test_reject_bad_kind():
    d = _minimal_cli_reviewer()
    d["kind"] = "wizard"
    with pytest.raises(ValueError):
        cp.validate_profile("fake", d)


def test_reject_cli_reviewer_missing_detect_command():
    d = _minimal_cli_reviewer()
    del d["detect"]
    with pytest.raises(ValueError):
        cp.validate_profile("fake", d)


def test_reject_cli_reviewer_missing_invoke_transport():
    d = _minimal_cli_reviewer()
    del d["invoke"]
    with pytest.raises(ValueError):
        cp.validate_profile("fake", d)


def test_reject_cli_reviewer_missing_output():
    d = _minimal_cli_reviewer()
    del d["output"]
    with pytest.raises(ValueError):
        cp.validate_profile("fake", d)


def test_reject_bad_transport_enum():
    d = _minimal_cli_reviewer()
    d["invoke"]["transport"] = "pipe"
    with pytest.raises(ValueError):
        cp.validate_profile("fake", d)


def test_reject_flag_transport_without_prompt_flag():
    d = _minimal_cli_reviewer()
    d["invoke"]["transport"] = "flag"
    d["invoke"]["prompt_flag"] = None
    with pytest.raises(ValueError):
        cp.validate_profile("fake", d)


def test_flag_transport_with_prompt_flag_validates():
    d = _minimal_cli_reviewer()
    d["invoke"]["transport"] = "flag"
    d["invoke"]["prompt_flag"] = "-p"
    cp.validate_profile("fake", d)  # must not raise


def test_reject_stdin_transport_with_prompt_flag():
    d = _minimal_cli_reviewer()
    d["invoke"]["transport"] = "stdin"
    d["invoke"]["prompt_flag"] = "-p"
    with pytest.raises(ValueError):
        cp.validate_profile("fake", d)


def test_reject_bad_install_os_key():
    d = _minimal_cli_reviewer()
    d["install"] = {"windows": "pipx install x", "solaris": "pkg install x"}
    with pytest.raises(ValueError):
        cp.validate_profile("fake", d)


def test_valid_install_os_keys():
    d = _minimal_cli_reviewer()
    d["install"] = {"windows": "x", "linux": "x", "darwin": "x"}
    cp.validate_profile("fake", d)  # must not raise


def test_host_kind_needs_no_detect_or_invoke():
    """claude (host) need not have detect/invoke/output."""
    d = {"name": "claude", "kind": "host", "instructions": {"filename": "CLAUDE.md"}}
    cp.validate_profile("claude", d)  # must not raise


# ---------- v1.20.0 host_peer kind ----------

def _minimal_host_peer() -> dict:
    return {
        "name": "claude-swe-reviewer",
        "kind": "host_peer",
        "family": "claude",
        "role": "swe_reviewer",
        "weight": "supplementary",
        "gate_eligible": False,
    }


def test_builtin_claude_swe_reviewer_is_host_peer():
    profiles = cp.load_builtin_profiles()
    p = profiles["claude-swe-reviewer"]
    assert p["kind"] == "host_peer"
    assert p["family"] == "claude"
    assert p["role"] == "swe_reviewer"
    assert p["gate_eligible"] is False
    assert p["weight"] == "supplementary"


def test_host_peer_needs_no_detect_or_invoke():
    """host_peer (like host) needs no detect/invoke/output."""
    cp.validate_profile("claude-swe-reviewer", _minimal_host_peer())  # must not raise


def test_host_peer_requires_family():
    d = _minimal_host_peer()
    del d["family"]
    with pytest.raises(ValueError, match="family"):
        cp.validate_profile("claude-swe-reviewer", d)


def test_host_peer_requires_role():
    d = _minimal_host_peer()
    del d["role"]
    with pytest.raises(ValueError, match="role"):
        cp.validate_profile("claude-swe-reviewer", d)


def test_host_peer_gate_eligible_must_be_bool_when_present():
    d = _minimal_host_peer()
    d["gate_eligible"] = "false"  # string, not bool
    with pytest.raises(ValueError, match="gate_eligible"):
        cp.validate_profile("claude-swe-reviewer", d)


def test_host_peer_weight_and_gate_eligible_optional():
    d = _minimal_host_peer()
    del d["weight"]
    del d["gate_eligible"]
    cp.validate_profile("claude-swe-reviewer", d)  # must not raise


# ---------- merge_profiles ----------

def test_merge_override_by_name():
    builtin = cp.load_builtin_profiles()
    override = {"kimi": {"name": "kimi", "kind": "cli_reviewer", "model": "kimi-OVERRIDDEN",
                         "detect": {"command": "kimi"},
                         "invoke": {"transport": "stdin", "prompt_flag": None},
                         "output": {"schema_enforced": False}}}
    merged = cp.merge_profiles(builtin, override)
    assert merged["kimi"]["model"] == "kimi-OVERRIDDEN"
    # other built-ins untouched
    assert merged["codex"]["name"] == "codex"


def test_merge_adds_new_name():
    builtin = cp.load_builtin_profiles()
    new = {"grok": {"name": "grok", "kind": "cli_reviewer",
                    "detect": {"command": "grok"},
                    "invoke": {"transport": "flag", "prompt_flag": "-p"},
                    "output": {"schema_enforced": False}}}
    merged = cp.merge_profiles(builtin, new)
    assert "grok" in merged
    assert set(merged) == BUILTIN_NAMES | {"grok"}


def test_merge_does_not_mutate_inputs():
    builtin = cp.load_builtin_profiles()
    builtin_snapshot = deepcopy(builtin)
    override = {"kimi": {"name": "kimi", "kind": "cli_reviewer", "model": "x",
                         "detect": {"command": "kimi"},
                         "invoke": {"transport": "stdin", "prompt_flag": None},
                         "output": {"schema_enforced": False}}}
    cp.merge_profiles(builtin, override)
    assert builtin == builtin_snapshot


def test_merge_empty_config_returns_builtins():
    builtin = cp.load_builtin_profiles()
    merged = cp.merge_profiles(builtin, {})
    assert set(merged) == BUILTIN_NAMES


# ---------- v1.20.1 profile-kind helpers ----------

def _profiles():
    return {
        "claude": {"name": "claude", "kind": "host"},
        "codex": {"name": "codex", "kind": "cli_reviewer"},
        "gemini": {"name": "gemini", "kind": "cli_reviewer"},
        "claude-swe-reviewer": {
            "name": "claude-swe-reviewer", "kind": "host_peer", "family": "claude",
        },
    }


def test_resolve_kind_known_and_unknown():
    p = _profiles()
    assert cp.resolve_kind("codex", p) == "cli_reviewer"
    assert cp.resolve_kind("claude-swe-reviewer", p) == "host_peer"
    assert cp.resolve_kind("some-custom-ai", p) is None


def test_independent_count_excludes_host_peer_counts_unknown():
    p = _profiles()
    assert cp.independent_count(["claude", "codex"], p) == 2
    assert cp.independent_count(["claude", "claude-swe-reviewer"], p) == 1
    assert cp.independent_count(["claude", "codex", "claude-swe-reviewer"], p) == 2
    assert cp.independent_count(["claude", "my-ai"], p) == 2


def test_host_family_host_and_host_peer():
    p = _profiles()
    assert cp.host_family("claude", p) == "claude"
    assert cp.host_family("claude-swe-reviewer", p) == "claude"
    assert cp.host_family("codex", p) is None


def test_matching_host_peers():
    p = _profiles()
    assert cp.matching_host_peers("claude", p) == ["claude-swe-reviewer"]
    assert cp.matching_host_peers("codex", p) == []


def test_orphan_host_peers():
    p = _profiles()
    assert cp.orphan_host_peers(["claude", "claude-swe-reviewer"], p) == []
    assert cp.orphan_host_peers(["codex", "claude-swe-reviewer"], p) == ["claude-swe-reviewer"]
    assert cp.orphan_host_peers(["claude", "codex"], p) == []
