"""loop.run_goal MCP tool - Phase 4 supervisor for the bounded quorum self-drive loop.

This is the SUPERVISOR pattern, not an autonomous Anthropic-API executor.
It is a state-machine coordinator: detects current iteration state from
filesystem inspection, advances as far as it can without driving Claude,
optionally dispatches the codex reviewer when ready, and returns the
next-action hint for the external orchestrator.

States detected (precedence order, top wins):

  1. goal_packet_invalid          -- _self_drive.cmd_validate fails
  2. closed                        -- iteration-outcome.yaml has closing_state
  3. blocked_reviewer_disagreement -- claude/codex goal_satisfied differ
  4. blocked_stop_rule_fired       -- any of the 9 stop rules fires
  5. needs_implementation          -- no review-packet.yaml
  6. needs_claude_review           -- review-packet.yaml exists, no claude-review
  7. ready_for_codex               -- claude-review exists, no codex-review
                                       (auto-dispatches if auto_dispatch_codex=True)

  Task #25 (iter-0015) patch-verification states (between codex-review and
  consensus when codex emits patch_proposals):

  8a. codex_patch_proposed
                              -- codex-review has finding(s) with
                                 patch_proposal AND no matching verification
                                 yaml in codex-patch-verifications/.
  8b. patch_corrected_by_claude_ready_for_codex_resubmit
                              -- verification yaml verdict=corrected_resubmit;
                                 corrections must be applied + codex must
                                 re-review. **Forbidden transition**:
                                 corrected_resubmit -> ready_to_close. Must
                                 route through codex_re_reviewing_after_claude_correction
                                 first per codex 2026-05-10 v4 directive.
  8c. patch_verified_ready_for_codex_resubmit
                              -- verification yaml verdict=approved;
                                 the patch may be applied via apply.codex_patch
                                 (Task #26 / iter-0016) under operator
                                 authorization (env CONSENSUS_MCP_CODEX_PATCH_APPLY=1
                                 + goal_packet.authorization.codex_patch_apply_authorized=true);
                                 close still requires codex post-correction re-review.

  9. needs_consensus               -- both reviews exist, no consensus.yaml
  10. ready_to_close                -- consensus exists, no outcome, gates green,
                                       both reviewers goal_satisfied=True

Anti-scope (per Task #13 brief):
  - Does NOT auto-dispatch the claude reviewer.
  - Does NOT synthesize consensus.yaml mechanically.
  - Does NOT author iteration-outcome.yaml mechanically.
  - Does NOT call Anthropic API; this is a SUPERVISOR, not autonomous executor.

Helper reuse: cmd_validate, cmd_check_stop_rules, _scope_signature, etc. are
imported from _self_drive. reviewer_dispatch_codex.handle is called directly
when auto_dispatch_codex=True.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

from consensus_mcp import _self_drive
from consensus_mcp._self_drive import _read_yaml_or_empty
from consensus_mcp.tools import reviewer_dispatch_codex


SCHEMA = {
    "name": "loop.run_goal",
    "description": (
        "Supervisor for the bounded quorum self-drive loop. Detects current "
        "iteration state from filesystem; advances as far as possible "
        "non-autonomously; dispatches codex review when ready; returns "
        "next-action for the external orchestrator."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal_packet_path": {
                "type": "string",
                "description": "Repo-relative or absolute path to goal_packet.yaml.",
            },
            "iteration_dir": {
                "type": "string",
                "description": "Repo-relative or absolute path to the iteration directory.",
            },
            "auto_dispatch_codex": {
                "type": ["boolean", "null"],
                "description": (
                    "If True (default), when state is 'ready_for_codex' the "
                    "tool calls reviewer.dispatch_codex internally and seals "
                    "the review. If False, returns ready_for_codex without "
                    "dispatching."
                ),
            },
        },
        "required": ["goal_packet_path", "iteration_dir"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "state": {"type": "string"},
            "next_action": {"type": "string"},
            "actions_taken": {"type": "array"},
            "stop_rules_fired": {"type": "array"},
            "error": {"type": ["string", "null"]},
        },
        "required": ["ok", "state", "next_action"],
        # iter-0030 F1 (claude-rev-001): mirror input_schema's
        # additionalProperties:False so stray keys leaking out of handle()
        # (e.g., from a future refactor) are caught at the MCP contract
        # layer instead of silently passed to callers.
        "additionalProperties": False,
    },
}


# Map state -> human-readable next_action hint.
_NEXT_ACTION = {
    "goal_packet_invalid": "blocked: fix goal_packet",
    "closed": "iteration already closed",
    "blocked_reviewer_disagreement": "address reviewer disagreement (claude vs codex)",
    "blocked_stop_rule_fired": "address stop rule",
    "blocked_closure_invariant_failed": (
        "address closure invariant failure (see closure-certificate.yaml or stop_rules_fired)"
    ),
    "needs_implementation": "implement, then author review-packet.yaml",
    "needs_claude_review": "dispatch claude reviewer; seal claude-review.yaml",
    "ready_for_codex": "dispatch codex reviewer (or call this tool with auto_dispatch_codex=True)",
    "codex_patch_proposed": (
        "dispatch claude patch-verifier subagent; call loop.verify_codex_patch (build_inputs)"
    ),
    "patch_verified_ready_for_codex_resubmit": (
        "claude approved the patch; dispatch codex for post-correction re-review"
    ),
    "patch_corrected_by_claude_ready_for_codex_resubmit": (
        "claude returned corrected_resubmit; apply corrections + dispatch codex re-review "
        "(forbidden: corrected_resubmit -> ready_to_close)"
    ),
    "needs_consensus": "synthesize consensus.yaml",
    "ready_to_close": "author iteration-outcome.yaml",
}


def _validate_goal_packet(goal_packet_path: str) -> tuple[bool, str | None]:
    """Run _self_drive.cmd_validate in-process; capture its JSON stdout.

    Returns (is_valid, error_message). is_valid means the packet is structurally
    sound and authorized - a scope_signature mismatch (cmd_validate exit 2) is
    treated as INVALID for supervisor purposes (the operator must re-seal).
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _self_drive.cmd_validate(argparse.Namespace(goal_packet=goal_packet_path))
    except Exception as exc:
        # cmd_validate raises FileNotFoundError when the goal_packet doesn't
        # exist (and may surface other read/parse errors). Convert to the
        # supervisor's tuple contract so handle() never lets an exception
        # escape and break the MCP {ok, state, next_action} return shape.
        return False, f"goal_packet_read_failed: {exc}"
    output = buf.getvalue().strip()
    if rc == 0:
        return True, None
    try:
        parsed = json.loads(output)
        return False, parsed.get("error") or output
    except Exception:
        return False, output or f"cmd_validate exit {rc}"


