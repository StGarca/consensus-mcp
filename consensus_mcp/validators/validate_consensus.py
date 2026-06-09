"""validate_consensus.py - Phase 0 consensus-artifact validator (P0-V2).

Per spec section 13 (consensus schema, severity_gate, corroboration ownership)
and section 12 (canonical_finding_key uniqueness). Audits a synthesizer-emitted
consensus.yaml against the schema and the corroboration / severity / scope
coherence rules.

Detects:
  - missing required top-level keys
  - invalid enum values (consensus_state, production_state, merge_method,
    severity, status, corroboration_strength)
  - production_state vs production_allowed/production_ready incoherence
  - blocking severity + (unresolved|accepted) but consensus_state != blocked
  - severity gate violations (independent corroboration + high/blocking
    + claim_class in {safety, correctness, security, process} must produce
    severity=blocking and status != rejected)
  - canonical_finding_key duplicates (file, section, claim_class,
    normalized_claim_text_hash)
  - canonical_finding with empty source_findings
  - accepted_changes referencing nonexistent canonical_id (dangling source)
  - accepted_changes edit block missing file/old_string/new_string
  - implementation_scope incoherence (implementation_ready=true with
    allowed_files empty; forbidden_files/forbidden_actions empty)
  - corroborated_by entry missing reviewer/finding_id/corroboration_strength

Usage:
  python consensus_mcp/validators/validate_consensus.py --consensus PATH [--out PATH] [--json]
  python consensus_mcp/validators/validate_consensus.py --self-test

Exit codes:
  0 - validator ran cleanly; report written
  2 - validator could not run (consensus missing, parse error)

Findings count does NOT gate exit code (Path C / consistent with sibling validators).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # executed as a script: prefer the co-located source tree
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from consensus_mcp.validators._shared import _dependency_version, _sha256_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / "consensus-state" / "state" / "validate-consensus-report.yaml"

REQUIRED_TOP_LEVEL_KEYS = [
    "schema_version",
    "iteration_id",
    "created_utc",
    "consensus_state",
    "production_state",
    "review_consensus",
    "implementation_ready",
    "production_ready",
    "production_allowed",
    "canonical_findings",
    "accepted_changes",
    "implementation_scope",
    "checks_required",
]

VALID_CONSENSUS_STATE = {"blocked", "implementation_ready", "implemented", "verified"}
VALID_PRODUCTION_STATE = {"not_ready", "ready_pending_operator_approval", "approved"}
VALID_MERGE_METHOD = {"exact", "deterministic_key", "bounded_micro_call"}
VALID_SEVERITY = {"blocking", "high", "medium", "low"}
VALID_STATUS = {"accepted", "rejected", "deferred", "unresolved"}
VALID_CORROBORATION_STRENGTH = {"independent", "suggested_by_context"}

# v1.7.4 (canonical-004 ratification): observational_mode flag relaxes enum
# constraints when consensus.observational_mode is True. Spec: section 13
# observational_mode_enum_extensions block.
OBSERVATIONAL_CONSENSUS_STATE_EXTRA = {"implementation_ready_observational_only"}
OBSERVATIONAL_MERGE_METHOD_EXTRA = {
    "shared_section_shared_concern",
    "related_topic_shared_concern",
}
# status field accepts documented templates plus a small set of legacy literals
# from iteration-0000. Phase 0 implementation: exact-string set; templates in
# spec but not enforced as regex (would require pattern matching; out of scope
# for first pass).
OBSERVATIONAL_STATUS_EXTRA = {
    "deferred_to_v1_X",
    "deferred_to_v1_X_per_observational_mode",
    "deferred_to_v1_7",
    "deferred_to_v1_7_per_observational_mode",
    "deferred_to_v1_8",
    "deferred_to_v1_8_per_observational_mode",
    "recorded_for_iteration_0000_observation",
    "recorded_for_iteration_0001_observation",
    "recorded_for_iteration_0002_observation",
}

# Severity gate (section 13): claim classes that auto-promote to blocking when
# combined with independent corroboration and high/blocking severity.
GATE_CLAIM_CLASSES = {"safety", "correctness", "security", "process"}
GATE_SEVERITIES = {"high", "blocking"}


def _parse_yaml_file(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit("pyyaml required (pip install pyyaml)")
    if not path.exists():
        raise SystemExit(f"consensus file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SystemExit(f"yaml parse error in {path}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"consensus file root must be a mapping: {path}")
    return data


def _build_provenance(consensus_path: Path) -> dict:
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
            "consensus_path": str(consensus_path.relative_to(REPO_ROOT)) if consensus_path.is_relative_to(REPO_ROOT) else str(consensus_path),
            "consensus_sha256": _sha256_file(consensus_path),
            "validator_script_path": "consensus_mcp/validators/validate_consensus.py",
            "validator_script_sha256": _sha256_file(Path(__file__).resolve()),
        },
    }


def _normalize_claim_text(text: str) -> str:
    """Normalize claim text for canonical-key hashing.

    Strips surrounding whitespace, lowercases, and collapses internal
    whitespace runs to single spaces. Used as a deterministic stand-in
    when file/section/claim_class fields are absent on a finding.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(text.strip().lower().split())


