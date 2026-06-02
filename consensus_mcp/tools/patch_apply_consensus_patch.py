"""patch.apply_consensus_patch MCP tool. Phase 1 G2 (canonical-006 enforcement).

The only authorized writer to artifacts under consensus-state/active/<iter>/ after
Phase 0 sealing. Every apply is gated by patch.stage_and_dry_run (no high/blocking
findings), atomically written, and recorded via audit.append_event.

Failure modes:
  - iteration_not_found: iteration dir does not exist; refuse before staging.
  - dry_run_failed: stage_and_dry_run gate returned BLOCKED or had high/blocking
    findings; artifacts are NOT touched.
  - invalid_patches: a patch path would escape the iteration dir (path traversal);
    extra fields: reason (str).
  - audit_write_failed: artifacts HAVE been written but audit.append_event failed.
    Operator must investigate manually; rollback is out of scope for v1.0.
    Extra fields: applied_files (list[str]), audit_error (str).
  - partial_apply_failed: an OS error (disk full, permission denied, antivirus lock)
    interrupted the per-file write loop after one or more files were already written.
    Extra fields: applied_files (partial list written so far), failed_relpath (str),
    exception (str: "ExcType: message").
    NOTE: v1.0 does NOT roll back prior atomic writes on mid-loop failure; operator
    must manually inspect applied_files to reconcile the partially-written state.
    audit.append_event is NOT called in this branch (the partial state is
    operator-recoverable; auto-recording would lie about completion).

CONCURRENCY (v1.0 limitation): This tool is single-writer. Concurrent invocations
on the same iteration_id with overlapping relpaths will race - both calls may pass
the dry-run gate independently and the per-file os.replace order is non-deterministic.
The audit log is also a non-locked read-modify-write. **Do not invoke concurrently
for the same iteration_id.** Phase 1.x will add per-iteration filelock.

require_dry_run_clean=False raises NotImplementedError. Force-override / operator-bypass
is deferred to Phase 1.x.
"""
from __future__ import annotations

import os
from pathlib import Path

from consensus_mcp._paths import project_root, active_dir
from consensus_mcp.tools.patch_stage_and_dry_run import handle as stage_handle  # noqa: E402
from consensus_mcp.tools.audit_append_event import handle as audit_handle  # noqa: E402

# iter-0035 (Phase B step 6 per iter-0024 plan): migrated from module-level
# REPO_ROOT / ACTIVE_DIR captures to lazy `_paths` resolvers.


def __getattr__(name: str):
    """PEP 562 backward compat for external callers that referenced
    REPO_ROOT / ACTIVE_DIR as module attributes."""
    if name == "REPO_ROOT":
        return project_root()
    if name == "ACTIVE_DIR":
        return active_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_HIGH_SEVERITIES = {"high", "blocking", "critical"}

SCHEMA = {
    "name": "patch.apply_consensus_patch",
    "description": (
        "Gate-then-apply: runs patch.stage_and_dry_run first; if APPROVED and no "
        "high/blocking findings, atomically writes patches to the iteration dir and "
        "appends an apply_step_landed audit event. Refuses on BLOCKED or any "
        "high/blocking/critical finding. No force flag in v1.0."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration_id": {
                "type": "string",
                "description": "Must match an existing dir under consensus-state/active/",
            },
            "patches": {
                "type": "object",
                "description": (
                    "Keys: relative paths under the iteration dir (e.g. 'consensus.yaml'). "
                    "Values: full new YAML text to write."
                ),
                "additionalProperties": {"type": "string"},
            },
            "rationale": {
                "type": "string",
                "description": "Short human reason recorded in audit log.",
            },
            "require_dry_run_clean": {
                "type": "boolean",
                "description": (
                    "If False, raises NotImplementedError (Phase 1.x). "
                    "Always pass True (default) in v1.0."
                ),
                "default": True,
            },
            "validators_to_run": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": (
                    "Validator names to pass to stage_and_dry_run. "
                    "None (default) = all 4 main validators."
                ),
            },
        },
        "required": ["iteration_id", "patches", "rationale"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "description": (
            "Success: {applied_files, dry_run_findings, audit_event_id}. "
            "Failure: {error, ...} where error is one of: "
            "dry_run_failed | iteration_not_found | invalid_patches | "
            "audit_write_failed | partial_apply_failed."
        ),
        "oneOf": [
            {
                "title": "success",
                "type": "object",
                "properties": {
                    "applied_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relative paths within iteration dir that were written.",
                    },
                    "dry_run_findings": {
                        "type": "array",
                        "description": "Echoed from stage_and_dry_run for traceability.",
                    },
                    "audit_event_id": {
                        "type": "string",
                        "description": "event_id returned by audit.append_event.",
                    },
                },
                "required": ["applied_files", "dry_run_findings", "audit_event_id"],
            },
            {
                "title": "failure",
                "type": "object",
                "properties": {
                    "error": {
                        "type": "string",
                        "enum": [
                            "dry_run_failed",
                            "iteration_not_found",
                            "invalid_patches",
                            "audit_write_failed",
                            "partial_apply_failed",
                        ],
                    },
                    "dry_run_findings": {
                        "type": "array",
                        "description": "Only present when error==dry_run_failed.",
                    },
                    # invalid_patches branch
                    "reason": {
                        "type": ["string", "null"],
                        "description": "Only present when error==invalid_patches.",
                    },
                    # audit_write_failed branch + partial_apply_failed branch
                    "applied_files": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            "Present when error==audit_write_failed (full list) or "
                            "error==partial_apply_failed (partial list written so far)."
                        ),
                    },
                    # audit_write_failed branch
                    "audit_error": {
                        "type": ["string", "null"],
                        "description": "Only present when error==audit_write_failed.",
                    },
                    # partial_apply_failed branch
                    "failed_relpath": {
                        "type": ["string", "null"],
                        "description": "Relpath that raised on write; only when error==partial_apply_failed.",
                    },
                    "exception": {
                        "type": ["string", "null"],
                        "description": "ExcType: message string; only when error==partial_apply_failed.",
                    },
                },
                "required": ["error"],
            },
        ],
    },
}


