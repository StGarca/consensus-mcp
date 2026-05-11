"""_build_gate_fixtures.py - regenerate approval.yaml with computed hashes.

Run this AFTER editing consensus.yaml or verification.yaml in the
gate_state_approved fixture so the embedded hashes track the canonical
YAML hash of the inputs.

The canonical YAML hash convention is the same one consensus_gate.py
uses at runtime:
    sha256(yaml.safe_dump(yaml.safe_load(path), sort_keys=True))

Usage:
  python_env\\python.exe agent-loop/tests/fixtures/gate_state_approved/_build_gate_fixtures.py

This script is committed alongside the fixtures so the operator (or v7
test harness) can reproduce the embedded hashes without trusting them
blindly.
"""
from __future__ import annotations
import hashlib
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CONSENSUS = HERE / "consensus.yaml"
VERIFICATION = HERE / "verification.yaml"
APPROVAL = HERE / "approval.yaml"

# Must match SELF_TEST_TARGET_SHA256 in consensus_gate.py
TARGET_SHA256 = "ab" * 32


def canonical_sha256(path: Path) -> str:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    canonical = yaml.safe_dump(data, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def main() -> int:
    consensus_h = canonical_sha256(CONSENSUS)
    verification_h = canonical_sha256(VERIFICATION)

    approval = {
        "schema_version": 2,
        "iteration_id": "gate-fixture-approved",
        "approved_by": "fixture-operator",
        "approved_utc": "2026-05-08T00:00:00Z",
        "approved_target_sha256": TARGET_SHA256,
        "approved_consensus_sha256": consensus_h,
        "approved_verification_sha256": verification_h,
        "production_scope": {
            "type": "render",
            "target": "fixture/path.txt",
        },
        "signature_or_operator_nonce": "fixture-nonce-do-not-trust",
    }

    APPROVAL.write_text(
        yaml.safe_dump(approval, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"wrote {APPROVAL}")
    print(f"  consensus_sha256     = {consensus_h}")
    print(f"  verification_sha256  = {verification_h}")
    print(f"  target_sha256        = {TARGET_SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
