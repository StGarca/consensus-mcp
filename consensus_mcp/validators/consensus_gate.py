"""consensus_gate.py - Phase 0 production-readiness gate (P0-V6).

Per spec section 17 (production-readiness state machine), §0 anti-injection
rule, codex-rev-011 (three-way hash bind resolution).

Evaluates the production_state machine for an iteration by reading:
  - consensus.yaml (current synthesizer-owned consensus)
  - verification.yaml (current verifier-owned verification)
  - approval.yaml (operator-owned, OUTSIDE the repo at
    <operator-approval-path>/operator-production-approval.yaml)

The gate is authoritative; it does NOT trust consensus.yaml's claim of
production_state. It computes the state from raw inputs.

State machine (§17):
  not_ready
    -> technical gates not clear
  ready_pending_operator_approval
    -> technical gates clear; protected approval missing or unmatched
  approved
    -> technical gates clear AND protected approval matches all three
       binds (target + consensus + verification)

production_ready_if (all five must hold):
  codex_production_clearance
  claude_production_clearance
  verification_passed
  production_scope_verified
  unresolved_consensus_disagreements_empty

production_allowed_if (only checked when production_ready):
  production_ready
  operator_production_approval_valid
  operator_production_scope_matches
  all_three_hash_binds_match

Usage:
  python consensus_mcp/validators/consensus_gate.py \
    --consensus PATH \
    --verification PATH \
    [--approval PATH] \
    [--target-sha256 HEX] \
    [--out PATH] [--json] [--self-test]

Exit codes:
  0 - validator ran cleanly
  2 - parse / missing-file error for required inputs
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
DEFAULT_OUT = REPO_ROOT / "consensus-state" / "state" / "consensus-gate-report.yaml"

VALID_SCOPE_TYPES = {"render", "merge", "deploy", "data-mutation"}
APPROVAL_REQUIRED_FIELDS = ("approved_by", "approved_utc", "signature_or_operator_nonce")


def _yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        raise SystemExit("pyyaml required (pip install pyyaml)")


def _parse_yaml_file(path: Path) -> dict:
    yaml = _yaml()
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SystemExit(f"yaml parse error in {path}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"yaml root must be a mapping: {path}")
    return data


def _canonical_yaml_sha256(path: Path) -> str:
    """Per spec convention: sha256 of canonical (sort_keys=True) yaml dump."""
    yaml = _yaml()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    canonical = yaml.safe_dump(data, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dependency_version(dist_name: str) -> str | None:
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _build_provenance(consensus_path: Path, verification_path: Path,
                      approval_path: Path | None) -> dict:
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
            "consensus_path": str(consensus_path),
            "consensus_sha256_file": _sha256_file(consensus_path),
            "verification_path": str(verification_path),
            "verification_sha256_file": _sha256_file(verification_path),
            "approval_path": str(approval_path) if approval_path else None,
            "approval_sha256_file": _sha256_file(approval_path) if approval_path else None,
            "validator_script_path": "consensus_mcp/validators/consensus_gate.py",
            "validator_script_sha256": _sha256_file(Path(__file__).resolve()),
        },
    }


def evaluate_gate(consensus_path: Path, verification_path: Path,
                  approval_path: Path | None,
                  current_target_sha256: str | None) -> dict:
    """Evaluate the §17 production-readiness state machine.

    Returns the structured report dict (see module docstring / output schema).
    """
    findings: list[dict] = []

    consensus = _parse_yaml_file(consensus_path)
    verification = _parse_yaml_file(verification_path)

    current_consensus_sha256 = _canonical_yaml_sha256(consensus_path)
    current_verification_sha256 = _canonical_yaml_sha256(verification_path)

    # ---- production_ready_if evaluation ----
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
        })
    if not claude_clearance:
        findings.append({
            "id": "MISSING_CLAUDE_CLEARANCE",
            "severity": "high",
            "claim": "consensus.production_clearances.claude != 'approved'",
        })

    unresolved = consensus.get("unresolved_disagreements")
    unresolved_empty = isinstance(unresolved, list) and len(unresolved) == 0
    if not unresolved_empty:
        findings.append({
            "id": "UNRESOLVED_DISAGREEMENTS_PRESENT",
            "severity": "high",
            "claim": "consensus.unresolved_disagreements is not an empty list",
        })

    impl_scope = consensus.get("implementation_scope")
    scope_verified = bool(impl_scope) and (
        (isinstance(impl_scope, dict) and len(impl_scope) > 0)
        or (isinstance(impl_scope, list) and len(impl_scope) > 0)
    )
    if not scope_verified:
        findings.append({
            "id": "IMPLEMENTATION_SCOPE_MISSING",
            "severity": "high",
            "claim": "consensus.implementation_scope absent or empty",
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
            "verification_passed": ver_passed,
            "scope_check_passed": scope_check_passed,
        })

    production_ready_if = {
        "codex_production_clearance": codex_clearance,
        "claude_production_clearance": claude_clearance,
        "verification_passed": verification_ok,
        "production_scope_verified": scope_verified,
        "unresolved_consensus_disagreements_empty": unresolved_empty,
    }
    production_ready = all(production_ready_if.values())

    # ---- production_allowed_if evaluation ----
    operator_approval_valid = False
    operator_scope_matches = False
    hash_binds_match = False
    approval_loaded = False
    approval: dict = {}

    if production_ready:
        if approval_path is None or not approval_path.exists():
            findings.append({
                "id": "APPROVAL_MISSING",
                "severity": "medium",
                "claim": "approval store path not provided or file does not exist",
                "approval_path": str(approval_path) if approval_path else None,
            })
        else:
            try:
                approval = _parse_yaml_file(approval_path)
                approval_loaded = True
            except SystemExit as e:
                findings.append({
                    "id": "APPROVAL_INCOMPLETE",
                    "severity": "high",
                    "claim": f"approval file unparseable: {e}",
                })

        if approval_loaded:
            schema_v = approval.get("schema_version")
            if schema_v != 2:
                findings.append({
                    "id": "APPROVAL_SCHEMA_VERSION_INVALID",
                    "severity": "high",
                    "claim": f"approval.schema_version={schema_v!r}; expected 2",
                })
            else:
                # required field presence
                missing = [k for k in APPROVAL_REQUIRED_FIELDS
                           if not approval.get(k)]
                if missing:
                    findings.append({
                        "id": "APPROVAL_INCOMPLETE",
                        "severity": "high",
                        "claim": f"approval missing required field(s): {missing}",
                    })
                else:
                    operator_approval_valid = True

                # scope matches (Phase 0 lenient: just enum membership)
                prod_scope = approval.get("production_scope") or {}
                scope_type = prod_scope.get("type") if isinstance(prod_scope, dict) else None
                if scope_type not in VALID_SCOPE_TYPES:
                    findings.append({
                        "id": "APPROVAL_SCOPE_INVALID",
                        "severity": "high",
                        "claim": f"approval.production_scope.type={scope_type!r} not in {sorted(VALID_SCOPE_TYPES)}",
                    })
                else:
                    operator_scope_matches = True

                # three-way hash bind
                cons_match = approval.get("approved_consensus_sha256") == current_consensus_sha256
                ver_match = approval.get("approved_verification_sha256") == current_verification_sha256

                if not cons_match:
                    findings.append({
                        "id": "CONSENSUS_HASH_MISMATCH",
                        "severity": "high",
                        "claim": "approved_consensus_sha256 != current_consensus_sha256",
                        "approved": approval.get("approved_consensus_sha256"),
                        "current": current_consensus_sha256,
                    })
                if not ver_match:
                    findings.append({
                        "id": "VERIFICATION_HASH_MISMATCH",
                        "severity": "high",
                        "claim": "approved_verification_sha256 != current_verification_sha256",
                        "approved": approval.get("approved_verification_sha256"),
                        "current": current_verification_sha256,
                    })

                target_match = False
                if current_target_sha256 is None:
                    findings.append({
                        "id": "TARGET_HASH_NOT_PROVIDED",
                        "severity": "high",
                        "claim": "--target-sha256 not supplied; cannot verify approved_target_sha256 bind",
                    })
                elif approval.get("approved_target_sha256") != current_target_sha256:
                    findings.append({
                        "id": "TARGET_HASH_MISMATCH",
                        "severity": "high",
                        "claim": "approved_target_sha256 != current_target_sha256 (caller-supplied)",
                        "approved": approval.get("approved_target_sha256"),
                        "current": current_target_sha256,
                    })
                else:
                    target_match = True

                hash_binds_match = cons_match and ver_match and target_match

    production_allowed_if = {
        "production_ready": production_ready,
        "operator_production_approval_valid": operator_approval_valid,
        "operator_production_scope_matches": operator_scope_matches,
        "all_three_hash_binds_match": hash_binds_match,
    }
    production_allowed = production_ready and all(
        v for k, v in production_allowed_if.items() if k != "production_ready"
    ) and production_ready

    # ---- terminal state ----
    if not production_ready:
        production_state = "not_ready"
    elif production_allowed:
        production_state = "approved"
    else:
        production_state = "ready_pending_operator_approval"

    gate_decision = {
        "production_state": production_state,
        "production_ready": production_ready,
        "production_allowed": production_allowed,
        "production_ready_if": production_ready_if,
        "production_allowed_if": production_allowed_if,
        "current_consensus_sha256": current_consensus_sha256,
        "current_verification_sha256": current_verification_sha256,
        "current_target_sha256": current_target_sha256,
    }

    return _wrap(findings, gate_decision, consensus_path, verification_path, approval_path)


def _wrap(findings: list[dict], gate_decision: dict,
          consensus_path: Path, verification_path: Path,
          approval_path: Path | None) -> dict:
    severity_counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    return {
        "schema_version": 1,
        "validator": "consensus_gate",
        "validator_version": "0.1.0",
        "provenance": _build_provenance(consensus_path, verification_path, approval_path),
        "stats": {
            "total_findings": len(findings),
            "severity_counts": severity_counts,
        },
        "gate_decision": gate_decision,
        "findings": findings,
    }


# ---- self-test ----

FIXTURE_ROOT = REPO_ROOT / "consensus-state" / "tests" / "fixtures"
SELF_TEST_TARGET_SHA256 = "ab" * 32


def _run_self_test() -> bool:
    cases = [
        {
            "name": "gate_state_not_ready",
            "consensus": FIXTURE_ROOT / "gate_state_not_ready" / "consensus.yaml",
            "verification": FIXTURE_ROOT / "gate_state_not_ready" / "verification.yaml",
            "approval": None,
            "target": None,
            "expect_state": "not_ready",
            "expect_finding_min": 1,
            "must_contain_id": "VERIFICATION_NOT_PASSED",
        },
        {
            "name": "gate_state_pending_approval",
            "consensus": FIXTURE_ROOT / "gate_state_pending_approval" / "consensus.yaml",
            "verification": FIXTURE_ROOT / "gate_state_pending_approval" / "verification.yaml",
            "approval": None,
            "target": None,
            "expect_state": "ready_pending_operator_approval",
            "expect_finding_count": 1,
            "must_contain_id": "APPROVAL_MISSING",
        },
        {
            "name": "gate_state_approved",
            "consensus": FIXTURE_ROOT / "gate_state_approved" / "consensus.yaml",
            "verification": FIXTURE_ROOT / "gate_state_approved" / "verification.yaml",
            "approval": FIXTURE_ROOT / "gate_state_approved" / "approval.yaml",
            "target": SELF_TEST_TARGET_SHA256,
            "expect_state": "approved",
            "expect_finding_count": 0,
        },
    ]

    all_ok = True
    for case in cases:
        try:
            report = evaluate_gate(
                case["consensus"], case["verification"],
                case["approval"], case["target"],
            )
        except SystemExit as e:
            print(f"FAIL {case['name']}: parse error: {e}")
            all_ok = False
            continue

        state = report["gate_decision"]["production_state"]
        n_findings = report["stats"]["total_findings"]
        ids = {f["id"] for f in report["findings"]}

        ok = True
        if state != case["expect_state"]:
            print(f"FAIL {case['name']}: state={state!r} expected {case['expect_state']!r}")
            ok = False
        if "expect_finding_count" in case and n_findings != case["expect_finding_count"]:
            print(f"FAIL {case['name']}: findings={n_findings} expected {case['expect_finding_count']}")
            ok = False
        if "expect_finding_min" in case and n_findings < case["expect_finding_min"]:
            print(f"FAIL {case['name']}: findings={n_findings} expected >= {case['expect_finding_min']}")
            ok = False
        if "must_contain_id" in case and case["must_contain_id"] not in ids:
            print(f"FAIL {case['name']}: missing finding id {case['must_contain_id']!r}; got {sorted(ids)}")
            ok = False

        if ok:
            print(f"PASS {case['name']}: state={state} findings={n_findings}")
        else:
            all_ok = False

    return all_ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--consensus", type=Path, help="consensus.yaml path")
    p.add_argument("--verification", type=Path, help="verification.yaml path")
    p.add_argument("--approval", type=Path, default=None,
                   help="operator approval YAML path (default: None means no approval yet)")
    p.add_argument("--target-sha256", type=str, default=None, dest="target_sha256",
                   help="caller-supplied sha256 of artifact being promoted (required for approved state)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--json", action="store_true", help="emit JSON to stdout in addition to YAML to --out")
    p.add_argument("--self-test", action="store_true", help="run bundled fixtures, exit 0 iff all pass")
    args = p.parse_args(argv)

    if args.self_test:
        ok = _run_self_test()
        return 0 if ok else 1

    if args.consensus is None or args.verification is None:
        p.error("--consensus and --verification are required (or use --self-test)")

    report = evaluate_gate(args.consensus, args.verification, args.approval, args.target_sha256)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        args.out.write_text(yaml.safe_dump(report, sort_keys=False, default_flow_style=False), encoding="utf-8")
    except ImportError:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        gd = report["gate_decision"]
        sev = report["stats"]["severity_counts"]
        print(f"consensus_gate: state={gd['production_state']} ready={gd['production_ready']} "
              f"allowed={gd['production_allowed']} findings={report['stats']['total_findings']} "
              f"({sev}) -> {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
