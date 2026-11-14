"""iter-0026 F2: regression tests for hunk-line-number-anchored applier.

Codex review codex-rev-001 (2026-05-10, archived in
iteration-0025-apply-pipeline-flavor-b-review/codex-review.yaml) flagged that
``_apply_unified_diff`` ignored ``@@ -orig_start,orig_count +new_start,new_count @@``
headers and did pure linear matching. With repeated context lines, the wrong
occurrence could be silently mutated.

Fix anchors each hunk at ``orig_start - 1`` (zero-indexed) into the original
file. Context lines at the hunk start MUST match the original at that offset
or the apply refuses with ``hunk_context_mismatch``.

These tests force the diff-applier path (no _old_content/_new_content
side-channel, single-file or multi-file).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp.tools import apply_codex_patch  # noqa: E402
from consensus_mcp._closure_invariant import bundle_sha  # noqa: E402

# Reuse helpers + fixtures from the test_apply_codex_patch module.
from consensus_mcp.tests.test_apply_codex_patch import (  # type: ignore  # noqa: E402
    _make_active_iter,
    _write_target_file,
    _write_codex_review,
    _write_goal_packet,
    _write_claude_verification,
    _make_actor,
)


@pytest.fixture
def repo_root_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    import importlib

    import consensus_mcp.tools.audit_append_event as _aae
    import consensus_mcp.tools.patch_stage_and_dry_run as _psd
    import consensus_mcp.tools.patch_apply_consensus_patch as _pac
    importlib.reload(_aae)
    importlib.reload(_psd)
    importlib.reload(_pac)
    importlib.reload(apply_codex_patch)
    return tmp_path


@pytest.fixture
def stub_clean_dry_run(monkeypatch):
    def _stub(iteration_id=None, proposed_patches=None, validators_to_run=None):
        return {
            "staging_dir_used": "/tmp/stub",
            "dry_run_findings": [],
            "gate_decision": "APPROVED",
            "dry_run_isolation_caveats": [],
        }
    import consensus_mcp.tools.patch_stage_and_dry_run as _psd_mod
    monkeypatch.setattr(_psd_mod, "handle", _stub)
    return _stub


def _build_proposal_no_sidechannel(
    repo_root: Path,
    files_touched: list[str],
    unified_diff: str,
) -> dict:
    """Force the unified_diff applier path."""
    return {
        "patch_id": "codex-rev-001-patch",
        "applies_to_findings": ["codex-rev-001"],
        "base_sha": bundle_sha(repo_root, list(files_touched)),
        "unified_diff": unified_diff,
        "files_touched": list(files_touched),
    }


def _setup_iter(repo_root: Path) -> Path:
    iter_dir = _make_active_iter(repo_root)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    return iter_dir


# ---------------------------------------------------------------------------
# CORE F2 BEHAVIOUR
# ---------------------------------------------------------------------------


def test_repeated_context_picks_correct_hunk(
    repo_root_env, monkeypatch, stub_clean_dry_run,
):
    """The original file has two identical ``if x:\\n    foo()\\n`` blocks.
    The unified diff anchors at the SECOND occurrence via @@ line numbers.
    After apply, ONLY the second occurrence is mutated.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter(repo_root_env)

    rel = "scripts/repeated.py"
    original = (
        "header\n"
        "if x:\n"
        "    foo()\n"
        "middle\n"
        "if x:\n"
        "    foo()\n"
        "footer\n"
    )
    _write_target_file(repo_root_env, rel, original)

    # Hunk targets the SECOND ``if x: / foo()`` block at lines 5-6,
    # adding a new line after foo(). @@ -5,2 +5,3 @@ (orig_start=5).
    unified_diff = (
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -5,2 +5,3 @@\n"
        " if x:\n"
        "     foo()\n"
        "+    second_only()\n"
    )
    pp = _build_proposal_no_sidechannel(repo_root_env, [rel], unified_diff)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"

    after = (repo_root_env / rel).read_text(encoding="utf-8")
    # Only the SECOND foo() block gains the new line.
    expected = (
        "header\n"
        "if x:\n"
        "    foo()\n"
        "middle\n"
        "if x:\n"
        "    foo()\n"
        "    second_only()\n"
        "footer\n"
    )
    assert after == expected, (
        f"hunk applied to wrong occurrence; got:\n{after!r}\nwant:\n{expected!r}"
    )


