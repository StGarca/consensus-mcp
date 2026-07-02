"""audit.append_event MCP tool. Phase 1 G1 partial.

Appends events to consensus-state/active/<iteration_id>/independence-audit.yaml.
Validates event_type against canonical list (claude-rev-045 enforcement
code-side). Validates required fields per event type.

iter-0018 hardening (codex 2026-05-10 v5):
- Finding 3: at iteration_closed time, refuse if working-tree paths exist
  that are NOT in any apply_step_landed event's files_touched set
  (unaudited_mutation_detected).
- Finding 5: when apply_step_landed events exist but the closure invariant
  evaluator returns None or raises, REFUSE (closure_invariant_evaluation_failed).
  Treat un-evaluable as failed (fail-closed).

Event-specific required fields are first-class kwargs and input_schema
properties (sealed_inputs, staging_dir, validator, effect, closing_state).
Pass them as top-level arguments - not via extra_fields.
extra_fields is a catch-all for unspecified extensions only.

CONCURRENCY - M1 (consult iteration-m1-hardening-design-4d7d2469) Q1: the
read-modify-write is serialized ACROSS PROCESSES via
`_atomic_io.locked_mutation(audit_path)` (mkdir lock dir + stale takeover),
and the write-back is the blessed unique-tmp + os.replace atomic writer
(`_atomic_io.atomic_write_text`) - a crash mid-write can no longer truncate
independence-audit.yaml (the pre-M1 docstring claimed os.replace atomicity
while the code used a plain truncating write_text; both halves are now true).
Concurrent appends for the same iteration_id serialize; an unacquirable lock
is a structured `{"error": "state_lock_timeout", ...}` refusal carrying the
holder's owner.json fields. Stale-takeover events for the audit file go to
the DISPATCH LOG ONLY (never the audit file itself - that would recurse into
the very lock being held; see _emit_state_lock_takeover).
"""
from __future__ import annotations
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from consensus_mcp._atomic_io import LockTimeout, atomic_write_text, locked_mutation
from consensus_mcp._paths import project_root, active_dir

# M1 (consult iteration-m1-hardening-design-4d7d2469) Q1: lock-acquisition
# budget for the audit-file mutation window.
#
# M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q2+Q11:
# the timeout budget and the structured `state_lock_timeout` refusal builder
# have ONE definition site (review_write_and_seal) - imported here so the 30s
# constant (now Q11 env-overridable via CONSENSUS_MCP_STATE_LOCK_TIMEOUT_SECONDS)
# and the refusal shape can never drift between the two seal-pipeline writers.
# `_STATE_LOCK_TIMEOUT_S` re-binds into this module's namespace as a real
# attribute, so existing tests may still shrink it via
# `monkeypatch.setattr(audit_tool, "_STATE_LOCK_TIMEOUT_S", ...)` and handle()
# reads this module's own binding. (No top-level import cycle:
# review_write_and_seal imports audit_append_event only lazily, inside its
# handle().)
from consensus_mcp.tools.review_write_and_seal import (
    _STATE_LOCK_TIMEOUT_S,
    _state_lock_timeout_refusal,
)

# iter-0036 (Phase B step 9 per iter-0024 plan, HIGH-impact audit trail
# tool): migrated from module-level REPO_ROOT/ACTIVE_DIR captures to lazy
# `_paths` resolvers. Tests redirect paths via `monkeypatch.setenv`
# (CONSENSUS_MCP_REPO_ROOT / CONSENSUS_MCP_STATE_ROOT), NOT
# `monkeypatch.setattr` on this module - the latter is unsafe because
# pytest's monkeypatch saves __getattr__-synthesized values into __dict__
# at teardown, permanently shadowing the lazy resolver for subsequent
# tests. iter_0018 already uses the setenv pattern; closure_invariant
# was updated to match in this iteration. PEP 562 `__getattr__` still
# provides backward compat for any external read of `module.REPO_ROOT`
# / `module.ACTIVE_DIR`.


