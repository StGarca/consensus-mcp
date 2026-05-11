"""iter-0026 F1: CRLF/encoding mismatch in _compute_per_patch_base_sha.

Defect (surfaced in iter-0025 operational_caveat ``iter_0024_F2_helper_crlf_mismatch``):

  - iter-0024 F2 helper ``_compute_per_patch_base_sha`` computes bundle_sha by
    encoding text from ``defect_target.touched_files_contents`` (UTF-8 strings
    loaded via yaml.safe_load + read_text which normalises CRLF -> LF on Windows).
  - apply.codex_patch's drift detection calls ``_closure_invariant.bundle_sha``
    which reads raw on-disk bytes (CRLF on Windows).
  - Result: every helper-stamped per-patch base_sha returns base_sha_drift at
    apply time on Windows. Operator forced into manual re-anchor for every
    landed codex patch (iter-0023, iter-0025 caveat).

Fix: helper accepts an optional ``repo_root`` and prefers ``bundle_sha(repo_root,
files_touched)`` (disk bytes — same code path as apply.codex_patch) when supplied.
Falls back to the text-encoding path when repo_root is None (backward compat
with unit-level tests that don't set up on-disk files).

The threading: ``_parse_codex_output`` -> ``_validate_patch_proposal`` ->
``_compute_per_patch_base_sha`` all gain an optional ``repo_root`` parameter.
The CLI ``main()`` already resolves repo_root for codex --cd; it passes it to
``_parse_codex_output`` so the helper has it for the stamp.
"""
from __future__ import annotations

import hashlib
import json as _json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _dispatch_codex  # noqa: E402
from consensus_mcp._closure_invariant import bundle_sha  # noqa: E402


def _build_finding(patch_id_suffix: str, files: list[str]) -> dict:
    return {
        "id": f"codex-rev-{patch_id_suffix}",
        "severity": "medium",
        "summary": "iter-0026 CRLF test finding",
        "citation": "consensus_mcp/_dispatch_codex.py:1",
        "risk": "Low; test fixture",
        "recommendation": "Test recommendation",
        "patch_proposal": {
            "patch_id": f"codex-rev-{patch_id_suffix}-patch",
            "applies_to_findings": [f"codex-rev-{patch_id_suffix}"],
            "base_sha": "codex-placeholder",
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
        "goal_satisfied_rationale": "iter-0026 CRLF test fixture",
        "blocking_objections": [],
    })


# ---------------------------------------------------------------------------
# F1: helper-stamped base_sha must equal apply.codex_patch's on-disk bundle_sha
# ---------------------------------------------------------------------------


def test_helper_stamped_base_sha_matches_disk_bundle_sha_with_crlf(tmp_path):
    """Write a file with CRLF line endings to disk. The review-packet's
    touched_files_contents (text) has LF-only (Python text-mode read normalises).
    With repo_root supplied, the helper-stamped base_sha must equal the disk-
    bytes bundle_sha — which is what apply.codex_patch computes at drift-check time.
    """
    rel = "scripts/crlf_file.py"
    # Write raw CRLF bytes (simulates a Windows-checkout file).
    crlf_text = "line_one\r\nline_two\r\n"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(crlf_text.encode("utf-8"))

    # touched_files_contents holds the LF-normalised version (what text-mode
    # read_text() returns on Windows). This is the prior bug surface.
    lf_text = "line_one\nline_two\n"
    review_packet = {
        "defect_target": {
            "files": [rel],
            "base_sha": "ignored-when-per-patch-stamp-succeeds",
            "touched_files_contents": {rel: lf_text},
        },
    }
    text = _codex_output([_build_finding("001", [rel])])
    parsed = _dispatch_codex._parse_codex_output(
        text, review_packet=review_packet, repo_root=tmp_path,
    )
    pp = parsed["findings"][0]["patch_proposal"]

    # The disk-bytes bundle_sha is what apply.codex_patch computes.
    expected = bundle_sha(tmp_path, [rel])
    assert pp["base_sha"] == expected, (
        f"helper-stamped base_sha {pp['base_sha']!r} does not match disk-bytes "
        f"bundle_sha {expected!r}; CRLF/text mismatch still present"
    )

    # Sanity: the LF-text-encoded hash differs from the disk-bytes hash.
    # (This is the pre-fix value the helper would have stamped.)
    lf_pseudo_bundle = hashlib.sha256(
        f"{rel}\0{hashlib.sha256(lf_text.encode('utf-8')).hexdigest()}".encode("utf-8")
    ).hexdigest()
    assert pp["base_sha"] != lf_pseudo_bundle, (
        "test fixture: expected disk-bytes hash to differ from LF-text hash"
    )


