# Workflow B audit target — v1.15.1 converged-plan convention machine-enforcement

This is the IMPLEMENTATION of the converged plan
`converged-plan.yaml` in this iteration dir (Workflow A
weighted-synthesis: claude + codex + gemini; shared-prior self-check
PASSED). Audit it against that converged plan's
`deliverable.acceptance_gates_gating`.

## What was implemented

1. **First acceptance gate (codex external-refuting-observation) —
   verified by code-reading, not assumed.** There is NO pre-existing
   orchestrator/human-authored post-convergence intake into
   `_seal_converged_plan`; it is fed only by `convergence_artifacts`
   (sealed contributor converge outputs) + `ConvergenceOutcome`
   (fixed dataclass keys) + `iteration_dir`. The MCP tool's
   `claude_proposal_yaml` flows to the ClaudeAdapter PROPOSE phase,
   not post-convergence. The ONE channel that already reaches seal
   time is `iteration_dir` itself (every artifact is a file there).
   Therefore the wiring reuses that channel: `_seal_converged_plan`
   reads an optional `convention-input.yaml` from `iteration_dir` —
   no new parameter threaded through `run_iteration` / the MCP tool.

2. **Schema** `consensus_mcp/schemas/converged_plan_convention.schema.json`
   (NEW; `schemas/` net-new). `empirical_status` enum
   `proven|pending|refuted|n/a` verbatim from
   docs/workflows/converged-plan-convention.md.

3. **Validator** `consensus_mcp/validators/validate_converged_plan.py`
   — structure + consequence ONLY. Enforces the consequence of the
   orchestrator-attested `falsifiable_from_artifacts` bool; does NOT
   classify the defect (no keyword heuristic). Result keys are
   deliberately neutral (`presence_ok`, `violations`, `hard_reject`,
   `hard_reject_reasons`, `gate_scope`); NO field asserts the
   hypothesis/safeguard is right.

4. **Recursive-trap defense (HIGHEST-ORDER).** Structural, not
   cosmetic: `test_validator_source_sets_no_correctness_state` greps
   the validator source for any `(is_)?(approved|correct|sound|ready
   |valid_hypothesis|hypothesis_correct|safeguard_adequate)` followed
   by `:`/`=` and fails if any exist. Every result carries the
   unconditional `GATE_SCOPE_DISCLAIMER`; `consensus_get_iteration_
   outcome` surfaces it as `convention_gate_scope` adjacent to the
   enforcement marker so a reader cannot see "passed" without it.

5. **Engine** `_seal_converged_plan` — ingests + validates +
   FAIL-CLOSED: a hard-reject raises `WorkflowError` BEFORE writing,
   so no `converged-plan.yaml` is sealed. On accept, the convention is
   sealed INTO `converged-plan.yaml` (same `write_text`, same hash)
   with `convention_schema_version`, `convention_violations`,
   `convention_gate`. Absent convention ⇒ `convention_schema_version:
   0` + `convention_gate.enforcement_status: doctrine-only`.

6. **Graduated strictness** `convergence.converged_plan_enforcement`
   (`off|warn|graduated|strict`, default `graduated`) in `config.py`
   default + legacy-synth + `validate()`. Hard-reject ONLY (i)
   declared safety/data-loss/bricking/irreversible risk class without
   a conforming decoupled safeguard, (ii) `empirical_status:proven`
   with no recorded `experiment_result`.

7. **Outcome reader** surfaces `enforcement` /
   `convention_gate_scope` / `convention_violations`; legacy/absent
   ⇒ `enforcement: doctrine-only` (NOT silently valid, NOT rejected).

## Verification evidence

- `consensus_mcp/tests/test_converged_plan_convention.py` — 30 NEW
  tests, all green; covers every `acceptance_gates_gating` item.
- Full suite: **956 passed, 1 skipped, 0 regressions** (the 30 new
  tests are in that count).

## Audit questions

- Q1 goal_satisfied: does the implementation satisfy the converged
  plan's deliverable + every acceptance gate?
- Q2: is the recursive-trap defense STRUCTURAL (the source-grep test
  + unconditional disclaimer surfaced adjacent to the pass marker),
  not cosmetic? Try to find a code path that derives correctness/
  approval state from the convention blocks.
- Q3: any blocking objection? Verify the fail-closed claim (no
  `converged-plan.yaml` on hard-reject) and the legacy-grandfather
  claim (iter-0043..v1.15.0 plans load, marked doctrine-only) against
  the embedded code, not from memory.
- State the differential/prior you reasoned from.
