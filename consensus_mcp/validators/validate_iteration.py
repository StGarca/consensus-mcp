"""validate_iteration.py - Phase 0 iteration-directory validator (P0-V4).

Per spec sections 7, 9, 10, 11, 13, 16, 20, 22 of the multi-agent-consensus-mcp
spec at v1.7.2. Audits an entire iteration directory
(`consensus-state/active/<iteration>/`) for:

  - presence of all required artifacts (§20 step list)
  - YAML parseability of each artifact
  - iteration_id coherence across artifacts (matches dir name)
  - schema_version == 1 wherever the field appears
  - independence-audit hash chain (review_returned_and_sealed sha256 must equal
    the canonical sha256 of the referenced file)
  - independence_proof condition (§13 / cf-001) seal-before-invoke
  - §22 metrics presence on iteration-outcome.yaml
  - independent_finding_rate in [0.0, 1.0] or null with rationale
  - cross-artifact reference integrity (review.reviewed_packet_sha256 ==
    canonical sha256 of review-packet.yaml)
  - review-packet.yaml contains all 10 §7 required_packet_fields
  - reviewer artifacts contain no corroborated_by anywhere (§13 synthesizer-only)

Output: structured report (YAML by default, JSON via --json).

Usage:
  python consensus_mcp/validators/validate_iteration.py --iteration-dir PATH \\
      [--out PATH] [--json] [--self-test]

Exit codes:
  0 - validator ran cleanly; report written
  2 - validator could not run (iteration dir missing, parse error)

Findings count does NOT gate exit code (Path C; consistent with sibling
validators).

Canonical YAML sha256 convention (used everywhere a "yaml file sha256" is
computed in this validator and across the validator suite):

    hashlib.sha256(
        yaml.safe_dump(yaml.safe_load(open(p)), sort_keys=True)
        .encode("utf-8")
    ).hexdigest()

This is a content-stable hash insensitive to formatting/key-ordering and is
the same convention used by build_review_packet.py and validate_consensus.py.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / "consensus-state" / "state" / "validate-iteration-report.yaml"

REQUIRED_ARTIFACTS = [
    "input.yaml",
    "review-packet.yaml",
    "codex-review.yaml",
    "claude-review.yaml",
    "independence-audit.yaml",
    "consensus.yaml",
    "verification.yaml",
    "iteration-outcome.yaml",
]

# §7 required_packet_fields (10 fields).
REQUIRED_PACKET_FIELDS = [
    "objective",
    "mode",
    "gate_state",
    "decision_ledger_hash",
    "claude_md_hash",
    "karpathy_principle_summary",
    "changed_sections",
    "open_blockers",
    "check_results_if_any",
    "requested_output_schema",
]

# §22 metric blocks and their required sub-fields.
METRICS_BLOCKS = {
    "tokens_per_iteration": [
        "codex_input_estimate",
        "claude_input_estimate",
        "codex_output_estimate",
        "claude_output_estimate",
    ],
    "context_reuse": [
        "sections_sent",
        "unchanged_sections_omitted",
        "context_requests_served",
    ],
    "review_quality": [
        "blockers_found",
        "blockers_confirmed_by_other_agent",
        "blockers_rejected",
        "assumptions_challenged",
        "self_resolved_questions",
        "operator_escalated_questions",
        "independent_finding_rate",
    ],
    "outcome_quality": [
        "decision_reversal_rate",
        "operator_override_rate",
        "time_to_correct_error_iterations",
    ],
    "implementation_quality": [
        "accepted_changes",
        "implemented_changes",
        "out_of_scope_changes",
        "checks_passed",
    ],
}

REVIEW_FILES = {"codex-review.yaml", "claude-review.yaml"}


def _import_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        raise SystemExit("pyyaml required (pip install pyyaml)")


def _sha256_file_bytes(path: Path) -> str | None:
    """Raw byte sha256 of a file (used for provenance only)."""
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_yaml_sha256(path: Path) -> str | None:
    """Canonical YAML sha256 per validator-suite convention. Returns None if
    the file is missing or unparseable."""
    yaml = _import_yaml()
    if not path.exists() or not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    canon = yaml.safe_dump(data, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _dependency_version(dist_name: str) -> str | None:
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _build_provenance(iteration_dir: Path) -> dict:
    return {
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "command_line": sys.argv,
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "dependency_versions": {
            "PyYAML": _dependency_version("PyYAML"),
        },
        "inputs": {
            "iteration_dir": str(iteration_dir.relative_to(REPO_ROOT)) if iteration_dir.is_relative_to(REPO_ROOT) else str(iteration_dir),
            "validator_script_path": "consensus_mcp/validators/validate_iteration.py",
            "validator_script_sha256": _sha256_file_bytes(Path(__file__).resolve()),
        },
    }


def _contains_corroborated_by(obj, path: str = "") -> list[str]:
    """Recurse; return list of dotted paths where corroborated_by appears."""
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}" if path else k
            if k == "corroborated_by":
                hits.append(sub)
            hits.extend(_contains_corroborated_by(v, sub))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(_contains_corroborated_by(item, f"{path}[{i}]"))
    return hits


def validate_iteration(iteration_dir: Path) -> dict:
    """Validate an iteration directory; return structured report."""
    findings: list[dict] = []
    yaml = _import_yaml()

    if not iteration_dir.exists() or not iteration_dir.is_dir():
        raise SystemExit(f"iteration dir not found: {iteration_dir}")

    iteration_name = iteration_dir.name  # e.g. "iteration-0000"

    # ---- Rule 1: required artifacts exist ----
    artifact_paths: dict[str, Path] = {}
    for name in REQUIRED_ARTIFACTS:
        p = iteration_dir / name
        artifact_paths[name] = p
        if not p.exists():
            findings.append({
                "id": "MISSING_REQUIRED_ARTIFACT",
                "severity": "high",
                "artifact": name,
                "claim": f"required artifact {name!r} missing from iteration dir",
            })

    # ---- Rule 2: each present YAML parses cleanly ----
    parsed: dict[str, dict | None] = {}
    for name, p in artifact_paths.items():
        if not p.exists():
            parsed[name] = None
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                findings.append({
                    "id": "YAML_PARSE_ERROR",
                    "severity": "high",
                    "artifact": name,
                    "claim": f"{name} root is not a YAML mapping",
                })
                parsed[name] = None
            else:
                parsed[name] = data
        except yaml.YAMLError as e:
            findings.append({
                "id": "YAML_PARSE_ERROR",
                "severity": "high",
                "artifact": name,
                "claim": f"{name} yaml parse error: {e}",
            })
            parsed[name] = None

    # ---- Rule 3: iteration_id coherence ----
    for name, data in parsed.items():
        if data is None:
            continue
        if "iteration_id" in data:
            iid = data["iteration_id"]
            if iid != iteration_name:
                findings.append({
                    "id": "ITERATION_ID_MISMATCH",
                    "severity": "high",
                    "artifact": name,
                    "found": iid,
                    "expected": iteration_name,
                    "claim": f"{name}.iteration_id={iid!r} does not match dir name {iteration_name!r}",
                })

    # ---- Rule 4: schema_version present and equal to integer 1 ----
    for name, data in parsed.items():
        if data is None:
            continue
        if "schema_version" in data:
            sv = data["schema_version"]
            if sv != 1:
                findings.append({
                    "id": "INVALID_SCHEMA_VERSION",
                    "severity": "medium",
                    "artifact": name,
                    "found": sv,
                    "claim": f"{name}.schema_version={sv!r} (expected integer 1)",
                })

    # ---- Compute canonical sha256 of each present file (for hash-chain + xref) ----
    canonical_sha: dict[str, str | None] = {
        name: _canonical_yaml_sha256(p) if p.exists() else None
        for name, p in artifact_paths.items()
    }

    # ---- Rule 5: independence-audit hash chain ----
    audit = parsed.get("independence-audit.yaml")
    audit_log: list = []
    if isinstance(audit, dict):
        log = audit.get("audit_log")
        if isinstance(log, list):
            audit_log = log

    event_counts: dict[str, int] = {}
    for entry in audit_log:
        if isinstance(entry, dict):
            ev = entry.get("event")
            if isinstance(ev, str):
                event_counts[ev] = event_counts.get(ev, 0) + 1

    # required event types: review_packet_built (>=1), reviewer_invoked (>=2),
    # review_returned_and_sealed (>=2)
    required_events = {
        "review_packet_built": 1,
        "reviewer_invoked": 2,
        "review_returned_and_sealed": 2,
    }
    is_iteration_0000_for_grandfather = (iteration_name == "iteration-0000")
    for ev, min_count in required_events.items():
        actual = event_counts.get(ev, 0)
        if actual < min_count:
            # v1.7.7 (iteration-0003 Option B; canonical-iter0003-001):
            # iteration-0000 was authored before canonical-pin event names locked
            # (v1.7.4). Audit's per-agent prefixed events (codex_reviewer_invoked,
            # claude_reviewer_invocation_pending) are visible to validator as
            # AUDIT_EVENT_NAME_NON_CANONICAL low (already grandfathered v1.7.4),
            # but missing CANONICAL events trigger HASH_CHAIN_BROKEN high.
            # Subtype-gated grandfather: iteration-0000 specifically downgrades
            # HASH_CHAIN_BROKEN.subtype=missing_event_type from high to low.
            # Bounded to iteration-0000 by literal string equality.
            severity = "low" if is_iteration_0000_for_grandfather else "high"
            findings.append({
                "id": "HASH_CHAIN_BROKEN",
                "severity": severity,
                "subtype": "missing_event_type",
                "event": ev,
                "expected_min": min_count,
                "actual": actual,
                "grandfathered_iteration_0000": is_iteration_0000_for_grandfather,
                "claim": f"independence-audit.audit_log has {actual} {ev!r} event(s); expected >= {min_count}"
                         + (" (informational; iteration-0000 grandfathered as pre-canonical-pin per v1.7.7 / canonical-iter0003-001)" if is_iteration_0000_for_grandfather else ""),
            })

    # ---- Rule 5b (v1.7.4 / claude-rev-045 ratification): canonical event names ----
    # Per-agent prefixes (codex_reviewer_invoked, claude_review_returned_and_sealed,
    # etc.) are forbidden. Agent identity goes in actor field, not event name.
    # iteration-0000 grandfathered to informational severity (pre-canonical-pin period).
    canonical_events = set(required_events.keys()) | {"reviewer_invocation_pending"}
    is_iteration_0000 = (iteration_name == "iteration-0000")
    for i, entry in enumerate(audit_log):
        if not isinstance(entry, dict):
            continue
        ev = entry.get("event")
        if not isinstance(ev, str):
            continue
        if ev in canonical_events:
            continue
        # Check if it's a per-agent prefixed variant of a canonical event
        non_canonical_prefix_match = None
        for canonical in canonical_events:
            if ev.endswith("_" + canonical) or ev.startswith(canonical + "_"):
                non_canonical_prefix_match = canonical
                break
            for agent in ("codex", "claude"):
                if ev == f"{agent}_{canonical}" or ev == f"{canonical}_{agent}":
                    non_canonical_prefix_match = canonical
                    break
            if non_canonical_prefix_match:
                break
        if non_canonical_prefix_match is not None:
            findings.append({
                "id": "AUDIT_EVENT_NAME_NON_CANONICAL",
                "severity": "low" if is_iteration_0000 else "high",
                "subtype": "per_agent_prefix",
                "audit_log_index": i,
                "found_event": ev,
                "canonical_form": non_canonical_prefix_match,
                "grandfathered": is_iteration_0000,
                "claim": f"audit_log[{i}].event={ev!r} uses per-agent prefix; "
                         f"canonical name is {non_canonical_prefix_match!r} "
                         f"with agent identity in actor field"
                         + (" (informational; iteration-0000 grandfathered as pre-canonical-pin)" if is_iteration_0000 else ""),
            })

    # for each review_returned_and_sealed event, sha256 must match the
    # canonical sha256 of the referenced artifact
    for i, entry in enumerate(audit_log):
        if not isinstance(entry, dict):
            continue
        if entry.get("event") != "review_returned_and_sealed":
            continue
        artifact_ref = entry.get("artifact")
        recorded_sha = entry.get("sha256")
        if not isinstance(artifact_ref, str):
            findings.append({
                "id": "HASH_CHAIN_BROKEN",
                "severity": "high",
                "subtype": "missing_artifact_field",
                "audit_log_index": i,
                "claim": "review_returned_and_sealed event has no 'artifact' field",
            })
            continue
        # Resolve the referenced file: artifact_ref is a bare basename in
        # canonical schema. Tolerate slashes by stripping to basename.
        ref_basename = artifact_ref.split("/")[-1].split("\\")[-1]
        if ref_basename not in artifact_paths:
            findings.append({
                "id": "HASH_CHAIN_BROKEN",
                "severity": "high",
                "subtype": "unknown_artifact_ref",
                "audit_log_index": i,
                "artifact": artifact_ref,
                "claim": f"review_returned_and_sealed.artifact={artifact_ref!r} not in known iteration artifacts",
            })
            continue
        actual_sha = canonical_sha.get(ref_basename)
        if actual_sha is None:
            # File missing or unparseable. If a MISSING_REQUIRED_ARTIFACT was
            # already raised for it, this is downstream noise; still record so
            # the chain is fully audited.
            findings.append({
                "id": "HASH_CHAIN_BROKEN",
                "severity": "high",
                "subtype": "referenced_file_unreadable",
                "audit_log_index": i,
                "artifact": artifact_ref,
                "claim": f"review_returned_and_sealed references {artifact_ref!r} but file is missing/unparseable",
            })
            continue
        if recorded_sha != actual_sha:
            findings.append({
                "id": "HASH_CHAIN_BROKEN",
                "severity": "high",
                "subtype": "sha256_mismatch",
                "audit_log_index": i,
                "artifact": artifact_ref,
                "recorded": recorded_sha,
                "computed": actual_sha,
                "claim": f"review_returned_and_sealed.sha256 for {artifact_ref!r} ({recorded_sha!r}) "
                         f"!= canonical sha256 of file ({actual_sha!r})",
            })

    # ---- Rule 6: independence proof condition (seal-before-invoke flag) ----
    for i, entry in enumerate(audit_log):
        if not isinstance(entry, dict):
            continue
        if entry.get("event") != "reviewer_invoked":
            continue
        ia = entry.get("independence_attestation") or {}
        if not isinstance(ia, dict):
            continue
        other_existed = ia.get("other_review_existed_at_invocation")
        if other_existed is True:
            findings.append({
                "id": "INDEPENDENCE_AT_RISK",
                "severity": "medium",
                "audit_log_index": i,
                "reviewer": entry.get("reviewer"),
                "claim": "reviewer_invoked.independence_attestation.other_review_existed_at_invocation=true; "
                         "if any corroboration is reported it should be corroboration_strength=suggested_by_context",
                "note": "section 13 independence_proof_required; cf-001 weaponization defense",
            })

    # ---- Rule 7: §22 metrics presence on iteration-outcome.yaml ----
    outcome = parsed.get("iteration-outcome.yaml")
    if isinstance(outcome, dict):
        for block_name, sub_fields in METRICS_BLOCKS.items():
            blk = outcome.get(block_name)
            if not isinstance(blk, dict):
                findings.append({
                    "id": "MISSING_METRICS_BLOCK",
                    "severity": "high",
                    "block": block_name,
                    "claim": f"iteration-outcome.yaml missing required §22 block {block_name!r}",
                })
                continue
            for sf in sub_fields:
                if sf not in blk:
                    findings.append({
                        "id": "MISSING_METRICS_BLOCK",
                        "severity": "high",
                        "subtype": "missing_subfield",
                        "block": block_name,
                        "subfield": sf,
                        "claim": f"iteration-outcome.yaml.{block_name} missing required subfield {sf!r}",
                    })

    # ---- Rule 8: independent_finding_rate range ----
    if isinstance(outcome, dict):
        rq = outcome.get("review_quality")
        if isinstance(rq, dict) and "independent_finding_rate" in rq:
            ifr = rq["independent_finding_rate"]
            if ifr is None:
                rationale = rq.get("independent_finding_rate_rationale")
                if not isinstance(rationale, str) or not rationale.strip():
                    findings.append({
                        "id": "INVALID_INDEPENDENT_FINDING_RATE",
                        "severity": "medium",
                        "value": None,
                        "claim": "independent_finding_rate is null without independent_finding_rate_rationale",
                    })
            elif isinstance(ifr, bool) or not isinstance(ifr, (int, float)):
                findings.append({
                    "id": "INVALID_INDEPENDENT_FINDING_RATE",
                    "severity": "medium",
                    "value": ifr,
                    "claim": f"independent_finding_rate={ifr!r} is not a number or null",
                })
            elif ifr < 0.0 or ifr > 1.0:
                findings.append({
                    "id": "INVALID_INDEPENDENT_FINDING_RATE",
                    "severity": "medium",
                    "value": ifr,
                    "claim": f"independent_finding_rate={ifr} not in [0.0, 1.0]",
                })

    # ---- Rule 9: cross-artifact packet sha256 reference integrity ----
    # v1.7.4 (codex-q-001 ratification): pre_canonical_pin_marker downgrades
    # PACKET_SHA_MISMATCH to PACKET_SHA_HISTORICAL (informational/low) when
    # the review yaml carries hash_convention: pre-canonical-pin AND
    # do_not_recompute: true. Spec: section 7 pre_canonical_pin_marker block.
    actual_packet_sha = canonical_sha.get("review-packet.yaml")
    for review_name in REVIEW_FILES:
        rdata = parsed.get(review_name)
        if not isinstance(rdata, dict):
            continue
        recorded = rdata.get("reviewed_packet_sha256")
        if not isinstance(recorded, str):
            continue
        if actual_packet_sha is None:
            # packet missing/unparseable -> already reported
            continue
        if recorded != actual_packet_sha:
            # Check for pre-canonical-pin marker on the review yaml.
            # Marker requires BOTH fields: hash_convention=pre-canonical-pin
            # AND do_not_recompute=true. Either alone is not honored.
            hash_convention = rdata.get("hash_convention")
            do_not_recompute = rdata.get("do_not_recompute")
            historical_marker_present = (
                hash_convention == "pre-canonical-pin"
                and do_not_recompute is True
            )
            if historical_marker_present:
                findings.append({
                    "id": "PACKET_SHA_HISTORICAL",
                    "severity": "low",
                    "review": review_name,
                    "recorded": recorded,
                    "computed": actual_packet_sha,
                    "marker": "pre-canonical-pin",
                    "claim": f"{review_name}.reviewed_packet_sha256 differs from canonical "
                             f"sha256 but pre_canonical_pin_marker is present "
                             f"(hash_convention=pre-canonical-pin, do_not_recompute=true); "
                             f"informational only per spec section 7 pre_canonical_pin_marker rule",
                })
            else:
                findings.append({
                    "id": "PACKET_SHA_MISMATCH",
                    "severity": "high",
                    "review": review_name,
                    "recorded": recorded,
                    "computed": actual_packet_sha,
                    "claim": f"{review_name}.reviewed_packet_sha256={recorded!r} != canonical sha256 of "
                             f"review-packet.yaml ({actual_packet_sha!r})",
                })

    # ---- Rule 10: review-packet contains all 10 §7 required_packet_fields ----
    packet = parsed.get("review-packet.yaml")
    if isinstance(packet, dict):
        for field in REQUIRED_PACKET_FIELDS:
            if field not in packet:
                findings.append({
                    "id": "PACKET_MISSING_REQUIRED_FIELD",
                    "severity": "high",
                    "field": field,
                    "claim": f"review-packet.yaml missing §7 required_packet_fields entry {field!r}",
                })

    # ---- Rule 11: no corroborated_by anywhere on review yamls ----
    for review_name in REVIEW_FILES:
        rdata = parsed.get(review_name)
        if not isinstance(rdata, dict):
            continue
        for hit_path in _contains_corroborated_by(rdata):
            findings.append({
                "id": "CORROBORATED_BY_ON_REVIEW",
                "severity": "high",
                "review": review_name,
                "path": hit_path,
                "claim": f"{review_name} contains corroborated_by at {hit_path!r}; "
                         f"synthesizer-only per §13 / codex-rev-009",
            })

    # ---- Rule 12 (v1.7.5; operator finding 2026-05-08): closure-state coherence ----
    # When iteration-outcome.yaml.closing_state declares the iteration is closed
    # implementation_ready (or any non-blocked terminal state), there must NOT be
    # contradicting "still pending operator decision" or "not applied" sentinels
    # in iteration-outcome / verification / consensus. Catches the stale-closure
    # text class of bug introduced in iteration-0002.
    if isinstance(outcome, dict):
        closing = outcome.get("closing_state", "")
        if isinstance(closing, str) and "implementation_ready" in closing and "blocked" not in closing:
            # iteration declares CLOSED-AND-APPLIED; flag stale "blocked" / "not_applied" / "pick path" text
            stale_sentinels_in_outcome = [
                ("artifacts_staged_but_not_applied", "outcome lists artifacts as staged-not-applied"),
                ("operator_action_required", "outcome still asks operator to pick a path"),
            ]
            for key, claim in stale_sentinels_in_outcome:
                if key in outcome:
                    findings.append({
                        "id": "ITERATION_CLOSURE_INCOHERENT",
                        "severity": "high",
                        "artifact": "iteration-outcome.yaml",
                        "stale_field": key,
                        "claim": f"iteration-outcome.yaml.closing_state declares closed/applied but {claim} (field {key!r} present); stale post-apply text",
                    })

            # consensus.yaml stale sentinels
            consensus = parsed.get("consensus.yaml")
            if isinstance(consensus, dict):
                cs = consensus.get("consensus_state", "")
                acs = consensus.get("accepted_changes_status", "")
                if isinstance(cs, str) and "blocked" in cs:
                    findings.append({
                        "id": "ITERATION_CLOSURE_INCOHERENT",
                        "severity": "high",
                        "artifact": "consensus.yaml",
                        "stale_field": "consensus_state",
                        "value": cs[:80],
                        "claim": f"outcome closing_state=implementation_ready but consensus.yaml.consensus_state contains 'blocked' ({cs[:60]!r}); stale post-apply text",
                    })
                if isinstance(acs, str) and ("not_applied" in acs or "blocked_post_dry_run" in acs):
                    findings.append({
                        "id": "ITERATION_CLOSURE_INCOHERENT",
                        "severity": "high",
                        "artifact": "consensus.yaml",
                        "stale_field": "accepted_changes_status",
                        "value": acs[:80],
                        "claim": f"outcome closing_state=implementation_ready but consensus.yaml.accepted_changes_status={acs[:60]!r}; stale post-apply text",
                    })
                # v1.7.6 (operator finding 2026-05-08 medium-4): nested apply_status: staged_not_applied
                # in consensus.accepted_changes[].apply_status; misses top-level scan
                # v1.7.8 (operator finding 2026-05-08 high-1): also catch pending_apply
                # (iteration-0003 had 5 entries with apply_status: pending_apply on a
                # closed-as-implementation_ready iteration; same stale-closure class)
                # v1.8.1 (operator finding 2026-05-08 medium-3): case-insensitive
                # match (iteration-0005 had apply_status: BLOCKED_PENDING_OPERATOR_AUTHORIZATION
                # uppercase that lowercase-only "pending" check missed) + scan for
                # operator_response_pending: true boolean field anywhere in the consensus.
                changes = consensus.get("accepted_changes")
                if isinstance(changes, list):
                    for i, ch in enumerate(changes):
                        if not isinstance(ch, dict):
                            continue
                        astatus = ch.get("apply_status", "")
                        astatus_lower = astatus.lower() if isinstance(astatus, str) else ""
                        if astatus_lower and (
                            "staged_not_applied" in astatus_lower
                            or "not_applied" in astatus_lower
                            or "pending_apply" in astatus_lower
                            or "pending" in astatus_lower
                            or "blocked" in astatus_lower
                        ):
                            findings.append({
                                "id": "ITERATION_CLOSURE_INCOHERENT",
                                "severity": "high",
                                "artifact": "consensus.yaml",
                                "stale_field": f"accepted_changes[{i}].apply_status",
                                "change_id": ch.get("id", f"<index {i}>"),
                                "value": astatus[:80],
                                "claim": f"outcome closing_state=implementation_ready but consensus.accepted_changes[{i}].apply_status={astatus[:60]!r}; nested stale post-apply text (case-insensitive match)",
                            })

                # v1.8.1: scan ENTIRE consensus tree for operator_response_pending: true
                # in a closed iteration. iteration-0005 had this in 5 fields after the
                # cf-007 authorization landed; closure should clear them.
                def _scan_for_operator_response_pending(obj, path=""):
                    hits = []
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            sub = f"{path}.{k}" if path else k
                            if k == "operator_response_pending" and v is True:
                                hits.append(sub)
                            hits.extend(_scan_for_operator_response_pending(v, sub))
                    elif isinstance(obj, list):
                        for j, item in enumerate(obj):
                            hits.extend(_scan_for_operator_response_pending(item, f"{path}[{j}]"))
                    return hits

                pending_hits = _scan_for_operator_response_pending(consensus)
                for hit in pending_hits:
                    findings.append({
                        "id": "ITERATION_CLOSURE_INCOHERENT",
                        "severity": "high",
                        "artifact": "consensus.yaml",
                        "stale_field": hit,
                        "value": True,
                        "claim": f"outcome closing_state=implementation_ready but consensus has operator_response_pending=true at {hit}; should be false/removed post-apply",
                    })

            # verification.yaml stale sentinels
            verification = parsed.get("verification.yaml")
            if isinstance(verification, dict):
                if verification.get("passed") is False:
                    findings.append({
                        "id": "ITERATION_CLOSURE_INCOHERENT",
                        "severity": "high",
                        "artifact": "verification.yaml",
                        "stale_field": "passed",
                        "value": False,
                        "claim": "outcome closing_state=implementation_ready but verification.yaml.passed=false; stale post-apply text",
                    })
                gate = verification.get("gate", {})
                if isinstance(gate, dict):
                    gs = gate.get("consensus_state", "")
                    if isinstance(gs, str) and "blocked" in gs:
                        findings.append({
                            "id": "ITERATION_CLOSURE_INCOHERENT",
                            "severity": "high",
                            "artifact": "verification.yaml",
                            "stale_field": "gate.consensus_state",
                            "value": gs[:80],
                            "claim": f"outcome closing_state=implementation_ready but verification.yaml.gate.consensus_state={gs[:60]!r}; stale post-apply text",
                        })

    return _wrap(findings, iteration_dir, iteration_name, parsed)


def _wrap(findings: list[dict], iteration_dir: Path, iteration_name: str,
          parsed: dict) -> dict:
    severity_counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    return {
        "schema_version": 1,
        "validator": "validate_iteration.py",
        "validator_version": "0.3.0-v1.7.8-hygiene",   # 0.1.0 (initial) -> 0.2.0-v1.7.5 (ITERATION_CLOSURE_INCOHERENT + AUDIT_EVENT_NAME_NON_CANONICAL grandfather + pre-canonical-pin marker) -> 0.2.1-v1.7.6 (nested apply_status detection) -> 0.3.0-v1.7.7 (subtype-gated iteration-0000 grandfather for HASH_CHAIN_BROKEN.subtype=missing_event_type) -> 0.3.0-v1.7.8 (pending_apply added to nested stale-closure detection)
        "iteration_id": iteration_name,
        "provenance": _build_provenance(iteration_dir),
        "stats": {
            "total_findings": len(findings),
            "severity_counts": severity_counts,
            "artifacts_present": sorted(
                name for name in REQUIRED_ARTIFACTS
                if (iteration_dir / name).exists()
            ),
            "artifacts_missing": sorted(
                name for name in REQUIRED_ARTIFACTS
                if not (iteration_dir / name).exists()
            ),
        },
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--iteration-dir", type=Path, help="consensus-state/active/<iteration>/ directory to validate")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--json", action="store_true", help="emit JSON to stdout in addition to YAML to --out")
    p.add_argument("--self-test", action="store_true", help="run validator against bundled fixtures")
    args = p.parse_args(argv)

    if args.self_test:
        good = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "iteration_known_good"
        bad = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "iteration_known_bad"
        rg = validate_iteration(good)
        rb = validate_iteration(bad)
        ok_good = rg["stats"]["total_findings"] == 0
        expected = {
            "MISSING_REQUIRED_ARTIFACT", "ITERATION_ID_MISMATCH", "CORROBORATED_BY_ON_REVIEW",
            "INVALID_SCHEMA_VERSION", "HASH_CHAIN_BROKEN", "PACKET_MISSING_REQUIRED_FIELD",
            "PACKET_SHA_MISMATCH", "MISSING_METRICS_BLOCK", "INVALID_INDEPENDENT_FINDING_RATE",
        }
        seen = {f["id"] for f in rb["findings"]}
        missing = expected - seen
        ok_bad = not missing
        print(f"good={rg['stats']['total_findings']} bad_seen={len(seen)} bad_missing={sorted(missing)}")
        return 0 if ok_good and ok_bad else 1

    if args.iteration_dir is None:
        p.error("--iteration-dir required (or pass --self-test)")

    report = validate_iteration(args.iteration_dir)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        args.out.write_text(yaml.safe_dump(report, sort_keys=False, default_flow_style=False), encoding="utf-8")
    except ImportError:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        sev = report["stats"]["severity_counts"]
        print(f"validate_iteration: {report['stats']['total_findings']} finding(s) "
              f"({sev}) -> {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
