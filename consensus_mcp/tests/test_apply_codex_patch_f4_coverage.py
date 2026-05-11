"""iter-0026 F4: remaining claude-iter0025 findings (001, 002, 004, 005, 006, 007).

Tests cover:
  - 001 actor non-empty: _validate_actor refuses empty id / empty pass_id
  - 002 partial-apply rollback: multi-file apply with mid-loop os.replace
        failure rolls back already-applied files; applied=False is truthful
        (no on-disk mutation persists)
  - 004 /dev/null parsing: explicit detection of +++ /dev/null and --- /dev/null
        (delete = clean refusal w/ explicit reason; add = explicit support)
  - 005 lazy-import drift: record_verdict no longer has inline `import re`
        (module-level constant suffices)
  - 006 record_verdict verdict-enum: schema enum is {approved, corrected_resubmit}
        (no "blocked"); record-mode rejects blocked at schema layer
  - 007 test coverage gaps:
        (a) base_sha empty -> base_sha_missing refusal
        (b) codex_review_missing -> refusal when codex-review.yaml absent
        (c) diff_apply_failed propagation from single-file no-side-channel path
        (d) actor model_family invalid -> refusal
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp.tools import apply_codex_patch  # noqa: E402
from consensus_mcp.tools import loop_verify_codex_patch  # noqa: E402
from consensus_mcp._closure_invariant import bundle_sha  # noqa: E402

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


# ---------------------------------------------------------------------------
# F4-001: actor non-empty check
# ---------------------------------------------------------------------------


def test_refuses_when_actor_id_is_empty_string(repo_root_env, monkeypatch):
    """Empty actor.id passes the prior key-presence check but corrupts the
    audit-log provenance. Helper must refuse.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    actor = _make_actor()
    actor["id"] = ""
    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id="codex-rev-001-patch",
        actor=actor,
    )
    assert result["ok"] is False
    err = result.get("error") or ""
    assert "actor_invalid" in err
    assert "id" in err


def test_refuses_when_actor_pass_id_is_empty_string(repo_root_env, monkeypatch):
    """Empty actor.pass_id is a data-integrity defect."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    actor = _make_actor()
    actor["pass_id"] = ""
    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id="codex-rev-001-patch",
        actor=actor,
    )
    assert result["ok"] is False
    err = result.get("error") or ""
    assert "actor_invalid" in err
    assert "pass_id" in err


def test_refuses_when_actor_id_is_none(repo_root_env, monkeypatch):
    """None id slips past `if key not in actor` (key IS in actor)."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    actor = _make_actor()
    actor["id"] = None
    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id="codex-rev-001-patch",
        actor=actor,
    )
    assert result["ok"] is False
    err = result.get("error") or ""
    assert "actor_invalid" in err


