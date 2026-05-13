"""Unit tests for consensus_mcp.config — .consensus/config.yaml schema/validator.

Per iter-0016a goal: pin the schema constants, default behavior, alias
resolution, and every validation rule from converged-plan.yaml Section B.
"""
from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from consensus_mcp import config as cfg


# ---------- defaults ----------

def test_default_config_validates():
    """Default config must pass its own validator (sanity)."""
    cfg.validate(cfg.default_config())


def test_default_workflow_is_propose_converge():
    """Per converged-plan: default mode is propose-converge when ≥2 contributors."""
    assert cfg.default_config()["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


def test_default_independence_is_blind_first():
    assert cfg.default_config()["workflow"]["independence"] == cfg.INDEPENDENCE_BLIND


def test_default_convergence_strict_majority():
    assert cfg.default_config()["convergence"]["rule"] == cfg.CONVERGE_STRICT_MAJ


def test_default_disposition_all_or_nothing():
    """Per D5 majority: workflow #4 forces all-or-nothing in v1."""
    assert cfg.default_config()["convergence"]["finding_disposition"] == cfg.DISPOSITION_ALL_OR_NOTHING


def test_default_timeout_policy_no_vote():
    assert cfg.default_config()["workflow"]["timeout_policy"] == cfg.TIMEOUT_NO_VOTE


def test_default_snapshot_on_close():
    assert cfg.default_config()["snapshots"]["trigger"] == cfg.SNAPSHOT_ON_CLOSE


def test_default_patch_authoring_claude_only():
    assert cfg.default_config()["patches"]["authoring"] == cfg.PATCH_CLAUDE_ONLY


def test_default_contributors_include_all_three():
    assert cfg.default_config()["contributors"]["enabled"] == ["claude", "codex", "gemini"]


# ---------- normalize / aliases ----------

def test_normalize_numeric_workflow_alias_3():
    c = {"workflow": {"mode": 3}}
    n = cfg.normalize(c)
    assert n["workflow"]["mode"] == cfg.WORKFLOW_POST_REVIEW


def test_normalize_numeric_workflow_alias_4():
    c = {"workflow": {"mode": 4}}
    n = cfg.normalize(c)
    assert n["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


def test_normalize_string_alias_3():
    c = {"workflow": {"mode": "3"}}
    n = cfg.normalize(c)
    assert n["workflow"]["mode"] == cfg.WORKFLOW_POST_REVIEW


def test_normalize_semantic_string_passes_through():
    c = {"workflow": {"mode": cfg.WORKFLOW_ADVISORY}}
    n = cfg.normalize(c)
    assert n["workflow"]["mode"] == cfg.WORKFLOW_ADVISORY


def test_normalize_fills_missing_keys_from_defaults():
    """Sparse config should be filled in by defaults."""
    sparse = {"schema_version": 1, "workflow": {"mode": cfg.WORKFLOW_POST_REVIEW}}
    n = cfg.normalize(sparse)
    assert n["convergence"]["rule"] == cfg.CONVERGE_STRICT_MAJ  # from defaults
    assert "contributors" in n
    assert "adapters" in n["contributors"]


def test_normalize_rejects_non_dict_root():
    with pytest.raises(cfg.ConfigValidationError, match="mapping"):
        cfg.normalize([])
    with pytest.raises(cfg.ConfigValidationError, match="mapping"):
        cfg.normalize(None)


# ---------- validation: schema_version ----------

def test_validate_rejects_missing_schema_version():
    c = cfg.default_config()
    del c["schema_version"]
    with pytest.raises(cfg.ConfigValidationError, match="schema_version"):
        cfg.validate(c)


def test_validate_rejects_wrong_schema_version():
    c = cfg.default_config()
    c["schema_version"] = 99
    with pytest.raises(cfg.ConfigValidationError, match="schema_version"):
        cfg.validate(c)


# ---------- validation: workflow ----------

def test_validate_rejects_unknown_workflow_mode():
    c = cfg.default_config()
    c["workflow"]["mode"] = "wrong"
    with pytest.raises(cfg.ConfigValidationError, match="workflow.mode"):
        cfg.validate(c)


def test_validate_rejects_unknown_independence():
    c = cfg.default_config()
    c["workflow"]["independence"] = "wrong"
    with pytest.raises(cfg.ConfigValidationError, match="workflow.independence"):
        cfg.validate(c)


def test_validate_rejects_unknown_timeout_policy():
    c = cfg.default_config()
    c["workflow"]["timeout_policy"] = "wrong"
    with pytest.raises(cfg.ConfigValidationError, match="timeout_policy"):
        cfg.validate(c)


def test_validate_rejects_zero_max_rounds():
    c = cfg.default_config()
    c["workflow"]["max_convergence_rounds"] = 0
    with pytest.raises(cfg.ConfigValidationError, match="max_convergence_rounds"):
        cfg.validate(c)


# ---------- validation: contributors ----------

def test_validate_rejects_empty_contributors():
    c = cfg.default_config()
    c["contributors"]["enabled"] = []
    with pytest.raises(cfg.ConfigValidationError, match="non-empty"):
        cfg.validate(c)


def test_validate_rejects_duplicate_contributors():
    c = cfg.default_config()
    c["contributors"]["enabled"] = ["claude", "codex", "codex"]
    with pytest.raises(cfg.ConfigValidationError, match="unique"):
        cfg.validate(c)


def test_validate_rejects_unknown_contributor():
    c = cfg.default_config()
    c["contributors"]["enabled"] = ["claude", "aider"]
    with pytest.raises(cfg.ConfigValidationError, match="unknown identifier"):
        cfg.validate(c)


def test_validate_requires_claude():
    """Per converged-plan: schema_version 1 requires claude as orchestrator."""
    c = cfg.default_config()
    c["contributors"]["enabled"] = ["codex", "gemini"]
    with pytest.raises(cfg.ConfigValidationError, match="must contain 'claude'"):
        cfg.validate(c)


def test_validate_rejects_missing_adapter():
    c = cfg.default_config()
    c["contributors"]["enabled"] = ["claude", "codex", "gemini"]
    del c["contributors"]["adapters"]["gemini"]
    with pytest.raises(cfg.ConfigValidationError, match="adapters.gemini"):
        cfg.validate(c)


# ---------- validation: convergence ----------

def test_validate_rejects_unknown_convergence_rule():
    c = cfg.default_config()
    c["convergence"]["rule"] = "wrong"
    with pytest.raises(cfg.ConfigValidationError, match="convergence.rule"):
        cfg.validate(c)


def test_validate_rejects_unknown_finding_disposition():
    c = cfg.default_config()
    c["convergence"]["finding_disposition"] = "wrong"
    with pytest.raises(cfg.ConfigValidationError, match="finding_disposition"):
        cfg.validate(c)


# ---------- cross-validation rules ----------

def test_validate_propose_converge_requires_two_contributors():
    """Workflow #4 with only claude is invalid."""
    c = cfg.default_config()
    c["workflow"]["mode"] = cfg.WORKFLOW_PROPOSE_CONVERGE
    c["contributors"]["enabled"] = ["claude"]
    with pytest.raises(cfg.ConfigValidationError, match="propose-converge requires at least 2"):
        cfg.validate(c)


def test_validate_propose_converge_forces_all_or_nothing():
    """D5 majority resolution: per-finding restricted to post-review + advisory."""
    c = cfg.default_config()
    c["workflow"]["mode"] = cfg.WORKFLOW_PROPOSE_CONVERGE
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_PER_FINDING
    with pytest.raises(cfg.ConfigValidationError, match="all-or-nothing"):
        cfg.validate(c)


def test_validate_per_finding_allowed_in_post_review():
    """per-finding is legal in workflow #3."""
    c = cfg.default_config()
    c["workflow"]["mode"] = cfg.WORKFLOW_POST_REVIEW
    c["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_PER_FINDING
    cfg.validate(c)  # should not raise


def test_validate_advisory_mode_requires_advisory_rule():
    c = cfg.default_config()
    c["workflow"]["mode"] = cfg.WORKFLOW_ADVISORY
    # rule stays at strict-majority
    with pytest.raises(cfg.ConfigValidationError, match="advisory.*requires"):
        cfg.validate(c)


def test_validate_advisory_rule_requires_advisory_mode():
    c = cfg.default_config()
    c["workflow"]["mode"] = cfg.WORKFLOW_PROPOSE_CONVERGE
    c["convergence"]["rule"] = cfg.CONVERGE_ADVISORY
    with pytest.raises(cfg.ConfigValidationError, match="advisory.*valid only"):
        cfg.validate(c)


def test_validate_strict_majority_with_one_contributor_invalid():
    c = cfg.default_config()
    c["workflow"]["mode"] = cfg.WORKFLOW_POST_REVIEW
    c["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    c["contributors"]["enabled"] = ["claude"]
    c["convergence"]["rule"] = cfg.CONVERGE_STRICT_MAJ
    with pytest.raises(cfg.ConfigValidationError, match="strict-majority is invalid with only 1"):
        cfg.validate(c)


def test_validate_sequential_requires_two_contributors():
    c = cfg.default_config()
    c["workflow"]["mode"] = cfg.WORKFLOW_POST_REVIEW
    c["workflow"]["independence"] = cfg.INDEPENDENCE_SEQUENTIAL
    c["contributors"]["enabled"] = ["claude"]
    c["convergence"]["rule"] = cfg.CONVERGE_UNANIMOUS
    with pytest.raises(cfg.ConfigValidationError, match="sequential requires at least 2"):
        cfg.validate(c)


# ---------- patches ----------

def test_validate_rejects_unknown_patch_authoring():
    c = cfg.default_config()
    c["patches"]["authoring"] = "wrong"
    with pytest.raises(cfg.ConfigValidationError, match="patches.authoring"):
        cfg.validate(c)


def test_validate_rejects_negative_max_patch_lines():
    c = cfg.default_config()
    c["patches"]["max_patch_lines"] = -1
    with pytest.raises(cfg.ConfigValidationError, match="max_patch_lines"):
        cfg.validate(c)


# ---------- snapshots ----------

def test_validate_rejects_unknown_snapshot_trigger():
    c = cfg.default_config()
    c["snapshots"]["trigger"] = "wrong"
    with pytest.raises(cfg.ConfigValidationError, match="snapshots.trigger"):
        cfg.validate(c)


def test_validate_periodic_requires_period():
    c = cfg.default_config()
    c["snapshots"]["trigger"] = cfg.SNAPSHOT_PERIODIC
    # both periodic fields null
    with pytest.raises(cfg.ConfigValidationError, match="periodic requires"):
        cfg.validate(c)


def test_validate_periodic_accepts_every_iterations():
    c = cfg.default_config()
    c["snapshots"]["trigger"] = cfg.SNAPSHOT_PERIODIC
    c["snapshots"]["periodic"]["every_iterations"] = 5
    cfg.validate(c)  # should not raise


def test_validate_periodic_accepts_every_minutes():
    c = cfg.default_config()
    c["snapshots"]["trigger"] = cfg.SNAPSHOT_PERIODIC
    c["snapshots"]["periodic"]["every_minutes"] = 30
    cfg.validate(c)  # should not raise (v1 will WARN at load time, not raise here)


def test_validate_non_periodic_requires_null_periods():
    c = cfg.default_config()
    c["snapshots"]["trigger"] = cfg.SNAPSHOT_ON_CLOSE
    c["snapshots"]["periodic"]["every_iterations"] = 5
    with pytest.raises(cfg.ConfigValidationError, match="periodic.every_iterations.*null"):
        cfg.validate(c)


def test_validate_rejects_zero_every_iterations():
    c = cfg.default_config()
    c["snapshots"]["trigger"] = cfg.SNAPSHOT_PERIODIC
    c["snapshots"]["periodic"]["every_iterations"] = 0
    with pytest.raises(cfg.ConfigValidationError, match="every_iterations"):
        cfg.validate(c)


# ---------- load + file IO ----------

def test_load_round_trip(tmp_path):
    """Write default config to disk, load it back, verify."""
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg.default_config()), encoding="utf-8")
    loaded = cfg.load(p)
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


def test_load_alias_resolved_at_load(tmp_path):
    """Numeric aliases in YAML resolve to semantic names on load."""
    p = tmp_path / "config.yaml"
    p.write_text("schema_version: 1\nworkflow:\n  mode: 4\n", encoding="utf-8")
    loaded = cfg.load(p)
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


def test_load_sparse_yaml_fills_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("schema_version: 1\n", encoding="utf-8")
    loaded = cfg.load(p)
    # Should have all default keys populated.
    assert loaded["convergence"]["rule"] == cfg.CONVERGE_STRICT_MAJ
    assert loaded["contributors"]["enabled"] == ["claude", "codex", "gemini"]


def test_load_invalid_raises(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("schema_version: 1\nworkflow:\n  mode: wrong\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigValidationError):
        cfg.load(p)


def test_load_malformed_yaml_raises_config_validation_error(tmp_path):
    """codex-rev-001 fix: malformed YAML is wrapped in ConfigValidationError per module contract."""
    p = tmp_path / "config.yaml"
    p.write_text("schema_version: [\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigValidationError, match="malformed YAML"):
        cfg.load(p)


# ---------- effective_config_sha256 ----------

def test_sha256_deterministic():
    """Same config → same sha."""
    c1 = cfg.default_config()
    c2 = cfg.default_config()
    assert cfg.effective_config_sha256(c1) == cfg.effective_config_sha256(c2)


def test_sha256_changes_on_modification():
    c1 = cfg.default_config()
    c2 = deepcopy(c1)
    c2["workflow"]["mode"] = cfg.WORKFLOW_POST_REVIEW
    c2["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    c2["contributors"]["enabled"] = ["claude", "codex"]
    c2["convergence"]["rule"] = cfg.CONVERGE_UNANIMOUS
    assert cfg.effective_config_sha256(c1) != cfg.effective_config_sha256(c2)


# ---------- legacy mode helpers ----------

def test_is_legacy_mode_repo_true_when_config_absent(tmp_path):
    assert cfg.is_legacy_mode_repo(tmp_path) is True


def test_is_legacy_mode_repo_false_when_config_present(tmp_path):
    (tmp_path / ".consensus").mkdir()
    (tmp_path / ".consensus" / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    assert cfg.is_legacy_mode_repo(tmp_path) is False


def test_synthesize_legacy_config_has_sentinel(tmp_path):
    legacy = cfg.synthesize_legacy_config(tmp_path)
    assert legacy["schema_version"] == 0  # sentinel
    assert legacy.get("_legacy_mode_synthesis") is True


def test_synthesize_legacy_config_workflow_post_review(tmp_path):
    """Per converged-plan Section D: legacy mode emulates workflow #3."""
    legacy = cfg.synthesize_legacy_config(tmp_path)
    assert legacy["workflow"]["mode"] == cfg.WORKFLOW_POST_REVIEW
    assert legacy["contributors"]["enabled"] == ["claude", "codex"]
    assert legacy["snapshots"]["trigger"] == cfg.SNAPSHOT_MANUAL  # legacy didn't auto-snapshot
    assert legacy["patches"]["authoring"] == cfg.PATCH_CLAUDE_ONLY


def test_synthesize_legacy_config_validate_rejected_because_v0_sentinel(tmp_path):
    """Legacy synthesis is schema_version=0; validate() rejects it (engine uses separate path)."""
    legacy = cfg.synthesize_legacy_config(tmp_path)
    with pytest.raises(cfg.ConfigValidationError, match="schema_version"):
        cfg.validate(legacy)
