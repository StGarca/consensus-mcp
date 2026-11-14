"""run_validator_tests.py - smoke test suite for Phase 0 validators.

Per spec section 23 phase_0_deliverables and rev-071 reframe:
deterministic validators tested against known-good and known-bad fixtures
BEFORE running on real spec.

Covers:
  - validate_disposition_index.py (V0, pre-existing)
  - validate_review.py            (V1)
  - validate_consensus.py         (V2)
  - build_review_packet.py        (V3 - build + validate)
  - validate_iteration.py         (V4)
  - scope_check.py                (V5)
  - consensus_gate.py             (V6, subprocess self-test wrapper)

Usage:
  python consensus_mcp/validators/run_validator_tests.py

Exit codes:
  0 - all tests passed
  1 - one or more tests failed
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if __package__ in (None, ""):  # executed as a script: prefer the co-located source tree
    sys.path.insert(0, str(REPO_ROOT))

from consensus_mcp.validators.validate_disposition_index import validate_disposition_index  # noqa: E402
from consensus_mcp.validators.validate_review import validate_review  # noqa: E402
from consensus_mcp.validators.validate_consensus import validate_consensus  # noqa: E402
from consensus_mcp.validators.build_review_packet import (  # noqa: E402
    build_review_packet,
    validate_review_packet,
    REQUIRED_PACKET_FIELDS,
)
from consensus_mcp.validators.validate_iteration import validate_iteration  # noqa: E402
from consensus_mcp.validators.scope_check import scope_check  # noqa: E402

# V0 fixtures
KNOWN_GOOD = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "spec_known_good" / "spec.md"
KNOWN_BAD = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "spec_known_bad" / "spec.md"

# V1 fixtures
REVIEW_KNOWN_GOOD = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "review_known_good" / "codex-review.yaml"
REVIEW_KNOWN_BAD = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "review_known_bad" / "codex-review.yaml"

# V2 fixtures
CONSENSUS_KNOWN_GOOD = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "consensus_known_good" / "consensus.yaml"
CONSENSUS_KNOWN_BAD = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "consensus_known_bad" / "consensus.yaml"

# V3 fixtures
PACKET_GOOD_INPUT = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "review_packet_known_good" / "input.yaml"
PACKET_KNOWN_BAD = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "review_packet_known_bad" / "packet.yaml"

# V4 fixtures
ITERATION_KNOWN_GOOD = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "iteration_known_good"
ITERATION_KNOWN_BAD = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "iteration_known_bad"

# V5 fixtures
SCOPE_GOOD_CONSENSUS = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "scope_check_known_good" / "consensus.yaml"
SCOPE_BAD_CONSENSUS = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "scope_check_known_bad" / "consensus.yaml"
SCOPE_MISSING_CONSENSUS = REPO_ROOT / "consensus-state" / "tests" / "fixtures" / "scope_check_missing_scope" / "consensus.yaml"


def _expect(condition: bool, msg: str) -> bool:
    if condition:
        print(f"  PASS: {msg}")
        return True
    print(f"  FAIL: {msg}")
    return False


# === V0: validate_disposition_index ===

def test_known_good_returns_zero_findings() -> bool:
    print("test_known_good_returns_zero_findings")
    report = validate_disposition_index(KNOWN_GOOD)
    return _expect(
        report["stats"]["total_findings"] == 0,
        f"clean spec yields 0 findings (got {report['stats']['total_findings']})",
    )


def test_known_bad_catches_dead_promoted_to() -> bool:
    print("test_known_bad_catches_dead_promoted_to")
    report = validate_disposition_index(KNOWN_BAD)
    has_dead = any(f["id"] == "PROMOTED_TO_DEAD_REFERENCE" for f in report["findings"])
    return _expect(has_dead, "validator catches PROMOTED_TO_DEAD_REFERENCE")


def test_known_bad_catches_missing_archived_file() -> bool:
    print("test_known_bad_catches_missing_archived_file")
    report = validate_disposition_index(KNOWN_BAD)
    has_missing = any(f["id"] == "ARCHIVED_FILE_MISSING" for f in report["findings"])
    return _expect(has_missing, "validator catches ARCHIVED_FILE_MISSING")


def test_known_bad_catches_section_24_prose_drift() -> bool:
    print("test_known_bad_catches_section_24_prose_drift")
    report = validate_disposition_index(KNOWN_BAD)
    has_prose = any(f["id"] == "SECTION_24_INDEX_ONLY_VIOLATION" for f in report["findings"])
    return _expect(has_prose, "validator catches SECTION_24_INDEX_ONLY_VIOLATION")


def test_known_bad_catches_known_blocker_no_marker() -> bool:
    print("test_known_bad_catches_known_blocker_no_marker")
    report = validate_disposition_index(KNOWN_BAD)
    has_no_marker = any(
        f["id"] == "KNOWN_BLOCKER_SECTION_LACKS_DISABLE_MARKER" for f in report["findings"]
    )
    return _expect(has_no_marker, "validator catches KNOWN_BLOCKER_SECTION_LACKS_DISABLE_MARKER")


# === V1: validate_review ===

def test_review_known_good_zero_findings() -> bool:
    print("test_review_known_good_zero_findings")
    report = validate_review(REVIEW_KNOWN_GOOD)
    return _expect(
        report["stats"]["total_findings"] == 0,
        f"clean review: 0 findings (got {report['stats']['total_findings']})",
    )


def test_review_known_bad_catches_missing_key() -> bool:
    print("test_review_known_bad_catches_missing_key")
    report = validate_review(REVIEW_KNOWN_BAD)
    has = any(f["id"] == "MISSING_REQUIRED_KEY" for f in report["findings"])
    return _expect(has, "validator catches MISSING_REQUIRED_KEY")


def test_review_known_bad_catches_invalid_enum() -> bool:
    print("test_review_known_bad_catches_invalid_enum")
    report = validate_review(REVIEW_KNOWN_BAD)
    has = any(f["id"] == "INVALID_ENUM_VALUE" for f in report["findings"])
    return _expect(has, "validator catches INVALID_ENUM_VALUE")


def test_review_known_bad_catches_empty_assumptions() -> bool:
    print("test_review_known_bad_catches_empty_assumptions")
    report = validate_review(REVIEW_KNOWN_BAD)
    has = any(f["id"] == "EMPTY_ASSUMPTIONS_CHALLENGED" for f in report["findings"])
    return _expect(has, "validator catches EMPTY_ASSUMPTIONS_CHALLENGED")


def test_review_known_bad_catches_no_blockers_no_rationale() -> bool:
    print("test_review_known_bad_catches_no_blockers_no_rationale")
    report = validate_review(REVIEW_KNOWN_BAD)
    has = any(f["id"] == "NO_BLOCKERS_WITHOUT_RATIONALE" for f in report["findings"])
    return _expect(has, "validator catches NO_BLOCKERS_WITHOUT_RATIONALE")


def test_review_known_bad_catches_corroborated_by_forbidden() -> bool:
    print("test_review_known_bad_catches_corroborated_by_forbidden")
    report = validate_review(REVIEW_KNOWN_BAD)
    has = any(f["id"] == "CORROBORATED_BY_FORBIDDEN" for f in report["findings"])
    return _expect(has, "validator catches CORROBORATED_BY_FORBIDDEN")


# === V2: validate_consensus ===

CONSENSUS_EXPECTED_BAD_IDS = {
    "MISSING_REQUIRED_KEY",
    "INVALID_ENUM_VALUE",
    "PRODUCTION_STATE_INCOHERENT",
    "BLOCKING_FINDING_NOT_REFLECTED_IN_CONSENSUS_STATE",
    "SEVERITY_GATE_VIOLATION",
    "CANONICAL_KEY_COLLISION",
    "CANONICAL_FINDING_NO_SOURCES",
    "ACCEPTED_CHANGE_DANGLING_SOURCE",
    "ACCEPTED_CHANGE_MISSING_EDIT_FIELDS",
    "IMPLEMENTATION_SCOPE_INCOHERENT",
    "CORROBORATED_BY_MALFORMED",
}


def test_consensus_known_good_zero_findings() -> bool:
    print("test_consensus_known_good_zero_findings")
    report = validate_consensus(CONSENSUS_KNOWN_GOOD)
    return _expect(
        report["stats"]["total_findings"] == 0,
        f"clean consensus: 0 findings (got {report['stats']['total_findings']})",
    )


def test_consensus_known_bad_catches_all_classes() -> bool:
    print("test_consensus_known_bad_catches_all_classes")
    report = validate_consensus(CONSENSUS_KNOWN_BAD)
    seen = {f["id"] for f in report["findings"]}
    missing = CONSENSUS_EXPECTED_BAD_IDS - seen
    return _expect(not missing, f"all 11 consensus finding classes caught (missing={sorted(missing)})")


# === V3: build_review_packet ===

def test_packet_build_sanitizes_injection() -> bool:
    print("test_packet_build_sanitizes_injection")
    p = build_review_packet(PACKET_GOOD_INPUT)
    has_log = isinstance(p.get("sanitization_log"), list) and len(p["sanitization_log"]) >= 7
    all_required = all(k in p for k in REQUIRED_PACKET_FIELDS)
    return _expect(
        has_log and all_required,
        "packet sanitizes >=7 injection patterns and has all required fields",
    )


def test_packet_validate_known_bad_reports_missing_keys() -> bool:
    print("test_packet_validate_known_bad_reports_missing_keys")
    r = validate_review_packet(PACKET_KNOWN_BAD)
    n = sum(1 for f in r["findings"] if f["id"] == "MISSING_REQUIRED_KEY")
    return _expect(n >= 1, f"known-bad packet yields >=1 MISSING_REQUIRED_KEY (got {n})")


# === V4: validate_iteration ===

ITERATION_EXPECTED_BAD_IDS = {
    "MISSING_REQUIRED_ARTIFACT",
    "ITERATION_ID_MISMATCH",
    "CORROBORATED_BY_ON_REVIEW",
    "INVALID_SCHEMA_VERSION",
    "HASH_CHAIN_BROKEN",
    "PACKET_MISSING_REQUIRED_FIELD",
    "PACKET_SHA_MISMATCH",
    "MISSING_METRICS_BLOCK",
    "INVALID_INDEPENDENT_FINDING_RATE",
}


def test_iteration_known_good_zero_findings() -> bool:
    print("test_iteration_known_good_zero_findings")
    report = validate_iteration(ITERATION_KNOWN_GOOD)
    return _expect(
        report["stats"]["total_findings"] == 0,
        f"clean iteration: 0 findings (got {report['stats']['total_findings']})",
    )


def test_iteration_known_bad_catches_all_seeded() -> bool:
    print("test_iteration_known_bad_catches_all_seeded")
    report = validate_iteration(ITERATION_KNOWN_BAD)
    seen = {f["id"] for f in report["findings"]}
    missing = ITERATION_EXPECTED_BAD_IDS - seen
    return _expect(not missing, f"all 9 seeded iteration finding classes caught (missing={sorted(missing)})")


# === V5: scope_check ===

def test_scope_check_known_good_passes() -> bool:
    print("test_scope_check_known_good_passes")
    report = scope_check(
        SCOPE_GOOD_CONSENSUS,
        _touched_files_override=[
            "wiki/consensus-mcp/foo.md",
            "consensus_mcp/validators/build_review_packet.py",
        ],
    )
    return _expect(
        report["scope_check_block"]["passed"] and report["stats"]["total_findings"] == 0,
        "in-scope diff: passed=True, 0 findings",
    )


def test_scope_check_known_bad_flags_violations() -> bool:
    print("test_scope_check_known_bad_flags_violations")
    report = scope_check(
        SCOPE_BAD_CONSENSUS,
        _touched_files_override=[
            "dist/wheel.whl",
            "scripts/other/baz.py",
            "wiki/foo.md",
        ],
    )
    ids = [f["id"] for f in report["findings"]]
    has_forbidden = "FORBIDDEN_FILE_TOUCHED" in ids
    has_outside = "FILE_OUTSIDE_ALLOWED_SCOPE" in ids
    not_passed = report["scope_check_block"]["passed"] is False
    return _expect(
        has_forbidden and has_outside and not_passed,
        "out-of-scope diff: FORBIDDEN_FILE_TOUCHED + FILE_OUTSIDE_ALLOWED_SCOPE + passed=False",
    )


def test_scope_check_missing_scope_flagged() -> bool:
    print("test_scope_check_missing_scope_flagged")
    report = scope_check(SCOPE_MISSING_CONSENSUS, _touched_files_override=[])
    has = any(
        f["id"] == "IMPLEMENTATION_SCOPE_MISSING_FROM_CONSENSUS" for f in report["findings"]
    )
    return _expect(has, "missing implementation_scope flagged")


# === V6: consensus_gate (subprocess wrapper - fixtures use baked-in hashes) ===

def test_consensus_gate_self_test() -> bool:
    print("test_consensus_gate_self_test")
    script = REPO_ROOT / "consensus_mcp" / "validators" / "consensus_gate.py"
    result = subprocess.run(
        [sys.executable, str(script), "--self-test"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    return _expect(
        result.returncode == 0,
        f"consensus_gate --self-test exit 0 (got {result.returncode}); "
        f"stdout tail={result.stdout.strip().splitlines()[-3:] if result.stdout else []}",
    )


def main() -> int:
    tests = [
        # V0
        test_known_good_returns_zero_findings,
        test_known_bad_catches_dead_promoted_to,
        test_known_bad_catches_missing_archived_file,
        test_known_bad_catches_section_24_prose_drift,
        test_known_bad_catches_known_blocker_no_marker,
        # V1
        test_review_known_good_zero_findings,
        test_review_known_bad_catches_missing_key,
        test_review_known_bad_catches_invalid_enum,
        test_review_known_bad_catches_empty_assumptions,
        test_review_known_bad_catches_no_blockers_no_rationale,
        test_review_known_bad_catches_corroborated_by_forbidden,
        # V2
        test_consensus_known_good_zero_findings,
        test_consensus_known_bad_catches_all_classes,
        # V3
        test_packet_build_sanitizes_injection,
        test_packet_validate_known_bad_reports_missing_keys,
        # V4
        test_iteration_known_good_zero_findings,
        test_iteration_known_bad_catches_all_seeded,
        # V5
        test_scope_check_known_good_passes,
        test_scope_check_known_bad_flags_violations,
        test_scope_check_missing_scope_flagged,
        # V6
        test_consensus_gate_self_test,
    ]
    results = []
    for t in tests:
        results.append(t())
    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} tests passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
