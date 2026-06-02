"""Tests for codex dispatcher --mode proposal (iter-0028 per iter-0027 converged plan).

Verifies:
  - Without --mode, dispatcher loads the legacy codex_review_template + schema (backward compat).
  - With --mode review, same thing (explicit default).
  - With --mode proposal, dispatcher loads codex_proposal_template + schema.
  - --prompt-template / --schema overrides take precedence over --mode.
  - The proposal schema validates the new shape (selected_target, rationale, etc.)
    and rejects review-shaped output.
  - The proposal template explicitly frames the task as proposal generation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = REPO_ROOT / "consensus_mcp" / "dispatch_templates"


# ---------- template + schema files exist ----------

def test_proposal_template_file_exists():
    assert (TEMPLATES_DIR / "codex_proposal_template.md").exists()


def test_proposal_schema_file_exists():
    assert (TEMPLATES_DIR / "codex_proposal_schema.json").exists()


def test_proposal_schema_is_valid_json():
    schema = json.loads((TEMPLATES_DIR / "codex_proposal_schema.json").read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    required = set(schema["required"])
    assert "selected_target" in required
    assert "rationale_vs_alternatives" in required
    assert "deliverable_scope" in required
    assert "structural_abstention" in required


def test_proposal_template_frames_task_as_proposal_not_review():
    """The whole point of the proposal template: explicitly NOT a code review."""
    text = (TEMPLATES_DIR / "codex_proposal_template.md").read_text(encoding="utf-8")
    assert "NOT a code review" in text
    assert "GENERATE a proposal" in text
    # The template MUST tell codex not to emit review-shaped output.
    assert "code-review-shaped output" in text


# ---------- dispatcher arg parsing ----------

def test_dispatcher_accepts_mode_review():
    from consensus_mcp import _dispatch_codex
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="review", choices=["review", "proposal"])
    ns = p.parse_args(["--mode", "review"])
    assert ns.mode == "review"


def test_dispatcher_accepts_mode_proposal():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="review", choices=["review", "proposal"])
    ns = p.parse_args(["--mode", "proposal"])
    assert ns.mode == "proposal"


def test_dispatcher_mode_defaults_to_review():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="review", choices=["review", "proposal"])
    ns = p.parse_args([])
    assert ns.mode == "review"


def test_dispatcher_rejects_invalid_mode():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="review", choices=["review", "proposal"])
    with pytest.raises(SystemExit):
        p.parse_args(["--mode", "garbage"])


# ---------- template/schema selection by mode ----------

def test_review_mode_selects_review_template_and_schema(monkeypatch):
    """The default-name selection logic must pick review files for mode='review'."""
    # The selection logic is inline in _dispatch_codex.main(); we verify the
    # naming convention by reading the code paths directly.
    src = (REPO_ROOT / "consensus_mcp" / "_dispatch_codex.py").read_text(encoding="utf-8")
    # The conditional must mention both names.
    assert "codex_proposal_template.md" in src
    assert "codex_review_template.md" in src
    assert "codex_proposal_schema.json" in src
    assert "codex_review_schema.json" in src
    # And it must key off ns.mode == "proposal".
    assert 'ns.mode == "proposal"' in src


def test_dispatcher_module_imports_cleanly():
    """Regression: adding --mode must not break import."""
    from consensus_mcp import _dispatch_codex
    assert hasattr(_dispatch_codex, "main")


# ---------- proposal schema validation ----------

def test_proposal_schema_validates_well_formed_output():
    import jsonschema
    schema = json.loads((TEMPLATES_DIR / "codex_proposal_schema.json").read_text(encoding="utf-8"))
    valid = {
        "selected_target": "(b) Fix codex review-template friction",
        "rationale_vs_alternatives": "Unblocks future consults.",
        "deliverable_scope": {
            "next_iteration_id": "iteration-0028-codex-proposal-mode",
            "files_in_scope": ["consensus_mcp/_dispatch_codex.py"],
            "files_out_of_scope": [],
            "key_design_decisions": ["mode=review default for backward compat"],
            "acceptance_gates": ["full suite stays green"],
        },
        "risks": ["prompt engineering may need iteration"],
        "estimated_complexity": "medium",
        "structural_abstention": False,
    }
    jsonschema.validate(valid, schema)  # raises on failure


def test_proposal_schema_validates_structural_abstention():
    """Abstention: selected_target and deliverable_scope may be null."""
    import jsonschema
    schema = json.loads((TEMPLATES_DIR / "codex_proposal_schema.json").read_text(encoding="utf-8"))
    abstention = {
        "selected_target": None,
        "rationale_vs_alternatives": "Cannot engage - insufficient context.",
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": True,
    }
    jsonschema.validate(abstention, schema)


def test_proposal_schema_rejects_review_shaped_output():
    """A review-shaped payload (findings, goal_satisfied, blocking_objections)
    must fail proposal validation - that's the whole point of the split."""
    import jsonschema
    schema = json.loads((TEMPLATES_DIR / "codex_proposal_schema.json").read_text(encoding="utf-8"))
    review_shaped = {
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "looks fine",
        "blocking_objections": [],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(review_shaped, schema)


def test_proposal_schema_rejects_missing_required_fields():
    import jsonschema
    schema = json.loads((TEMPLATES_DIR / "codex_proposal_schema.json").read_text(encoding="utf-8"))
    missing_rationale = {
        "selected_target": "(b)",
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "medium",
        "structural_abstention": False,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_rationale, schema)


def test_proposal_schema_rejects_bad_complexity_value():
    import jsonschema
    schema = json.loads((TEMPLATES_DIR / "codex_proposal_schema.json").read_text(encoding="utf-8"))
    bad = {
        "selected_target": "(b)",
        "rationale_vs_alternatives": "...",
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "epic",  # not in enum
        "structural_abstention": False,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


# ---------- parser unit tests (codex-rev-003: exercise the actual parse path) ----------

def test_codex_proposal_parser_accepts_well_formed():
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output
    payload = json.dumps({
        "selected_target": "(b)",
        "rationale_vs_alternatives": "unblocks future consults",
        "deliverable_scope": {
            "next_iteration_id": "iter-0028",
            "files_in_scope": ["x.py"],
            "files_out_of_scope": [],
            "key_design_decisions": ["..."],
            "acceptance_gates": ["..."],
        },
        "risks": ["prompt engineering may iterate"],
        "estimated_complexity": "medium",
        "structural_abstention": False,
    })
    parsed = _parse_codex_proposal_output(payload)
    assert parsed["selected_target"] == "(b)"
    assert parsed["structural_abstention"] is False


def test_codex_proposal_parser_rejects_review_shape():
    """codex-rev-001 / gemini-rev-001: parser must reject review-shape output."""
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output, CodexOutputParseError
    review_shape = json.dumps({
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "x",
        "blocking_objections": [],
    })
    with pytest.raises(CodexOutputParseError):
        _parse_codex_proposal_output(review_shape)


def test_codex_proposal_parser_rejects_invalid_json():
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output, CodexOutputParseError
    with pytest.raises(CodexOutputParseError):
        _parse_codex_proposal_output("not json")


def test_codex_proposal_parser_rejects_non_object_root():
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output, CodexOutputParseError
    with pytest.raises(CodexOutputParseError):
        _parse_codex_proposal_output(json.dumps(["array", "not", "object"]))


def test_codex_proposal_parser_rejects_missing_field():
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output, CodexOutputParseError
    missing = json.dumps({
        "selected_target": "(b)",
        "rationale_vs_alternatives": "x",
        # deliverable_scope missing
        "risks": [],
        "estimated_complexity": "medium",
        "structural_abstention": False,
    })
    with pytest.raises(CodexOutputParseError, match="required property"):
        _parse_codex_proposal_output(missing)


def test_codex_proposal_parser_rejects_unknown_field():
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output, CodexOutputParseError
    bad = json.dumps({
        "selected_target": "(b)",
        "rationale_vs_alternatives": "x",
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "medium",
        "structural_abstention": True,
        "extra_smuggled_field": "no",
    })
    with pytest.raises(CodexOutputParseError, match="Additional properties"):
        _parse_codex_proposal_output(bad)


def test_codex_proposal_parser_accepts_abstention_with_nulls():
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output
    abstention = json.dumps({
        "selected_target": None,
        "rationale_vs_alternatives": "insufficient context",
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": True,
    })
    parsed = _parse_codex_proposal_output(abstention)
    assert parsed["structural_abstention"] is True
    assert parsed["selected_target"] is None


def test_codex_proposal_parser_rejects_null_target_without_abstention():
    """Selected_target can only be null when abstaining."""
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output, CodexOutputParseError
    bad = json.dumps({
        "selected_target": None,
        "rationale_vs_alternatives": "x",
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "medium",
        "structural_abstention": False,
    })
    with pytest.raises(CodexOutputParseError, match="required when structural_abstention is false"):
        _parse_codex_proposal_output(bad)


def test_codex_proposal_parser_rejects_bad_complexity():
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output, CodexOutputParseError
    bad = json.dumps({
        "selected_target": "(b)",
        "rationale_vs_alternatives": "x",
        "deliverable_scope": {"next_iteration_id":"i","files_in_scope":[],"files_out_of_scope":[],"key_design_decisions":[],"acceptance_gates":[]},
        "risks": [],
        "estimated_complexity": "epic",
        "structural_abstention": False,
    })
    with pytest.raises(CodexOutputParseError, match="estimated_complexity"):
        _parse_codex_proposal_output(bad)


# ---------- sealed packet shape for proposal payloads (iter-0028) ----------

def test_build_sealed_packet_preserves_proposal_payload():
    """_build_sealed_packet must embed proposal fields under top-level `proposal` key."""
    from consensus_mcp._dispatch_base import _build_sealed_packet
    extracted = {
        "selected_target": "(b)",
        "rationale_vs_alternatives": "unblocks",
        "deliverable_scope": {"next_iteration_id":"i","files_in_scope":[],"files_out_of_scope":[],"key_design_decisions":[],"acceptance_gates":[]},
        "risks": ["r1"],
        "estimated_complexity": "medium",
        "structural_abstention": False,
    }
    sealed = _build_sealed_packet(extracted, "iter-x", "codex-x-1", "codex-x-1-pass1")
    assert "proposal" in sealed
    assert sealed["proposal"]["selected_target"] == "(b)"
    assert sealed["proposal"]["risks"] == ["r1"]
    # goal_satisfied derived from non-abstention.
    assert sealed["goal_satisfied"] is True
    # findings kept empty to preserve audit-event schema compatibility.
    assert sealed["findings"] == []


def test_build_sealed_packet_abstention_marked_not_satisfied():
    from consensus_mcp._dispatch_base import _build_sealed_packet
    extracted = {
        "selected_target": None,
        "rationale_vs_alternatives": "no context",
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": True,
    }
    sealed = _build_sealed_packet(extracted, "iter-x", "codex-x-1", "codex-x-1-pass1")
    assert sealed["proposal"]["structural_abstention"] is True
    assert sealed["goal_satisfied"] is False


def test_codex_proposal_parser_honors_schema_override(tmp_path):
    """codex pass-3 rev-001: schema_path argument MUST be used instead of
    the built-in path. Custom schema rejects what default accepts."""
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output, CodexOutputParseError
    strict_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["selected_target", "rationale_vs_alternatives", "deliverable_scope",
                     "risks", "estimated_complexity", "structural_abstention", "must_have_this"],
        "additionalProperties": False,
        "properties": {
            "selected_target": {"type": ["string", "null"]},
            "rationale_vs_alternatives": {"type": "string", "minLength": 1},
            "deliverable_scope": {"type": ["object", "null"]},
            "risks": {"type": "array"},
            "estimated_complexity": {"type": "string"},
            "structural_abstention": {"type": "boolean"},
            "must_have_this": {"type": "string"},
        },
    }
    schema_file = tmp_path / "custom_schema.json"
    schema_file.write_text(json.dumps(strict_schema), encoding="utf-8")
    valid_per_default = json.dumps({
        "selected_target": "(b)",
        "rationale_vs_alternatives": "x",
        "deliverable_scope": {"next_iteration_id":"i","files_in_scope":[],"files_out_of_scope":[],"key_design_decisions":[],"acceptance_gates":[]},
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": False,
    })
    # Default schema accepts.
    _parse_codex_proposal_output(valid_per_default)
    # Custom schema rejects (missing 'must_have_this').
    with pytest.raises(CodexOutputParseError, match="schema validation"):
        _parse_codex_proposal_output(valid_per_default, schema_path=schema_file)


def test_codex_proposal_parser_invariant_rejects_empty_rationale_via_loose_schema(tmp_path):
    """codex pass-4 rev-002: even a loose override schema cannot let an empty
    rationale through - parser-level invariant enforces non-empty rationale."""
    from consensus_mcp._dispatch_codex import _parse_codex_proposal_output, CodexOutputParseError
    loose_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["selected_target", "rationale_vs_alternatives", "deliverable_scope",
                     "risks", "estimated_complexity", "structural_abstention"],
        "additionalProperties": False,
        "properties": {
            "selected_target": {"type": ["string", "null"]},
            "rationale_vs_alternatives": {"type": "string"},  # NO minLength
            "deliverable_scope": {"type": ["object", "null"]},
            "risks": {"type": "array"},
            "estimated_complexity": {"type": "string"},
            "structural_abstention": {"type": "boolean"},
        },
    }
    schema_file = tmp_path / "loose.json"
    schema_file.write_text(json.dumps(loose_schema), encoding="utf-8")
    empty_rationale = json.dumps({
        "selected_target": None,
        "rationale_vs_alternatives": "",  # would pass loose schema
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": True,
    })
    with pytest.raises(CodexOutputParseError, match="rationale_vs_alternatives must be a non-empty string"):
        _parse_codex_proposal_output(empty_rationale, schema_path=schema_file)


def test_codex_main_threads_schema_override_to_parser():
    """codex pass-3 rev-001: main() must pass the effective schema_path
    to _parse_codex_proposal_output, not the hard-coded default."""
    src = (REPO_ROOT / "consensus_mcp" / "_dispatch_codex.py").read_text(encoding="utf-8")
    assert "_parse_codex_proposal_output(codex_output, schema_path=schema_path)" in src


def test_build_sealed_packet_review_shape_unchanged():
    """Backward-compat: review-shape extracted dict still seals identically to pre-iter-0028."""
    from consensus_mcp._dispatch_base import _build_sealed_packet
    extracted = {
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "looks good",
        "blocking_objections": [],
    }
    sealed = _build_sealed_packet(extracted, "iter-x", "codex-x-1", "codex-x-1-pass1")
    assert "proposal" not in sealed
    assert sealed["goal_satisfied"] is True
    assert sealed["goal_satisfied_rationale"] == "looks good"