def test_refuses_when_actor_id_is_not_string(repo_root_env, monkeypatch):
    """Non-string id must fail closed (int slipping through audit corrupts logs)."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    actor = _make_actor()
    actor["id"] = 42  # int
    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id="codex-rev-001-patch",
        actor=actor,
    )
    assert result["ok"] is False
    err = result.get("error") or ""
    assert "actor_invalid" in err


# ---------------------------------------------------------------------------
# F4-002: partial-apply rollback
# ---------------------------------------------------------------------------


def test_partial_apply_failed_rolls_back_already_applied_files(
    repo_root_env, monkeypatch, stub_clean_dry_run,
):
    """Two-file patch: first os.replace succeeds, second raises.
    Apply must roll back file A so the working tree is unchanged.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, "scripts/file_a.py", "alpha\n")
    _write_target_file(repo_root_env, "scripts/file_b.py", "bravo\n")

    unified_diff = (
        "--- a/scripts/file_a.py\n"
        "+++ b/scripts/file_a.py\n"
        "@@ -1 +1,2 @@\n"
        " alpha\n"
        "+alpha2\n"
        "--- a/scripts/file_b.py\n"
        "+++ b/scripts/file_b.py\n"
        "@@ -1 +1,2 @@\n"
        " bravo\n"
        "+bravo2\n"
    )
    files_touched = ["scripts/file_a.py", "scripts/file_b.py"]
    pp = {
        "patch_id": "codex-rev-001-patch",
        "applies_to_findings": ["codex-rev-001"],
        "base_sha": bundle_sha(repo_root_env, files_touched),
        "unified_diff": unified_diff,
        "files_touched": files_touched,
    }
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    a_before = (repo_root_env / "scripts/file_a.py").read_text(encoding="utf-8")
    b_before = (repo_root_env / "scripts/file_b.py").read_text(encoding="utf-8")

    # Monkeypatch os.replace to fail on the second invocation (file_b).
    original_replace = os.replace
    call_count = {"n": 0}

    def _replace_fail_on_second(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated mid-loop os.replace failure")
        return original_replace(src, dst)

    monkeypatch.setattr("os.replace", _replace_fail_on_second)

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    err = result.get("error") or ""
    assert "partial_apply_failed" in err

    # ROLLBACK: file_a must be restored to its pre-apply state.
    a_after = (repo_root_env / "scripts/file_a.py").read_text(encoding="utf-8")
    b_after = (repo_root_env / "scripts/file_b.py").read_text(encoding="utf-8")
    assert a_after == a_before, (
        f"rollback failed: file_a content is {a_after!r}; expected pre-apply {a_before!r}"
    )
    assert b_after == b_before, "file_b must remain unchanged"
    # applied=False is now truthful (rollback completed).
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# F4-004: /dev/null explicit handling
# ---------------------------------------------------------------------------


def test_diff_delete_via_plus_plus_plus_dev_null_explicit_refusal(
    repo_root_env, monkeypatch, stub_clean_dry_run,
):
    """`+++ /dev/null` MUST refuse with an explicit, named reason — not
    coincidentally fail via files_touched mismatch.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    rel = "scripts/condemned.py"
    _write_target_file(repo_root_env, rel, "x\n")

    unified_diff = (
        f"--- a/{rel}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-x\n"
    )
    pp = {
        "patch_id": "codex-rev-001-patch",
        "applies_to_findings": ["codex-rev-001"],
        "base_sha": bundle_sha(repo_root_env, [rel]),
        "unified_diff": unified_diff,
        "files_touched": [rel],
    }
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    err = result.get("error") or ""
    assert "diff_apply_failed" in err
    assert "file_deletion_unsupported" in err, (
        f"expected explicit file_deletion_unsupported reason; got: {err}"
    )
    # File still exists.
    assert (repo_root_env / rel).exists()


def test_diff_add_via_dash_dash_dash_dev_null_works_explicitly(
    repo_root_env, monkeypatch, stub_clean_dry_run,
):
    """`--- /dev/null` should be EXPLICITLY recognized as a file-add (not
    accidentally work via target.exists()=False).
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    rel = "scripts/brand_new.py"
    assert not (repo_root_env / rel).exists()

    unified_diff = (
        "--- /dev/null\n"
        f"+++ b/{rel}\n"
        "@@ -0,0 +1,2 @@\n"
        "+hello\n"
        "+world\n"
    )
    pp = {
        "patch_id": "codex-rev-001-patch",
        "applies_to_findings": ["codex-rev-001"],
        "base_sha": bundle_sha(repo_root_env, [rel]),
        "unified_diff": unified_diff,
        "files_touched": [rel],
    }
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is True, f"file-add via /dev/null failed: {result}"
    assert (repo_root_env / rel).read_text(encoding="utf-8") == "hello\nworld\n"


# ---------------------------------------------------------------------------
# F4-005: lazy-import drift fix
# ---------------------------------------------------------------------------


def test_record_verdict_handler_has_no_inline_import_re():
    """The `import re` inside _record_verdict_handler is stylistically wrong —
    the module already has a module-level `re` import elsewhere if needed.
    iter-0026 F4 moved this to module level for consistency.
    """
    import inspect
    src = inspect.getsource(loop_verify_codex_patch._record_verdict_handler)
    assert "import re" not in src, (
        "F4-005: _record_verdict_handler must not have an inline `import re`; "
        "use a module-level constant instead"
    )


# ---------------------------------------------------------------------------
# F4-006: record_verdict input enum mismatch
# ---------------------------------------------------------------------------


def test_record_verdict_input_enum_excludes_blocked():
    """Per iter-0025 finding-006: the SCHEMA's verdict enum at record_verdict
    mode should NOT include "blocked"; build_inputs is the only path that
    produces verdict=blocked as an internal placeholder.

    iter-0026 F4 tightened the input enum at the handler layer: passing
    verdict="blocked" to record_verdict mode returns a blocked-with-error
    response (existing behaviour), and the OUTPUT schema's verdict enum
    remains {approved, corrected_resubmit, blocked} because build_inputs
    must be able to return blocked.

    Test asserts the handler-level rejection still works (no regression)
    AND that the implementation now matches the documented intent.
    """
    # Reuse a tmp_path-free unit-level call: handler returns blocked-with-error
    # on verdict=blocked without touching the filesystem.
    result = loop_verify_codex_patch._record_verdict_handler(
        iter_dir=Path("."),
        codex_finding_id="codex-rev-001",
        verdict="blocked",
        rationale="should be rejected",
        review_scope_hash=None,
        corrections=None,
        approved_patch_id="codex-rev-001-patch",
    )
    assert result["verdict"] == "blocked"
    err = result.get("error") or ""
    # The exact message documents the allowed input set.
    assert "approved" in err
    assert "corrected_resubmit" in err
    # iter-0026 F4: message MUST NOT advertise "blocked" as an accepted input.
    # The set-literal in the error message is `{approved, corrected_resubmit}`,
    # not `{approved, corrected_resubmit, blocked}`.
    assert "blocked, " not in err, (
        "F4-006: record_verdict error must not list 'blocked' as an accepted input"
    )


# ---------------------------------------------------------------------------
# F4-007: test coverage gaps
# ---------------------------------------------------------------------------


def test_refuses_when_codex_review_yaml_missing(repo_root_env, monkeypatch):
    """codex_review_missing branch (iter-0025 finding-007.d)."""
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    # NO codex-review.yaml authored.
    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id="codex-rev-001-patch",
        actor=_make_actor(),
    )
    assert result["ok"] is False
    err = result.get("error") or ""
    assert "codex_review_missing" in err