def _check_stop_rules(goal_packet_path: str, iteration_dir: str) -> list[dict]:
    """Run _self_drive.cmd_check_stop_rules in-process; return stop_rules_fired list.

    iter-0030 F3 (claude-rev-002): wrap the call in try/except mirroring
    _validate_goal_packet's defensive depth. Audit-log YAML corruption
    (yaml.YAMLError), unexpected helper errors, or any other exception MUST
    NOT escape handle() and break the MCP {ok, state, next_action} return
    contract. On exception we surface a synthetic 'check_stop_rules_failed'
    BREADCRUMB entry (joins the breadcrumb_rules filter at the call site so
    it does not trip blocked_stop_rule_fired by itself - orchestrator can
    inspect the list).
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _self_drive.cmd_check_stop_rules(
                argparse.Namespace(
                    goal_packet=goal_packet_path,
                    iteration_dir=iteration_dir,
                )
            )
    except Exception as exc:
        return [{"rule": "check_stop_rules_failed", "detail": str(exc)}]
    output = buf.getvalue().strip()
    try:
        parsed = json.loads(output)
        return parsed.get("stop_rules_fired", []) or []
    except Exception as exc:
        # Parse-failure sibling of the exception handler above: the helper ran
        # but emitted output we cannot decode. Silently returning [] would hide
        # a real stop rule that fired, so surface the same check_stop_rules_failed
        # breadcrumb (a breadcrumb_rule, so it does not trip blocked_stop_rule_fired).
        return [{
            "rule": "check_stop_rules_failed",
            "detail": f"could not parse cmd_check_stop_rules output as JSON: {exc}; raw={output!r}",
        }]


def _detect_state_from_files(iter_dir: Path, stop_rules_fired: list[dict]) -> str:
    """Detect state by file presence + stop-rule cross-check.

    Precedence is enforced by the order of checks below. The disagreement
    rule is special-cased to a more-specific state name; other firing rules
    map to the generic blocked_stop_rule_fired (with the rule list returned
    in the output).

    'review_yaml_parse_failed' breadcrumbs from _self_drive are NOT real
    stop rules - they're parse-failure breadcrumbs. They're surfaced in
    stop_rules_fired but do not trigger blocked_stop_rule_fired on their
    own; the orchestrator can inspect the list.
    """
    outcome_path = iter_dir / "iteration-outcome.yaml"
    if outcome_path.exists():
        outcome = _read_yaml_or_empty(outcome_path)
        if outcome.get("closing_state"):
            # iter-0030 F4 (claude-rev-004): closed-wins precedence is
            # preserved (operator may legitimately hand-author a close), but
            # if a corrected_resubmit verification is still pending we mutate
            # the caller-supplied stop_rules_fired list to record the
            # irregularity. Operator sees it in the audit signal without
            # being blocked from closing.
            codex_review = iter_dir / "codex-review.yaml"
            if codex_review.exists() and _has_pending_corrected_resubmit(iter_dir, codex_review):
                stop_rules_fired.append({
                    "rule": "iteration_closed_despite_corrected_resubmit_pending",
                    "detail": (
                        "iteration-outcome.yaml has closing_state set while at least one "
                        "codex-patch-verifications/*.yaml has verdict=corrected_resubmit; "
                        "operator close honored but the unresolved patch state is logged."
                    ),
                })
            return "closed"

    rule_names = {r.get("rule") for r in stop_rules_fired if isinstance(r, dict)}
    if "claude_codex_goal_satisfaction_disagreement" in rule_names:
        return "blocked_reviewer_disagreement"
    # iter-0030 F2 (codex-rev-001 + claude-rev-003): closure-invariant
    # failures are surfaced by _self_drive.cmd_check_stop_rules as the
    # 'closure_cross_verification_failed' rule. Map to the more-specific
    # blocked_closure_invariant_failed state (mirrors the disagreement
    # special-case above) so callers receive an actionable hint instead of
    # the generic blocked_stop_rule_fired.
    if "closure_cross_verification_failed" in rule_names:
        return "blocked_closure_invariant_failed"

    # Filter parse-failure breadcrumbs out of the "real stop rules" set.
    # iter-0030 F3: check_stop_rules_failed is the supervisor's own
    # exception-containment breadcrumb (see _check_stop_rules try/except);
    # it must not trip blocked_stop_rule_fired by itself.
    breadcrumb_rules = {
        "review_yaml_parse_failed",
        "git_check_failed",
        "check_stop_rules_failed",
    }
    real_rules = rule_names - breadcrumb_rules
    if real_rules:
        return "blocked_stop_rule_fired"

    review_packet = iter_dir / "review-packet.yaml"
    claude_review = iter_dir / "claude-review.yaml"
    codex_review = iter_dir / "codex-review.yaml"
    consensus = iter_dir / "consensus.yaml"

    if not review_packet.exists():
        return "needs_implementation"
    if not claude_review.exists():
        return "needs_claude_review"
    if not codex_review.exists():
        return "ready_for_codex"

    # Task #25 (iter-0015): patch-verification states. If codex-review.yaml
    # has any finding with a patch_proposal, the supervisor must route
    # through claude verification before close.
    patch_state = _detect_patch_verification_state(iter_dir, codex_review)
    if patch_state is not None:
        return patch_state

    if not consensus.exists():
        return "needs_consensus"
    # Consensus exists, but ready_to_close requires both reviewers to have
    # explicitly emitted goal_satisfied=True. A null/missing verdict from
    # either reviewer means the consensus exists but isn't yet "valid for
    # close" - orchestrator must add the missing verdict (or fix the
    # disagreement) before closing.
    claude_data = _read_yaml_or_empty(claude_review)
    codex_data = _read_yaml_or_empty(codex_review)
    if (_self_drive._extract_goal_satisfied(claude_data) is True
            and _self_drive._extract_goal_satisfied(codex_data) is True):
        # Task #28: closure-invariant guard. Before returning ready_to_close,
        # verify the closer (most-recent post-mutation review) passes the
        # cross-actor + hash-match + freshness invariant.
        if _closure_invariant_blocks_close(iter_dir):
            return "blocked_closure_invariant_failed"
        return "ready_to_close"
    return "needs_consensus"


def _closure_invariant_blocks_close(iter_dir: Path) -> bool:
    """Return True iff the closure invariant fails and a close attempt is pending.

    Read last_mutation from independence-audit.yaml; if no apply_step_landed
    events, no gating. Otherwise pick the most recent review (claude/codex)
    whose created_at_utc came AFTER last_mutation.timestamp and run
    check_closure_invariant. Return True on failure.
    """
    try:
        from consensus_mcp._closure_invariant import (
            check_closure_invariant,
            last_mutation_from_audit,
        )
    except Exception:
        return False
    audit_path = iter_dir / "independence-audit.yaml"
    if not audit_path.exists():
        return False
    audit = _read_yaml_or_empty(audit_path)
    log = audit.get("audit_log", []) or []
    last_mutation = last_mutation_from_audit(log)
    if last_mutation is None:
        return False
    lm_ts = last_mutation.get("timestamp") or last_mutation.get("timestamp_utc") or ""
    candidates = []
    for review_path in (iter_dir / "claude-review.yaml", iter_dir / "codex-review.yaml"):
        if review_path.exists():
            review = _read_yaml_or_empty(review_path)
            closer_ts = review.get("created_at_utc")
            if closer_ts and closer_ts > lm_ts:
                candidates.append((closer_ts, review))
    if not candidates:
        # Asymmetric with T6 by design (per Task #28 v4 spec): the supervisor
        # transition guard does NOT block here - it returns "not blocked" so
        # the supervisor can advance through normal states (needs_consensus,
        # needs_claude_review, etc.) when no fresh post-mutation review exists
        # yet. The fail-closed gate is at T6 (audit_append_event for
        # iteration_closed events). Real loop.run_goal-driven flow can't reach
        # ready_to_close without both reviews present, so this asymmetry only
        # matters for direct audit.append_event bypass paths - which T6 catches.
        return False
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, closer_verdict = candidates[0]
    inv = check_closure_invariant(last_mutation, closer_verdict)
    return not inv["ok"]


def _has_pending_corrected_resubmit(iter_dir: Path, codex_review: Path) -> bool:
    """iter-0030 F4 helper. Return True iff codex-review.yaml has any
    patch_proposal whose codex-patch-verifications/<patch_id>.yaml has
    verdict=='corrected_resubmit'. Used to attach a breadcrumb when the
    iteration is being closed despite an unresolved patch verification.
    """
    return _detect_patch_verification_state(iter_dir, codex_review) == (
        "patch_corrected_by_claude_ready_for_codex_resubmit"
    )


def _detect_patch_verification_state(iter_dir: Path, codex_review: Path) -> str | None:
    """Task #25 (iter-0015): detect patch-verification sub-states.

    Returns one of:
      - "codex_patch_proposed"
      - "patch_corrected_by_claude_ready_for_codex_resubmit"
      - "patch_verified_ready_for_codex_resubmit"
      - None (no patch_proposal in codex-review; supervisor proceeds normally)

    The forbidden transition `corrected_resubmit -> ready_to_close` is
    enforced here by returning the patch_corrected state regardless of
    consensus/outcome presence - this state takes precedence over
    ready_to_close (called BEFORE the consensus/ready_to_close branch
    in _detect_state_from_files).
    """
    review = _read_yaml_or_empty(codex_review)
    findings = review.get("findings") or []
    patch_findings = [
        f for f in findings
        if isinstance(f, dict) and isinstance(f.get("patch_proposal"), dict)
    ]
    if not patch_findings:
        return None

    verifications_dir = iter_dir / "codex-patch-verifications"

    # Collect (patch_id, verdict) for each patch-bearing finding.
    statuses: list[tuple[str, str | None]] = []
    for f in patch_findings:
        pp = f.get("patch_proposal")
        patch_id = pp.get("patch_id") if isinstance(pp, dict) else None
        if not patch_id:
            statuses.append((str(patch_id), None))
            continue
        v_path = verifications_dir / f"{patch_id}.yaml"
        if not v_path.exists():
            statuses.append((patch_id, None))
            continue
        v_data = _read_yaml_or_empty(v_path)
        statuses.append((patch_id, v_data.get("verdict")))

    # Any unverified -> codex_patch_proposed (orchestrator must run verifier).
    if any(verdict is None for _pid, verdict in statuses):
        return "codex_patch_proposed"

    # Any corrected_resubmit -> forbidden close path; require codex re-review.
    if any(verdict == "corrected_resubmit" for _pid, verdict in statuses):
        return "patch_corrected_by_claude_ready_for_codex_resubmit"

    # All approved -> still requires codex post-correction re-review per spec.
    if all(verdict == "approved" for _pid, verdict in statuses):
        return "patch_verified_ready_for_codex_resubmit"

    # Unknown verdict mixture -> remain in codex_patch_proposed (be conservative).
    return "codex_patch_proposed"


def handle(
    goal_packet_path: str,
    iteration_dir: str,
    auto_dispatch_codex: bool | None = None,
) -> dict:
    """Supervisor entry point.

    Returns a dict matching SCHEMA["output_schema"]. ok=False iff goal_packet
    is invalid or an unrecoverable error occurred. ok=True for all other
    states (including blocked_*) -- the state itself signals what to do
    next.
    """
    actions_taken: list[dict] = []

    # Step 1: validate goal_packet structure + authorization + signature.
    is_valid, err = _validate_goal_packet(goal_packet_path)
    if not is_valid:
        return {
            "ok": False,
            "state": "goal_packet_invalid",
            "next_action": _NEXT_ACTION["goal_packet_invalid"],
            "actions_taken": actions_taken,
            "stop_rules_fired": [],
            "error": err,
        }

    iter_dir = Path(iteration_dir)

    # Step 2: stop-rule check.
    stop_rules_fired = _check_stop_rules(goal_packet_path, iteration_dir)

    # Step 3: detect state.
    state = _detect_state_from_files(iter_dir, stop_rules_fired)

    # Step 4: optional auto-dispatch of codex when ready.
    if state == "ready_for_codex":
        # Default auto_dispatch_codex=True per task brief.
        do_dispatch = True if auto_dispatch_codex is None else bool(auto_dispatch_codex)
        if do_dispatch:
            try:
                # Per codex-rev-001 (iter-0010): pass review_target_path so codex
                # gets explicit review-target-binding instead of fallback-scope review.
                # By the time auto-dispatch fires the state cascade guarantees
                # review-packet.yaml exists in iter_dir.
                dispatch_result = reviewer_dispatch_codex.handle(
                    goal_packet_path=goal_packet_path,
                    iteration_dir=iteration_dir,
                    review_target_path=str(iter_dir / "review-packet.yaml"),
                )
            except Exception as exc:
                # The wrapper hardening (Task #15) only contains JSONDecodeError;
                # other exceptions (yaml.YAMLError pre-check, ImportError,
                # ValueError, etc.) must NOT escape and break the MCP contract.
                dispatch_result = {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            actions_taken.append({
                "action": "reviewer.dispatch_codex",
                "result": dispatch_result,
            })
            # Re-detect state after the dispatch attempt. If the seal succeeded,
            # codex-review.yaml now exists and the state advances. If it failed,
            # state stays ready_for_codex (the file wasn't written).
            stop_rules_fired = _check_stop_rules(goal_packet_path, iteration_dir)
            state = _detect_state_from_files(iter_dir, stop_rules_fired)

    return {
        "ok": True,
        "state": state,
        "next_action": _NEXT_ACTION.get(state, "unknown state - orchestrator decision"),
        "actions_taken": actions_taken,
        "stop_rules_fired": stop_rules_fired,
        "error": None,
    }


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
