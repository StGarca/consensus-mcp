# Converged-plan convention: falsification, independent safeguard, decisive experiment

Status: **authoring convention, doctrine-enforced.** Machine
validation (engine/validator) is a sequenced follow-up — see the
named blocker at the end.

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

## Named blocker — machine enforcement (sequenced follow-up)

Engine/validator enforcement of these blocks is **deferred** with
a concrete, file-verified blocker: there is no standalone
converged-plan schema; the engine reads/writes generic YAML plan
keys (`workflow_engine.py:505-525`,
`consensus_get_iteration_outcome.py:114-123`). Enforcement needs
its own schema-design consult plus a not-falsifiable-from-artifacts
classifier, or it becomes a rejected-goal_packet papercut.
Starting design for that follow-up (gemini's proposal in this
consult): a `severity` field on the goal packet + a
`consensus_gate.py` check that fails a critical-severity proposal
lacking a decoupled `independent_safeguard`. Until then this
convention is enforced by doctrine (the bundled consensus-workflow
skill, loaded every consult) and by Workflow B audit.
