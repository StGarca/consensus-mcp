"""Multi-file unified-diff applier tests for tools/apply_codex_patch.py.

Exercises `_apply_unified_diff` in apply_codex_patch with multi-file,
multi-hunk, file-create, file-delete, and zero-context patches. The
single-file `_old_content`/`_new_content` fast-path used in the existing
test suite bypasses `_apply_unified_diff` entirely; these tests force the
diff-applier path by either omitting those side-channel fields or by
supplying multi-file `files_touched` (the fast-path requires
len(files_touched)==1).

Documents observed semantics inline; cases known to be unsupported by the
v1.0 minimal applier are asserted as clean refusals (DiffApplyError ->
diff_apply_failed) rather than silent corruption.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp.tools import apply_codex_patch  # noqa: E402
from consensus_mcp._closure_invariant import bundle_sha  # noqa: E402

# Reuse the helpers + repo_root_env + stub_clean_dry_run fixtures from
# test_apply_codex_patch.py to keep this file surgical. pytest collects
# fixtures from conftest.py only, so we re-import the helpers directly and
# redeclare the two fixtures we need (parameter-style copies of the
# originals).
from consensus_mcp.tests.test_apply_codex_patch import (  # type: ignore  # noqa: E402
    _make_active_iter,
    _write_target_file,
    _write_codex_review,
    _write_goal_packet,
    _write_claude_verification,
    _make_actor,
)


# ---- fixtures (mirrored from test_apply_codex_patch.py) --------------------


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
    """Stub patch.stage_and_dry_run -> APPROVED with no findings."""
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


# ---- helpers specific to multi-file tests ----------------------------------


def _build_multifile_proposal(
    repo_root: Path,
    files_touched: list[str],
    unified_diff: str,
    base_sha: str | None = None,
) -> dict:
    """Build a patch_proposal with NO _old_content/_new_content side-channel.

    Forces apply_codex_patch into the `_apply_unified_diff` code path
    (the fast-path requires both side-channel fields AND single-file).
    """
    if base_sha is None:
        base_sha = bundle_sha(repo_root, list(files_touched))
    # iter-0020: patch_id is finding-id-derived, not content-bound.
    patch_id = "codex-rev-001-patch"
    return {
        "patch_id": patch_id,
        "applies_to_findings": ["codex-rev-001"],
        "base_sha": base_sha,
        "unified_diff": unified_diff,
        "files_touched": list(files_touched),
        # NO _old_content / _new_content -> forces _apply_unified_diff path
    }


def _setup_iter_for_apply(repo_root: Path) -> Path:
    """Standard pre-apply scaffold: iter_dir + goal_packet + env."""
    iter_dir = _make_active_iter(repo_root)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    return iter_dir


# ---- test 1: multi-file diff applies all files -----------------------------


def test_multi_file_unified_diff_applies_all_files(
    repo_root_env, monkeypatch, stub_clean_dry_run
):
    """Two-file patch: both diffs apply, both files mutated correctly."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter_for_apply(repo_root_env)

    _write_target_file(repo_root_env, "scripts/file_a.py", "alpha\nbeta\n")
    _write_target_file(repo_root_env, "scripts/file_b.py", "one\ntwo\n")

    unified_diff = (
        "--- a/scripts/file_a.py\n"
        "+++ b/scripts/file_a.py\n"
        "@@ -1,2 +1,3 @@\n"
        " alpha\n"
        " beta\n"
        "+gamma\n"
        "--- a/scripts/file_b.py\n"
        "+++ b/scripts/file_b.py\n"
        "@@ -1,2 +1,3 @@\n"
        " one\n"
        " two\n"
        "+three\n"
    )
    pp = _build_multifile_proposal(
        repo_root_env,
        ["scripts/file_a.py", "scripts/file_b.py"],
        unified_diff,
    )
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"
    assert result["applied"] is True

    a_after = (repo_root_env / "scripts/file_a.py").read_text(encoding="utf-8")
    b_after = (repo_root_env / "scripts/file_b.py").read_text(encoding="utf-8")
    assert a_after == "alpha\nbeta\ngamma\n"
    assert b_after == "one\ntwo\nthree\n"


# ---- test 2: partial failure semantics -------------------------------------