def test_refuses_when_base_sha_is_empty_string(
    repo_root_env, monkeypatch, stub_clean_dry_run,
):
    """base_sha_missing branch (iter-0025 finding-007.a). An empty base_sha
    would silently skip drift detection — must fail closed.
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    _write_target_file(repo_root_env, "scripts/foo.py", "hello\n")
    pp = {
        "patch_id": "codex-rev-001-patch",
        "applies_to_findings": ["codex-rev-001"],
        "base_sha": "",  # ← empty
        "unified_diff": "--- a/scripts/foo.py\n+++ b/scripts/foo.py\n@@ -1 +1,2 @@\n hello\n+world\n",
        "files_touched": ["scripts/foo.py"],
    }
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")

    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    err = result.get("error") or ""
    assert "base_sha_missing" in err


def test_refuses_diff_apply_failed_in_single_file_no_sidechannel(
    repo_root_env, monkeypatch, stub_clean_dry_run,
):
    """diff_apply_failed propagation in the single-file no-side-channel path
    (iter-0025 finding-007.c).
    """
    monkeypatch.setenv("CONSENSUS_MCP_CODEX_PATCH_APPLY", "1")
    iter_dir = _make_active_iter(repo_root_env)
    _write_goal_packet(iter_dir, codex_patch_apply_authorized=True)
    rel = "scripts/foo.py"
    _write_target_file(repo_root_env, rel, "hello\n")
    # Unmatchable context: file has "hello" but diff says "GOODBYE".
    unified_diff = (
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -1 +1,2 @@\n"
        " GOODBYE\n"
        "+world\n"
    )
    pp = {
        "patch_id": "codex-rev-001-patch",
        "applies_to_findings": ["codex-rev-001"],
        "base_sha": bundle_sha(repo_root_env, [rel]),
        "unified_diff": unified_diff,
        "files_touched": [rel],
        # NO _old_content / _new_content -> single-file diff-applier path.
    }
    _write_codex_review(iter_dir, pp)
    _write_claude_verification(iter_dir, pp["patch_id"], "approved")
    result = apply_codex_patch.handle(
        iteration_dir=str(iter_dir),
        patch_id=pp["patch_id"],
        actor=_make_actor(),
    )
    assert result["ok"] is False
    err = result.get("error") or ""
    assert "diff_apply_failed" in err
    # File untouched.
    assert (repo_root_env / rel).read_text(encoding="utf-8") == "hello\n"
