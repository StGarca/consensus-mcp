"""Tests for the autonomy_scope check helper in
consensus_mcp/validators/scope_check.py (iter-workflow-abc-introduce).

Workflow C (autonomous-execute) auto-approves emergent scope items if
they fall within an operator-pre-declared autonomy_contract block in
the goal_packet. This test module covers the schema validator and the
approve/park/halt decision logic.
"""
from __future__ import annotations

import pytest

from consensus_mcp.validators.scope_check import (
    AUTONOMY_CONTRACT_REQUIRED_FIELDS,
    DEFAULT_HALT_ON,
    check_autonomy_scope,
    validate_autonomy_contract,
)


# ---------- validate_autonomy_contract ----------

def _valid_contract() -> dict:
    return {
        "max_iterations": 10,
        "max_wall_clock_minutes": 240,
        "allowed_file_patterns": ["consensus_mcp/**/*.py", "docs/**/*.md"],
    }


def test_valid_contract_returns_no_errors():
    assert validate_autonomy_contract(_valid_contract()) == []


def test_missing_max_iterations_returns_error():
    c = _valid_contract()
    del c["max_iterations"]
    errors = validate_autonomy_contract(c)
    assert any("max_iterations" in e for e in errors)


def test_missing_allowed_patterns_returns_error():
    c = _valid_contract()
    del c["allowed_file_patterns"]
    errors = validate_autonomy_contract(c)
    assert any("allowed_file_patterns" in e for e in errors)


def test_negative_max_iterations_returns_error():
    c = _valid_contract()
    c["max_iterations"] = -1
    errors = validate_autonomy_contract(c)
    assert any("positive int" in e for e in errors)


def test_non_list_allowed_patterns_returns_error():
    c = _valid_contract()
    c["allowed_file_patterns"] = "consensus_mcp/**/*.py"  # string, not list
    errors = validate_autonomy_contract(c)
    assert any("list of strings" in e for e in errors)


def test_skip_halt_on_unknown_condition_returns_error():
    c = _valid_contract()
    c["skip_halt_on"] = ["not_a_real_halt_condition"]
    errors = validate_autonomy_contract(c)
    assert any("unknown halt condition" in e for e in errors)


def test_skip_halt_on_known_condition_validates():
    c = _valid_contract()
    c["skip_halt_on"] = ["test_suite_regression"]
    assert validate_autonomy_contract(c) == []


def test_required_fields_constant_is_complete():
    """The required fields const must match what the validator checks."""
    c = {}  # empty contract
    errors = validate_autonomy_contract(c)
    for field in AUTONOMY_CONTRACT_REQUIRED_FIELDS:
        assert any(field in e for e in errors), f"missing field {field!r} should be flagged"


# ---------- check_autonomy_scope: approved ----------

def test_approved_when_all_files_in_allowed_patterns():
    contract = _valid_contract()
    proposed = ["consensus_mcp/foo.py", "docs/bar.md"]
    result = check_autonomy_scope(proposed, contract)
    assert result["decision"] == "approved"
    assert result["violations"] == []


def test_approved_empty_proposed_files():
    """An empty proposed-scope is vacuously approved (no out-of-scope items)."""
    contract = _valid_contract()
    result = check_autonomy_scope([], contract)
    assert result["decision"] == "approved"


# ---------- check_autonomy_scope: parked ----------

def test_parked_when_file_outside_allowed_patterns():
    contract = _valid_contract()
    proposed = ["consensus_mcp/foo.py", "random/path/file.py"]
    result = check_autonomy_scope(proposed, contract)
    assert result["decision"] == "parked"
    assert "random/path/file.py" in result["violations"]


def test_parked_with_multiple_out_of_scope():
    contract = _valid_contract()
    proposed = ["consensus_mcp/ok.py", "out1.txt", "out2.py"]
    result = check_autonomy_scope(proposed, contract)
    assert result["decision"] == "parked"
    assert set(result["violations"]) == {"out1.txt", "out2.py"}


# ---------- check_autonomy_scope: halt ----------

def test_halt_when_file_in_forbidden_patterns():
    contract = _valid_contract()
    contract["forbidden_file_patterns"] = [".github/**", "pyproject.toml"]
    proposed = ["consensus_mcp/ok.py", "pyproject.toml"]
    result = check_autonomy_scope(proposed, contract)
    assert result["decision"] == "halt"
    assert "pyproject.toml" in result["violations"]


def test_halt_takes_precedence_over_parked():
    """If both forbidden AND out-of-allowed files are present, halt wins."""
    contract = _valid_contract()
    contract["forbidden_file_patterns"] = [".github/**"]
    proposed = ["random/out.txt", ".github/workflows/release.yml"]
    result = check_autonomy_scope(proposed, contract)
    assert result["decision"] == "halt"


def test_halt_when_contract_invalid():
    """An invalid contract halts even before checking files."""
    proposed = ["any_file.py"]
    bad_contract = {}  # missing required fields
    result = check_autonomy_scope(proposed, bad_contract)
    assert result["decision"] == "halt"
    assert "autonomy_contract is invalid" in result["reason"]


# ---------- glob matching edge cases ----------

def test_glob_pattern_matches_nested_dir():
    contract = _valid_contract()
    proposed = ["consensus_mcp/contributors/codex.py"]
    result = check_autonomy_scope(proposed, contract)
    assert result["decision"] == "approved"


def test_glob_pattern_does_not_match_outside():
    contract = {
        "max_iterations": 10,
        "max_wall_clock_minutes": 240,
        "allowed_file_patterns": ["consensus_mcp/contributors/**"],
    }
    proposed = ["consensus_mcp/_dispatch_codex.py"]  # outside contributors/
    result = check_autonomy_scope(proposed, contract)
    assert result["decision"] == "parked"


# ---------- DEFAULT_HALT_ON constant ----------

def test_default_halt_on_includes_all_safety_conditions():
    """The minimal halt set per iter-workflow-abc-introduce convergence."""
    required = {
        "blocking_objection",
        "test_suite_regression",
        "schema_change_proposed",
        "max_iterations_exceeded",
        "max_wall_clock_minutes_exceeded",
        "convergence_failure_after_n_rounds",
        "reviewer_dispatch_permanent_failure",
        "files_outside_allowed_patterns",
        "files_in_forbidden_patterns",
        "operator_interrupt_file_present",
        "reviewer_explicit_recommend_operator_review",
    }
    assert required.issubset(set(DEFAULT_HALT_ON))