def handle(
    iteration_id: str,
    patches: dict,
    rationale: str,
    require_dry_run_clean: bool = True,
    validators_to_run: list | None = None,
) -> dict:
    """Apply consensus patches to an iteration dir, gated by dry-run.

    Steps:
      1. Guard: iteration dir must exist.
      2. Guard: no path traversal in patch keys.
      3. Build proposed_patches for stage_and_dry_run (full-file replacement).
      4. Run stage_and_dry_run gate; refuse if BLOCKED or any high/blocking/critical finding.
      5. Atomic write each patch (tmp + os.replace).
      6. Append apply_step_landed audit event; return event_id.

    validators_to_run: passed through to stage_and_dry_run. None = default (all 4 main validators).

    Failure modes documented in module docstring.
    """
    if not require_dry_run_clean:
        raise NotImplementedError(
            "require_dry_run_clean=False is deferred to Phase 1.x; "
            "always pass True in v1.0."
        )

    # --- Step 1: iteration existence guard ---
    iter_dir = active_dir() / iteration_id
    if not iter_dir.is_dir():
        return {"error": "iteration_not_found"}

    # --- Step 2: path traversal guard ---
    for relpath in patches:
        target = (iter_dir / relpath).resolve()
        try:
            target.relative_to(iter_dir.resolve())
        except ValueError:
            return {
                "error": "invalid_patches",
                "reason": f"path_traversal:{relpath}",
            }

    # --- Step 3: build proposed_patches for stage_and_dry_run ---
    # stage_and_dry_run expects {file: repo-relative str, old_string, new_string}.
    # We do a full-file replacement: old_string = current content (or "" if new file).
    proposed_patches = []
    for relpath, new_text in patches.items():
        target = iter_dir / relpath
        if target.exists():
            old_text = target.read_text(encoding="utf-8")
        else:
            old_text = ""
        # Repo-relative path for stage_and_dry_run.
        repo_rel = str(target.relative_to(project_root()))
        proposed_patches.append({
            "file": repo_rel,
            "old_string": old_text,
            "new_string": new_text,
        })

    # --- Step 4: dry-run gate ---
    stage_kwargs: dict = {
        "iteration_id": iteration_id,
        "proposed_patches": proposed_patches,
    }
    if validators_to_run is not None:
        stage_kwargs["validators_to_run"] = validators_to_run
    dry_run_result = stage_handle(**stage_kwargs)

    dry_run_findings = dry_run_result.get("dry_run_findings", [])

    # Refuse on internal error from stage_handle.
    if "error" in dry_run_result:
        return {
            "error": "dry_run_failed",
            "dry_run_findings": dry_run_findings,
        }

    gate_decision = dry_run_result.get("gate_decision", "BLOCKED")
    high_findings = [
        f for f in dry_run_findings
        if f.get("severity") in _HIGH_SEVERITIES
    ]

    if gate_decision != "APPROVED" or high_findings:
        return {
            "error": "dry_run_failed",
            "dry_run_findings": dry_run_findings,
        }

    # --- Step 5: atomic apply ---
    applied_files: list[str] = []
    try:
        for relpath, new_text in patches.items():
            target = iter_dir / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target.with_suffix(target.suffix + ".tmp")
            tmp_path.write_text(new_text, encoding="utf-8")
            os.replace(str(tmp_path), str(target))
            applied_files.append(relpath)
    except Exception as exc:
        # Mid-loop IO failure (disk full, permission denied, antivirus lock, etc.).
        # Prior writes already on disk; audit is NOT recorded (partial state must be
        # reconciled manually - auto-recording would lie about completion).
        exception_str = type(exc).__name__ + ": " + str(exc)
        return {
            "error": "partial_apply_failed",
            "applied_files": applied_files,
            "failed_relpath": relpath,
            "exception": exception_str,
        }

    # --- Step 6: audit event ---
    # Canonical event type for a landed apply is apply_step_landed (required field: effect).
    audit_result = audit_handle(
        iteration_id=iteration_id,
        event_type="apply_step_landed",
        effect=rationale,
        files_modified=applied_files,
        extra_fields={
            "dry_run_findings_count": len(dry_run_findings),
        },
    )

    if "error" in audit_result:
        # Artifacts already written; operator must investigate.
        return {
            "error": "audit_write_failed",
            "applied_files": applied_files,
            "audit_error": audit_result["error"],
        }

    return {
        "applied_files": applied_files,
        "dry_run_findings": dry_run_findings,
        "audit_event_id": audit_result["event_id"],
    }


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
