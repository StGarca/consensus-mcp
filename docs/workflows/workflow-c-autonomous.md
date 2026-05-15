# Workflow C — autonomous-execute

**Status (as of v1.15.2):** Contract shipped (v1.14.4). The
multi-iteration engine is **UNIMPLEMENTED as of v1.15.2; no
committed target version.** The earlier "v1.15.0" forward-reference
came due
unfulfilled (v1.15.0 = convergence-correctness doctrine; v1.15.1 =
converged-plan machine-enforcement; v1.15.2 = gemini-dispatch fix —
none shipped the Workflow C engine) and was corrected in v1.15.3
rather than re-promised against another version. This doc is the
single source of truth for status.

Workflow C runs consensus iterations to completion **without
operator-in-the-loop**, auto-approving emergent scope items if they
fall within an operator-pre-declared `autonomy_contract`. Designed
for overnight runs and operator-unavailable windows.

## What v1.14.4 ships

- Letter aliases (A/B/C) replace numeric (3/4) as canonical operator
  vocabulary. Numeric aliases stay accepted for one cycle with
  `DeprecationWarning`.
- `WORKFLOW_AUTONOMOUS_EXECUTE = "autonomous-execute"` constant in
  `consensus_mcp/config.py`.
- `consensus_mcp/validators/scope_check.py` gains
  `validate_autonomy_contract()` + `check_autonomy_scope()` functions
  for use by the engine (and by operators authoring contracts).
- Workflow C requires exactly 3 contributors (claude + codex + gemini)
  enforced at config-load.
- `workflow_engine.py` recognizes Workflow C and raises a clear
  `NotImplementedError` (stating it is unimplemented as of v1.15.2,
  no committed target) when an operator tries to RUN one. Operators
  can stage and validate Workflow C goal_packets; execution is
  gated until the engine ships.

## What the engine will add (named blocker)

The multi-iteration auto-execution loop. Specifically:

- Cross-platform interrupt-file watching (Windows `ReadDirectoryChangesW`
  vs Unix `select`/`poll` differences need validation).
- Integration tests with real peer dispatches (cost: dispatcher
  latency × N iterations per test run).
- Resume-after-halt semantics (operator returns, hits a parked item,
  approves it; how does the loop pick up?).
- Autonomy-ledger replay for failure recovery.

## What's deferred (no committed version)

Project-level `.consensus/autonomous-policy.yaml` as a default that
goal_packets can override per-run. Deferred until empirical evidence
operators want it across multiple Workflow C runs (we have zero runs
today; designing for hypothetical reuse is premature). No target
version is committed — status tracked here, not promised against a
version number (the v1.15.3 currency hot-patch removed all such
forward-references).

## Authoring a Workflow C goal_packet

Add an `autonomy_contract` block to your goal_packet:

```yaml
schema_version: 1
pilot_id: my-overnight-run
workflow_mode: autonomous-execute  # or alias "C"
contributors:
  enabled: [claude, codex, gemini]  # all 3 required for Workflow C

autonomy_contract:
  max_iterations: 10
  max_wall_clock_minutes: 240
  allowed_file_patterns:
    - "consensus_mcp/contributors/**"
    - "consensus_mcp/tests/test_contributors.py"
    - "docs/**/*.md"
  forbidden_file_patterns:
    - ".github/**"
    - "pyproject.toml"
    - "consensus_mcp/_dispatch_*.py"
  # Optional: opt OUT of specific halt conditions for less-safe runs
  # (default is wide-by-default; all halt conditions active)
  skip_halt_on:
    # - test_suite_regression
    # - schema_change_proposed
```

## Halt conditions (wide-by-default)

Every Workflow C iteration boundary checks for these conditions; ANY
one halts the run and surfaces to operator. Operator opts OUT per-run
via `autonomy_contract.skip_halt_on`.

| Halt condition | Triggered when |
|---|---|
| `blocking_objection` | Any peer raises a blocking_objection |
| `test_suite_regression` | New test failures vs baseline |
| `schema_change_proposed` | config.py / goal_packet schema / sealed-artifact schemas modified |
| `max_iterations_exceeded` | Iteration count > `autonomy_contract.max_iterations` |
| `max_wall_clock_minutes_exceeded` | Wall-clock > `autonomy_contract.max_wall_clock_minutes` |
| `convergence_failure_after_n_rounds` | Convergence fails after N rounds (default 3) |
| `reviewer_dispatch_permanent_failure` | Peer dispatch fails after 3 retries |
| `files_outside_allowed_patterns` | Proposed scope touches files outside allowed_file_patterns |
| `files_in_forbidden_patterns` | Proposed scope touches files in forbidden_file_patterns |
| `operator_interrupt_file_present` | `.consensus/interrupt` file marker exists |
| `reviewer_explicit_recommend_operator_review` | A peer's sealed packet has `recommend_operator_review: true` |

## Scope-check decisions

`check_autonomy_scope(proposed_files, contract)` returns one of:

- `approved` — every file is within `allowed_file_patterns` AND none
  in `forbidden_file_patterns`. Auto-proceed.
- `parked` — at least one file is OUTSIDE allowed_file_patterns but
  none are forbidden. Item parked for operator review when they
  return. Other (in-bounds) work continues.
- `halt` — at least one file is in `forbidden_file_patterns`. Hard
  stop; operator must review before any further autonomous action.

## Operator interrupt mechanism

To halt a running Workflow C from outside the session, create the
file `.consensus/interrupt` in the repo root. The engine (once
implemented — see Status above) checks for this file at every
iteration boundary
and halts cleanly if present, deletes the file, and surfaces a
"halted by operator interrupt" status in the audit log.

## Audit log

Workflow C writes to
`consensus-state/autonomous-runs/<run-id>/log.jsonl` with one entry
per iteration boundary:

```json
{
  "schema_version": 1,
  "iteration_id": "...",
  "started_utc": "...",
  "completed_utc": "...",
  "halt_check_results": {...},
  "scope_check_decision": "approved" | "parked" | "halt",
  "files_touched": [...],
  "next_iteration_id": "..." | null
}
```

When the operator returns, the audit log tells the complete story:
which iterations ran, which halted and why, which scope items were
parked.

## Why ONLY 3-AI contributors?

Autonomous mode means "no operator-in-the-loop." The wide cross-AI
safety net is mandatory by default (Workflow C requires exactly 3
contributors, enforced at config-load). A future relaxation
(explicit operator opt-in, e.g. 2-AI Workflow C with a brief safety
warning) is possible but is NOT committed to any version — when the
engine lands it ships safety-first.

## Provenance

- iter-workflow-abc-introduce convergence (workflow #4 weighted-
  synthesis across claude + codex + gemini, no blocking objections).
- See `consensus-state/active/iteration-workflow-abc-introduce/`
  for the full converged plan; archived snapshot under
  `consensus-state-snapshots` branch.
