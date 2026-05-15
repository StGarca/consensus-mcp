"""Tests for v1.15.1 machine-enforcement of the converged-plan convention.

Converged plan: iteration-converged-plan-machine-enforcement
(Workflow A weighted-synthesis: claude + codex + gemini; shared-prior
self-check PASSED; no blocking objections).

These tests are the discriminating experiment named in that converged
plan's own `falsification` block (falsifiable_from_artifacts: true):

  refutation_observation: "any test shows the validator setting/implying
  an approved|correct|ready|sound state from the blocks, OR a legacy
  iter-0043..v1.15.0 plan failing to load through
  consensus_get_iteration_outcome"

Every acceptance gate in `deliverable.acceptance_gates_gating` has a
test here.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from consensus_mcp import config as cfg
from consensus_mcp.contributors.base import FakeAlwaysApprove
from consensus_mcp.tools import consensus_get_iteration_outcome as cgio
from consensus_mcp.validators import validate_converged_plan as vcp
from consensus_mcp.workflow_engine import WorkflowEngine

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

_SCHEMA_PATH = (
    Path(vcp.__file__).resolve().parent.parent
    / "schemas"
    / "converged_plan_convention.schema.json"
)


def _good_convention(**overrides) -> dict:
    base = {
        "convention_schema_version": vcp.CONVENTION_SCHEMA_VERSION,
        "falsification": {
            "hypothesis": "sdStorage_init shared-LDO disturbance perturbs the I2C rail",
            "falsifiable_from_artifacts": False,
            "discriminating_experiment": "build flag skips sdStorage_init before display; capture boot-1 serial",
            "refutation_observation": "first i2c_master_transmit still returns ESP_ERR_INVALID_STATE at i2c.cpp:91 with SD_MMC.begin() provably absent",
            "empirical_status": "pending",
        },
        "independent_safeguard": {
            "applicable": True,
            "mechanism": "boot-loop breaker: on 2nd crashed boot, skip display, continue headless",
            "works_if_root_cause_wrong": True,
            "why": "triggers on the crash symptom (reboot count), not on any I2C/LDO theory",
            "ships_with_fix": True,
        },
        "decisive_experiment_before_next_iteration": "flash unmodified reference under our toolchain",
        "cited_pass_ids": ["claude-cpme-1-pass1", "codex-cpme-1-pass1"],
    }
    base.update(overrides)
    return base


def _three_contributor_config() -> dict:
    c = deepcopy(cfg.default_config())
    c["workflow"]["mode"] = cfg.WORKFLOW_PROPOSE_CONVERGE
    c["convergence"]["rule"] = cfg.CONVERGE_STRICT_MAJ
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING
    cfg.validate(c)
    return c


def _iter_dir(tmp_path: Path, goal_extra: dict | None = None) -> tuple[Path, Path, Path]:
    d = tmp_path / "iter-cpme"
    d.mkdir()
    goal = d / "goal_packet.yaml"
    gp = {"pilot_id": "iter-cpme", "type": "design_consult"}
    if goal_extra:
        gp.update(goal_extra)
    goal.write_text(yaml.safe_dump(gp), encoding="utf-8")
    target = d / "problem.md"
    target.write_text("problem statement\n", encoding="utf-8")
    return d, goal, target


# --------------------------------------------------------------------------
# Gate: schema file is valid JSON Schema; empirical_status enum matches v1.15.0
# --------------------------------------------------------------------------

def test_schema_file_is_valid_json_and_enum_matches_v1150():
    assert _SCHEMA_PATH.exists(), f"schema missing: {_SCHEMA_PATH}"
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema.get("$schema"), "must declare a JSON Schema dialect"
    enum = schema["properties"]["falsification"]["properties"]["empirical_status"]["enum"]
    # v1.15.0 docs/workflows/converged-plan-convention.md enum, verbatim.
    assert set(enum) == {"proven", "pending", "refuted", "n/a"}


# --------------------------------------------------------------------------
# Gate: consequence rule
#   falsifiable_from_artifacts==False ⇒ discriminating_experiment &
#   refutation_observation non-empty & empirical_status∈{pending,refuted}
# --------------------------------------------------------------------------

def test_consequence_rule_satisfied_passes_presence():
    r = vcp.validate_convention(_good_convention(), risk_class=None, enforcement="graduated")
    assert r["presence_ok"] is True
    assert r["violations"] == []
    assert r["hard_reject"] is False


@pytest.mark.parametrize("bad", [
    {"discriminating_experiment": ""},
    {"refutation_observation": "  "},
    {"empirical_status": "proven"},
    {"empirical_status": "n/a"},
])
def test_consequence_rule_violation_when_not_falsifiable_from_artifacts(bad):
    conv = _good_convention()
    conv["falsification"].update(bad)
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["presence_ok"] is False
    assert any("falsifiable_from_artifacts" in v or "empirical_status" in v
               or "discriminating_experiment" in v or "refutation_observation" in v
               for v in r["violations"])


def test_falsifiable_from_artifacts_true_allows_na():
    conv = _good_convention()
    conv["falsification"] = {
        "hypothesis": "off-by-one in seal filename token count",
        "falsifiable_from_artifacts": True,
        "empirical_status": "n/a",
        "reason_na": "tooling defect; proving test ships in this iteration",
    }
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["presence_ok"] is True
    assert r["hard_reject"] is False


def test_refutation_observation_must_not_echo_hypothesis():
    conv = _good_convention()
    conv["falsification"]["refutation_observation"] = conv["falsification"]["hypothesis"]
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["presence_ok"] is False
    assert any("echo" in v.lower() or "distinct" in v.lower() for v in r["violations"])


# --------------------------------------------------------------------------
# Gate: graduated strictness — hard-reject ONLY (i) safety-class missing a
#   conforming independent_safeguard; (ii) empirical_status:proven w/o a
#   recorded experiment result. Else warn+annotate.
# --------------------------------------------------------------------------

def test_safety_class_missing_safeguard_hard_rejects_under_graduated():
    conv = _good_convention()
    conv["independent_safeguard"] = {"applicable": False, "why": "n/a"}
    r = vcp.validate_convention(conv, risk_class="bricking", enforcement="graduated")
    assert r["hard_reject"] is True
    assert any("independent_safeguard" in reason for reason in r["hard_reject_reasons"])


def test_safety_class_safeguard_not_decoupled_hard_rejects():
    conv = _good_convention()
    conv["independent_safeguard"]["works_if_root_cause_wrong"] = False
    r = vcp.validate_convention(conv, risk_class="safety", enforcement="graduated")
    assert r["hard_reject"] is True


def test_proven_without_recorded_experiment_hard_rejects():
    conv = _good_convention()
    conv["falsification"] = {
        "hypothesis": "h",
        "falsifiable_from_artifacts": False,
        "discriminating_experiment": "exp",
        "refutation_observation": "obs distinct from h",
        "empirical_status": "proven",
    }
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["hard_reject"] is True
    assert any("proven" in reason for reason in r["hard_reject_reasons"])


def test_proven_with_recorded_experiment_does_not_hard_reject():
    conv = _good_convention()
    conv["falsification"] = {
        "hypothesis": "h",
        "falsifiable_from_artifacts": False,
        "discriminating_experiment": "exp",
        "refutation_observation": "obs distinct from h",
        "empirical_status": "proven",
        "experiment_result": "ran 2026-05-15; refutation observation did NOT occur",
    }
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["hard_reject"] is False


def test_non_safety_violation_warns_not_hard_rejects_under_graduated():
    conv = _good_convention()
    conv["falsification"]["discriminating_experiment"] = ""
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["violations"]
    assert r["hard_reject"] is False  # graduated: non-safety ⇒ warn


def test_strict_hard_rejects_any_violation():
    conv = _good_convention()
    conv["falsification"]["discriminating_experiment"] = ""
    r = vcp.validate_convention(conv, risk_class=None, enforcement="strict")
    assert r["hard_reject"] is True


def test_off_disables_blocking_but_preserves_visibility():
    """codex-rev-001 (pass-3): `off` must disable BLOCKING only, not
    hide that required thinking was not recorded. Fabricating a clean
    presence pass under `off` would be the very recursive-trap
    masquerade this iteration defends against."""
    conv = _good_convention()
    conv["independent_safeguard"] = {"applicable": False, "why": "n/a"}
    r = vcp.validate_convention(conv, risk_class="bricking", enforcement="off")
    assert r["hard_reject"] is False           # off never blocks
    assert r["presence_ok"] is False           # real value preserved
    assert r["violations"]                     # real violations preserved
    assert r["enforcement_disabled"] is True   # explicit disabled status
    # Still no correctness state leaks even when disabled.
    for k in r:
        assert k.lower() not in {"approved", "correct", "sound", "ready"}


def test_warn_surfaces_violations_never_hard_rejects():
    conv = _good_convention()
    conv["independent_safeguard"] = {"applicable": False, "why": "n/a"}
    r = vcp.validate_convention(conv, risk_class="safety", enforcement="warn")
    assert r["hard_reject"] is False
    assert r["violations"]


# --------------------------------------------------------------------------
# Gate (HIGHEST-ORDER): recursive-trap structural defense.
# grep validate_converged_plan.py ⇒ ZERO approved/correct/ready/sound
# state-setting from convention blocks.
# --------------------------------------------------------------------------

def test_validator_source_sets_no_correctness_state():
    src = Path(vcp.__file__).read_text(encoding="utf-8")
    # No assignment / dict-key / return that derives an
    # approved|correct|ready|sound *state* from the convention.
    forbidden = re.compile(
        r'["\']?\b(is_)?(approved|correct|sound|ready|valid_hypothesis|'
        r'hypothesis_correct|safeguard_adequate)\b["\']?\s*[:=]',
        re.IGNORECASE,
    )
    offenders = [ln for ln in src.splitlines() if forbidden.search(ln)]
    assert not offenders, f"validator must not set correctness state: {offenders}"


def test_gate_scope_disclaimer_always_present_and_non_soundness():
    for enf in ("off", "warn", "graduated", "strict"):
        r = vcp.validate_convention(_good_convention(), risk_class=None, enforcement=enf)
        gs = r["gate_scope"]
        assert gs == vcp.GATE_SCOPE_DISCLAIMER
        low = gs.lower()
        assert "presence" in low and "not" in low and "soundness" in low
        assert "human judgement" in low or "human judgment" in low
    # The result must NOT carry any correctness boolean.
    r = vcp.validate_convention(_good_convention(), risk_class=None, enforcement="graduated")
    for k in r:
        assert k.lower() not in {"approved", "correct", "sound", "ready"}


# --------------------------------------------------------------------------
# Gate: blocks persisted INTO the sealed converged-plan.yaml (same write),
# each block path requires cited_pass_ids; provenance-by-citation.
# --------------------------------------------------------------------------

def test_cited_pass_ids_required():
    conv = _good_convention(cited_pass_ids=[])
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["presence_ok"] is False
    assert any("cited_pass_ids" in v for v in r["violations"])


def test_engine_seals_convention_into_converged_plan(tmp_path):
    config = _three_contributor_config()
    adapters = {n: FakeAlwaysApprove() for n in ("claude", "codex", "gemini")}
    engine = WorkflowEngine(config, adapters, tmp_path)
    d, goal, target = _iter_dir(tmp_path)
    (d / "convention-input.yaml").write_text(
        yaml.safe_dump({"convention": _good_convention()}), encoding="utf-8"
    )
    outcome = engine.run_iteration(d, goal, target)
    assert outcome.error is None
    plan = yaml.safe_load((d / "converged-plan.yaml").read_text(encoding="utf-8"))
    assert plan["convention"]["falsification"]["hypothesis"]
    assert plan["convention_schema_version"] == vcp.CONVENTION_SCHEMA_VERSION
    assert plan["convention_gate"]["gate_scope"] == vcp.GATE_SCOPE_DISCLAIMER
    assert plan["convention"]["cited_pass_ids"]


def test_engine_hard_reject_fails_closed_no_seal(tmp_path):
    config = _three_contributor_config()
    adapters = {n: FakeAlwaysApprove() for n in ("claude", "codex", "gemini")}
    engine = WorkflowEngine(config, adapters, tmp_path)
    d, goal, target = _iter_dir(tmp_path, goal_extra={"risk_class": "bricking"})
    bad = _good_convention()
    bad["independent_safeguard"] = {"applicable": False, "why": "n/a"}
    (d / "convention-input.yaml").write_text(
        yaml.safe_dump({"convention": bad}), encoding="utf-8"
    )
    outcome = engine.run_iteration(d, goal, target)
    assert outcome.error is not None
    assert "convention" in outcome.error.lower()
    # Fail-closed: no sealed converged-plan.yaml.
    assert not (d / "converged-plan.yaml").exists()


def test_engine_absent_convention_at_seal_is_annotated_not_doctrine_only(tmp_path):
    """codex-rev-001: a NEW convergence missing convention-input must NOT
    be sealed as doctrine-only (that would let new plans bypass
    enforcement). It is validated as missing-blocks and annotated under
    the configured level; doctrine-only is a READ-time legacy
    classification only."""
    config = _three_contributor_config()
    adapters = {n: FakeAlwaysApprove() for n in ("claude", "codex", "gemini")}
    engine = WorkflowEngine(config, adapters, tmp_path)
    d, goal, target = _iter_dir(tmp_path)  # no risk_class, no convention-input
    outcome = engine.run_iteration(d, goal, target)
    assert outcome.error is None  # graduated + non-safety ⇒ warn, still seals
    plan = yaml.safe_load((d / "converged-plan.yaml").read_text(encoding="utf-8"))
    assert plan["convention_schema_version"] == vcp.CONVENTION_SCHEMA_VERSION
    assert plan["convention_gate"]["enforcement"] == "graduated"
    assert "enforcement_status" not in plan["convention_gate"]  # NOT doctrine-only at seal
    assert plan["convention_violations"]  # missing blocks annotated
    assert plan["convention_gate"]["gate_scope"] == vcp.GATE_SCOPE_DISCLAIMER


def test_engine_absent_convention_with_safety_risk_class_hard_rejects(tmp_path):
    """codex-rev-001: a NEW safety-class convergence with no convention
    blocks must fail closed, not seal as doctrine-only."""
    config = _three_contributor_config()
    adapters = {n: FakeAlwaysApprove() for n in ("claude", "codex", "gemini")}
    engine = WorkflowEngine(config, adapters, tmp_path)
    d, goal, target = _iter_dir(tmp_path, goal_extra={"risk_class": "bricking"})
    outcome = engine.run_iteration(d, goal, target)
    assert outcome.error is not None
    assert "convention" in outcome.error.lower()
    assert not (d / "converged-plan.yaml").exists()


def test_engine_present_invalid_version_not_rewritten_to_current(tmp_path):
    """codex-rev-002 (pass-2): a present convention with a bad/missing
    schema version must be stamped VERBATIM, never silently rewritten
    to the current version (that would undo the pass-1 rev-003 fix)."""
    config = _three_contributor_config()
    adapters = {n: FakeAlwaysApprove() for n in ("claude", "codex", "gemini")}
    engine = WorkflowEngine(config, adapters, tmp_path)
    d, goal, target = _iter_dir(tmp_path)  # non-safety ⇒ graduated warn ⇒ seals
    conv = _good_convention(convention_schema_version=999)
    (d / "convention-input.yaml").write_text(
        yaml.safe_dump({"convention": conv}), encoding="utf-8"
    )
    outcome = engine.run_iteration(d, goal, target)
    assert outcome.error is None
    plan = yaml.safe_load((d / "converged-plan.yaml").read_text(encoding="utf-8"))
    assert plan["convention_schema_version"] == 999  # verbatim, NOT 1
    assert any("convention_schema_version" in v for v in plan["convention_violations"])


def test_engine_warn_seal_carries_explicit_enforcement_note(tmp_path):
    """codex-rev-001 (pass-2) transparency: a graduated warn seal of an
    incomplete/absent convention must be loudly marked, never a silent
    clean pass."""
    config = _three_contributor_config()
    adapters = {n: FakeAlwaysApprove() for n in ("claude", "codex", "gemini")}
    engine = WorkflowEngine(config, adapters, tmp_path)
    d, goal, target = _iter_dir(tmp_path)
    outcome = engine.run_iteration(d, goal, target)
    assert outcome.error is None
    plan = yaml.safe_load((d / "converged-plan.yaml").read_text(encoding="utf-8"))
    gate = plan["convention_gate"]
    assert gate["convention_present"] is False
    assert gate["presence_ok"] is False
    note = gate["enforcement_note"].lower()
    assert "not a clean pass" in note
    assert "papercut" in note  # cites the deliberate converged-plan q4/q5 rationale


def test_decisive_experiment_block_required(tmp_path):
    """codex-rev-002: decisive_experiment_before_next_iteration is one of
    the three named blocks — omitting it is a violation. null is allowed
    only for the documented proven/n-a case."""
    conv = _good_convention()
    del conv["decisive_experiment_before_next_iteration"]
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["presence_ok"] is False
    assert any("decisive_experiment" in v for v in r["violations"])


def test_decisive_experiment_null_allowed_only_for_proven_or_na():
    # n/a (falsifiable_from_artifacts true) ⇒ null is legitimate.
    conv = _good_convention()
    conv["falsification"] = {
        "hypothesis": "tooling off-by-one",
        "falsifiable_from_artifacts": True,
        "empirical_status": "n/a",
        "reason_na": "proving test ships here",
    }
    conv["decisive_experiment_before_next_iteration"] = None
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["presence_ok"] is True
    # pending (defined class) ⇒ null is NOT allowed.
    conv2 = _good_convention()  # empirical_status pending
    conv2["decisive_experiment_before_next_iteration"] = None
    r2 = vcp.validate_convention(conv2, risk_class=None, enforcement="graduated")
    assert r2["presence_ok"] is False
    assert any("decisive_experiment" in v for v in r2["violations"])


def test_schema_requires_decisive_experiment_key():
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert "decisive_experiment_before_next_iteration" in schema["required"]


def test_convention_schema_version_must_be_exactly_one():
    """codex-rev-003: a present convention with a missing or non-1
    schema version must be flagged, not defaulted to 1."""
    conv = _good_convention()
    del conv["convention_schema_version"]
    r = vcp.validate_convention(conv, risk_class=None, enforcement="graduated")
    assert r["presence_ok"] is False
    assert any("convention_schema_version" in v for v in r["violations"])
    conv2 = _good_convention(convention_schema_version=999)
    r2 = vcp.validate_convention(conv2, risk_class=None, enforcement="graduated")
    assert r2["presence_ok"] is False
    assert any("convention_schema_version" in v for v in r2["violations"])


def test_hard_reject_removes_stale_converged_plan(tmp_path):
    """codex-rev-004: a stale converged-plan.yaml from a prior run must
    not survive a later hard reject (fail-closed integrity)."""
    config = _three_contributor_config()
    adapters = {n: FakeAlwaysApprove() for n in ("claude", "codex", "gemini")}
    engine = WorkflowEngine(config, adapters, tmp_path)
    d, goal, target = _iter_dir(tmp_path, goal_extra={"risk_class": "safety"})
    # Simulate a stale successful seal from an earlier run.
    (d / "converged-plan.yaml").write_text(
        yaml.safe_dump({"iteration_id": "stale", "convention_gate": {"enforcement": "graduated"}}),
        encoding="utf-8",
    )
    bad = _good_convention()
    bad["independent_safeguard"] = {"applicable": False, "why": "n/a"}
    (d / "convention-input.yaml").write_text(
        yaml.safe_dump({"convention": bad}), encoding="utf-8"
    )
    outcome = engine.run_iteration(d, goal, target)
    assert outcome.error is not None
    # Stale obsolete plan must be gone — outcome reader must not report it.
    assert not (d / "converged-plan.yaml").exists()


# --------------------------------------------------------------------------
# Gate: legacy plan (this session iter-0043..v1.15.0, no
# convention_schema_version) loads via consensus_get_iteration_outcome,
# marked enforcement: doctrine-only; gate_scope adjacent to pass marker.
# --------------------------------------------------------------------------

def test_legacy_plan_loads_marked_doctrine_only(tmp_path):
    d = tmp_path / "iteration-0043-legacy"
    d.mkdir()
    # Shape of a real pre-v1.15.1 converged-plan.yaml (no convention keys).
    legacy = {
        "iteration_id": "iteration-0043-legacy",
        "workflow_mode": "propose-converge",
        "convergence_rule": "weighted-synthesis",
        "converged_at_round": 1,
        "rationale": "legacy",
    }
    (d / "converged-plan.yaml").write_text(yaml.safe_dump(legacy), encoding="utf-8")
    res = cgio.handle(str(d), repo_root=str(tmp_path))
    assert res["ok"] is True
    assert res["converged_plan"]["iteration_id"] == "iteration-0043-legacy"
    # NOT silently valid, NOT rejected — explicitly doctrine-only.
    assert res["enforcement"] == "doctrine-only"


def test_outcome_surfaces_gate_scope_adjacent_to_pass_marker(tmp_path):
    d = tmp_path / "iteration-cpme"
    d.mkdir()
    plan = {
        "iteration_id": "iteration-cpme",
        "convention_schema_version": vcp.CONVENTION_SCHEMA_VERSION,
        "convention": _good_convention(),
        "convention_gate": {
            "gate_scope": vcp.GATE_SCOPE_DISCLAIMER,
            "enforcement": "graduated",
            "hard_reject": False,
        },
        "convention_violations": [],
    }
    (d / "converged-plan.yaml").write_text(yaml.safe_dump(plan), encoding="utf-8")
    res = cgio.handle(str(d), repo_root=str(tmp_path))
    assert res["ok"] is True
    # gate_scope must be surfaced so a reader cannot see "passed" without it.
    assert res["convention_gate_scope"] == vcp.GATE_SCOPE_DISCLAIMER
    assert res["enforcement"] == "graduated"


# --------------------------------------------------------------------------
# Gate: config knob converged_plan_enforcement
#   (off|warn|graduated|strict; default graduated)
# --------------------------------------------------------------------------

def test_config_default_enforcement_is_graduated():
    c = cfg.default_config()
    assert c["convergence"]["converged_plan_enforcement"] == "graduated"
    cfg.validate(c)


def test_config_rejects_invalid_enforcement():
    c = cfg.default_config()
    c["convergence"]["converged_plan_enforcement"] = "bogus"
    with pytest.raises(cfg.ConfigValidationError, match="converged_plan_enforcement"):
        cfg.validate(c)


@pytest.mark.parametrize("val", ["off", "warn", "graduated", "strict"])
def test_config_accepts_all_enforcement_levels(val):
    c = cfg.default_config()
    c["convergence"]["converged_plan_enforcement"] = val
    cfg.validate(c)
