"""Unit tests for iter-0024 F2 - per-patch base_sha helper-stamp.

Per iter-0023 operational_caveat per_patch_base_sha_reanchor_required:
the iter-0022 helper stamped every patch_proposal.base_sha with the
multi-file defect_target.base_sha. When defect_target spans more files
than any single patch (the typical Flavor B subsystem review shape),
the stamp is structurally mismatched - apply.codex_patch's drift check
computes bundle_sha against the patch's OWN files_touched and refuses
base_sha_drift.

iter-0024 fix: helper now computes a PER-PATCH base_sha from the patch's
files_touched subset against the review-packet's
defect_target.touched_files_contents map (canonical bundle_sha formula
matching _closure_invariant.bundle_sha).

Backward compat: when touched_files_contents is missing, the helper
falls back to iter-0022 behaviour (stamps defect_target.base_sha as-is).
"""
from __future__ import annotations

import hashlib
import json as _json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _dispatch_codex  # noqa: E402
from consensus_mcp._closure_invariant import _normalize_path  # noqa: E402


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical_bundle_sha(file_pairs: list[tuple[str, str]]) -> str:
    """Compute the canonical bundle_sha for a list of (path, content) pairs.

    Mirrors _closure_invariant.bundle_sha exactly so test expected values
    are derived analytically rather than copy-pasted.
    """
    parts = []
    normalised = [(_normalize_path(p), c) for p, c in file_pairs]
    for p, content in sorted(normalised):
        parts.append(f"{p}\0{_file_hash(content)}")
    canonical = "\n".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_finding(patch_id_suffix: str, files: list[str]) -> dict:
    return {
        "id": f"codex-rev-{patch_id_suffix}",
        "severity": "medium",
        "summary": "iter-0024 test finding",
        "citation": "consensus_mcp/_dispatch_codex.py:42",
        "risk": "Low; test fixture",
        "recommendation": "Test recommendation",
        "patch_proposal": {
            "patch_id": f"codex-rev-{patch_id_suffix}-patch",
            "applies_to_findings": [f"codex-rev-{patch_id_suffix}"],
            "base_sha": "codex-emitted-placeholder",
            "unified_diff": (
                f"--- a/{files[0]}\n+++ b/{files[0]}\n@@ -1 +1,2 @@\n hello\n+world\n"
            ),
            "files_touched": files,
            "expected_tests": ["pytest_smoke"],
        },
    }


def _codex_output(findings: list[dict]) -> str:
    return _json.dumps({
        "findings": findings,
        "goal_satisfied": False,
        "goal_satisfied_rationale": "iter-0024 test fixture",
        "blocking_objections": [],
    })


# ---------------------------------------------------------------------------
# Primary: per-patch base_sha computed from subset of touched_files_contents
# ---------------------------------------------------------------------------


def test_per_patch_base_sha_uses_patch_subset_not_full_defect_target():
    """F2 core fix: when defect_target lists A+B+C but the patch only touches
    A+B, the stamped base_sha is bundle_sha(A,B) not bundle_sha(A,B,C).
    """
    # Defect target: 3 files
    file_a = "consensus_mcp/_dispatch_codex.py"
    file_b = "consensus_mcp/_closure_invariant.py"
    file_c = "consensus_mcp/_validate_closure_invariant.py"
    content_a = "contents of A\n"
    content_b = "contents of B\n"
    content_c = "contents of C\n"

    # Full-set base_sha (the iter-0022-stamped value).
    full_base_sha = _canonical_bundle_sha([(file_a, content_a), (file_b, content_b), (file_c, content_c)])
    # Patch subset (A+B) base_sha - what F2 should stamp.
    subset_base_sha = _canonical_bundle_sha([(file_a, content_a), (file_b, content_b)])
    assert full_base_sha != subset_base_sha, "test fixture must distinguish subset from full"

    review_packet = {
        "defect_target": {
            "files": [file_a, file_b, file_c],
            "base_sha": full_base_sha,
            "touched_files_contents": {
                file_a: content_a,
                file_b: content_b,
                file_c: content_c,
            },
        },
    }
    # Codex emits a patch that touches only A+B (not C).
    text = _codex_output([_build_finding("001", [file_a, file_b])])
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    pp = parsed["findings"][0]["patch_proposal"]
    # Helper stamped the PATCH-SCOPED bundle_sha, not the defect_target's full-set.
    assert pp["base_sha"] == subset_base_sha
    assert pp["base_sha"] != full_base_sha