def test_helper_stamped_base_sha_matches_disk_bundle_sha_with_lf(tmp_path):
    """LF-only files on disk: helper-stamped base_sha still equals disk-bytes
    bundle_sha (no CRLF translation involved; both paths agree).
    """
    rel = "scripts/lf_file.py"
    lf_text = "line_one\nline_two\n"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(lf_text.encode("utf-8"))

    review_packet = {
        "defect_target": {
            "files": [rel],
            "base_sha": "ignored",
            "touched_files_contents": {rel: lf_text},
        },
    }
    text = _codex_output([_build_finding("001", [rel])])
    parsed = _dispatch_codex._parse_codex_output(
        text, review_packet=review_packet, repo_root=tmp_path,
    )
    pp = parsed["findings"][0]["patch_proposal"]
    expected = bundle_sha(tmp_path, [rel])
    assert pp["base_sha"] == expected


def test_helper_no_repo_root_falls_back_to_text_encoding(tmp_path):
    """Backward compat: when repo_root is None (legacy unit tests), the helper
    falls back to encoding touched_files_contents text. Existing iter-0024
    behaviour preserved.
    """
    rel = "scripts/lf_file.py"
    lf_text = "line_one\nline_two\n"
    # NOTE: NO file written to disk. Helper must produce a value from text alone.
    review_packet = {
        "defect_target": {
            "files": [rel],
            "base_sha": "ignored",
            "touched_files_contents": {rel: lf_text},
        },
    }
    text = _codex_output([_build_finding("001", [rel])])
    parsed = _dispatch_codex._parse_codex_output(
        text, review_packet=review_packet, repo_root=None,
    )
    pp = parsed["findings"][0]["patch_proposal"]
    # Helper falls back to text-encoding hash.
    # bundle_sha formula on (rel, lf_text encoded utf-8):
    from consensus_mcp._closure_invariant import _normalize_path
    norm = _normalize_path(rel)
    h = hashlib.sha256(lf_text.encode("utf-8")).hexdigest()
    canonical = f"{norm}\0{h}"
    expected_text_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert pp["base_sha"] == expected_text_sha, (
        f"no-repo_root fallback produced {pp['base_sha']!r}; expected text-encoded "
        f"hash {expected_text_sha!r}"
    )


def test_helper_with_repo_root_but_file_missing_returns_disk_empty_hash(tmp_path):
    """If the disk file is missing but repo_root is supplied, bundle_sha treats
    missing files as empty (b"") — that's the canonical apply.codex_patch
    contract. Helper must follow suit so the stamp matches apply-time hash.

    This is the "add-file" case: file not yet on disk; codex's patch will
    create it. The base_sha for "before apply" must reflect the empty-file
    state, which is exactly what bundle_sha with a missing file produces.
    """
    rel = "scripts/will_be_added.py"
    # No file on disk.
    review_packet = {
        "defect_target": {
            "files": [rel],
            "base_sha": "ignored",
            "touched_files_contents": {rel: ""},
        },
    }
    text = _codex_output([_build_finding("001", [rel])])
    parsed = _dispatch_codex._parse_codex_output(
        text, review_packet=review_packet, repo_root=tmp_path,
    )
    pp = parsed["findings"][0]["patch_proposal"]
    expected = bundle_sha(tmp_path, [rel])
    assert pp["base_sha"] == expected
