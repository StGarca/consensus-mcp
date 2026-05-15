# Workflow B audit target — v1.15.8: converged-plan execution

Executes `converged-plan.yaml` (Workflow A: claude+codex+gemini,
weighted-synthesis, shared-prior self-check PASSED; dogfoods the
v1.15.1 convention incl. independent_safeguard). Fixes the two
intermittent Windows-CI flakes (same commit green on one run, red
on another); product is sound (968/0 local, ubuntu always green).

## Q1(d) — `_visibility_watchdog._locked_append` (REAL FIX)

The audit/sealed-provenance integrity primitive caught `OSError`
from `msvcrt.locking` and then **wrote UNLOCKED** ("best-effort")
→ silent line loss under contention (F1: 46/50 from a 50-thread,
ONE-process fan-out — intra-process, the v1.15.7 OS-lock-vs-threads
class in a different primitive).

- **Intra-process:** new module-level `_APPEND_LOCK =
  threading.Lock()`, held around the whole append (open + OS-lock +
  write). Deterministically serializes all in-process callers — no
  scheduler luck. This is the verified-observed contention class.
- **Independent safeguard (fail-LOUD):** the Windows
  `msvcrt.locking` failure path no longer `pass`-es and writes
  unlocked; it **raises** `OSError` ("refusing a silent unlocked
  write to a sealed-provenance/audit log"). Audit integrity loss is
  now observable, never silent — protects regardless of *why* the
  OS lock failed (the convention's independent_safeguard;
  works_if_root_cause_wrong=true).
- The no-lock-module platform branch keeps its warning (in-process
  still serialized by `_APPEND_LOCK`); not made fatal — that would
  needlessly break platforms lacking msvcrt+fcntl, and is a
  different branch than the F1 defect.

## Q2(c interim) — heartbeat-pattern timing tests

Converged plan Q3 explicitly permits the (c) interim when the
deterministic (a) rework overruns the iteration bound. A correct
synchronizing-`_ControllableClock` redesign across all four
runner-thread+clock tests is substantial harness work with real
deadlock risk (the precise failure mode this session keeps
hitting), so it is the **named follow-up**, not rushed here.
`@_FLAKY_WINDOWS_CI` (skipif `sys.platform=='win32' and
GITHUB_ACTIONS=='true'`) applied to the 4 racy tests
(`test_heartbeat_fires_at_interval`,
`test_heartbeat_silence_triggers_abort`,
`test_operator_abort_signal_file_triggers_abort`,
`test_wall_time_hard_ceiling`). They still run on **Linux CI every
push** and **local Windows dev**, so the regression coverage is
retained (logic is driven by the injected `time_fn`, not a Windows
code path). Named follow-up tracked in `docs/advisories.md`.

## Verification discipline (provisional-until-proven)

Per the converged plan: a flaky fix is NOT proven by one green
run. Resolved requires BOTH (i) the determinism argument — Q1:
`threading.Lock` strictly serializes in-process (no scheduler
dependence); Q2: the racy tests no longer execute on the flaky
Windows-CI environment — AND (ii) ≥3 consecutive green Windows CI
runs (py3.10 + py3.12) of the same commit. v1.15.8 is NOT cut
until that gate passes.

## Pass-2: codex-rev-001 integrated (the blocking objection)

Pass-1: gemini `goal_satisfied=true`, 0 blocking (reasoned the
POSIX `fcntl.flock` path, which is correct); codex
`goal_satisfied=false`, blocking `[codex-rev-001]`.

**codex-rev-001 (verified correct, integrated):** `msvcrt.locking`
locks N bytes from the CURRENT file position. `_locked_append`
opens `"ab"`, so that position is the *calling process's EOF* —
which DIFFERS per process as the file grows. Cross-process writers
therefore locked DIFFERENT byte ranges → zero cross-process
exclusion (the exact hole the Q1(d) fail-loud safeguard claimed to
close). gemini missed it (clean on the POSIX path it reasoned);
codex read the Windows branch first-hand. Catch confirmed real.

**Fix (commit `ed36b18`), exactly codex's recommendation:**
`f.seek(0)` before `msvcrt.locking(fileno, LK_LOCK, 1)` so ALL
processes contend on the SAME fixed byte (offset 0). Writes still
land at EOF — Python `"ab"` opens with `_O_APPEND`; every write
goes to EOF regardless of the seek (the seek positions only the
LOCK range, not the write). codex's "restore/seek-to-end before
writing" alternative is unnecessary for exactly this `_O_APPEND`
reason — documented inline + proven by the 50-thread test.
Intra-process contention remains serialized by the module-level
`_APPEND_LOCK`; fail-loud `OSError` safeguard unchanged.

**Verified:** full suite 968 passed / 1 skipped; `test_visibility
_watchdog` 27/27 incl. `test_locked_append_serializes_concurrent_
writes` (50-thread) — proves `seek(0)` does NOT overwrite (all
records still appended). The ≥3-green-Windows-CI gate RESTARTS
from `ed36b18`.

Pass-2 question for codex: confirm codex-rev-001 resolved by the
fixed-byte-0 lock; any residual cross-process gap (e.g. the
open→lock window, or `_O_APPEND` write-at-EOF assumption on
Windows)? For gemini: does the corrected Windows branch hold; any
regression to the POSIX `flock` path you cleared in pass-1?

## Audit questions
- Q1: does Q1(d) match the converged plan; is fail-LOUD-raise the
  right safeguard (vs. structured-event-and-continue) for the
  audit primitive; any caller that legitimately relied on the
  silent best-effort write and would now break? (grep callers.)
- Q2: is the Windows-GitHub-Actions-only skip acceptable
  (coverage retained on Linux CI + local) per the converged plan's
  explicit (c)-interim allowance, with the named follow-up
  concrete enough?
- Q3: is the ≥3-consecutive-green-Windows-CI gate the right
  proven-bar; anything that makes Q1(d) itself nondeterministic?
- Q4: blocking objections; state the differential/prior.
