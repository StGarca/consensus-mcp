"""Tests for gemini dispatcher --mode proposal (iter-0028 per iter-0027 converged plan).

Mirrors test_dispatch_codex_proposal_mode for symmetry - both dispatchers
must support the same --mode contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = REPO_ROOT / "consensus_mcp" / "dispatch_templates"


def test_gemini_proposal_template_exists():
    assert (TEMPLATES_DIR / "gemini_proposal_template.md").exists()


def test_gemini_proposal_schema_exists():
    assert (TEMPLATES_DIR / "gemini_proposal_schema.json").exists()


def test_gemini_proposal_schema_is_valid_json():
    """gemini-rev-003: assert ALL required fields, not just a subset."""
    schema = json.loads((TEMPLATES_DIR / "gemini_proposal_schema.json").read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    required = set(schema["required"])
    expected = {
        "selected_target",
        "rationale_vs_alternatives",
        "deliverable_scope",
        "risks",
        "estimated_complexity",
        "structural_abstention",
    }
    assert required == expected, f"required mismatch: {required ^ expected}"


def test_gemini_proposal_template_frames_task_as_proposal():
    text = (TEMPLATES_DIR / "gemini_proposal_template.md").read_text(encoding="utf-8")
    assert "NOT a code review" in text
    assert "GENERATE a proposal" in text


def test_gemini_dispatcher_module_imports_cleanly():
    from consensus_mcp import _dispatch_gemini
    assert hasattr(_dispatch_gemini, "main")


def test_gemini_dispatcher_references_both_templates():
    src = (REPO_ROOT / "consensus_mcp" / "_dispatch_gemini.py").read_text(encoding="utf-8")
    assert "gemini_proposal_template.md" in src
    assert "gemini_review_template.md" in src
    assert 'ns.mode == "proposal"' in src


def test_gemini_proposal_schema_validates_well_formed():
    import jsonschema
    schema = json.loads((TEMPLATES_DIR / "gemini_proposal_schema.json").read_text(encoding="utf-8"))
    valid = {
        "selected_target": "(b)",
        "rationale_vs_alternatives": "...",
        "deliverable_scope": {
            "next_iteration_id": "iter-0028",
            "files_in_scope": [],
            "files_out_of_scope": [],
            "key_design_decisions": [],
            "acceptance_gates": [],
        },
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": False,
    }
    jsonschema.validate(valid, schema)


def test_gemini_proposal_schema_allows_abstention():
    import jsonschema
    schema = json.loads((TEMPLATES_DIR / "gemini_proposal_schema.json").read_text(encoding="utf-8"))
    abstention = {
        "selected_target": None,
        "rationale_vs_alternatives": "Cannot engage.",
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": True,
    }
    jsonschema.validate(abstention, schema)


# ---------- parser unit tests (gemini-rev-003: exercise the actual parse path) ----------

def test_gemini_proposal_parser_accepts_well_formed():
    from consensus_mcp._dispatch_gemini import _parse_gemini_proposal_output
    payload = json.dumps({
        "selected_target": "(b)",
        "rationale_vs_alternatives": "unblocks",
        "deliverable_scope": {"next_iteration_id":"i","files_in_scope":[],"files_out_of_scope":[],"key_design_decisions":[],"acceptance_gates":[]},
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": False,
    })
    assert _parse_gemini_proposal_output(payload)["selected_target"] == "(b)"


def test_gemini_proposal_parser_rejects_review_shape():
    from consensus_mcp._dispatch_gemini import _parse_gemini_proposal_output, GeminiOutputParseError
    review_shape = json.dumps({
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "x",
        "blocking_objections": [],
    })
    with pytest.raises(GeminiOutputParseError):
        _parse_gemini_proposal_output(review_shape)


def test_gemini_proposal_parser_strips_markdown_fences():
    """Gemini sometimes wraps JSON in ```json fences; parser must handle that."""
    from consensus_mcp._dispatch_gemini import _parse_gemini_proposal_output
    fenced = (
        "```json\n"
        + json.dumps({
            "selected_target": "(b)",
            "rationale_vs_alternatives": "x",
            "deliverable_scope": {"next_iteration_id":"i","files_in_scope":[],"files_out_of_scope":[],"key_design_decisions":[],"acceptance_gates":[]},
            "risks": [],
            "estimated_complexity": "small",
            "structural_abstention": False,
        })
        + "\n```"
    )
    parsed = _parse_gemini_proposal_output(fenced)
    assert parsed["selected_target"] == "(b)"


