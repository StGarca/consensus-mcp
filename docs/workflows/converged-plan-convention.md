# Converged-plan convention: falsification, independent safeguard, decisive experiment

Status: **authoring convention, doctrine-enforced AND
machine-enforced as of v1.15.1.** The v1.15.0 tag `4e81f9e` shipped
this convention as doctrine only (bundled skill + Workflow B audit,
zero engine code). v1.15.1 adds the seal-time gate — see "Machine
enforcement (shipped v1.15.1)" at the end.

Origin: `iteration-convergence-correctness-doctrine` (Workflow A
weighted-synthesis: claude + codex + gemini; no blocking
objections), derived from the ChilipadScreen i2c boot-loop
consensus-failure report (2026-05-15), where two clean
strict-majority convergences were both refuted on-device.

These blocks belong in `converged-plan.yaml` for any iteration
whose `type` is a defect/root-cause investigation. They are
**not** new voting mechanics — correctness discipline layered on
top of the existing strict-majority / weighted-synthesis rules.

---

## 1. `falsification` (required for defect/root-cause iterations)

```yaml
falsification:
  hypothesis: <the converged root cause, one sentence>
  falsifiable_from_artifacts: true|false
    # true  → refutable by reading the evidence already in the
    #         packet (code, logs, diffs). Its proof is a test you
    #         can add now; no external experiment required.
    # false → refutable ONLY by an external observation
    #         (device/runtime/integration/toolchain). This is the
    #         DEFINED CLASS: hardware/firmware state, environment/
    #         toolchain, concurrency/timing.
  discriminating_experiment: <the single external test that would
    refute the hypothesis — concrete enough to run as-is>
  refutation_observation: <the specific observable that, if seen,
    proves the hypothesis WRONG. Pre-specified. Not "we'll test
    it" — name the observable, e.g. "i2c_master_transmit still
    returns 0x103 at i2c.cpp:91 on a clean POWERON">
  empirical_status: proven | pending | refuted | n/a
    # n/a       → falsifiable_from_artifacts:true and the proving
    #             test is included in this iteration.
    # pending   → defined class, experiment named but not yet run.
    #             "fixed"/"shipped"/"root-cause-correct" language
    #             is FORBIDDEN while pending.
    # proven    → the experiment ran and did NOT produce the
    #             refutation_observation.
    # refuted   → the experiment ran and DID produce the
    #             refutation_observation. The hypothesis is dead;
    #             the iteration does NOT close on this root cause.
    #             This is the load-bearing terminal state — the
    #             ChilipadScreen report's iter-0012 and iter-0013
    #             both ended here, which is precisely why this
    #             convention exists. A refuted status MUST carry
    #             forward decisive_experiment_before_next_iteration.
```

**Anti-theater property (doctrine):** a falsification is real
only if `refutation_observation` is (1) pre-specified, (2) a
specific observable, and (3) for the defined class, EXTERNAL to
the reasoning that produced the hypothesis. External tests can't
be rationalized in the room — that is exactly why the device
refuted what code-reading could not.

## 2. `independent_safeguard` (required for the risk class)

Required when the defect is **safety-critical / data-loss /
bricking / irreversible-risk**.

```yaml
independent_safeguard:
  mechanism: <what it does>
  works_if_root_cause_wrong: true   # MUST be true
  why: <why it still protects even if the hypothesis is 100%
    false — answerable from the mechanism alone>
  ships_with_fix: true              # same change as the
                                    # hypothesized fix, not later
```

Auditable bar: **"would this safeguard still work if the root
cause were entirely different?"** If a reviewer cannot answer
*yes* from `mechanism` + `why` alone, it is a disguised bet on
the hypothesis, not an interlock, and does not satisfy the
requirement. Stopping the bleeding outranks perfecting the
diagnosis (field-proven: an independent boot-loop breaker
un-bricked a medical-safety device across two failed root-cause
iterations).

## 3. `decisive_experiment_before_next_iteration`

```yaml
decisive_experiment_before_next_iteration: <the single test that
  most cleanly partitions the remaining hypothesis space, to run
  BEFORE opening the next iteration> | null
  # null only when empirical_status: proven (or n/a) AND the fix
  # is confirmed.
```

The ChilipadScreen report's "Exp-4" (build the unmodified
reference under our toolchain) is the canonical example: one test
that cleanly separates "our firmware" from "the toolchain" and
"should precede any further consensus iteration."

---

## Worked example (abbreviated, from the report)

