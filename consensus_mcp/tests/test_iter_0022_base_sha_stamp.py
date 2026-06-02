"""Unit tests for iter-0022 - helper stamps patch_proposal.base_sha from review-packet.

Per iter-0021 empirical finding: codex's read-only sandbox cannot reliably
compute or propagate base_sha; iter-0021 confirmed codex emits a hallucinated
base_sha while operator-supplied defect_target.base_sha is the canonical value
(verified via bundle_sha). The drift gate correctly refused apply, blocking
end-to-end closure.

iter-0022 fix: helper validator OVERWRITES patch_proposal.base_sha with
review_packet.defect_target.base_sha (the canonical operator-stamped value).
Codex's emission of base_sha is ignored; the schema still requires the field
(codex emits any string) but the helper authoritatively replaces it post-parse.

Tests:
  1. Helper stamps base_sha from defect_target when review_packet supplied.
  2. Backward compat: no review_packet -> codex's emission kept.
  3. Partial review_packet without defect_target.base_sha -> codex emission kept.
"""
from __future__ import annotations

import json as _json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _dispatch_codex  # noqa: E402


_CODEX_EMITTED_BASE_SHA = "f612e7d465f2a027cf470fd69251265b0f6d2f15de766bd203235fd50c6db3d9"
_HELPER_STAMPED_BASE_SHA = "0c9507575598bfebc25afe42eaa27358f967fc1ccbea287af7ae695f07adb94b"


def _build_finding_with_patch(base_sha: str) -> dict:
    """One finding with a well-formed patch_proposal carrying the given base_sha."""
    return {
        "id": "codex-rev-001",
        "severity": "medium",
        "summary": "Test finding for iter-0022",
        "citation": "consensus_mcp/_dispatch_codex.py:42",
        "risk": "Low; test fixture",
        "recommendation": "Test recommendation",
        "patch_proposal": {
            "patch_id": "codex-rev-001-patch",
            "applies_to_findings": ["codex-rev-001"],
            "base_sha": base_sha,
            "unified_diff": (
                "--- a/consensus_mcp/_dispatch_codex.py\n"
                "+++ b/consensus_mcp/_dispatch_codex.py\n"
                "@@ -1 +1,2 @@\n"
                " hello\n"
                "+world\n"
            ),
            "files_touched": ["consensus_mcp/_dispatch_codex.py"],
            "expected_tests": ["pytest_smoke"],
        },
    }


def _build_codex_output_text(base_sha: str) -> str:
    return _json.dumps({
        "findings": [_build_finding_with_patch(base_sha)],
        "goal_satisfied": False,
        "goal_satisfied_rationale": "Test fixture for iter-0022 base_sha stamping",
        "blocking_objections": [],
    })


# ---------------------------------------------------------------------------
# Primary behavior: helper stamps base_sha from review_packet.defect_target
# ---------------------------------------------------------------------------


def test_helper_stamps_base_sha_from_defect_target():
    """Codex emits one base_sha; helper overwrites with review_packet.defect_target.base_sha."""
    text = _build_codex_output_text(base_sha=_CODEX_EMITTED_BASE_SHA)
    review_packet = {
        "defect_target": {
            "files": ["consensus_mcp/_dispatch_codex.py"],
            "base_sha": _HELPER_STAMPED_BASE_SHA,
        },
    }
    parsed = _dispatch_codex._parse_codex_output(
        text,
        review_packet=review_packet,
    )
    pp = parsed["findings"][0]["patch_proposal"]
    # Helper-stamped: the defect_target value wins, NOT codex's emission.
    assert pp["base_sha"] == _HELPER_STAMPED_BASE_SHA
    assert pp["base_sha"] != _CODEX_EMITTED_BASE_SHA


def test_helper_stamp_overwrites_even_when_codex_emits_empty_string():
    """Codex emission can be any string (schema permits); helper still overwrites."""
    # patch_proposal validation requires base_sha to be a string; codex must emit
    # something. Helper stamps regardless of codex's value.
    text = _build_codex_output_text(base_sha="any-codex-placeholder-value")
    review_packet = {
        "defect_target": {
            "files": ["consensus_mcp/_dispatch_codex.py"],
            "base_sha": _HELPER_STAMPED_BASE_SHA,
        },
    }
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == _HELPER_STAMPED_BASE_SHA