def __getattr__(name: str):
    """PEP 562 backward compat for external `module.REPO_ROOT` /
    `module.ACTIVE_DIR` reads. Internal code should call `project_root()`
    / `active_dir()` directly."""
    if name == "REPO_ROOT":
        return project_root()
    if name == "ACTIVE_DIR":
        return active_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class GitUnavailableError(RuntimeError):
    """Raised when NO git command could be executed (git binary missing,
    timeout, or OS error on every sub-command).

    iter-0018 H-5: consensus-mcp is inherently git-based - the closure
    invariant uses SHAs and snapshots use git. There is no "git is optional /
    not a git repo" design. When git is wholly unavailable the
    mutation-completeness gate cannot be verified, so the gate must FAIL CLOSED
    (refuse the close) rather than allow-empty (fail-open)."""

CANONICAL_EVENT_TYPES: dict[str, dict] = {
    "review_packet_built": {"required": ["artifact", "sha256"], "optional": []},
    "reviewer_invoked": {"required": ["actor", "artifact", "independence_attestation"], "optional": ["invocation_protocol"]},
    "review_returned_and_sealed": {"required": ["actor", "artifact", "sha256", "independence_attestation"], "optional": ["validator_status"]},
    "reviewer_invocation_pending": {"required": ["actor"], "optional": ["next_constraint"]},
    "sealed_inputs_recorded": {"required": ["sealed_inputs"], "optional": []},
    "both_reviews_sealed": {"required": ["actor"], "optional": ["note"]},
    "consensus_built": {"required": ["artifact", "sha256"], "optional": ["consensus_state"]},
    "operator_authorization_received": {"required": ["actor"], "optional": ["authorization_form_response", "interpretation"]},
    "staged_changes": {"required": ["staging_dir"], "optional": ["staged_files"]},
    "canonical_006_dry_run_executed": {"required": ["validator"], "optional": ["findings", "gate_decision"]},
    "apply_step_landed": {"required": ["effect"], "optional": ["files_modified"]},
    "iteration_closed": {"required": ["closing_state"], "optional": ["governance_milestone_closed"]},
}

# Per-agent-prefixed names grandfathered at validator level but REJECTED by this tool.
FORBIDDEN_PER_AGENT_PREFIXED: dict[str, str] = {
    "codex_reviewer_invoked": "reviewer_invoked",
    "claude_reviewer_invoked": "reviewer_invoked",
    "codex_review_returned_and_sealed": "review_returned_and_sealed",
    "claude_review_returned_and_sealed": "review_returned_and_sealed",
    "codex_reviewer_invocation_pending": "reviewer_invocation_pending",
    "claude_reviewer_invocation_pending": "reviewer_invocation_pending",
}