def _canonical_key(finding: dict) -> tuple:
    """Build (file, section, claim_class, normalized_claim_text_hash) tuple
    for canonical-key uniqueness checks per section 12. Falls back to empty
    strings for absent fields; the hash uses required_change as deterministic
    text input (consensus.yaml fixtures often lack the structured key fields)."""
    file_v = finding.get("file") or ""
    section_v = finding.get("section") or ""
    claim_class_v = finding.get("claim_class") or ""
    text = _normalize_claim_text(finding.get("required_change", ""))
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
    return (str(file_v), str(section_v), str(claim_class_v), text_hash)


def validate_consensus(consensus_path: Path) -> dict:
    """Validate a consensus.yaml artifact; return structured report."""
    findings: list[dict] = []
    consensus = _parse_yaml_file(consensus_path)

    # v1.7.4: observational_mode flag relaxes enum constraints (canonical-004).
    # When True, validate_consensus accepts OBSERVATIONAL_*_EXTRA values silently.
    obs_mode = bool(consensus.get("observational_mode", False))
    valid_consensus_state = VALID_CONSENSUS_STATE | (OBSERVATIONAL_CONSENSUS_STATE_EXTRA if obs_mode else set())
    valid_merge_method = VALID_MERGE_METHOD | (OBSERVATIONAL_MERGE_METHOD_EXTRA if obs_mode else set())
    valid_status = VALID_STATUS | (OBSERVATIONAL_STATUS_EXTRA if obs_mode else set())

    # ---- Required top-level keys ----
    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in consensus:
            findings.append({
                "id": "MISSING_REQUIRED_KEY",
                "severity": "high",
                "field": key,
                "claim": f"required top-level key missing: {key!r}",
            })

    # ---- Top-level enum bounds ----
    cs = consensus.get("consensus_state")
    if cs is not None and cs not in valid_consensus_state:
        findings.append({
            "id": "INVALID_ENUM_VALUE",
            "severity": "medium",
            "field": "consensus_state",
            "value": cs,
            "claim": f"consensus_state={cs!r} not in {sorted(valid_consensus_state)}"
                     + (" (observational_mode active)" if obs_mode else ""),
        })

    ps = consensus.get("production_state")
    if ps is not None and ps not in VALID_PRODUCTION_STATE:
        findings.append({
            "id": "INVALID_ENUM_VALUE",
            "severity": "medium",
            "field": "production_state",
            "value": ps,
            "claim": f"production_state={ps!r} not in {sorted(VALID_PRODUCTION_STATE)}",
        })

    # ---- Production state coherence ----
    pa = consensus.get("production_allowed")
    pr = consensus.get("production_ready")
    if ps == "approved" and pa is not True:
        findings.append({
            "id": "PRODUCTION_STATE_INCOHERENT",
            "severity": "high",
            "field": "production_state",
            "claim": f"production_state='approved' but production_allowed={pa!r} (must be true)",
        })
    if ps == "not_ready" and pa is True:
        findings.append({
            "id": "PRODUCTION_STATE_INCOHERENT",
            "severity": "high",
            "field": "production_state",
            "claim": "production_state='not_ready' but production_allowed=true (must be false)",
        })
    if pr is False and pa is True:
        findings.append({
            "id": "PRODUCTION_STATE_INCOHERENT",
            "severity": "high",
            "field": "production_allowed",
            "claim": "production_ready=false but production_allowed=true (must be false)",
        })

    # ---- canonical_findings ----
    cfs = consensus.get("canonical_findings")
    if not isinstance(cfs, list):
        cfs = []

    canonical_ids: set[str] = set()
    key_to_ids: dict[tuple, list[str]] = {}

    for i, f in enumerate(cfs):
        if not isinstance(f, dict):
            continue
        cid = f.get("canonical_id", f"<index {i}>")
        if isinstance(cid, str):
            canonical_ids.add(cid)

        # merge_method enum
        mm = f.get("merge_method")
        if mm is not None and mm not in valid_merge_method:
            findings.append({
                "id": "INVALID_ENUM_VALUE",
                "severity": "medium",
                "field": f"canonical_findings[{i}].merge_method",
                "value": mm,
                "claim": f"merge_method={mm!r} on {cid} not in {sorted(valid_merge_method)}"
                         + (" (observational_mode active)" if obs_mode else ""),
            })
        # severity enum
        sev = f.get("severity")
        if sev is not None and sev not in VALID_SEVERITY:
            findings.append({
                "id": "INVALID_ENUM_VALUE",
                "severity": "medium",
                "field": f"canonical_findings[{i}].severity",
                "value": sev,
                "claim": f"severity={sev!r} on {cid} not in {sorted(VALID_SEVERITY)}",
            })
        # status enum
        status = f.get("status")
        if status is not None and status not in valid_status:
            findings.append({
                "id": "INVALID_ENUM_VALUE",
                "severity": "medium",
                "field": f"canonical_findings[{i}].status",
                "value": status,
                "claim": f"status={status!r} on {cid} not in {sorted(valid_status)}"
                         + (" (observational_mode active)" if obs_mode else ""),
            })

        # source_findings non-empty
        sf = f.get("source_findings")
        if not isinstance(sf, list) or len(sf) == 0:
            findings.append({
                "id": "CANONICAL_FINDING_NO_SOURCES",
                "severity": "high",
                "field": f"canonical_findings[{i}].source_findings",
                "entry_id": cid,
                "claim": f"canonical_finding {cid} has empty/missing source_findings",
            })

        # corroborated_by structure + enum
        cb = f.get("corroborated_by")
        if cb is not None:
            if isinstance(cb, list):
                for j, e in enumerate(cb):
                    if not isinstance(e, dict):
                        findings.append({
                            "id": "CORROBORATED_BY_MALFORMED",
                            "severity": "medium",
                            "field": f"canonical_findings[{i}].corroborated_by[{j}]",
                            "entry_id": cid,
                            "claim": f"corroborated_by entry on {cid} is not a mapping",
                        })
                        continue
                    missing = [k for k in ("reviewer", "finding_id", "corroboration_strength") if k not in e]
                    if missing:
                        findings.append({
                            "id": "CORROBORATED_BY_MALFORMED",
                            "severity": "medium",
                            "field": f"canonical_findings[{i}].corroborated_by[{j}]",
                            "entry_id": cid,
                            "missing_keys": missing,
                            "claim": f"corroborated_by entry on {cid} missing keys: {missing}",
                        })
                    cstr = e.get("corroboration_strength")
                    if cstr is not None and cstr not in VALID_CORROBORATION_STRENGTH:
                        findings.append({
                            "id": "INVALID_ENUM_VALUE",
                            "severity": "medium",
                            "field": f"canonical_findings[{i}].corroborated_by[{j}].corroboration_strength",
                            "value": cstr,
                            "claim": f"corroboration_strength={cstr!r} on {cid} not in {sorted(VALID_CORROBORATION_STRENGTH)}",
                        })

        # severity gate (section 13): independent corroboration + (high/blocking severity
        # OR claim_class in gate set) -> severity must be blocking AND status != rejected.
        has_independent = False
        if isinstance(cb, list):
            for e in cb:
                if isinstance(e, dict) and e.get("corroboration_strength") == "independent":
                    has_independent = True
                    break
        claim_class = f.get("claim_class")
        gate_predicate = (sev in GATE_SEVERITIES) or (claim_class in GATE_CLAIM_CLASSES)
        if has_independent and gate_predicate:
            if sev != "blocking":
                findings.append({
                    "id": "SEVERITY_GATE_VIOLATION",
                    "severity": "high",
                    "field": f"canonical_findings[{i}].severity",
                    "entry_id": cid,
                    "claim": f"severity gate triggered on {cid} (independent + severity/claim_class) but severity={sev!r}; must be 'blocking'",
                })
            if status == "rejected":
                findings.append({
                    "id": "SEVERITY_GATE_VIOLATION",
                    "severity": "high",
                    "field": f"canonical_findings[{i}].status",
                    "entry_id": cid,
                    "claim": f"severity gate triggered on {cid} but status='rejected'; must not be rejected",
                })

        # canonical-key collection
        ck = _canonical_key(f)
        # only register if at least one of the structured key parts OR the hash is non-empty
        if any(part for part in ck):
            key_to_ids.setdefault(ck, []).append(str(cid))

    # ---- Canonical key uniqueness ----
    for ck, ids in key_to_ids.items():
        if len(ids) > 1:
            findings.append({
                "id": "CANONICAL_KEY_COLLISION",
                "severity": "medium",
                "field": "canonical_findings",
                "duplicate_ids": ids,
                "key": {"file": ck[0], "section": ck[1], "claim_class": ck[2], "normalized_claim_hash": ck[3]},
                "claim": f"canonical_finding_key collision: {ids} share (file={ck[0]!r}, section={ck[1]!r}, claim_class={ck[2]!r}, normalized_claim_hash={ck[3][:12]}...)",
                "note": "section 12 uniqueness rule",
            })

    # ---- Blocking-severity reflection in consensus_state ----
    has_blocking_active = any(
        isinstance(f, dict) and f.get("severity") == "blocking" and f.get("status") in ("unresolved", "accepted")
        for f in cfs
    )
    if has_blocking_active and (cs != "blocked" or consensus.get("implementation_ready") is True):
        findings.append({
            "id": "BLOCKING_FINDING_NOT_REFLECTED_IN_CONSENSUS_STATE",
            "severity": "high",
            "field": "consensus_state",
            "claim": f"canonical_findings has blocking entry with status in (unresolved, accepted) but consensus_state={cs!r} (must be 'blocked') or implementation_ready=True",
        })

    # ---- accepted_changes coherence ----
    acs = consensus.get("accepted_changes")
    if not isinstance(acs, list):
        acs = []
    for i, ac in enumerate(acs):
        if not isinstance(ac, dict):
            continue
        aid = ac.get("id", f"<index {i}>")
        ac_sources = ac.get("source_findings")
        if isinstance(ac_sources, list):
            for src in ac_sources:
                if isinstance(src, str) and src not in canonical_ids:
                    findings.append({
                        "id": "ACCEPTED_CHANGE_DANGLING_SOURCE",
                        "severity": "high",
                        "field": f"accepted_changes[{i}].source_findings",
                        "entry_id": aid,
                        "missing_canonical_id": src,
                        "claim": f"accepted_change {aid} references canonical_id {src!r} which is not in canonical_findings",
                    })
        edit = ac.get("edit")
        if not isinstance(edit, dict):
            findings.append({
                "id": "ACCEPTED_CHANGE_MISSING_EDIT_FIELDS",
                "severity": "high",
                "field": f"accepted_changes[{i}].edit",
                "entry_id": aid,
                "claim": f"accepted_change {aid} has no edit block",
            })
        else:
            missing = [k for k in ("file", "old_string", "new_string") if k not in edit]
            if missing:
                findings.append({
                    "id": "ACCEPTED_CHANGE_MISSING_EDIT_FIELDS",
                    "severity": "high",
                    "field": f"accepted_changes[{i}].edit",
                    "entry_id": aid,
                    "missing_keys": missing,
                    "claim": f"accepted_change {aid} edit block missing keys: {missing}",
                })

    # ---- implementation_scope coherence ----
    scope = consensus.get("implementation_scope") or {}
    if not isinstance(scope, dict):
        scope = {}
    allowed_files = scope.get("allowed_files")
    forbidden_files = scope.get("forbidden_files")
    forbidden_actions = scope.get("forbidden_actions")
    if consensus.get("implementation_ready") is True:
        if not isinstance(allowed_files, list) or len(allowed_files) == 0:
            findings.append({
                "id": "IMPLEMENTATION_SCOPE_INCOHERENT",
                "severity": "medium",
                "field": "implementation_scope.allowed_files",
                "claim": "implementation_ready=true but implementation_scope.allowed_files is empty",
            })
    if not isinstance(forbidden_files, list) or len(forbidden_files) == 0:
        findings.append({
            "id": "IMPLEMENTATION_SCOPE_INCOHERENT",
            "severity": "medium",
            "field": "implementation_scope.forbidden_files",
            "claim": "implementation_scope.forbidden_files must be non-empty (spec default)",
        })
    if not isinstance(forbidden_actions, list) or len(forbidden_actions) == 0:
        findings.append({
            "id": "IMPLEMENTATION_SCOPE_INCOHERENT",
            "severity": "medium",
            "field": "implementation_scope.forbidden_actions",
            "claim": "implementation_scope.forbidden_actions must be non-empty (spec default)",
        })

    return _wrap(findings, consensus, consensus_path)