# ---------------------------------------------------------------------------
# Backward compat: no review_packet supplied -> codex emission kept
# ---------------------------------------------------------------------------


def test_no_review_packet_keeps_codex_base_sha():
    """When review_packet is not supplied, helper does NOT overwrite (backward compat)."""
    text = _build_codex_output_text(base_sha=_CODEX_EMITTED_BASE_SHA)
    parsed = _dispatch_codex._parse_codex_output(text)
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == _CODEX_EMITTED_BASE_SHA


def test_review_packet_none_keeps_codex_base_sha():
    """Explicit review_packet=None is equivalent to omitting it."""
    text = _build_codex_output_text(base_sha=_CODEX_EMITTED_BASE_SHA)
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=None)
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == _CODEX_EMITTED_BASE_SHA


# ---------------------------------------------------------------------------
# Partial review_packet: no defect_target or no base_sha -> codex emission kept
# ---------------------------------------------------------------------------


def test_review_packet_without_defect_target_keeps_codex_base_sha():
    """review_packet present but no defect_target -> no overwrite."""
    text = _build_codex_output_text(base_sha=_CODEX_EMITTED_BASE_SHA)
    review_packet = {"some_other_field": "value"}
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == _CODEX_EMITTED_BASE_SHA


def test_review_packet_defect_target_without_base_sha_keeps_codex_emission():
    """review_packet has defect_target but defect_target.base_sha is missing -> no overwrite."""
    text = _build_codex_output_text(base_sha=_CODEX_EMITTED_BASE_SHA)
    review_packet = {
        "defect_target": {
            "files": ["consensus_mcp/_dispatch_codex.py"],
            # no base_sha
        },
    }
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == _CODEX_EMITTED_BASE_SHA


def test_review_packet_defect_target_base_sha_non_string_keeps_codex_emission():
    """defect_target.base_sha must be a string; non-string -> no overwrite (defensive)."""
    text = _build_codex_output_text(base_sha=_CODEX_EMITTED_BASE_SHA)
    review_packet = {
        "defect_target": {"base_sha": 12345},  # int, not string
    }
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == _CODEX_EMITTED_BASE_SHA


# ---------------------------------------------------------------------------
# Stamping survives across multiple findings + multiple patch_proposals
# ---------------------------------------------------------------------------


def test_helper_stamps_base_sha_across_multiple_findings():
    """All patch_proposals in the output get the same helper-stamped base_sha."""
    f1 = _build_finding_with_patch(base_sha=_CODEX_EMITTED_BASE_SHA)
    f2 = _build_finding_with_patch(base_sha=_CODEX_EMITTED_BASE_SHA)
    f2["id"] = "codex-rev-002"
    f2["patch_proposal"]["patch_id"] = "codex-rev-002-patch"
    f2["patch_proposal"]["applies_to_findings"] = ["codex-rev-002"]
    text = _json.dumps({
        "findings": [f1, f2],
        "goal_satisfied": False,
        "goal_satisfied_rationale": "Test fixture",
        "blocking_objections": [],
    })
    review_packet = {
        "defect_target": {"base_sha": _HELPER_STAMPED_BASE_SHA},
    }
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == _HELPER_STAMPED_BASE_SHA
    assert parsed["findings"][1]["patch_proposal"]["base_sha"] == _HELPER_STAMPED_BASE_SHA


# ---------------------------------------------------------------------------
# Goal-packet kwarg still works alongside review_packet (no kwarg conflict)
# ---------------------------------------------------------------------------


def test_helper_stamp_works_with_goal_packet_kwarg():
    """Passing both goal_packet and review_packet still works; base_sha stamped."""
    text = _build_codex_output_text(base_sha=_CODEX_EMITTED_BASE_SHA)
    goal_packet = {
        "allowed_files": ["consensus_mcp/_dispatch_codex.py"],
    }
    review_packet = {
        "defect_target": {"base_sha": _HELPER_STAMPED_BASE_SHA},
    }
    parsed = _dispatch_codex._parse_codex_output(
        text, goal_packet=goal_packet, review_packet=review_packet,
    )
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == _HELPER_STAMPED_BASE_SHA