def test_multi_file_diff_partial_failure_aborts(
    repo_root_env, monkeypatch, stub_clean_dry_run
):
    """When the second file's diff has unmatchable context, the apply must
    refuse cleanly. Documented semantics: failure is detected during the
    `_apply_unified_diff` PRE-APPLY computation phase (before any
    os.replace), so neither file is mutated. The partial_apply_failed
    rollback branch in apply_codex_patch handles only mid-write filesystem
    errors, not diff-content failures.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter_for_apply(repo_root_env)

    _write_target_file(repo_root_env, "scripts/file_a.py", "alpha\nbeta\n")
    _write_target_file(repo_root_env, "scripts/file_b.py", "one\ntwo\n")

    # file_b's hunk references context lines that DON'T exist in the file
    # ("THREE" / "FOUR" - file actually has "one"/"two"). Applier must reject.
    unified_diff = (
        "--- a/scripts/file_a.py\n"
        "+++ b/scripts/file_a.py\n"
        "@@ -1,2 +1,3 @@\n"
        " alpha\n"
        " beta\n"
        "+gamma\n"
        "--- a/scripts/file_b.py\n"
        "+++ b/scripts/file_b.py\n"
        "@@ -1,2 +1,3 @@\n"
        " THREE\n"
        " FOUR\n"
        "+FIVE\n"
    )
    pp = _build_multifile_proposal(
        repo_root_env,
        ["scripts/file_a.py", "scripts/file_b.py"],
        unified_diff,
    )
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    a_before = (repo_root_env / "scripts/file_a.py").read_text(encoding="utf-8")
    b_before = (repo_root_env / "scripts/file_b.py").read_text(encoding="utf-8")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    assert result["applied"] is False
    err = result.get("error") or ""
    assert "diff_apply_failed" in err, f"expected diff_apply_failed, got: {err}"

    # NEITHER file mutated: failure happens pre-apply during diff
    # computation; no os.replace runs.
    a_after = (repo_root_env / "scripts/file_a.py").read_text(encoding="utf-8")
    b_after = (repo_root_env / "scripts/file_b.py").read_text(encoding="utf-8")
    assert a_after == a_before, "file_a must not be mutated when applier refuses"
    assert b_after == b_before, "file_b must not be mutated when applier refuses"


# ---- test 3: multi-hunk single file ----------------------------------------


def test_multi_hunk_single_file_diff(
    repo_root_env, monkeypatch, stub_clean_dry_run
):
    """Single file, two `@@` hunks across separate change zones."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter_for_apply(repo_root_env)

    original = "L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8\n"
    _write_target_file(repo_root_env, "scripts/multi.py", original)

    # Hunk 1: insert "INS_AFTER_L2" after L2.
    # Hunk 2: replace L7 with "L7_NEW".
    unified_diff = (
        "--- a/scripts/multi.py\n"
        "+++ b/scripts/multi.py\n"
        "@@ -1,3 +1,4 @@\n"
        " L1\n"
        " L2\n"
        "+INS_AFTER_L2\n"
        " L3\n"
        "@@ -6,3 +7,3 @@\n"
        " L6\n"
        "-L7\n"
        "+L7_NEW\n"
        " L8\n"
    )
    # NOTE: force diff-applier path by using NO side-channel.
    # files_touched is single-file but we omit _old_content/_new_content.
    pp = _build_multifile_proposal(
        repo_root_env, ["scripts/multi.py"], unified_diff
    )
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"

    after = (repo_root_env / "scripts/multi.py").read_text(encoding="utf-8")
    assert after == "L1\nL2\nINS_AFTER_L2\nL3\nL4\nL5\nL6\nL7_NEW\nL8\n", (
        f"multi-hunk apply produced unexpected content: {after!r}"
    )


# ---- test 4: new file creation --------------------------------------------


def test_diff_with_added_file(
    repo_root_env, monkeypatch, stub_clean_dry_run
):
    """`--- /dev/null` / `+++ b/path` introduces a new file."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter_for_apply(repo_root_env)

    new_rel = "scripts/new_module.py"
    # Pre-state: file does not exist.
    assert not (repo_root_env / new_rel).exists()

    unified_diff = (
        "--- /dev/null\n"
        f"+++ b/{new_rel}\n"
        "@@ -0,0 +1,2 @@\n"
        "+line_one\n"
        "+line_two\n"
    )
    pp = _build_multifile_proposal(repo_root_env, [new_rel], unified_diff)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"

    target = repo_root_env / new_rel
    assert target.exists(), "new file must be created"
    content = target.read_text(encoding="utf-8")
    assert content == "line_one\nline_two\n", f"new file content unexpected: {content!r}"


# ---- test 5: file deletion -------------------------------------------------


def test_diff_with_deleted_file(
    repo_root_env, monkeypatch, stub_clean_dry_run
):
    """`+++ /dev/null` deletes a file.

    DOCUMENTED LIMITATION: the v1.0 minimal applier in
    `_apply_unified_diff` does NOT support deletions. The +++ header
    parsing strips a leading "b/" prefix only; "/dev/null" is left as the
    segment key, which then fails to match files_touched. The apply step
    in apply_codex_patch.handle() also has no os.unlink branch - it only
    writes new_text via os.replace. Deletions therefore fail cleanly with
    `diff_apply_failed` rather than executing.

    Future work: extend the applier to recognize `+++ /dev/null` as a
    delete signal and have handle() unlink the file.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter_for_apply(repo_root_env)

    rel = "scripts/condemned.py"
    _write_target_file(repo_root_env, rel, "doomed_line_a\ndoomed_line_b\n")

    unified_diff = (
        f"--- a/{rel}\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-doomed_line_a\n"
        "-doomed_line_b\n"
    )
    pp = _build_multifile_proposal(repo_root_env, [rel], unified_diff)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    # Documented unsupported -> clean refusal.
    assert result["ok"] is False
    assert result["applied"] is False
    err = result.get("error") or ""
    assert "diff_apply_failed" in err, (
        f"deletion via +++ /dev/null should fail with diff_apply_failed; got: {err}"
    )
    # File must still exist: refusal occurred before any apply step.
    assert (repo_root_env / rel).exists(), "file must remain on refusal"


