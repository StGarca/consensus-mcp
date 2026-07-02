"""gate.evaluate_production_with_scope_match MCP tool. Phase 2 G4 (T11).

Replaces the Phase 0 lenient-enum-membership-only scope match in
consensus_mcp/validators/consensus_gate.py with strict prefix-OR-exact target
match per consensus.production_scope.scope_match_mode.

Closes claude-rev-050 (production-scope tightening) per spec section 17 +
section 13 consensus.production_scope schema (added v1.9.2 as Phase 2 G4 prereq).

Design spec ref: docs/architecture/phase-1-completion.md
lines 322-341 (gate.evaluate_production_with_scope_match block).

Read-only contract
------------------
This tool reads consensus.yaml + verification.yaml + approval.yaml and
returns a structured production-state evaluation. It writes no files,
emits no audit events, and mutates no state. Mirrors the read-only pattern
of state.read_decision_ledger (T2) and review.read_post_seal (T7).

The audit trail is the responsibility of the consume-side caller (e.g., the
orchestrator that ACTS on the result records its own apply-step audit
event).

scope_match_mode semantics
--------------------------
consensus.production_scope.scope_match_mode is operator-configurable per
iteration (set in consensus.yaml at synthesis time):

  exact   : approval.production_scope.target == consensus.production_scope.target
            (byte-equality)
  prefix  : segment-bounded narrowing. Both targets are normalized (posix
            separators: backslashes become '/', trailing '/' stripped), then
            match iff consensus.target == approval.target OR
            consensus.target.startswith(approval.target + '/'). The operator
            approves a parent scope and consensus may only narrow to a
            path-segment CHILD of it. Example:
              approval.target  = "ssb-ch1"
              consensus.target = "ssb-ch1/final"  -> match (segment child)
              consensus.target = "ssb-ch1-final"  -> NO match (a sibling whose
                                 name merely shares the string prefix)

If consensus does not specify scope_match_mode, default is "exact" (the
strict default; loosening to prefix is opt-in only).

Fail-closed refusals -- M1 (consult iteration-m1-hardening-design-4d7d2469)
---------------------------------------------------------------------------
- Empty or whitespace-only production_scope.target on EITHER side (consensus
  or approval) is a structured refusal {"error": "scope_target_empty"} under
  ALL match modes (exact AND prefix; kimi-rev-003 widening). A malformed
  approval must never widen scope: str.startswith("") is always True, so the
  pre-M1 raw-prefix code let an empty approval target approve every scope.
- production_scope.type outside VALID_SCOPE_TYPES on EITHER side is a
  structured refusal {"error": "invalid_scope_type", "detail": <value>}.
  The enum is the single authority; extending it is a one-line, reviewed
  change.

State-machine evaluation order (per spec section 17)
----------------------------------------------------
1. Parse all three YAML files; refuse on missing/invalid.
2. Extract consensus.production_scope (refuse if absent: spec section 13 v1.9.2 added it).
3. Extract approval.production_scope (refuse if absent).
4. Enforce VALID_SCOPE_TYPES membership on both sides (refuse
   invalid_scope_type), then compare types: refuse if mismatch (different
   scope kinds cannot match).
5. Compute scope-match per scope_match_mode (refuse on invalid mode; refuse
   scope_target_empty on empty/whitespace targets under ANY mode).
6. Compute technical-readiness (production_ready_if conditions).
7. Compute three-way hash binds (target / consensus / verification).
8. Synthesize production_state:
     not_ready                         : technical readiness fails
     ready_pending_operator_approval   : technical OK; approval bind/scope mismatch
     approved                          : technical OK AND all three binds match AND scope-match strict OK
9. Populate gate_findings for any failure path.

Documented limitations
----------------------
- The tool reads `consensus.production_clearances.{codex,claude} == "approved"`,
  `consensus.unresolved_disagreements == []`, `consensus.implementation_scope`
  presence, and `verification.passed && verification.scope_check.passed` to
  derive technical-readiness, mirroring consensus_mcp/validators/consensus_gate.py.
  The section 17 production_ready_if list ("codex_production_clearance,
  claude_production_clearance, verification_passed, production_scope_verified,
  unresolved_consensus_disagreements_empty") is reified the same way.
- observational_mode bypass is NOT implemented in this tool. If
  observational_mode is true and the operator wants the gate to short-circuit,
  that's a Phase 3 enhancement (caller can detect observational_mode and skip
  this tool).
- The tool requires current_target_sha256 from the caller (the artifact-or-
  commit hash at gate-evaluate time). If absent the result will reflect a
  TARGET_SHA_DRIFT-class finding when the approval has a different value.

MISSING-REQUIRED-FIELD CONTRACT (Round 6 F9 v1.9.2 disclosure): missing
required positional arguments raise Python TypeError, NOT a structured
{"error": "missing_*_field"} return. The output_schema's
missing_consensus_field / missing_approval_field error codes apply when the
YAML files load successfully but lack the documented structural fields
(production_scope.target, etc.); they do NOT apply to entirely-missing
function arguments. Same contract applies to T8/T9/T10.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from consensus_mcp._paths import project_root

# iter-0035 (Phase B step 7 per iter-0024 plan): migrated to lazy
# `_paths.project_root()` resolution.


def __getattr__(name: str):
    """PEP 562 backward compat for `REPO_ROOT` callers."""
    if name == "REPO_ROOT":
        return project_root()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

VALID_SCOPE_TYPES = {"render", "merge", "deploy", "data-mutation"}
VALID_SCOPE_MATCH_MODES = {"exact", "prefix"}


def _normalize_scope_target(target: str) -> str:
    """Normalize a scope target for segment-bounded prefix matching.

    M1 (consult iteration-m1-hardening-design-4d7d2469): posix separators
    (backslashes become '/') and trailing '/' stripped, so 'app\\mod\\' and
    'app/mod' compare equal and the prefix check can append exactly one '/'.
    """
    return target.replace("\\", "/").rstrip("/")


def _has_traversal_segment(target: str) -> bool:
    """True iff the target's normalized posix form contains a '.' or '..' path
    segment.

    M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q3: a
    '..'/'.' segment lets a consensus target escape (or obfuscate) the approved
    scope. Under prefix mode 'a/../evil' normalizes to 'a/../evil' which
    startswith 'a/' -- so the segment-bounded prefix check would MATCH an escape
    past the approved boundary 'a'; 'a/./b' could likewise disguise the compared
    path. Backslashes are normalized first so 'a\\..\\evil' is caught too.
    """
    posix = target.replace("\\", "/")
    return any(seg in ("..", ".") for seg in posix.split("/"))


def _canonical_yaml_sha256_text(text: str) -> str:
    """Canonical sha256 of YAML text (re-dump with sort_keys=True before hashing)."""
    loaded = yaml.safe_load(text)
    canonical = yaml.safe_dump(loaded, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_yaml(path: Path, missing_error_code: str) -> tuple[dict | None, dict | None, str | None]:
    """Return (data, error_dict, raw_text). On success error_dict is None."""
    if not path.exists():
        return None, {"error": missing_error_code, "detail": str(path)}, None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, {"error": "invalid_yaml", "detail": f"{path}: {exc}"}, None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, {"error": "invalid_yaml", "detail": f"{path}: {exc}"}, None
    if not isinstance(data, dict):
        return None, {
            "error": "invalid_yaml",
            "detail": f"{path}: root is not a mapping (got {type(data).__name__})",
        }, None
    return data, None, text


SCHEMA = {
    "name": "gate.evaluate_production_with_scope_match",
    "description": (
        "Read-only evaluation of the production_state machine with strict "
        "scope-match (exact|prefix). Replaces consensus_gate.py lenient enum-"
        "membership scope check. Reads consensus.yaml + verification.yaml + "
        "approval.yaml; returns production_state, gate_findings list, and "
        "operator_production_scope_match_strict_check bool. No writes; no audit "
        "event (the caller acting on the result records its own audit trail)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "consensus_yaml_path": {
                "type": "string",
                "description": (
                    "Absolute or repo-relative path to consensus.yaml for the iteration."
                ),
            },
            "verification_yaml_path": {
                "type": "string",
                "description": (
                    "Absolute or repo-relative path to verification.yaml for the iteration."
                ),
            },
            "approval_yaml_path": {
                "type": "string",
                "description": (
                    "Absolute path to operator-protected approval.yaml (typically "
                    "outside the repo at <operator-approval-path>/...)."
                ),
            },
            "current_target_sha256": {
                "type": "string",
                "description": (
                    "SHA-256 of the artifact or commit being evaluated for production. "
                    "Compared to approval.approved_target_sha256."
                ),
            },
        },
        "required": [
            "consensus_yaml_path",
            "verification_yaml_path",
            "approval_yaml_path",
            "current_target_sha256",
        ],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "description": (
            "Success: {production_state, gate_findings, "
            "operator_production_scope_match_strict_check, scope_match_mode_used, "
            "consensus_target, approval_target, technical_readiness}. "
            "Failure: {error, detail} where error is one of "
            "consensus_yaml_not_found | verification_yaml_not_found | "
            "approval_yaml_not_found | invalid_yaml | missing_production_scope | "
            "missing_consensus_field | missing_approval_field | invalid_scope_type | "
            "scope_type_mismatch | invalid_scope_match_mode | scope_target_empty | "
            "scope_target_traversal."
        ),
        "oneOf": [
            {
                "title": "success",
                "type": "object",
                "properties": {
                    "production_state": {
                        "type": "string",
                        "enum": [
                            "not_ready",
                            "ready_pending_operator_approval",
                            "approved",
                        ],
                    },
                    "gate_findings": {"type": "array"},
                    "operator_production_scope_match_strict_check": {"type": "boolean"},
                    "scope_match_mode_used": {
                        "type": "string",
                        "enum": ["exact", "prefix"],
                    },
                    "consensus_target": {"type": "string"},
                    "approval_target": {"type": "string"},
                    "technical_readiness": {"type": "object"},
                },
                "required": [
                    "production_state",
                    "gate_findings",
                    "operator_production_scope_match_strict_check",
                    "scope_match_mode_used",
                    "consensus_target",
                    "approval_target",
                ],
            },
            {
                "title": "failure",
                "type": "object",
                "properties": {
                    "error": {
                        "type": "string",
                        "enum": [
                            "consensus_yaml_not_found",
                            "verification_yaml_not_found",
                            "approval_yaml_not_found",
                            "invalid_yaml",
                            "missing_production_scope",
                            "missing_consensus_field",
                            "missing_approval_field",
                            # M1 (consult iteration-m1-hardening-design-4d7d2469):
                            # fail-closed scope-match refusal codes.
                            "invalid_scope_type",
                            "scope_type_mismatch",
                            "invalid_scope_match_mode",
                            "scope_target_empty",
                            # M1-remediation (consult
                            # iteration-path-to-a-remediation-260caad1) Q3:
                            # '.'/'..' path-segment refusal.
                            "scope_target_traversal",
                        ],
                    },
                    "detail": {"type": ["string", "null"]},
                },
                "required": ["error"],
            },
        ],
    },
}


def _resolve_path(p: str) -> Path:
    """Treat absolute strings literally; treat relative as repo-relative."""
    raw = Path(p)
    if raw.is_absolute():
        return raw
    return project_root() / raw


def handle(
    consensus_yaml_path: str,
    verification_yaml_path: str,
    approval_yaml_path: str,
    current_target_sha256: str,
) -> dict:
    """Evaluate production_state with strict scope-match.

    See module docstring for read-only contract, scope_match_mode semantics,
    and state-machine evaluation order.
    """
    consensus_path = _resolve_path(consensus_yaml_path)
    verification_path = _resolve_path(verification_yaml_path)
    # Approval path: operator-protected store is OUTSIDE the repo, so we honor
    # absolute paths verbatim (no REPO_ROOT prefix when absolute).
    approval_path = _resolve_path(approval_yaml_path)

    consensus, err, consensus_text = _load_yaml(consensus_path, "consensus_yaml_not_found")
    if err is not None:
        return err
    verification, err, _ = _load_yaml(verification_path, "verification_yaml_not_found")
    if err is not None:
        return err
    approval, err, _ = _load_yaml(approval_path, "approval_yaml_not_found")
    if err is not None:
        return err

    # ---- Step 2: extract consensus.production_scope (spec section 13 v1.9.2) ----
    cons_scope = consensus.get("production_scope")
    if not isinstance(cons_scope, dict):
        return {
            "error": "missing_production_scope",
            "detail": (
                "consensus.production_scope absent or non-mapping. Required by "
                "spec section 13 (v1.9.2 Phase 2 G4 prereq); see "
                "docs/architecture/orchestration-spec.md "
                "section 13."
            ),
        }
    cons_target = cons_scope.get("target")
    cons_type = cons_scope.get("type")
    if not isinstance(cons_target, str) or not isinstance(cons_type, str):
        return {
            "error": "missing_consensus_field",
            "detail": (
                "consensus.production_scope.{type,target} must both be strings; "
                f"got type={cons_type!r} target={cons_target!r}"
            ),
        }
    cons_match_mode = cons_scope.get("scope_match_mode", "exact")

    # ---- Step 3: extract approval.production_scope ----
    appr_scope = approval.get("production_scope")
    if not isinstance(appr_scope, dict):
        return {
            "error": "missing_approval_field",
            "detail": "approval.production_scope absent or non-mapping (spec section 17).",
        }
    appr_target = appr_scope.get("target")
    appr_type = appr_scope.get("type")
    if not isinstance(appr_target, str) or not isinstance(appr_type, str):
        return {
            "error": "missing_approval_field",
            "detail": (
                "approval.production_scope.{type,target} must both be strings; "
                f"got type={appr_type!r} target={appr_target!r}"
            ),
        }

    # ---- Step 4: scope-type enum + type match ----
    # M1 (consult iteration-m1-hardening-design-4d7d2469): VALID_SCOPE_TYPES
    # membership is enforced on BOTH sides. The enum was previously declared
    # but never consulted (dead), so an unknown type evaluated normally
    # despite the module docstring claiming it replaced the lenient Phase 0
    # check; it now refuses with a structured invalid_scope_type error.
    for side, side_type in (("consensus", cons_type), ("approval", appr_type)):
        if side_type not in VALID_SCOPE_TYPES:
            return {
                "error": "invalid_scope_type",
                "detail": (
                    f"{side}.production_scope.type={side_type!r}; must be one "
                    f"of {sorted(VALID_SCOPE_TYPES)}"
                ),
            }
    if cons_type != appr_type:
        return {
            "error": "scope_type_mismatch",
            "detail": (
                f"consensus.production_scope.type={cons_type!r} != "
                f"approval.production_scope.type={appr_type!r}"
            ),
        }

    # ---- Step 5: scope-match per mode ----
    if cons_match_mode not in VALID_SCOPE_MATCH_MODES:
        return {
            "error": "invalid_scope_match_mode",
            "detail": (
                f"consensus.production_scope.scope_match_mode={cons_match_mode!r}; "
                f"must be one of {sorted(VALID_SCOPE_MATCH_MODES)}"
            ),
        }
    # M1 (consult iteration-m1-hardening-design-4d7d2469, kimi-rev-003
    # widening): an empty/whitespace target on EITHER side refuses under ALL
    # match modes (exact AND prefix). Fail closed: str.startswith("") is
    # always True, so the pre-M1 raw-prefix match let a degenerate approval
    # target approve every scope; the same invariant is applied to exact mode
    # for cross-mode consistency.
    if not cons_target.strip() or not appr_target.strip():
        return {
            "error": "scope_target_empty",
            "detail": (
                "production_scope.target must be non-empty on both sides "
                f"(all match modes): consensus target={cons_target!r}, "
                f"approval target={appr_target!r}"
            ),
        }
    # M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q3: a
    # '.'/'..' path segment on EITHER side is a fail-closed refusal under ALL
    # match modes. Without it, prefix normalize+startswith would let a consensus
    # target like 'a/../evil' (which startswith 'a/') escape the approved scope
    # 'a', and 'a/./b' could obfuscate the compared path. A traversal/dot target
    # can therefore never match -- and it refuses outright rather than silently
    # non-matching under exact mode.
    for _side, _side_target in (("consensus", cons_target), ("approval", appr_target)):
        if _has_traversal_segment(_side_target):
            return {
                "error": "scope_target_traversal",
                "detail": (
                    f"{_side}.production_scope.target={_side_target!r} contains a "
                    "'.' or '..' path segment; traversal/dot segments are refused "
                    "on both sides under all match modes"
                ),
            }
    if cons_match_mode == "exact":
        scope_match_strict = (cons_target == appr_target)
    else:  # prefix
        # M1 (consult iteration-m1-hardening-design-4d7d2469): segment-bounded
        # prefix. Normalize both targets (posix separators, strip trailing
        # '/'); match iff equal OR consensus is a path-segment CHILD of the
        # approval. The old raw startswith let approval 'app/mod' match
        # consensus 'app/module_evil' -- scope WIDENING past the approved
        # boundary.
        cons_norm = _normalize_scope_target(cons_target)
        appr_norm = _normalize_scope_target(appr_target)
        if not cons_norm or not appr_norm:
            # A separator-only target (e.g. '/') normalizes to '' and would
            # degenerate back to match-everything; same fail-closed refusal.
            return {
                "error": "scope_target_empty",
                "detail": (
                    "production_scope.target normalizes to empty (separator-"
                    f"only): consensus target={cons_target!r}, "
                    f"approval target={appr_target!r}"
                ),
            }
        scope_match_strict = (
            cons_norm == appr_norm or cons_norm.startswith(appr_norm + "/")
        )

    # ---- Step 6: technical-readiness (mirrors consensus_gate.py logic) ----
    findings: list = []

    clearances = consensus.get("production_clearances") or {}
    if not isinstance(clearances, dict):
        clearances = {}
    codex_clearance = clearances.get("codex") == "approved"
    claude_clearance = clearances.get("claude") == "approved"
    if not codex_clearance:
        findings.append({
            "id": "MISSING_CODEX_CLEARANCE",
            "severity": "high",
            "claim": "consensus.production_clearances.codex != 'approved'",
            "field": "consensus.production_clearances.codex",
        })
    if not claude_clearance:
        findings.append({
            "id": "MISSING_CLAUDE_CLEARANCE",
            "severity": "high",
            "claim": "consensus.production_clearances.claude != 'approved'",
            "field": "consensus.production_clearances.claude",
        })

    unresolved = consensus.get("unresolved_disagreements")
    unresolved_empty = isinstance(unresolved, list) and len(unresolved) == 0
    if not unresolved_empty:
        findings.append({
            "id": "UNRESOLVED_DISAGREEMENTS_PRESENT",
            "severity": "high",
            "claim": "consensus.unresolved_disagreements is not an empty list",
            "field": "consensus.unresolved_disagreements",
        })

    impl_scope = consensus.get("implementation_scope")
    impl_scope_verified = bool(impl_scope) and (
        (isinstance(impl_scope, dict) and len(impl_scope) > 0)
        or (isinstance(impl_scope, list) and len(impl_scope) > 0)
    )
    if not impl_scope_verified:
        findings.append({
            "id": "IMPLEMENTATION_SCOPE_MISSING",
            "severity": "high",
            "claim": "consensus.implementation_scope absent or empty",
            "field": "consensus.implementation_scope",
        })

    ver_passed = verification.get("passed") is True
    scope_check = verification.get("scope_check") or {}
    scope_check_passed = isinstance(scope_check, dict) and scope_check.get("passed") is True
    verification_ok = ver_passed and scope_check_passed
    if not verification_ok:
        findings.append({
            "id": "VERIFICATION_NOT_PASSED",
            "severity": "high",
            "claim": "verification.passed=true AND verification.scope_check.passed=true required",
            "field": "verification.passed",
        })

    technical_readiness = {
        "codex_production_clearance": codex_clearance,
        "claude_production_clearance": claude_clearance,
        "verification_passed": verification_ok,
        "production_scope_verified": impl_scope_verified,
        "unresolved_consensus_disagreements_empty": unresolved_empty,
    }
    production_ready = all(technical_readiness.values())

    if not production_ready:
        findings.append({
            "id": "PRODUCTION_NOT_READY_TECHNICAL",
            "severity": "high",
            "claim": (
                "production_ready_if conditions not all true; see technical_readiness."
            ),
            "field": "technical_readiness",
        })

    # ---- Step 7: three-way hash binds ----
    current_consensus_sha = _canonical_yaml_sha256_text(consensus_text)
    current_verification_sha = _canonical_yaml_sha256_text(
        verification_path.read_text(encoding="utf-8")
    )

    target_sha_match = (approval.get("approved_target_sha256") == current_target_sha256)
    consensus_sha_match = (approval.get("approved_consensus_sha256") == current_consensus_sha)
    verification_sha_match = (approval.get("approved_verification_sha256") == current_verification_sha)

    if not target_sha_match:
        findings.append({
            "id": "TARGET_SHA_DRIFT",
            "severity": "high",
            "claim": "approval.approved_target_sha256 != current_target_sha256",
            "field": "approval.approved_target_sha256",
        })
    if not consensus_sha_match:
        findings.append({
            "id": "CONSENSUS_SHA_DRIFT",
            "severity": "high",
            "claim": "approval.approved_consensus_sha256 != current consensus.yaml canonical sha256",
            "field": "approval.approved_consensus_sha256",
        })
    if not verification_sha_match:
        findings.append({
            "id": "VERIFICATION_SHA_DRIFT",
            "severity": "high",
            "claim": "approval.approved_verification_sha256 != current verification.yaml canonical sha256",
            "field": "approval.approved_verification_sha256",
        })
    all_three_binds_match = target_sha_match and consensus_sha_match and verification_sha_match

    # ---- Step 8: scope-match strict-check finding (when not matched) ----
    if not scope_match_strict:
        findings.append({
            "id": "OPERATOR_SCOPE_MISMATCH",
            "severity": "high",
            "claim": (
                f"scope_match_mode={cons_match_mode!r}: "
                f"consensus.production_scope.target={cons_target!r} does not match "
                f"approval.production_scope.target={appr_target!r}"
            ),
            "field": "consensus.production_scope.target",
        })

    # ---- Step 9: synthesize production_state ----
    if not production_ready:
        production_state = "not_ready"
    elif all_three_binds_match and scope_match_strict:
        production_state = "approved"
    else:
        production_state = "ready_pending_operator_approval"

    return {
        "production_state": production_state,
        "gate_findings": findings,
        "operator_production_scope_match_strict_check": scope_match_strict,
        "scope_match_mode_used": cons_match_mode,
        "consensus_target": cons_target,
        "approval_target": appr_target,
        "technical_readiness": technical_readiness,
    }


def register(registry) -> None:
    """Register this tool with the server's ToolRegistry."""
    registry.register(SCHEMA["name"], SCHEMA, handle)