def test_gemini_proposal_parser_rejects_invalid_json():
    from consensus_mcp._dispatch_gemini import _parse_gemini_proposal_output, GeminiOutputParseError
    with pytest.raises(GeminiOutputParseError):
        _parse_gemini_proposal_output("definitely not json")


def test_gemini_proposal_parser_rejects_missing_field():
    from consensus_mcp._dispatch_gemini import _parse_gemini_proposal_output, GeminiOutputParseError
    missing = json.dumps({
        "selected_target": "(b)",
        # rationale_vs_alternatives missing
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": False,
    })
    with pytest.raises(GeminiOutputParseError, match="required property"):
        _parse_gemini_proposal_output(missing)


def test_gemini_dispatcher_threads_mode_to_retry():
    """The main() must pass ns.mode through to _invoke_gemini_with_retry."""
    src = (REPO_ROOT / "consensus_mcp" / "_dispatch_gemini.py").read_text(encoding="utf-8")
    assert "mode=ns.mode" in src


def test_gemini_proposal_parser_honors_schema_override(tmp_path):
    """codex pass-3 rev-002: schema_path argument MUST be used in place of the
    built-in. Same contract as the codex dispatcher."""
    from consensus_mcp._dispatch_gemini import _parse_gemini_proposal_output, GeminiOutputParseError
    strict_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["selected_target", "rationale_vs_alternatives", "deliverable_scope",
                     "risks", "estimated_complexity", "structural_abstention", "extra_required"],
        "additionalProperties": False,
        "properties": {
            "selected_target": {"type": ["string", "null"]},
            "rationale_vs_alternatives": {"type": "string", "minLength": 1},
            "deliverable_scope": {"type": ["object", "null"]},
            "risks": {"type": "array"},
            "estimated_complexity": {"type": "string"},
            "structural_abstention": {"type": "boolean"},
            "extra_required": {"type": "string"},
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
    _parse_gemini_proposal_output(valid_per_default)
    with pytest.raises(GeminiOutputParseError, match="schema validation"):
        _parse_gemini_proposal_output(valid_per_default, schema_path=schema_file)


def test_gemini_dispatcher_has_schema_flag():
    src = (REPO_ROOT / "consensus_mcp" / "_dispatch_gemini.py").read_text(encoding="utf-8")
    assert '"--schema"' in src
    # And it's threaded through.
    assert "proposal_schema_path" in src


def test_gemini_proposal_parser_invariant_rejects_empty_rationale(tmp_path):
    """codex pass-4 rev-002: same invariant as codex - empty rationale rejected
    regardless of schema."""
    from consensus_mcp._dispatch_gemini import _parse_gemini_proposal_output, GeminiOutputParseError
    loose_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["selected_target", "rationale_vs_alternatives", "deliverable_scope",
                     "risks", "estimated_complexity", "structural_abstention"],
        "additionalProperties": False,
        "properties": {
            "selected_target": {"type": ["string", "null"]},
            "rationale_vs_alternatives": {"type": "string"},
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
        "rationale_vs_alternatives": "   ",  # whitespace only
        "deliverable_scope": None,
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": True,
    })
    with pytest.raises(GeminiOutputParseError, match="rationale_vs_alternatives must be a non-empty string"):
        _parse_gemini_proposal_output(empty_rationale, schema_path=schema_file)


def test_gemini_provenance_records_proposal_schema_sha():
    """codex pass-4 rev-001: proposal-mode dispatches must record
    proposal_schema_path + proposal_schema_sha256 in dispatch_provenance."""
    src = (REPO_ROOT / "consensus_mcp" / "_dispatch_gemini.py").read_text(encoding="utf-8")
    assert "proposal_schema_path" in src
    assert "proposal_schema_sha256" in src