# ---- test 6: zero-context diff ---------------------------------------------


def test_diff_with_no_context_lines(
    repo_root_env, monkeypatch, stub_clean_dry_run
):
    """Edge case: diff body has only +/- lines, no surrounding context.

    The applier walks lines linearly; with no context, `-` lines must
    match the original sequentially from orig_idx=0 and `+` lines append
    to output. This test uses a full-file replacement: every original
    line removed, every new line added.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter_for_apply(repo_root_env)

    rel = "scripts/zerocontext.py"
    _write_target_file(repo_root_env, rel, "old_a\nold_b\n")

    unified_diff = (
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -1,2 +1,2 @@\n"
        "-old_a\n"
        "-old_b\n"
        "+new_a\n"
        "+new_b\n"
    )
    pp = _build_multifile_proposal(repo_root_env, [rel], unified_diff)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"

    after = (repo_root_env / rel).read_text(encoding="utf-8")
    # DOCUMENTED SEMANTICS: applier emits +s in encounter order, then
    # advances orig_idx for each `-`. With no context, the final
    # "remaining originals" tail-append loop adds nothing because all
    # originals were consumed by `-` lines. Result is the new lines only.
    assert after == "new_a\nnew_b\n", (
        f"zero-context full-replace produced unexpected content: {after!r}"
    )


# ---- test 7: multi-file post_sha reflects all mutations --------------------


def test_multi_file_post_sha_reflects_all_mutations(
    repo_root_env, monkeypatch, stub_clean_dry_run
):
    """post_sha (bundle_sha post-apply) differs from base_sha when MULTIPLE
    files mutate. Catches a regression where post_sha was computed against
    only the first file or against the staging_dir copies.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _setup_iter_for_apply(repo_root_env)

    _write_target_file(repo_root_env, "scripts/m_a.py", "AA1\nAA2\n")
    _write_target_file(repo_root_env, "scripts/m_b.py", "BB1\nBB2\n")

    unified_diff = (
        "--- a/scripts/m_a.py\n"
        "+++ b/scripts/m_a.py\n"
        "@@ -1,2 +1,3 @@\n"
        " AA1\n"
        " AA2\n"
        "+AA3\n"
        "--- a/scripts/m_b.py\n"
        "+++ b/scripts/m_b.py\n"
        "@@ -1,2 +1,3 @@\n"
        " BB1\n"
        " BB2\n"
        "+BB3\n"
    )
    files_touched = ["scripts/m_a.py", "scripts/m_b.py"]
    pp = _build_multifile_proposal(repo_root_env, files_touched, unified_diff)
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    pre_bundle = bundle_sha(repo_root_env, files_touched)
    assert pre_bundle == pp["base_sha"]

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"

    post_bundle = bundle_sha(repo_root_env, files_touched)
    lm = result["last_mutation"]
    assert lm["post_sha"] == post_bundle, (
        f"last_mutation.post_sha={lm['post_sha']} should equal "
        f"bundle_sha-post-apply={post_bundle}"
    )
    assert lm["post_sha"] != pre_bundle, (
        "post_sha must differ from base_sha after a real mutation"
    )

    # Spot-check: the per-file content hash of EACH touched file changed.
    a_after = (repo_root_env / "scripts/m_a.py").read_text(encoding="utf-8")
    b_after = (repo_root_env / "scripts/m_b.py").read_text(encoding="utf-8")
    assert a_after == "AA1\nAA2\nAA3\n"
    assert b_after == "BB1\nBB2\nBB3\n"

    # And: dropping either file from files_touched would change post_sha,
    # confirming the bundle hash truly covers BOTH mutations.
    only_a = bundle_sha(repo_root_env, ["scripts/m_a.py"])
    only_b = bundle_sha(repo_root_env, ["scripts/m_b.py"])
    assert post_bundle != only_a
    assert post_bundle != only_b