def test_per_patch_base_sha_full_overlap_equals_defect_target_sha():
    """F2 regression guard: when a single patch touches ALL defect_target files,
    its per-patch base_sha equals defect_target.base_sha (because the same set
    is hashed).
    """
    file_a = "a.py"
    file_b = "b.py"
    content_a = "alpha\n"
    content_b = "bravo\n"
    full_base_sha = _canonical_bundle_sha([(file_a, content_a), (file_b, content_b)])
    review_packet = {
        "defect_target": {
            "files": [file_a, file_b],
            "base_sha": full_base_sha,
            "touched_files_contents": {file_a: content_a, file_b: content_b},
        },
    }
    text = _codex_output([_build_finding("001", [file_a, file_b])])
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    pp = parsed["findings"][0]["patch_proposal"]
    assert pp["base_sha"] == full_base_sha


def test_per_patch_base_sha_multiple_patches_each_anchored_to_own_subset():
    """F2: two patches with distinct files_touched get distinct per-patch base_shas."""
    file_a = "a.py"
    file_b = "b.py"
    content_a = "alpha\n"
    content_b = "bravo\n"
    review_packet = {
        "defect_target": {
            "files": [file_a, file_b],
            "base_sha": _canonical_bundle_sha([(file_a, content_a), (file_b, content_b)]),
            "touched_files_contents": {file_a: content_a, file_b: content_b},
        },
    }
    # Patch 1 touches A only; patch 2 touches B only.
    text = _codex_output([
        _build_finding("001", [file_a]),
        _build_finding("002", [file_b]),
    ])
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    pp1 = parsed["findings"][0]["patch_proposal"]
    pp2 = parsed["findings"][1]["patch_proposal"]
    assert pp1["base_sha"] == _canonical_bundle_sha([(file_a, content_a)])
    assert pp2["base_sha"] == _canonical_bundle_sha([(file_b, content_b)])
    assert pp1["base_sha"] != pp2["base_sha"]


# ---------------------------------------------------------------------------
# Backward compat: no touched_files_contents -> fall back to defect_target.base_sha
# ---------------------------------------------------------------------------


def test_no_touched_files_contents_falls_back_to_defect_target_base_sha():
    """When touched_files_contents is absent (legacy / unit-test shape), helper
    falls back to iter-0022 behaviour (stamps defect_target.base_sha verbatim).
    """
    file_a = "a.py"
    full_base_sha = "0c9507575598bfebc25afe42eaa27358f967fc1ccbea287af7ae695f07adb94b"
    review_packet = {
        "defect_target": {
            "files": [file_a],
            "base_sha": full_base_sha,
            # no touched_files_contents
        },
    }
    text = _codex_output([_build_finding("001", [file_a])])
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == full_base_sha


def test_patch_file_missing_from_touched_files_contents_falls_back():
    """If a patch touches a file NOT in touched_files_contents, we can't compute
    per-patch base_sha; fall back to defect_target.base_sha.
    """
    file_a = "a.py"
    file_b = "b.py"
    full_base_sha = "fallback-sha-1234567890"
    review_packet = {
        "defect_target": {
            "files": [file_a],  # contents only carry A
            "base_sha": full_base_sha,
            "touched_files_contents": {file_a: "alpha\n"},
        },
    }
    # Patch touches B (not in touched_files_contents).
    text = _codex_output([_build_finding("001", [file_b])])
    parsed = _dispatch_codex._parse_codex_output(text, review_packet=review_packet)
    # Helper couldn't compute per-patch; fell back to defect_target.base_sha.
    assert parsed["findings"][0]["patch_proposal"]["base_sha"] == full_base_sha