def _wrap(findings: list[dict], consensus: dict, consensus_path: Path) -> dict:
    severity_counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    return {
        "schema_version": 1,
        "validator": "validate_consensus.py",
        "validator_version": "0.1.0",
        "consensus_iteration_id": consensus.get("iteration_id", "<unknown>"),
        "provenance": _build_provenance(consensus_path),
        "stats": {
            "total_findings": len(findings),
            "severity_counts": severity_counts,
        },
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--consensus", type=Path, required=True, help="consensus.yaml to validate")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--json", action="store_true", help="emit JSON to stdout in addition to YAML to --out")
    args = p.parse_args(argv)

    report = validate_consensus(args.consensus)

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
        print(f"validate_consensus: {report['stats']['total_findings']} finding(s) "
              f"({sev}) -> {args.out}")

    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        good = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "consensus_known_good" / "consensus.yaml"
        bad = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "consensus_known_bad" / "consensus.yaml"
        rg = validate_consensus(good)
        rb = validate_consensus(bad)
        ok = rg["stats"]["total_findings"] == 0
        expected_bad = {
            "MISSING_REQUIRED_KEY", "INVALID_ENUM_VALUE", "PRODUCTION_STATE_INCOHERENT",
            "BLOCKING_FINDING_NOT_REFLECTED_IN_CONSENSUS_STATE", "SEVERITY_GATE_VIOLATION",
            "CANONICAL_KEY_COLLISION", "CANONICAL_FINDING_NO_SOURCES",
            "ACCEPTED_CHANGE_DANGLING_SOURCE", "ACCEPTED_CHANGE_MISSING_EDIT_FIELDS",
            "IMPLEMENTATION_SCOPE_INCOHERENT", "CORROBORATED_BY_MALFORMED",
        }
        seen = {f["id"] for f in rb["findings"]}
        missing = expected_bad - seen
        ok = ok and not missing
        print(f"good={rg['stats']['total_findings']} bad_seen={len(seen)} bad_missing={sorted(missing)}")
        sys.exit(0 if ok else 1)
    sys.exit(main())
