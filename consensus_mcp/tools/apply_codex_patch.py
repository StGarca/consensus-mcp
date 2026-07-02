"""apply.codex_patch MCP tool - Task #26 (iter-0016).

Per codex 2026-05-10 v4 directive (memory/project_codex_fix_author_directive.md):
Staged-apply of a codex-emitted patch that has been claude-verified per
CLAUDE.md.

Refuses by default. Application requires BOTH:
  1. goal_packet.authorization.codex_patch_apply_authorized == True
  2. env CONSENSUS_MCP_CODEX_PATCH_APPLY == "1"

Refuses without claude verification (verdict=approved). Detects base_sha drift
between codex review time and apply time (touched-file content changed under
codex's feet). On success emits canonical apply_step_landed audit event with
the structured last_mutation event object (per #28 closure invariant).

The implementation:
  - REUSES patch.stage_and_dry_run (T4) for the staged dry-run validator gate
  - DOES NOT reuse patch.apply_consensus_patch (T5); the apply step is bespoke
    (per-file os.replace atomic apply or unified-diff applier). T5 expects
    consensus.yaml-driven full-file overwrites with consensus_patch_id
    semantics; codex patches use a different shape (patch_proposal with
    base_sha/unified_diff) so apply_codex_patch authors its own apply step.
    If T5's apply contract evolves to match patch_proposal shape, the bespoke
    apply could be replaced with a T5 call - currently they diverge.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


SCHEMA = {
    "name": "apply.codex_patch",
    "description": (
        "Apply a codex-emitted patch that has been claude-verified per CLAUDE.md. "
        "Refuses by default; requires both goal_packet.authorization."
        "codex_patch_apply_authorized=true AND env CONSENSUS_MCP_CODEX_PATCH_APPLY=1. "
        "Refuses if no claude verification with verdict=approved exists. "
        "Refuses if patch_proposal.base_sha doesn't match current state of touched files "
        "(drift detection). On success, emits apply_step_landed audit event with the "
        "structured last_mutation event object."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration_dir": {"type": "string"},
            "patch_id": {"type": "string", "pattern": "^codex-rev-\\d+-patch$"},
            "actor": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "model_family": {"enum": ["codex", "claude"]},
                    "role": {"enum": ["fix_author", "correction_author"]},
                    "pass_id": {"type": "string"},
                },
                "required": ["id", "model_family", "role", "pass_id"],
                "additionalProperties": False,
            },
        },
        "required": ["iteration_dir", "patch_id", "actor"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "applied": {"type": "boolean"},
            "last_mutation": {"type": ["object", "null"]},
            "error": {"type": ["string", "null"]},
            "audit_event_id": {"type": ["string", "null"]},
        },
        "required": ["ok", "applied"],
    },
}


_PATCH_ID_PATTERN = re.compile(r"^codex-rev-\d+-patch$")


def _resolve_repo_root() -> Path:
    """M1 (consult iteration-m1-hardening-design-4d7d2469) Q2 pinned fix: the
    prior source-tree-relative fallback
    (`Path(__file__).resolve().parent.parent.parent`) broke under the
    standard pipx bootstrap - with no env override the resolved "repo root"
    was the INSTALL tree (site-packages parent), not the governed project,
    failing patch application closed. Now a shim over the ONE blessed
    resolver (_paths.resolve_repo_root): env override (CONSENSUS_MCP_REPO_ROOT
    then CONSENSUS_MCP_PROJECT_ROOT - what `consensus init` writes into
    .mcp.json) > cwd-ancestor containment-marker walk > RepoRootError
    (handle() maps it to the structured `repo_root_unresolvable` refusal).
    T4 + T6 anchor via the same _paths module, so the tools keep agreeing on
    the project root."""
    from consensus_mcp._paths import resolve_repo_root
    return resolve_repo_root()


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _refuse(error: str, last_mutation: dict | None = None) -> dict:
    return {
        "ok": False,
        "applied": False,
        "last_mutation": last_mutation,
        "error": error,
        "audit_event_id": None,
    }


def _validate_actor(actor: dict) -> str | None:
    """Defense-in-depth actor validation for direct Python handle() calls.

    iter-0026 F4-001 (claude-iter0025-001 fix): the prior key-presence check
    accepted ``id=""`` / ``pass_id=None`` / ``id=42`` which corrupt the
    audit-log provenance. Both id and pass_id MUST be non-empty strings.
    """
    if not isinstance(actor, dict):
        return "actor_invalid: actor must be a dict"
    required = ("id", "model_family", "role", "pass_id")
    missing = [key for key in required if key not in actor]
    if missing:
        return "actor_invalid: missing required actor key(s): " + ", ".join(missing)
    # iter-0026 F4-001: id and pass_id must be non-empty strings.
    for key in ("id", "pass_id"):
        value = actor.get(key)
        if not isinstance(value, str) or not value:
            return (
                f"actor_invalid: {key} must be a non-empty string; "
                f"got {type(value).__name__}={value!r}"
            )
    if actor.get("model_family") not in ("codex", "claude"):
        return "actor_invalid: model_family must be 'codex' or 'claude'"
    if actor.get("role") not in ("fix_author", "correction_author"):
        return "actor_invalid: role must be 'fix_author' or 'correction_author'"
    return None


def handle(
    iteration_dir: str,
    patch_id: str,
    actor: dict,
) -> dict:
    """Apply a verified codex patch under operator authorization."""
    iter_dir = Path(iteration_dir)
    # M1 (consult iteration-m1-hardening-design-4d7d2469) Q2: an unresolvable
    # repo root is a structured refusal, not an escaping exception - the tool
    # contract is refuse-with-reason, and the message tells the operator
    # exactly which env vars / markers were searched.
    from consensus_mcp._paths import RepoRootError
    try:
        repo_root = _resolve_repo_root()
    except RepoRootError as exc:
        return _refuse(f"repo_root_unresolvable: {exc}")

    # --- CR-5 (2026-05-22 security review): iteration_dir trust boundary ------
    # iteration_dir is caller-supplied and authorization is read FROM it
    # (goal_packet.yaml below). A caller could point T6 at a crafted dir whose
    # own goal_packet self-grants codex_patch_apply_authorized, defeating that
    # half of the dual interlock. Require it under the canonical active root
    # before reading any authorization or patch artifact.
    from consensus_mcp._paths import is_contained
    _active_root = (repo_root / "consensus-state" / "active").resolve()
    if not is_contained(iter_dir.resolve(), _active_root):
        return _refuse(
            "iteration_dir_outside_canonical_root: iteration_dir must be under "
            f"consensus-state/active/ (got {iter_dir})"
        )

    actor_error = _validate_actor(actor)
    if actor_error:
        return _refuse(actor_error)

    # Defense-in-depth: validate patch_id format. The MCP layer enforces this
    # via the input_schema pattern; record_verdict validates likewise. We
    # re-check here so a misuse via direct python import can't slip past.
    if not _PATCH_ID_PATTERN.match(patch_id):
        return _refuse(
            f"invalid_patch_id_format: must match ^codex-rev-\\d+-patch$; "
            f"got {patch_id!r}"
        )

    # --- Step 1: authorization gate (FAILS-CLOSED) ---------------------------
    env_flag = os.environ.get("CONSENSUS_MCP_CODEX_PATCH_APPLY") == "1"
    goal_packet = _read_yaml(iter_dir / "goal_packet.yaml")
    auth = goal_packet.get("authorization") or {}
    gp_flag = auth.get("codex_patch_apply_authorized") is True

    if not (env_flag and gp_flag):
        missing: list[str] = []
        if not env_flag:
            missing.append("env CONSENSUS_MCP_CODEX_PATCH_APPLY=1")
        if not gp_flag:
            missing.append("goal_packet.authorization.codex_patch_apply_authorized=true")
        return _refuse(
            "operator_authorization_missing: " + " AND ".join(missing)
        )

    # --- Step 2: load codex-review.yaml and locate the patch_proposal --------
    review = _read_yaml(iter_dir / "codex-review.yaml")
    if not review:
        return _refuse("codex_review_missing: codex-review.yaml not found or unreadable")
    findings = review.get("findings") or []
    patch_proposal: dict | None = None
    finding_id: str | None = None
    for f in findings:
        if not isinstance(f, dict):
            continue
        pp = f.get("patch_proposal")
        if isinstance(pp, dict) and pp.get("patch_id") == patch_id:
            patch_proposal = pp
            finding_id = f.get("id")
            break
    if patch_proposal is None:
        return _refuse(
            f"patch_proposal_missing: no finding in codex-review.yaml has "
            f"patch_proposal.patch_id={patch_id!r}"
        )

    files_touched = list(patch_proposal.get("files_touched") or [])
    base_sha = patch_proposal.get("base_sha") or ""
    unified_diff = patch_proposal.get("unified_diff") or ""

    # --- CR-1 + CR-2 (2026-05-22 security review): containment + scope gate ---
    # files_touched is AI-supplied. Fail CLOSED before any read/write:
    #   CR-1 each path must resolve INSIDE repo_root (no ../ or absolute escape)
    #   CR-2 each path must be within goal_packet.allowed_files and must not
    #        match forbidden_files. Previously only validate_disposition_index
    #        ran in the staging gate, which never checks source-file targets.
    from consensus_mcp._paths import resolve_contained, PathTraversalError
    from consensus_mcp.validators.scope_check import _matches_any
    _gp_allowed = goal_packet.get("allowed_files")
    _gp_forbidden = goal_packet.get("forbidden_files")
    _gp_allowed = _gp_allowed if isinstance(_gp_allowed, list) else []
    _gp_forbidden = _gp_forbidden if isinstance(_gp_forbidden, list) else []
    for _rel in files_touched:
        try:
            resolve_contained(repo_root, _rel)
        except PathTraversalError as exc:
            return _refuse(f"path_traversal: {exc}")
        if _matches_any(_gp_forbidden, _rel) or not _matches_any(_gp_allowed, _rel):
            return _refuse(
                f"out_of_scope: files_touched entry {_rel!r} is not within "
                "goal_packet.allowed_files"
            )

    # --- Step 3: claude verification gate ------------------------------------
    verif_path = iter_dir / "codex-patch-verifications" / f"{patch_id}.yaml"
    if not verif_path.exists():
        return _refuse("claude_verification_missing")
    verif = _read_yaml(verif_path)
    verdict = verif.get("verdict")
    if verdict != "approved":
        return _refuse(f"claude_verification_not_approved: verdict={verdict!r}")

    # --- Step 4: drift detection --------------------------------------------
    # Lazy import: keep _closure_invariant a soft dependency.
    from consensus_mcp._closure_invariant import bundle_sha
    # Defense-in-depth: refuse if base_sha is empty/missing. Codex schema
    # requires it, but apply layer must not silently skip drift detection
    # when the field is empty (review caught this; per codex 2026-05-10 v4
    # the drift gate is non-optional).
    if not base_sha:
        return _refuse(
            "base_sha_missing: patch_proposal.base_sha is empty/missing; "
            "drift detection requires the codex-reviewed bundle hash"
        )
    current_bundle = bundle_sha(repo_root, files_touched)
    if current_bundle != base_sha:
        return _refuse(
            f"base_sha_drift: codex saw base_sha={base_sha}, "
            f"current bundle_sha={current_bundle} (touched files changed since codex review)"
        )

    # --- Step 5: stage + dry-run via existing T4 primitive -------------------
    # Build proposed_patches in the form patch.stage_and_dry_run expects:
    # full-file old_string -> new_string substitution. We synthesize old_string
    # from the file's current on-disk content and new_string from applying the
    # codex unified_diff. v1.0: codex's patch_proposal already serializes the
    # post-patch full content via the test fixture's _new_content side-channel
    # for unit tests, AND production codex emissions provide a unified_diff
    # we re-apply via a minimal text patcher.
    #
    # For #26 this tool accepts full-file replacement via the unified_diff
    # being apply-able by patching against the current file content. To keep
    # the surgical scope per the task brief we use the simplest viable path:
    # require the patch_proposal to contain pre/post content as a structured
    # `_old_content` + `_new_content` pair OR derive both from unified_diff.
    # Prefer structured fields when present; otherwise fall back to text
    # diff application.
    new_contents: dict[str, str] = {}
    old_content = patch_proposal.get("_old_content")
    new_content = patch_proposal.get("_new_content")
    if (
        old_content is not None
        and new_content is not None
        and len(files_touched) == 1
    ):
        # Single-file fast-path: structured fields specify pre/post directly.
        rel = files_touched[0]
        target = repo_root / rel
        if target.exists():
            on_disk = target.read_text(encoding="utf-8")
            if on_disk != old_content:
                # Should have been caught by base_sha_drift, but defense-in-depth.
                return _refuse(
                    "base_sha_drift: on-disk content of "
                    f"{rel} does not match patch_proposal._old_content"
                )
        new_contents[rel] = new_content
    else:
        # Multi-file or no structured fields: derive from unified_diff.
        try:
            new_contents = _apply_unified_diff(repo_root, files_touched, unified_diff)
        except _DiffApplyError as exc:
            return _refuse(f"diff_apply_failed: {exc}")

    # Build proposed_patches for the staging gate.
    proposed_patches: list[dict] = []
    for rel, new_text in new_contents.items():
        target = repo_root / rel
        old_text = target.read_text(encoding="utf-8") if target.exists() else ""
        proposed_patches.append({
            "file": rel,
            "old_string": old_text,
            "new_string": new_text,
        })

    # Run staged dry-run. Codex patches target source-code files in
    # goal_packet.allowed_files; the iteration/review/consensus validators
    # check different artifacts (yaml under iteration_dir) and do not apply
    # to source-code mutations. Limit the gate to validate_disposition_index,
    # which is canonical-006's primary anti-regression safeguard. On internal
    # error or BLOCKED gate or any high/blocking finding -> refuse.
    from consensus_mcp.tools.patch_stage_and_dry_run import handle as stage_handle
    stage_result = stage_handle(
        iteration_id=iter_dir.name,
        proposed_patches=proposed_patches,
        validators_to_run=["validate_disposition_index"],
    )
    if "error" in stage_result:
        return _refuse(f"stage_or_dry_run_failed: {stage_result['error']}")
    gate = stage_result.get("gate_decision", "BLOCKED")
    findings_out = stage_result.get("dry_run_findings", []) or []
    high = [f for f in findings_out if f.get("severity") in ("high", "blocking", "critical")]
    if gate != "APPROVED" or high:
        return _refuse(
            f"stage_or_dry_run_failed: gate_decision={gate}, high_findings={len(high)}"
        )

    # --- Step 6: apply ------------------------------------------------------
    # iter-0026 F4-002 (claude-iter0025-002 fix): capture per-file pre-apply
    # content BEFORE the os.replace loop so we can roll back already-applied
    # files on any per-file failure. Prior behaviour left file_a mutated on
    # disk while reporting applied=False (untruthful).
    pre_apply_state: dict[str, str | None] = {}
    for rel in new_contents:
        target = repo_root / rel
        if target.exists():
            try:
                pre_apply_state[rel] = target.read_text(encoding="utf-8")
            except OSError:
                pre_apply_state[rel] = None  # unreadable; treat as missing
        else:
            pre_apply_state[rel] = None  # didn't exist pre-apply

    applied_files: list[str] = []
    try:
        for rel, new_text in new_contents.items():
            target = repo_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target.with_suffix(target.suffix + ".tmp")
            tmp_path.write_text(new_text, encoding="utf-8")
            os.replace(str(tmp_path), str(target))
            applied_files.append(rel)
    except Exception as exc:
        # iter-0026 F4-002: roll back already-applied files. Truthful applied=False.
        rollback_errors: list[str] = []
        for rel_applied in applied_files:
            try:
                target = repo_root / rel_applied
                prior = pre_apply_state.get(rel_applied)
                if prior is None:
                    # File didn't exist pre-apply; remove the newly-written file.
                    if target.exists():
                        target.unlink()
                else:
                    target.write_text(prior, encoding="utf-8")
            except Exception as rb_exc:
                rollback_errors.append(
                    f"{rel_applied}:{type(rb_exc).__name__}:{rb_exc}"
                )
        err_msg = (
            f"partial_apply_failed: applied={applied_files} failed={rel} "
            f"exception={type(exc).__name__}:{exc}"
        )
        if rollback_errors:
            err_msg += f" rollback_errors={rollback_errors}"
        return _refuse(err_msg)

    # --- Step 7: compute post_sha + emit apply_step_landed audit event ------
    post_sha = bundle_sha(repo_root, files_touched)
    # iter-0020: prefer helper-stamped unified_diff_sha256 from patch_proposal
    # (computed by _dispatch_codex._validate_patch_proposal); recompute
    # defensively if absent (older patches written before iter-0020 helper
    # stamping landed).
    diff_sha = patch_proposal.get("unified_diff_sha256")
    if not diff_sha:
        diff_sha = (
            hashlib.sha256(unified_diff.encode("utf-8")).hexdigest()
            if unified_diff
            else ""
        )
    timestamp = _now_utc()

    last_mutation = {
        "actor": dict(actor),
        "patch_id": patch_id,
        "files_touched": list(files_touched),
        "base_sha": base_sha,
        "post_sha": post_sha,
        "unified_diff_sha256": diff_sha,
        "timestamp": timestamp,
    }

    from consensus_mcp.tools.audit_append_event import handle as audit_handle
    audit_result = audit_handle(
        iteration_id=iter_dir.name,
        event_type="apply_step_landed",
        actor=actor.get("id"),
        effect="codex_patch_applied",
        files_modified=applied_files,
        extra_fields={
            "mutation_actor": last_mutation["actor"],
            "patch_id": patch_id,
            "base_sha": base_sha,
            "post_sha": post_sha,
            "timestamp": timestamp,
            "last_mutation": last_mutation,
            "codex_finding_id": finding_id,
        },
    )
    if "error" in audit_result:
        # Files already on disk; surface the audit failure for operator follow-up.
        return {
            "ok": False,
            "applied": True,
            "last_mutation": last_mutation,
            "error": f"audit_write_failed: {audit_result['error']}",
            "audit_event_id": None,
        }

    return {
        "ok": True,
        "applied": True,
        "last_mutation": last_mutation,
        "error": None,
        "audit_event_id": audit_result.get("event_id"),
    }


# ---------------------------------------------------------------------------
# Minimal unified-diff applier (single-hunk-per-file, additive/replace)
# ---------------------------------------------------------------------------

class _DiffApplyError(Exception):
    pass


def _apply_unified_diff(
    repo_root: Path,
    files_touched: list[str],
    unified_diff: str,
) -> dict[str, str]:
    """Apply a unified diff against the current files; return new content per file.

    Minimal applier sufficient for the codex patch_proposal shape. Splits the
    diff into per-file segments by '+++ b/<path>' headers; for each file,
    walks each hunk and applies +/-/space lines to the current content.

    Raises _DiffApplyError on any hunk that doesn't match expected context.
    Production v1.0 caveat: this is intentionally simple. Codex emits patches
    we generated and reviewed; pathological diffs SHOULD be caught by the
    staged dry-run gate downstream (or fail here cleanly). Prefer the
    structured _old_content/_new_content side-channel when feasible.
    """
    if not unified_diff.strip():
        raise _DiffApplyError("empty unified_diff")

    # Split into per-file segments by +++ header lines.
    # iter-0026 F4-004 (claude-iter0025-004 fix): explicit `+++ /dev/null`
    # recognition. The v1.0 applier does NOT support file deletions; refuse
    # with named reason `file_deletion_unsupported` instead of coincidentally
    # failing via files_touched lookup further down the pipeline. The matching
    # `--- /dev/null` (file-add) case works correctly via the existing
    # target.exists()==False branch - no explicit handling needed.
    lines = unified_diff.splitlines(keepends=False)
    file_segments: dict[str, list[str]] = {}
    current_file: str | None = None
    seg_lines: list[str] = []
    for ln in lines:
        if ln.startswith("+++ "):
            if current_file is not None:
                file_segments[current_file] = seg_lines
            path = ln[4:].strip()
            # iter-0026 F4-004: explicit `+++ /dev/null` -> delete (unsupported).
            if path == "/dev/null":
                raise _DiffApplyError(
                    "file_deletion_unsupported: +++ /dev/null detected; v1.0 "
                    "unified-diff applier does not support file deletion"
                )
            if path.startswith("b/"):
                path = path[2:]
            current_file = path
            seg_lines = []
            continue
        if ln.startswith("--- "):
            # New file being introduced; reset for this segment.
            if current_file is not None:
                # Already inside a segment; finalize previous.
                file_segments[current_file] = seg_lines
                current_file = None
                seg_lines = []
            continue
        if current_file is not None:
            seg_lines.append(ln)
    if current_file is not None:
        file_segments[current_file] = seg_lines

    out: dict[str, str] = {}
    for rel in files_touched:
        seg = file_segments.get(rel)
        if seg is None:
            raise _DiffApplyError(f"no diff segment for file {rel!r}")
        target = repo_root / rel
        original = target.read_text(encoding="utf-8") if target.exists() else ""
        new_text = _apply_one_file_diff(original, seg, rel)
        out[rel] = new_text
    return out


# iter-0026 F2: parse @@ -orig_start,orig_count +new_start,new_count @@ headers.
# Counts are optional in the unified-diff format (default 1). When orig_count
# is 0 the hunk starts AT orig_start - 1 conceptually but anchors against the
# trailing context-line position; standard convention.
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<orig_start>\d+)(?:,(?P<orig_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def _apply_one_file_diff(original: str, seg_lines: list[str], rel: str) -> str:
    """Apply a list of hunks (split by @@ headers) to ``original``.

    Per iter-0026 F2 (codex review codex-rev-001, 2026-05-10): each hunk is
    anchored at ``orig_start - 1`` (zero-indexed) into the original file. The
    PRIOR implementation did pure linear context-matching, ignoring @@ line
    numbers, which mutated the wrong occurrence when context lines repeated.

    Behaviour at each hunk:
      - Emit all original lines BEFORE orig_start - 1 verbatim (gap copy).
      - Walk the hunk body:
          - context (" ") line: must match orig_lines[orig_idx]; emit + advance.
          - delete ("-") line: must match orig_lines[orig_idx]; skip in output.
          - add ("+") line: emit; do not advance orig_idx.
          - blank/other line: ignored (intra-hunk blank).
      - On any context-line OR delete-line mismatch at the anchored position,
        raise _DiffApplyError with reason ``hunk_context_mismatch`` referencing
        the hunk header so the operator can re-anchor.

    After all hunks: emit remaining original lines verbatim. Preserves trailing
    newline state of the original (or adds one when original was nonempty +
    newline-terminated and result is not).

    Hunks MUST be in increasing orig_start order (standard unified-diff form);
    the prior-line-cursor enforces it. Out-of-order hunks raise
    _DiffApplyError.
    """
    orig_lines = original.splitlines(keepends=False)
    has_trailing_nl = original.endswith("\n") if original else True

    # Split seg_lines into hunks at @@ headers.
    hunks: list[tuple[re.Match, list[str]]] = []
    current_header: re.Match | None = None
    current_body: list[str] = []
    for ln in seg_lines:
        if ln.startswith("@@"):
            if current_header is not None:
                hunks.append((current_header, current_body))
            m = _HUNK_HEADER_RE.match(ln)
            if m is None:
                raise _DiffApplyError(
                    f"hunk_header_malformed: cannot parse {ln!r} in {rel!r}"
                )
            current_header = m
            current_body = []
            continue
        if current_header is None:
            # Pre-@@ noise (e.g., stray blank lines between +++/--- headers and
            # the first hunk). Skip silently.
            continue
        current_body.append(ln)
    if current_header is not None:
        hunks.append((current_header, current_body))

    if not hunks:
        # No-op diff segment; return original unchanged.
        return original

    out_lines: list[str] = []
    orig_idx = 0  # zero-indexed position in orig_lines

    for header, body in hunks:
        orig_start = int(header.group("orig_start"))
        # Anchor: orig_start is 1-indexed; convert to 0-indexed.
        # Special case: orig_start=0 means file-add hunk (--- /dev/null); the
        # anchor is 0 (empty original).
        anchor = max(0, orig_start - 1) if orig_start > 0 else 0

        if anchor < orig_idx:
            raise _DiffApplyError(
                f"hunk_out_of_order: hunk at orig_start={orig_start} (anchor "
                f"{anchor}) precedes already-processed line {orig_idx} of {rel!r}"
            )

        # Emit gap (verbatim originals between previous hunk and this anchor).
        while orig_idx < anchor:
            if orig_idx >= len(orig_lines):
                raise _DiffApplyError(
                    f"hunk_context_mismatch: hunk anchor at line {orig_start} "
                    f"is past end of file ({len(orig_lines)} lines) in {rel!r}"
                )
            out_lines.append(orig_lines[orig_idx])
            orig_idx += 1

        # Walk hunk body anchored at orig_idx.
        for ln in body:
            if ln.startswith("+"):
                out_lines.append(ln[1:])
                continue
            if ln.startswith("-"):
                if orig_idx >= len(orig_lines) or orig_lines[orig_idx] != ln[1:]:
                    actual = (
                        orig_lines[orig_idx]
                        if orig_idx < len(orig_lines)
                        else "<eof>"
                    )
                    raise _DiffApplyError(
                        f"hunk_context_mismatch: delete line {ln[1:]!r} does "
                        f"not match original at line {orig_idx + 1} (got "
                        f"{actual!r}) for hunk @@ -{orig_start} in {rel!r}"
                    )
                orig_idx += 1
                continue
            if ln.startswith(" "):
                if orig_idx >= len(orig_lines) or orig_lines[orig_idx] != ln[1:]:
                    actual = (
                        orig_lines[orig_idx]
                        if orig_idx < len(orig_lines)
                        else "<eof>"
                    )
                    raise _DiffApplyError(
                        f"hunk_context_mismatch: context line {ln[1:]!r} does "
                        f"not match original at line {orig_idx + 1} (got "
                        f"{actual!r}) for hunk @@ -{orig_start} in {rel!r}"
                    )
                out_lines.append(orig_lines[orig_idx])
                orig_idx += 1
                continue
            # Blank or other line; ignore (intra-hunk blank).

    # Emit remaining original lines after the last hunk.
    while orig_idx < len(orig_lines):
        out_lines.append(orig_lines[orig_idx])
        orig_idx += 1

    result = "\n".join(out_lines)
    if has_trailing_nl and not result.endswith("\n"):
        result += "\n"
    return result


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