SCHEMA = {
    "name": "audit.append_event",
    "description": "Append canonical event to an iteration's independence-audit.yaml.",
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration_id": {"type": "string"},
            "event_type": {"type": "string"},
            "actor": {"type": "string"},
            "artifact": {"type": ["string", "null"]},
            "sha256": {"type": ["string", "null"]},
            "independence_attestation": {"type": ["object", "null"]},
            "sealed_inputs": {
                "type": ["object", "null"],
                "description": "required for sealed_inputs_recorded; the sealed input set",
            },
            "staging_dir": {
                "type": ["string", "null"],
                "description": "required for staged_changes; path to the staging directory",
            },
            "validator": {
                "type": ["string", "null"],
                "description": "required for canonical_006_dry_run_executed; validator identifier",
            },
            "effect": {
                "type": ["string", "null"],
                "description": "required for apply_step_landed; description of the landed effect",
            },
            "closing_state": {
                "type": ["string", "null"],
                "description": "required for iteration_closed; terminal state of the iteration",
            },
            "note": {"type": ["string", "null"]},
            "validator_status": {"type": ["string", "null"]},
            "invocation_protocol": {"type": ["string", "null"]},
            "consensus_state": {"type": ["string", "null"]},
            "authorization_form_response": {"type": ["string", "null"]},
            "interpretation": {"type": ["string", "null"]},
            "files_modified": {"type": ["array", "null"]},
            "findings": {"type": ["string", "null"]},
            "gate_decision": {"type": ["string", "null"]},
            "governance_milestone_closed": {"type": ["string", "null"]},
            "next_constraint": {"type": ["string", "null"]},
            "staged_files": {"type": ["array", "null"]},
            # v1.19.0 result-logging - ADDITIVE optional fields.
            # apply_step_landed may carry which findings a fix addressed.
            "finding_ids": {
                "type": ["array", "null"],
                "description": (
                    "v1.19.0: optional on apply_step_landed; finding ids this "
                    "fix addresses (drives results-log fixes_applied)."
                ),
            },
            "files_touched": {
                "type": ["array", "null"],
                "description": "v1.19.0: optional on apply_step_landed; files the fix touched.",
            },
            "fix_summary": {
                "type": ["string", "null"],
                "description": "v1.19.0: optional on apply_step_landed; one-line fix description.",
            },
            # iteration_closed may carry per-finding dispositions.
            "finding_dispositions": {
                "type": ["array", "null"],
                "description": (
                    "v1.19.0: optional on iteration_closed; "
                    "[{id, disposition, evidence_ref?}] for the results log."
                ),
            },
            "extra_fields": {
                "type": ["object", "null"],
                "description": (
                    "catch-all for unspecified extensions only; "
                    "use named top-level fields when present in input_schema"
                ),
            },
        },
        "required": ["iteration_id", "event_type"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string"},
            "audit_yaml_post_sha256": {"type": "string"},
        },
        "required": ["event_id", "audit_yaml_post_sha256"],
    },
}


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_sha256(path: Path) -> str:
    """Return SHA-256 of the YAML file in canonical (sorted-keys safe_dump) form.

    NOTE: this is the canonical_yaml_sha256 per spec section 7 - it hashes the
    re-serialized form, NOT raw file bytes. External consumers must re-canonicalize
    (yaml.safe_dump(yaml.safe_load(raw), sort_keys=True)) before comparing hashes.
    """
    raw = path.read_bytes()
    loaded = yaml.safe_load(raw)
    return hashlib.sha256(
        yaml.safe_dump(loaded, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _read_audit_data(audit_path: Path) -> dict:
    """Load the audit YAML mapping ({} when the file does not exist yet)."""
    if audit_path.exists():
        raw = audit_path.read_bytes()
        return yaml.safe_load(raw) or {}
    return {}


def _emit_state_lock_takeover(target: Path, status) -> None:
    """Report a stale-lock takeover to the dispatch log ONLY.

    M1 (consult iteration-m1-hardening-design-4d7d2469, gemini-rev-002 +
    kimi-rev-001): locked_mutation never emits events; the CALLER reports the
    takeover here. The sink is dispatch-log.jsonl via
    _dispatch_base._log_dispatch, which is LOCK-FREE by invariant (it takes
    no locked_mutation), so emitting from inside a hold can never recurse or
    deadlock - regardless of which file is locked. For the audit file
    specifically, the event must NEVER be appended to independence-audit.yaml
    itself: that append would re-enter the very lock being held. Best effort:
    a reporting failure must never fail the caller's mutation.
    """
    try:
        from consensus_mcp._dispatch_base import _log_dispatch
        from consensus_mcp._paths import dispatch_log_path

        owner = status.takeover_owner if isinstance(status.takeover_owner, dict) else {}
        _log_dispatch(
            dispatch_log_path(),
            {
                "event": "state_lock_stale_takeover",
                "target": str(target),
                "stale_owner_pid": owner.get("pid"),
                "stale_owner_host": owner.get("host"),
                "stale_owner_claimed_at_epoch": owner.get("claimed_at_epoch"),
                "waited_s": round(status.waited_s, 3),
                "taker_pid": os.getpid(),
            },
        )
    except Exception:
        pass


def handle(
    iteration_id: str,
    event_type: str,
    actor: str = None,
    artifact=None,
    sha256=None,
    independence_attestation=None,
    # Event-specific required fields - pass as top-level args, not via extra_fields.
    sealed_inputs=None,       # required for: sealed_inputs_recorded
    staging_dir=None,         # required for: staged_changes
    validator=None,           # required for: canonical_006_dry_run_executed
    effect=None,              # required for: apply_step_landed
    closing_state=None,       # required for: iteration_closed
    # Optional named fields for common event types.
    note=None,
    validator_status=None,
    invocation_protocol=None,
    consensus_state=None,
    authorization_form_response=None,
    interpretation=None,
    files_modified=None,
    findings=None,
    gate_decision=None,
    governance_milestone_closed=None,
    next_constraint=None,
    staged_files=None,
    # v1.19.0 result-logging - ADDITIVE optional fields (backward-compatible).
    finding_ids=None,          # optional on: apply_step_landed
    files_touched=None,        # optional on: apply_step_landed
    fix_summary=None,          # optional on: apply_step_landed
    finding_dispositions=None, # optional on: iteration_closed
    # Catch-all for unspecified extensions only; prefer named fields above.
    extra_fields: dict = None,
) -> dict:
    """Append event to <iteration>/independence-audit.yaml.

    Event-specific required fields (sealed_inputs, staging_dir, validator,
    effect, closing_state) must be passed as top-level kwargs. extra_fields
    is reserved for future extensions not yet in input_schema.

    Returns:
      {event_id: <utc-ts + event_type + actor>, audit_yaml_post_sha256: <sha>}

    Errors (returned as {"error": "..."}):
      - non-canonical event_type
      - per-agent-prefixed event_type (with hint)
      - missing required field for the given event_type
      - iteration directory does not exist
      - state_lock_timeout: the audit-file mutation lock could not be
        acquired within _STATE_LOCK_TIMEOUT_S; the refusal carries the
        holder's owner.json fields (owner_pid, owner_host,
        owner_claimed_at_epoch) - M1 Q1
    """
    # --- validate event_type ---
    if event_type in FORBIDDEN_PER_AGENT_PREFIXED:
        canonical = FORBIDDEN_PER_AGENT_PREFIXED[event_type]
        return {
            "error": (
                f"per-agent-prefixed event_type '{event_type}' is not allowed "
                f"(post-canonical-pin); use canonical name '{canonical}' instead"
            )
        }

    if event_type not in CANONICAL_EVENT_TYPES:
        allowed = ", ".join(sorted(CANONICAL_EVENT_TYPES))
        return {
            "error": (
                f"non-canonical event_type '{event_type}'; "
                f"allowed: {allowed}"
            )
        }

    # --- validate iteration dir ---
    iteration_dir = active_dir() / iteration_id
    if not iteration_dir.is_dir():
        return {"error": f"iteration directory not found: {iteration_dir}"}

    audit_path = iteration_dir / "independence-audit.yaml"

    # --- validate required fields ---
    spec = CANONICAL_EVENT_TYPES[event_type]
    # Build a flat dict of all supplied values so we can check required fields.
    supplied: dict = {}
    if actor is not None:
        supplied["actor"] = actor
    if artifact is not None:
        supplied["artifact"] = artifact
    if sha256 is not None:
        supplied["sha256"] = sha256
    if independence_attestation is not None:
        supplied["independence_attestation"] = independence_attestation
    if sealed_inputs is not None:
        supplied["sealed_inputs"] = sealed_inputs
    if staging_dir is not None:
        supplied["staging_dir"] = staging_dir
    if validator is not None:
        supplied["validator"] = validator
    if effect is not None:
        supplied["effect"] = effect
    if closing_state is not None:
        supplied["closing_state"] = closing_state
    if extra_fields:
        supplied.update(extra_fields)

    missing = [f for f in spec["required"] if f not in supplied]
    if missing:
        return {
            "error": (
                f"event_type '{event_type}' requires fields: {missing}; "
                f"provided: {sorted(supplied)}"
            )
        }

    # --- pre-lock audit snapshot for the iteration_closed gate ---
    # M1 (consult iteration-m1-hardening-design-4d7d2469) Q1: the close gate
    # below runs git subprocesses (_detect_unaudited_mutation, 15s timeouts)
    # and the hold-window discipline forbids subprocess work inside the lock,
    # so the gate evaluates on this PRE-LOCK snapshot. iteration_closed is
    # single-writer by design (one orchestrator closes an iteration); the
    # locked re-read at append time below is what guarantees no concurrent
    # APPEND is ever lost.
    # (Ordinary appends skip this read entirely - they read once, under the
    # lock, at append time.)

    # --- Task #28: closure-cross-verification-and-freshness invariant ---
    # T6 is the LAST gate; refuse to record `iteration_closed` when invariant
    # fails. Defense-in-depth pairs with the _self_drive stop rule and
    # loop.run_goal transition guard.
    invariant_result = None
    if event_type == "iteration_closed":
        audit_log: list = _read_audit_data(audit_path).get("audit_log", [])
        # iter-0018 Finding 3: mutation-completeness check. If the working tree
        # has paths that are NOT in any apply_step_landed event's files_touched
        # set, refuse. Conservative-fail-closed for manual edits.
        # iter-0018 H-5: if git is wholly unavailable the gate cannot be
        # verified - FAIL CLOSED rather than allow-empty (fail-open). Scoped to
        # only this call so it does not swallow the separate closure-invariant
        # try/except below.
        try:
            unaudited = _detect_unaudited_mutation(audit_log, project_root())
        except GitUnavailableError as exc:
            return {"error": f"mutation_completeness_unverifiable: {exc}"}
        if unaudited:
            return {
                "error": (
                    f"unaudited_mutation_detected: working-tree paths not "
                    f"covered by any apply_step_landed event: {sorted(unaudited)}"
                ),
            }

        # iter-0018 Finding 5: fail-closed when invariant evaluator returns
        # None or raises AND the audit log carries apply_step_landed events.
        # Treat un-evaluable as failed.
        has_apply_events = any(
            isinstance(e, dict) and e.get("event") == "apply_step_landed"
            for e in audit_log
        )
        try:
            invariant_result = _evaluate_closure_invariant(audit_log, iteration_dir)
        except Exception as exc:
            if has_apply_events:
                return {
                    "error": (
                        f"closure_invariant_evaluation_failed: evaluator raised "
                        f"{type(exc).__name__}: {exc}"
                    ),
                }
            invariant_result = None
        if invariant_result is None and has_apply_events:
            return {
                "error": (
                    "closure_invariant_evaluation_failed: evaluator returned "
                    "None despite apply_step_landed events being present"
                ),
            }
        if invariant_result is not None and not invariant_result["ok"]:
            return {
                "error": (
                    f"closure_cross_verification_failed: {invariant_result['reason']}"
                ),
                "checks": invariant_result["checks"],
            }

    # --- build event record ---
    timestamp = _now_utc()
    actor_tag = actor or "anon"
    event_id = f"{timestamp}_{event_type}_{actor_tag}"

    record: dict = {"event": event_type, "timestamp_utc": timestamp, "event_id": event_id}
    # Write all non-None fields into the record (preserves field order: common first).
    _named = {
        "actor": actor,
        "artifact": artifact,
        "sha256": sha256,
        "independence_attestation": independence_attestation,
        "sealed_inputs": sealed_inputs,
        "staging_dir": staging_dir,
        "validator": validator,
        "effect": effect,
        "closing_state": closing_state,
        "note": note,
        "validator_status": validator_status,
        "invocation_protocol": invocation_protocol,
        "consensus_state": consensus_state,
        "authorization_form_response": authorization_form_response,
        "interpretation": interpretation,
        "files_modified": files_modified,
        "findings": findings,
        "gate_decision": gate_decision,
        "governance_milestone_closed": governance_milestone_closed,
        "next_constraint": next_constraint,
        "staged_files": staged_files,
        # v1.19.0 result-logging additive fields.
        "finding_ids": finding_ids,
        "files_touched": files_touched,
        "fix_summary": fix_summary,
        "finding_dispositions": finding_dispositions,
    }
    for k, v in _named.items():
        if v is not None:
            record[k] = v
    if extra_fields:
        record.update(extra_fields)

    # --- locked read-append-write (M1 Q1) ---
    # M1 (consult iteration-m1-hardening-design-4d7d2469): the audit-file
    # read-modify-write races were deterministically reproduced (two
    # concurrent appends -> one event lost; crash mid-write_text -> truncated
    # file). The whole window is now serialized cross-process via
    # locked_mutation(audit_path) with a FRESH read under the lock, and the
    # write-back is the blessed unique-tmp + os.replace atomic writer. The
    # post-write canonical hash is computed inside the hold so it describes
    # exactly the state this append left behind.
    try:
        with locked_mutation(audit_path, timeout_s=_STATE_LOCK_TIMEOUT_S) as _lock_status:
            if _lock_status.takeover:
                # Dispatch log ONLY - never the audit file being locked.
                _emit_state_lock_takeover(audit_path, _lock_status)
            data = _read_audit_data(audit_path)
            locked_audit_log: list = data.get("audit_log", [])
            locked_audit_log.append(record)
            data["audit_log"] = locked_audit_log
            atomic_write_text(audit_path, yaml.safe_dump(data, sort_keys=False))
            post_sha = _canonical_sha256(audit_path)
    except LockTimeout as exc:
        # M1-remediation (consult iteration-path-to-a-remediation-260caad1)
        # Q2+Q11: structured refusal via the ONE shared builder (fail loud,
        # never proceed unlocked) - the holder's owner.json fields
        # (gemini-rev-001) plus the remedy-naming detail. Nothing was written.
        return _state_lock_timeout_refusal(exc, audit_path)

    # --- Task #28: author closure-certificate.yaml on PASS for iteration_closed ---
    if event_type == "iteration_closed" and invariant_result is not None and invariant_result["ok"]:
        try:
            _author_closure_certificate(
                iteration_dir,
                iteration_id,
                invariant_result,
                timestamp,
            )
        except Exception as exc:
            # Non-fatal: invariant gate already passed; certificate authoring
            # failure is a follow-up concern, not a close-blocker. BUT log to
            # stderr so the operator notices when their primary close-validity
            # artifact (closure-certificate.yaml) failed to author. Silent
            # failure here = operator reviews a non-existent certificate.
            print(
                f"WARN: closure-certificate.yaml authoring failed for "
                f"iteration {iteration_id}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    # post_sha was computed inside the locked hold above (M1 Q1) so it
    # reflects exactly this append's resulting file state, not a later
    # concurrent writer's.
    result: dict = {"event_id": event_id, "audit_yaml_post_sha256": post_sha}

    # --- v1.19.0: author the per-iteration results record on iteration_closed ---
    # Runs AFTER the closure invariant + closure-certificate, mirroring the
    # certificate author's non-fatal-but-warns pattern: a results-logging
    # failure must NEVER break a valid close and must NEVER be silently
    # skipped. Reuses this call's single-writer position (one orchestrator per
    # iteration). The audit log is already written, so the record derives from
    # the just-persisted state.
    if event_type == "iteration_closed":
        try:
            from consensus_mcp import _results_log
            _results_log.write_results_record(
                iteration_dir,
                finding_dispositions=finding_dispositions,
                source_audit_event_id=event_id,
                source_audit_yaml_post_sha256=post_sha,
            )
        except Exception as exc:
            # Non-fatal: the close already succeeded (invariant passed,
            # certificate authored). Surface the failure to stderr AND on the
            # result so neither the operator nor a programmatic caller can miss
            # that the results record was not written.
            warn = (
                f"iteration-results authoring failed for iteration "
                f"{iteration_id}: {type(exc).__name__}: {exc}"
            )
            print(f"WARN: {warn}", file=sys.stderr)
            result["results_log_warning"] = warn

    return result


def _detect_working_tree_changes(repo_root: Path) -> list[str]:
    """Return repo-relative paths that have working-tree or staged changes.

    Used by iter-0018 Finding 3 (mutation-completeness check). Implementation
    runs `git diff --name-only HEAD` (covers both staged and unstaged) plus
    `git ls-files --others --exclude-standard` (untracked files) and returns
    the union.

    A single sub-command that fails (non-zero exit, or raises
    FileNotFoundError/TimeoutExpired/OSError) is tolerated via `continue` so
    that one odd sub-command does not nuke the whole check while the other
    still succeeds. BUT if NO sub-command succeeds at all - git is wholly
    unavailable - this raises `GitUnavailableError` instead of returning [].
    iter-0018 H-5: consensus-mcp is inherently git-based, so an unverifiable
    mutation-completeness gate must FAIL CLOSED at the caller, never silently
    pass on an empty set (fail-open).

    Tests can monkeypatch this function to inject simulated working-tree
    state without needing a real git repo.
    """
    import subprocess
    paths: set[str] = set()
    any_ok = False
    for cmd in (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            result = subprocess.run(
                cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            if result.returncode != 0:
                continue
            any_ok = True
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    paths.add(line)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    if not any_ok:
        raise GitUnavailableError(
            "no git command succeeded (git missing, timed out, or errored on "
            "every sub-command); cannot verify mutation-completeness"
        )
    return sorted(paths)


def _detect_unaudited_mutation(audit_log: list, repo_root: Path) -> list[str]:
    """Return working-tree paths NOT covered by any apply_step_landed event.

    Iter-0018 Finding 3: if code is changed OUTSIDE apply.codex_patch (manual
    edit, direct write), the audit log has no apply_step_landed event for it,
    so the closure invariant trivially passes ("no mutation = no gate"). But
    there WAS a mutation. This helper computes the set difference:
      working_tree_paths - audited_paths
    and returns the unaudited subset. Empty list means nothing unaudited.
    """
    audited: set[str] = set()
    for e in audit_log or []:
        if not isinstance(e, dict):
            continue
        if e.get("event") != "apply_step_landed":
            continue
        # Top-level files_touched (post-#28 emitter shape)
        ft = e.get("files_touched")
        if isinstance(ft, list):
            audited.update(str(p) for p in ft)
        # Top-level files_modified (legacy shape preserved for compat)
        fm = e.get("files_modified")
        if isinstance(fm, list):
            audited.update(str(p) for p in fm)
        # Nested last_mutation.files_touched (apply_codex_patch shape)
        nested = e.get("last_mutation")
        if isinstance(nested, dict):
            nft = nested.get("files_touched")
            if isinstance(nft, list):
                audited.update(str(p) for p in nft)

    working = set(_detect_working_tree_changes(repo_root))
    return sorted(working - audited)


def _evaluate_closure_invariant(audit_log: list, iteration_dir: Path) -> dict | None:
    """Run check_closure_invariant against the iteration state.

    Returns None when no enforcement should occur (no last_mutation, or no
    closing-verdict review came after last_mutation timestamp). Returns the
    invariant result dict otherwise.
    """
    try:
        from consensus_mcp._closure_invariant import (
            check_closure_invariant,
            last_mutation_from_audit,
        )
    except Exception:
        return None

    last_mutation = last_mutation_from_audit(audit_log)
    if last_mutation is None:
        return None

    lm_ts = last_mutation.get("timestamp") or last_mutation.get("timestamp_utc") or ""
    candidates = []
    for review_name in ("claude-review.yaml", "codex-review.yaml"):
        review_path = iteration_dir / review_name
        if review_path.exists():
            try:
                review = yaml.safe_load(review_path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(review, dict):
                continue
            closer_ts = review.get("created_at_utc")
            if closer_ts and closer_ts > lm_ts:
                candidates.append((closer_ts, review))
    if not candidates:
        # No fresh closing verdict yet; nothing to gate on. Refuse to author
        # iteration_closed when the audit shows mutation but no fresh review
        # has been authored - this prevents a silent close on stale or
        # missing review data.
        return {
            "ok": False,
            "checks": {
                "cross_family": False,
                "hash_match": False,
                "freshness": False,
            },
            "reason": (
                "no closing-verdict review (claude-review.yaml or codex-review.yaml) "
                "found with created_at_utc > last_mutation.timestamp"
            ),
            "_last_mutation": last_mutation,
        }
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, closer_verdict = candidates[0]
    result = check_closure_invariant(last_mutation, closer_verdict)
    result["_last_mutation"] = last_mutation
    result["_closer_verdict"] = closer_verdict
    return result


def _author_closure_certificate(
    iteration_dir: Path,
    iteration_id: str,
    invariant_result: dict,
    timestamp: str,
) -> None:
    """Write iteration_dir/closure-certificate.yaml summarizing close validity.

    Operator reviews ONE artifact for close validity, not the full audit log.
    """
    last_mutation = invariant_result.get("_last_mutation") or {}
    closer_verdict = invariant_result.get("_closer_verdict") or {}

    def _yes_no(b: bool) -> str:
        return "PASS" if b else "FAIL"

    checks = invariant_result.get("checks", {}) or {}
    cert = {
        "schema_version": 1,
        "iteration_id": iteration_id,
        "last_mutation": last_mutation,
        "closing_verdict": {
            "actor": closer_verdict.get("actor"),
            "review_target_hash": closer_verdict.get("review_target_hash"),
            "created_at_utc": closer_verdict.get("created_at_utc"),
            "review_scope_hash": closer_verdict.get("review_scope_hash"),
        },
        "invariant_checks": {
            "cross_family": _yes_no(bool(checks.get("cross_family"))),
            "hash_match": _yes_no(bool(checks.get("hash_match"))),
            "freshness": _yes_no(bool(checks.get("freshness"))),
        },
        "overall": "PASS" if invariant_result.get("ok") else "FAIL",
        "generated_utc": timestamp,
    }
    cert_path = iteration_dir / "closure-certificate.yaml"
    cert_path.write_text(yaml.safe_dump(cert, sort_keys=False), encoding="utf-8")


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