def test_hunk_context_mismatch_refuses_cleanly(
    repo_root_env, monkeypatch, stub_clean_dry_run,
):
    """Diff specifies @@ -50,2 +50,3 @@ but the file is only 5 lines.
    Apply must refuse with hunk_context_mismatch (NOT silently scan elsewhere).
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter(repo_root_env)

    rel = "scripts/short.py"
    original = "A\nB\nC\nD\nE\n"
    _write_target_file(repo_root_env, rel, original)

    unified_diff = (
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -50,2 +50,3 @@\n"
        " A\n"
        " B\n"
        "+X\n"
    )
    pp = _build_proposal_no_sidechannel(repo_root_env, [rel], unified_diff)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    assert result["applied"] is False
    err = result.get("error") or ""
    # Both keywords present so the operator sees the specific failure class.
    assert "diff_apply_failed" in err
    assert "hunk_context_mismatch" in err, (
        f"expected hunk_context_mismatch in error, got: {err}"
    )
    # File untouched.
    assert (repo_root_env / rel).read_text(encoding="utf-8") == original


def test_multi_hunk_each_anchored_to_its_own_line_numbers(
    repo_root_env, monkeypatch, stub_clean_dry_run,
):
    """Two hunks in one file at distinct line ranges (around line 10 and
    line 80). Both apply correctly without cross-contamination.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter(repo_root_env)

    rel = "scripts/wide.py"
    # 100-line file: L1..L100. Anchor hunk1 at L10, hunk2 at L80.
    original_lines = [f"L{n}" for n in range(1, 101)]
    original = "\n".join(original_lines) + "\n"
    _write_target_file(repo_root_env, rel, original)

    # Hunk 1: insert "INS_AFTER_L10" after L10. Context: L9, L10, L11.
    # @@ -9,3 +9,4 @@   (orig_start=9, +new_start=9, new_count=4)
    # Hunk 2: replace L80 with L80_NEW. Context: L79, L80, L81.
    # @@ -79,3 +80,3 @@   (orig_start=79; new_start tracks accumulated insert)
    unified_diff = (
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -9,3 +9,4 @@\n"
        " L9\n"
        " L10\n"
        "+INS_AFTER_L10\n"
        " L11\n"
        "@@ -79,3 +80,3 @@\n"
        " L79\n"
        "-L80\n"
        "+L80_NEW\n"
        " L81\n"
    )
    pp = _build_proposal_no_sidechannel(repo_root_env, [rel], unified_diff)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"

    after = (repo_root_env / rel).read_text(encoding="utf-8")
    after_lines = after.rstrip("\n").split("\n")
    # Hunk 1 inserted "INS_AFTER_L10" between L10 and L11.
    assert "INS_AFTER_L10" in after_lines
    idx_ins = after_lines.index("INS_AFTER_L10")
    assert after_lines[idx_ins - 1] == "L10"
    assert after_lines[idx_ins + 1] == "L11"
    # Hunk 2 replaced L80 with L80_NEW.
    assert "L80_NEW" in after_lines
    assert "L80" not in after_lines
    idx_new = after_lines.index("L80_NEW")
    assert after_lines[idx_new - 1] == "L79"
    assert after_lines[idx_new + 1] == "L81"
    # Total line count = 101 (original 100 + 1 insert).
    assert len(after_lines) == 101


def test_repeated_context_first_hunk_targeted(
    repo_root_env, monkeypatch, stub_clean_dry_run,
):
    """Symmetric case: targeting the FIRST of two identical blocks via @@ -2,2.
    Guards against the line-anchor logic accidentally always picking the last.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter(repo_root_env)

    rel = "scripts/repeated2.py"
    original = (
        "header\n"        # L1
        "if x:\n"         # L2
        "    foo()\n"     # L3
        "middle\n"        # L4
        "if x:\n"         # L5
        "    foo()\n"     # L6
        "footer\n"        # L7
    )
    _write_target_file(repo_root_env, rel, original)

    # Hunk anchors at line 2 (first if-block).
    unified_diff = (
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -2,2 +2,3 @@\n"
        " if x:\n"
        "     foo()\n"
        "+    first_only()\n"
    )
    pp = _build_proposal_no_sidechannel(repo_root_env, [rel], unified_diff)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"

    after = (repo_root_env / rel).read_text(encoding="utf-8")
    expected = (
        "header\n"
        "if x:\n"
        "    foo()\n"
        "    first_only()\n"
        "middle\n"
        "if x:\n"
        "    foo()\n"
        "footer\n"
    )
    assert after == expected, (
        f"first-block hunk mis-anchored; got:\n{after!r}\nwant:\n{expected!r}"
    )