```yaml
falsification:
  hypothesis: "sdStorage_init shared-LDO disturbance perturbs the I2C/DSI rail"
  falsifiable_from_artifacts: false
  discriminating_experiment: "build flag skips sdStorage_init before display; capture boot-1 serial"
  refutation_observation: "first i2c_master_transmit still returns ESP_ERR_INVALID_STATE at i2c.cpp:91 with SD_MMC.begin() provably absent"
  empirical_status: pending     # the experiment was then run and DID
                                # produce the refutation_observation →
                                # terminal state becomes `refuted` (a
                                # legal enum value), NOT `proven`. The
                                # iteration does not close on this root
                                # cause; decisive_experiment carries fwd.
independent_safeguard:
  mechanism: "boot-loop breaker: on 2nd crashed boot, skip display and continue headless"
  works_if_root_cause_wrong: true
  why: "triggers on the crash symptom (reboot count), not on any I2C/LDO theory"
  ships_with_fix: true
decisive_experiment_before_next_iteration: "flash unmodified reference Drawing_board.ino under our pioarduino toolchain (Exp-4)"
```

The report's outcome is the lesson: the hypothesis was unanimous
and refuted; the **independent safeguard kept a medical device
serving anyway**; the decisive experiment was correctly named as
the next step instead of opening another speculative iteration.

---

## Machine enforcement (shipped v1.15.1)

The v1.15.0 named blocker is **closed**. Consult:
`iteration-converged-plan-machine-enforcement` (Workflow A
weighted-synthesis: claude + codex + gemini; shared-prior
self-check PASSED). The v1.15.0 recorded starting design
(`severity` + `consensus_gate.py`) was **partially refuted by
first-hand code-reading**: `consensus_mcp/validators/
consensus_gate.py` is the Phase-0 production-readiness gate (P0-V6),
the WRONG component. The shipped mechanism:

- **Schema:** `consensus_mcp/schemas/converged_plan_convention.schema.json`
  (machine contract; `empirical_status` enum identical to this doc).
- **Validator:** `consensus_mcp/validators/validate_converged_plan.py`
  — structure + consequence ONLY. It enforces the consequence of the
  orchestrator-attested `falsifiable_from_artifacts` bool; it does
  **not** classify the defect (no keyword heuristic — heuristics are
  the shared-prior trap this doctrine documents).
- **Provenance-by-citation:** the orchestrator authors the convention
  in `convention-input.yaml` in the iteration dir (the one channel
  that already reaches seal time). `_seal_converged_plan` validates it
  and seals it **INTO** `converged-plan.yaml` (same hash) with a
  required non-empty `cited_pass_ids` listing the contributor passes
  it synthesizes from. No loose untracked sidecar; no single-winner
  extraction.
- **Graduated strictness:** `convergence.converged_plan_enforcement`
  = `off|warn|graduated|strict` (default `graduated`). Hard-reject
  fail-closed ONLY for (i) operator-declared safety/data-loss/
  bricking/irreversible risk class missing a conforming
  root-cause-independent `independent_safeguard`, (ii)
  `empirical_status:proven` with no recorded `experiment_result`.
  Otherwise warn + annotate `convention_violations`.
- **Recursive-trap defense (the highest-order constraint — v1.15.0
  lesson 1 applied to our own gate):** a green gate must never become
  the new "convergence mistaken for correctness". The validator has
  zero code path deriving any approved/correct/ready/sound state from
  the blocks (pinned by a source-grep test), and every result stamps
  an unconditional `gate_scope` disclaimer surfaced by
  `consensus_get_iteration_outcome` adjacent to any pass marker: a
  pass means the required thinking was **recorded**, never that it is
  **true**. *"Would this safeguard still work if the root cause were
  entirely different?"* remains a human judgement.

**Backward compatibility:** plans without `convention_schema_version`
(this session's iter-0043 .. v1.15.0) still load through
`consensus_get_iteration_outcome`, explicitly marked
`enforcement: doctrine-only` — NOT silently valid, NOT rejected.

**Artifact truth:** the v1.15.0 tag `4e81f9e` is doctrine-only (zero
engine code). Machine enforcement exists only from the v1.15.1 tag
forward.

**Named blocker (need-evidence, deferred):** operator goal_packet
`defect_class`/`risk_class` declaration UX + an anti-gaming
cross-check on `falsifiable_from_artifacts` — ill-posed until the
shipped slice produces real usage data.
