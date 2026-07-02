"""Behavior tests for the gate.evaluate_production_with_scope_match MCP tool.

First coverage for consensus_mcp/tools/gate_evaluate_production_with_scope_match.py
(live server trust-surface module, previously zero tests). Exercises:

- every production_state the evaluator can return (not_ready /
  ready_pending_operator_approval / approved) with exact-output assertions;
- strict scope-match logic (exact vs prefix modes, direction, boundary edges);
- three-way hash binds (target / consensus / verification canonical sha256);
- refusal paths (missing files, malformed YAML, non-mapping roots, missing
  structural fields, type mismatch, invalid scope_match_mode);
- fail-closed vs fail-open behavior at each layer (asserting ACTUAL behavior);
- relative-vs-absolute path resolution and register(registry) wiring.

Provenance: v2.2.1 audit M0.1b (docs/audits/2026-07-01-v2.2.1-repo-audit.md)
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from consensus_mcp.tools import gate_evaluate_production_with_scope_match as tool

CURRENT_TARGET_SHA = "a" * 64


# ---------------------------------------------------------------------------
# Fixture-state builders
# ---------------------------------------------------------------------------


def _canonical_sha(data) -> str:
    """Independent reimplementation of the tool's canonical YAML hashing.

    Deliberately NOT the module's private helper, so a regression in the
    tool's canonicalization (load -> safe_dump(sort_keys=True) -> sha256)
    shows up as a bind mismatch here instead of a tautological pass.
    """
    return hashlib.sha256(
        yaml.safe_dump(data, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _consensus_doc(**overrides) -> dict:
    doc = {
        "production_scope": {
            "type": "deploy",
            "target": "ssb-ch1",
            "scope_match_mode": "exact",
        },
        "production_clearances": {"codex": "approved", "claude": "approved"},
        "unresolved_disagreements": [],
        "implementation_scope": {"files": ["module.py"]},
    }
    doc.update(overrides)
    return doc


def _verification_doc(**overrides) -> dict:
    doc = {"passed": True, "scope_check": {"passed": True}}
    doc.update(overrides)
    return doc


def _approval_doc(consensus: dict, verification: dict, **overrides) -> dict:
    doc = {
        "production_scope": {"type": "deploy", "target": "ssb-ch1"},
        "approved_target_sha256": CURRENT_TARGET_SHA,
        "approved_consensus_sha256": _canonical_sha(consensus),
        "approved_verification_sha256": _canonical_sha(verification),
    }
    doc.update(overrides)
    return doc


def _write_state(
    tmp_path: Path, consensus: dict, verification: dict, approval: dict
) -> tuple[Path, Path, Path]:
    cpath = tmp_path / "consensus.yaml"
    vpath = tmp_path / "verification.yaml"
    apath = tmp_path / "approval.yaml"
    cpath.write_text(yaml.safe_dump(consensus), encoding="utf-8")
    vpath.write_text(yaml.safe_dump(verification), encoding="utf-8")
    apath.write_text(yaml.safe_dump(approval), encoding="utf-8")
    return cpath, vpath, apath


def _ready_state(
    tmp_path: Path,
    consensus: dict | None = None,
    verification: dict | None = None,
    approval_overrides: dict | None = None,
) -> tuple[Path, Path, Path]:
    """Write a fully-bound, technically-ready iteration into tmp_path."""
    consensus = consensus if consensus is not None else _consensus_doc()
    verification = verification if verification is not None else _verification_doc()
    approval = _approval_doc(consensus, verification, **(approval_overrides or {}))
    return _write_state(tmp_path, consensus, verification, approval)


def _call(cpath, vpath, apath, sha: str = CURRENT_TARGET_SHA) -> dict:
    return tool.handle(str(cpath), str(vpath), str(apath), sha)


def _finding_ids(result: dict) -> list[str]:
    return [f["id"] for f in result["gate_findings"]]


# ---------------------------------------------------------------------------
# Happy path: the approved verdict, exact output shape
# ---------------------------------------------------------------------------


def test_fully_ready_and_bound_returns_approved_exact_payload(tmp_path):
    cpath, vpath, apath = _ready_state(tmp_path)
    result = _call(cpath, vpath, apath)
    assert result == {
        "production_state": "approved",
        "gate_findings": [],
        "operator_production_scope_match_strict_check": True,
        "scope_match_mode_used": "exact",
        "consensus_target": "ssb-ch1",
        "approval_target": "ssb-ch1",
        "technical_readiness": {
            "codex_production_clearance": True,
            "claude_production_clearance": True,
            "verification_passed": True,
            "production_scope_verified": True,
            "unresolved_consensus_disagreements_empty": True,
        },
    }


def test_list_valued_implementation_scope_is_accepted(tmp_path):
    consensus = _consensus_doc(implementation_scope=["module.py", "other.py"])
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "approved"
    assert result["technical_readiness"]["production_scope_verified"] is True


def test_unknown_scope_type_is_not_validated_against_enum(tmp_path):
    """VALID_SCOPE_TYPES exists in the module but is never enforced: a scope
    type outside {render, merge, deploy, data-mutation} evaluates normally as
    long as both sides agree. Documents actual behavior (see bugs_found)."""
    consensus = _consensus_doc(
        production_scope={
            "type": "banana",
            "target": "ssb-ch1",
            "scope_match_mode": "exact",
        }
    )
    verification = _verification_doc()
    approval = _approval_doc(
        consensus,
        verification,
        production_scope={"type": "banana", "target": "ssb-ch1"},
    )
    cpath, vpath, apath = _write_state(tmp_path, consensus, verification, approval)
    result = _call(cpath, vpath, apath)
    assert "error" not in result
    assert result["production_state"] == "approved"


# ---------------------------------------------------------------------------
# Scope-match logic: exact vs prefix, direction, edges
# ---------------------------------------------------------------------------


def test_default_scope_match_mode_is_exact_and_rejects_prefix_shape(tmp_path):
    """No scope_match_mode key -> 'exact' default; a child target that WOULD
    prefix-match must NOT match under the strict default."""
    consensus = _consensus_doc(
        production_scope={"type": "deploy", "target": "ssb-ch1-final"}
    )
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["scope_match_mode_used"] == "exact"
    assert result["operator_production_scope_match_strict_check"] is False
    assert result["production_state"] == "ready_pending_operator_approval"
    assert _finding_ids(result) == ["OPERATOR_SCOPE_MISMATCH"]
    mismatch = result["gate_findings"][0]
    assert mismatch["severity"] == "high"
    assert mismatch["field"] == "consensus.production_scope.target"
    assert "'ssb-ch1-final'" in mismatch["claim"]
    assert "'ssb-ch1'" in mismatch["claim"]


def test_prefix_mode_parent_approval_matches_child_consensus(tmp_path):
    """The documented prefix semantics: operator approves parent 'ssb-ch1',
    consensus narrows to child 'ssb-ch1-final' -> match -> approved."""
    consensus = _consensus_doc(
        production_scope={
            "type": "deploy",
            "target": "ssb-ch1-final",
            "scope_match_mode": "prefix",
        }
    )
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "approved"
    assert result["operator_production_scope_match_strict_check"] is True
    assert result["scope_match_mode_used"] == "prefix"
    assert result["consensus_target"] == "ssb-ch1-final"
    assert result["approval_target"] == "ssb-ch1"
    assert result["gate_findings"] == []


def test_prefix_mode_is_directional_wider_consensus_rejected(tmp_path):
    """Prefix is consensus.startswith(approval): a consensus target WIDER than
    the approval (approval is the child) must NOT match."""
    consensus = _consensus_doc(
        production_scope={
            "type": "deploy",
            "target": "ssb-ch1",
            "scope_match_mode": "prefix",
        }
    )
    cpath, vpath, apath = _ready_state(
        tmp_path,
        consensus=consensus,
        approval_overrides={
            "production_scope": {"type": "deploy", "target": "ssb-ch1-final"}
        },
    )
    result = _call(cpath, vpath, apath)
    assert result["operator_production_scope_match_strict_check"] is False
    assert result["production_state"] == "ready_pending_operator_approval"
    assert _finding_ids(result) == ["OPERATOR_SCOPE_MISMATCH"]


def test_prefix_mode_matches_across_segment_boundaries(tmp_path):
    """ACTUAL behavior: prefix is a raw string prefix, not segment-bounded.
    Approval 'app/mod' also matches consensus 'app/module_evil'. Flagged in
    bugs_found as a scope-widening edge vs the docstring's parent/child
    narrative."""
    consensus = _consensus_doc(
        production_scope={
            "type": "deploy",
            "target": "app/module_evil",
            "scope_match_mode": "prefix",
        }
    )
    cpath, vpath, apath = _ready_state(
        tmp_path,
        consensus=consensus,
        approval_overrides={
            "production_scope": {"type": "deploy", "target": "app/mod"}
        },
    )
    result = _call(cpath, vpath, apath)
    assert result["operator_production_scope_match_strict_check"] is True
    assert result["production_state"] == "approved"


def test_prefix_mode_empty_approval_target_matches_everything(tmp_path):
    """ACTUAL behavior: an empty-string approval target under prefix mode
    matches ANY consensus target (str.startswith('') is always True) -- the
    scope-match layer fails OPEN for this degenerate operator input. Flagged
    in bugs_found."""
    consensus = _consensus_doc(
        production_scope={
            "type": "deploy",
            "target": "anything-at-all",
            "scope_match_mode": "prefix",
        }
    )
    cpath, vpath, apath = _ready_state(
        tmp_path,
        consensus=consensus,
        approval_overrides={"production_scope": {"type": "deploy", "target": ""}},
    )
    result = _call(cpath, vpath, apath)
    assert result["operator_production_scope_match_strict_check"] is True
    assert result["production_state"] == "approved"


def test_exact_mode_empty_targets_only_match_each_other(tmp_path):
    consensus = _consensus_doc(
        production_scope={
            "type": "deploy",
            "target": "ssb-ch1",
            "scope_match_mode": "exact",
        }
    )
    cpath, vpath, apath = _ready_state(
        tmp_path,
        consensus=consensus,
        approval_overrides={"production_scope": {"type": "deploy", "target": ""}},
    )
    result = _call(cpath, vpath, apath)
    assert result["operator_production_scope_match_strict_check"] is False
    assert result["production_state"] == "ready_pending_operator_approval"


# ---------------------------------------------------------------------------
# not_ready: technical-readiness failures (fail-closed derivations)
# ---------------------------------------------------------------------------


def test_missing_codex_clearance_yields_not_ready(tmp_path):
    consensus = _consensus_doc(
        production_clearances={"codex": "pending", "claude": "approved"}
    )
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "not_ready"
    assert _finding_ids(result) == [
        "MISSING_CODEX_CLEARANCE",
        "PRODUCTION_NOT_READY_TECHNICAL",
    ]
    assert result["technical_readiness"]["codex_production_clearance"] is False
    assert result["technical_readiness"]["claude_production_clearance"] is True


def test_missing_claude_clearance_yields_not_ready(tmp_path):
    consensus = _consensus_doc(
        production_clearances={"codex": "approved", "claude": "rejected"}
    )
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "not_ready"
    assert "MISSING_CLAUDE_CLEARANCE" in _finding_ids(result)


@pytest.mark.parametrize(
    "clearances",
    ["approved", ["codex", "claude"], None],
    ids=["string", "list", "absent"],
)
def test_non_mapping_clearances_fail_closed(tmp_path, clearances):
    """A malformed production_clearances value is treated as empty -> BOTH
    clearance findings raised, never silently approved."""
    consensus = _consensus_doc(production_clearances=clearances)
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "not_ready"
    ids = _finding_ids(result)
    assert "MISSING_CODEX_CLEARANCE" in ids
    assert "MISSING_CLAUDE_CLEARANCE" in ids


def test_absent_unresolved_disagreements_fails_closed(tmp_path):
    """Absence of the unresolved_disagreements key is NOT treated as 'empty':
    the field must be an explicit empty list."""
    consensus = _consensus_doc()
    del consensus["unresolved_disagreements"]
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "not_ready"
    assert "UNRESOLVED_DISAGREEMENTS_PRESENT" in _finding_ids(result)
    assert (
        result["technical_readiness"]["unresolved_consensus_disagreements_empty"]
        is False
    )


def test_nonempty_unresolved_disagreements_yields_not_ready(tmp_path):
    consensus = _consensus_doc(unresolved_disagreements=["codex-vs-claude on retry"])
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "not_ready"
    assert "UNRESOLVED_DISAGREEMENTS_PRESENT" in _finding_ids(result)


@pytest.mark.parametrize(
    "impl_scope",
    [None, {}, [], "src/module.py"],
    ids=["absent", "empty-dict", "empty-list", "bare-string"],
)
def test_missing_or_malformed_implementation_scope_fails_closed(tmp_path, impl_scope):
    """Empty collections AND non-collection values (a bare string) all fail
    the production_scope_verified condition."""
    consensus = _consensus_doc()
    if impl_scope is None:
        del consensus["implementation_scope"]
    else:
        consensus["implementation_scope"] = impl_scope
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "not_ready"
    assert "IMPLEMENTATION_SCOPE_MISSING" in _finding_ids(result)
    assert result["technical_readiness"]["production_scope_verified"] is False


@pytest.mark.parametrize(
    "verification",
    [
        {"passed": "true", "scope_check": {"passed": True}},
        {"passed": True},
        {"passed": True, "scope_check": {"passed": "true"}},
        {"passed": False, "scope_check": {"passed": True}},
        {"passed": True, "scope_check": "passed"},
    ],
    ids=[
        "passed-string-not-bool",
        "scope-check-absent",
        "scope-check-passed-string",
        "passed-false",
        "scope-check-not-mapping",
    ],
)
def test_verification_requires_literal_true_booleans(tmp_path, verification):
    """verification.passed and scope_check.passed must be the boolean True;
    strings, absence, and malformed shapes all fail closed."""
    cpath, vpath, apath = _ready_state(tmp_path, verification=verification)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "not_ready"
    assert "VERIFICATION_NOT_PASSED" in _finding_ids(result)
    assert result["technical_readiness"]["verification_passed"] is False


def test_not_ready_takes_precedence_over_bind_and_scope_failures(tmp_path):
    """When technical readiness fails, the state is not_ready even if binds
    and scope ALSO fail -- and every failure is still itemized in findings."""
    consensus = _consensus_doc(
        production_clearances={"codex": "pending", "claude": "approved"}
    )
    cpath, vpath, apath = _ready_state(
        tmp_path,
        consensus=consensus,
        approval_overrides={
            "production_scope": {"type": "deploy", "target": "some-other-scope"},
            "approved_target_sha256": "b" * 64,
        },
    )
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "not_ready"
    ids = _finding_ids(result)
    assert "MISSING_CODEX_CLEARANCE" in ids
    assert "PRODUCTION_NOT_READY_TECHNICAL" in ids
    assert "TARGET_SHA_DRIFT" in ids
    assert "OPERATOR_SCOPE_MISMATCH" in ids


# ---------------------------------------------------------------------------
# ready_pending_operator_approval: hash-bind drift
# ---------------------------------------------------------------------------


def test_target_sha_drift_yields_ready_pending_with_exact_finding(tmp_path):
    cpath, vpath, apath = _ready_state(
        tmp_path, approval_overrides={"approved_target_sha256": "b" * 64}
    )
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "ready_pending_operator_approval"
    assert result["operator_production_scope_match_strict_check"] is True
    assert result["gate_findings"] == [
        {
            "id": "TARGET_SHA_DRIFT",
            "severity": "high",
            "claim": "approval.approved_target_sha256 != current_target_sha256",
            "field": "approval.approved_target_sha256",
        }
    ]


def test_consensus_sha_drift_yields_ready_pending(tmp_path):
    cpath, vpath, apath = _ready_state(
        tmp_path, approval_overrides={"approved_consensus_sha256": "c" * 64}
    )
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "ready_pending_operator_approval"
    assert _finding_ids(result) == ["CONSENSUS_SHA_DRIFT"]


def test_verification_sha_drift_yields_ready_pending(tmp_path):
    cpath, vpath, apath = _ready_state(
        tmp_path, approval_overrides={"approved_verification_sha256": "d" * 64}
    )
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "ready_pending_operator_approval"
    assert _finding_ids(result) == ["VERIFICATION_SHA_DRIFT"]


def test_approval_without_bind_fields_never_approves(tmp_path):
    """Fail-closed: an approval file lacking all three approved_* hash fields
    yields ready_pending with all three drift findings, never approved."""
    consensus = _consensus_doc()
    verification = _verification_doc()
    approval = {"production_scope": {"type": "deploy", "target": "ssb-ch1"}}
    cpath, vpath, apath = _write_state(tmp_path, consensus, verification, approval)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "ready_pending_operator_approval"
    assert _finding_ids(result) == [
        "TARGET_SHA_DRIFT",
        "CONSENSUS_SHA_DRIFT",
        "VERIFICATION_SHA_DRIFT",
    ]


def test_canonical_hash_ignores_yaml_key_order_and_comments(tmp_path):
    """The consensus bind hashes CANONICAL YAML (re-dump with sorted keys), so
    reordered keys + comments in the on-disk file still match an approval hash
    computed from the equivalent mapping."""
    consensus = _consensus_doc()
    verification = _verification_doc()
    approval = _approval_doc(consensus, verification)
    cpath, vpath, apath = _write_state(tmp_path, consensus, verification, approval)
    # Rewrite consensus.yaml with keys deliberately in reverse-sorted order
    # plus a comment; the loaded mapping is identical.
    scrambled_lines = ["# scrambled-on-disk ordering\n"]
    for key in sorted(consensus, reverse=True):
        scrambled_lines.append(yaml.safe_dump({key: consensus[key]}))
    cpath.write_text("".join(scrambled_lines), encoding="utf-8")
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "approved"
    assert result["gate_findings"] == []


def test_consensus_edited_after_approval_is_caught(tmp_path):
    """A semantic edit to consensus.yaml AFTER the approval hash was minted
    surfaces as CONSENSUS_SHA_DRIFT (technical readiness still passing)."""
    cpath, vpath, apath = _ready_state(tmp_path)
    edited = _consensus_doc(implementation_scope={"files": ["module.py", "extra.py"]})
    cpath.write_text(yaml.safe_dump(edited), encoding="utf-8")
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "ready_pending_operator_approval"
    assert _finding_ids(result) == ["CONSENSUS_SHA_DRIFT"]


# ---------------------------------------------------------------------------
# Refusal paths: missing / malformed inputs
# ---------------------------------------------------------------------------


def test_missing_consensus_yaml_refuses(tmp_path):
    cpath = tmp_path / "consensus.yaml"  # never written
    _, vpath, apath = _ready_state(tmp_path)
    (tmp_path / "consensus.yaml").unlink()
    result = _call(cpath, vpath, apath)
    assert result == {"error": "consensus_yaml_not_found", "detail": str(cpath)}


def test_missing_verification_yaml_refuses(tmp_path):
    cpath, vpath, apath = _ready_state(tmp_path)
    vpath.unlink()
    result = _call(cpath, vpath, apath)
    assert result == {"error": "verification_yaml_not_found", "detail": str(vpath)}


def test_missing_approval_yaml_refuses(tmp_path):
    cpath, vpath, apath = _ready_state(tmp_path)
    apath.unlink()
    result = _call(cpath, vpath, apath)
    assert result == {"error": "approval_yaml_not_found", "detail": str(apath)}


def test_unparseable_yaml_refuses_with_invalid_yaml(tmp_path):
    cpath, vpath, apath = _ready_state(tmp_path)
    cpath.write_text("production_scope: [unclosed\n  bad: :\n", encoding="utf-8")
    result = _call(cpath, vpath, apath)
    assert result["error"] == "invalid_yaml"
    assert str(cpath) in result["detail"]


@pytest.mark.parametrize(
    ("raw_text", "type_name"),
    [
        ("- a\n- b\n", "list"),
        ("just-a-plain-scalar\n", "str"),
        ("", "NoneType"),
    ],
    ids=["list-root", "scalar-root", "empty-file"],
)
def test_non_mapping_yaml_root_refuses(tmp_path, raw_text, type_name):
    cpath, vpath, apath = _ready_state(tmp_path)
    cpath.write_text(raw_text, encoding="utf-8")
    result = _call(cpath, vpath, apath)
    assert result["error"] == "invalid_yaml"
    assert f"root is not a mapping (got {type_name})" in result["detail"]


def test_unreadable_consensus_path_refuses_with_invalid_yaml(tmp_path):
    """A directory where consensus.yaml should be: exists() passes but the
    read fails -> invalid_yaml refusal, not a crash."""
    _, vpath, apath = _ready_state(tmp_path)
    dir_as_file = tmp_path / "consensus-dir"
    dir_as_file.mkdir()
    result = _call(dir_as_file, vpath, apath)
    assert result["error"] == "invalid_yaml"
    assert str(dir_as_file) in result["detail"]


@pytest.mark.parametrize(
    "scope_value",
    [None, "deploy:ssb-ch1", ["deploy", "ssb-ch1"]],
    ids=["absent", "string", "list"],
)
def test_missing_or_non_mapping_consensus_production_scope_refuses(
    tmp_path, scope_value
):
    consensus = _consensus_doc()
    if scope_value is None:
        del consensus["production_scope"]
    else:
        consensus["production_scope"] = scope_value
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["error"] == "missing_production_scope"
    assert "consensus.production_scope absent or non-mapping" in result["detail"]


def test_non_string_consensus_scope_fields_refuse(tmp_path):
    consensus = _consensus_doc(
        production_scope={"type": "deploy", "target": 123, "scope_match_mode": "exact"}
    )
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["error"] == "missing_consensus_field"
    assert "target=123" in result["detail"]
    assert "type='deploy'" in result["detail"]


def test_missing_approval_production_scope_refuses(tmp_path):
    consensus = _consensus_doc()
    verification = _verification_doc()
    approval = _approval_doc(consensus, verification)
    del approval["production_scope"]
    cpath, vpath, apath = _write_state(tmp_path, consensus, verification, approval)
    result = _call(cpath, vpath, apath)
    assert result["error"] == "missing_approval_field"
    assert "approval.production_scope absent or non-mapping" in result["detail"]


def test_non_string_approval_scope_fields_refuse(tmp_path):
    cpath, vpath, apath = _ready_state(
        tmp_path,
        approval_overrides={"production_scope": {"type": "deploy", "target": None}},
    )
    result = _call(cpath, vpath, apath)
    assert result["error"] == "missing_approval_field"
    assert "target=None" in result["detail"]


def test_scope_type_mismatch_refuses(tmp_path):
    cpath, vpath, apath = _ready_state(
        tmp_path,
        approval_overrides={"production_scope": {"type": "merge", "target": "ssb-ch1"}},
    )
    result = _call(cpath, vpath, apath)
    assert result["error"] == "scope_type_mismatch"
    assert "'deploy'" in result["detail"]
    assert "'merge'" in result["detail"]


def test_invalid_scope_match_mode_refuses(tmp_path):
    consensus = _consensus_doc(
        production_scope={
            "type": "deploy",
            "target": "ssb-ch1",
            "scope_match_mode": "glob",
        }
    )
    cpath, vpath, apath = _ready_state(tmp_path, consensus=consensus)
    result = _call(cpath, vpath, apath)
    assert result["error"] == "invalid_scope_match_mode"
    assert "'glob'" in result["detail"]
    assert "['exact', 'prefix']" in result["detail"]


def test_type_mismatch_is_checked_before_match_mode(tmp_path):
    """Evaluation order (spec section 17): step 4 type-compare refuses before
    step 5 ever inspects the (also-invalid) scope_match_mode."""
    consensus = _consensus_doc(
        production_scope={
            "type": "deploy",
            "target": "ssb-ch1",
            "scope_match_mode": "glob",
        }
    )
    cpath, vpath, apath = _ready_state(
        tmp_path,
        consensus=consensus,
        approval_overrides={"production_scope": {"type": "merge", "target": "ssb-ch1"}},
    )
    result = _call(cpath, vpath, apath)
    assert result["error"] == "scope_type_mismatch"


def test_missing_positional_arguments_raise_typeerror(tmp_path):
    """Round 6 F9 disclosure in the module docstring: entirely-missing
    arguments are a Python TypeError, NOT a structured error return."""
    with pytest.raises(TypeError):
        tool.handle(str(tmp_path / "consensus.yaml"))


# ---------------------------------------------------------------------------
# Path resolution + module attribute compat
# ---------------------------------------------------------------------------


def test_relative_paths_resolve_against_project_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(tmp_path))
    _ready_state(tmp_path)
    result = tool.handle(
        "consensus.yaml", "verification.yaml", "approval.yaml", CURRENT_TARGET_SHA
    )
    assert result["production_state"] == "approved"


def test_absolute_paths_are_honored_verbatim(tmp_path, monkeypatch):
    """Absolute paths bypass the project_root prefix entirely (the operator
    approval store lives OUTSIDE the repo by design)."""
    elsewhere = tmp_path / "unrelated-project-root"
    elsewhere.mkdir()
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(elsewhere))
    store = tmp_path / "store"
    store.mkdir()
    cpath, vpath, apath = _ready_state(store)
    result = _call(cpath, vpath, apath)
    assert result["production_state"] == "approved"


def test_repo_root_module_attr_is_lazy_project_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(tmp_path))
    assert tool.REPO_ROOT == Path(str(tmp_path)).resolve()


def test_unknown_module_attr_raises_attributeerror():
    with pytest.raises(AttributeError):
        getattr(tool, "NOT_A_REAL_ATTRIBUTE")


# ---------------------------------------------------------------------------
# Read-only contract + registry wiring
# ---------------------------------------------------------------------------


def test_evaluation_writes_no_files(tmp_path):
    cpath, vpath, apath = _ready_state(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    contents_before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    _call(cpath, vpath, apath)
    after = sorted(p.name for p in tmp_path.iterdir())
    assert after == before
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == contents_before


def test_register_attaches_tool_under_canonical_name():
    from consensus_mcp.tool_registry import ToolRegistry

    registry = ToolRegistry()
    tool.register(registry)
    names = {t["name"] for t in registry.list_tools()}
    assert "gate.evaluate_production_with_scope_match" in names
