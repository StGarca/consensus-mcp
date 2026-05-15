# Fix response — Workflow B pass-2 (codex pass-1 + gemini pass-2 integrated)

All findings verified against actual code first (peer-citation
doctrine: none were hallucinations — every citation checked out, and
all four correctly identified ways the slice UNDER-implemented the
converged plan). Integrated per receiving-code-review with TDD
(failing test added before each fix).

## codex-rev-001 (blocking) — RESOLVED

Missing convention-input was sealed as `doctrine-only`, letting a NEW
convergence bypass enforcement (safety hard-rejects never evaluated).

Fix (`workflow_engine._seal_converged_plan`): every plan SEALED by the
engine is new (v1.15.1+). A missing convention-input is now validated
under the configured level (validator runs on `{}` ⇒ missing-block
violations). `enforcement: doctrine-only` is now EXCLUSIVELY a
READ-time classification in `consensus_get_iteration_outcome` for
pre-v1.15.1 plans with no `convention_gate` at all. Safety/strict +
absent ⇒ hard-reject; graduated + non-safety + absent ⇒ warn+annotate
and seal. Tests: `test_engine_absent_convention_at_seal_is_annotated_
not_doctrine_only`, `test_engine_absent_convention_with_safety_risk_
class_hard_rejects`.

## codex-rev-002 (blocking) — RESOLVED

`decisive_experiment_before_next_iteration` (the THIRD named block)
was neither required by the schema nor validated.

Fix: added to `schema.required`; validator now requires the key
present, allows `null` ONLY when `empirical_status` ∈ {proven, n/a}
(convention doc §3), else requires a non-empty experiment string.
Tests: `test_decisive_experiment_block_required`,
`test_decisive_experiment_null_allowed_only_for_proven_or_na`,
`test_schema_requires_decisive_experiment_key`.

## codex-rev-003 (high) — RESOLVED

`convention_schema_version` was read but defaulted to 1 at seal,
accepting malformed/future conventions.

Fix: validator flags any present convention whose
`convention_schema_version != CONVENTION_SCHEMA_VERSION` (not
defaulted, not grandfathered). Engine no longer defaults a present
convention's version. Test:
`test_convention_schema_version_must_be_exactly_one`.

## codex-rev-004 (high) — RESOLVED

A stale `converged-plan.yaml` from a prior run could survive a later
hard-reject and be reported as a successful outcome.

Fix: on hard-reject the engine unlinks any existing
`converged-plan.yaml` before raising — fail-closed is now truly
closed. Test: `test_hard_reject_removes_stale_converged_plan`.

## gemini-rev-001 (medium, gemini pass-2 — goal_satisfied=True, no
blocking) — RESOLVED

Docs/CHANGELOG/README edits (artifact-scoped-truth, gate 10) were on
disk but not embedded in the pass-1 review packet, so unverifiable.
This pass-2 packet embeds `docs/workflows/converged-plan-convention.md`,
`CHANGELOG.md`, `README.md` for direct verification against gate 10.

## Verification

- `test_converged_plan_convention.py`: 36 tests green (30 original +
  6 net audit-fix; the one test encoding the now-corrected
  doctrine-only-at-seal behavior was replaced, not deleted silently).
- Full suite: **962 passed, 1 skipped, 0 regressions**.

## Separate follow-up (NOT fixed here — out of scope by goal_packet)

The gemini dispatcher does not set `GEMINI_CLI_TRUST_WORKSPACE` /
`--skip-trust`; gemini CLI ≥0.43.0-preview.0 then fails headless with
empty stdout (diagnosed this session — pass-1 gemini failed twice for
exactly this). `_dispatch_gemini.py` is in this iteration's
`forbidden_files`, so this is recorded as a v1.15.2 candidate, not
patched here.
