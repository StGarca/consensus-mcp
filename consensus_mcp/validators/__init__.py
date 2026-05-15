"""Validators sub-package for consensus_mcp.

Modules:

  - validate_disposition_index.py: implemented
  - validate_converged_plan.py: implemented (v1.15.1 — converged-plan
    convention machine-enforcement; structure/consequence only, no
    correctness-state derivation)
  - run_validator_tests.py: implemented
  - scope_check.py: implemented
  - validate_review.py: scaffold
  - validate_consensus.py: scaffold
  - validate_iteration.py: scaffold
  - consensus_gate.py: Phase-0 PRODUCTION-readiness gate (P0-V6); NOT
    the converged-plan convention gate (that is
    validate_converged_plan.py — the v1.15.0 recorded starting design
    that pointed here was refuted by code-reading in iter
    converged-plan-machine-enforcement)
  - build_review_packet.py: scaffold
"""
