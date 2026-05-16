# Workflow B audit target — v1.15.10: stderr-pipe backpressure modeling

Executes `converged-plan.yaml` (Workflow A: claude+codex+gemini,
weighted-synthesis; gemini r1 needed one retry — large embedded
prompt stalled once, succeeded on retry per the high-priority
gemini-skip doctrine; shared-prior self-check PASSED — distinct
differentials, divergent representations; convention validator
exit 0; `goal_packet.allowed_files` reconciled to include
`_dispatch_codex.py` BEFORE this audit per the v1.15.9
codex-rev-001 governance lesson).

## What changed (two files)

**`consensus_mcp/_dispatch_codex.py` — wire the test seam (zero
default behaviour change).** `_invoke_codex` already had a
declared-but-UNWIRED private keyword-only `_drain_stderr: bool =
True`. Now: `if _drain_stderr: t_stderr.start()`, and the
post-exit `t_stderr.join`/`is_alive` are guarded by the same gate
(joining a never-started thread raises `RuntimeError`). Default
`True` ⇒ production starts+joins the stderr reader exactly as
before — byte-identical. Only the backpressure mutant sets it
`False`. Mirrors the v1.15.9 `_sleep=` precedent exactly.

**`consensus_mcp/tests/test_dispatch_codex_streaming.py` —
backpressure model + nominal/mutant gate.** `_FakePipeReader`
gained a finite `capacity` (`deque(maxlen=)`), blocking `write()`
(parks on the `_SyncClock` Condition when full, notifies on
space), `_writer_blocked`; `StreamingFakeCodexPopen._service_
pipes()` is the producer; `poll()` refuses exit while any pipe
`_writer_blocked` (even past `exit_at`), with `release_all()`
still the FIRST override. `test_stderr_drain_prevents_deadlock`
rewritten: Case 1 (`drain_stderr=True`, `stderr_capacity=2`, 5
lines) — buffer drained empty, proc exits, `out=={"ok": true}`;
Case 2 mutant (`drain_stderr=False`) — writer blocks on the full
pipe, proc cannot exit, `_drive`'s safeguard catches it
(`pytest.raises(AssertionError, match="release_all fired")`),
then DURABLE proof: stderr buffer stayed SATURATED at capacity
(vs. nominal's empty) and no success output.

## Recovery work (this session, honest record)

The implementation existed UNCOMMITTED but was (a) syntactically
CORRUPTED — a formatter had rewritten `\n` inside ~9 string/byte
literals into real newlines (every one a `SyntaxError`; the file
did not parse), and (b) functionally INCOMPLETE — the
`_drain_stderr` seam was declared but `t_stderr.start()` was
unconditional, so the mutant could not actually disable the
drain. Both fixed deliberately (verify-before-complete): every
corruption found via authoritative `ast.parse` (not heuristics —
the file legitimately uses multi-line implicit string concat, so
quote-scanning false-positives), seam wired, then the mutant
assertions corrected from transient `_writer_blocked`
(release_all clears it) / `_schedule_idx<len` (release advances
it) to the DURABLE discriminator (buffer saturated vs. drained +
no output + `release_all fired`).

## Verification (provisional-until-proven)

Streaming file **8/8, 5/5 deterministic**. Full suite: pending
(running). Per the converged gate, still owed: ≥10 targeted +
≥3 full-module local repeats; the production-side mutant (skip
`t_stderr.start()` for the NOMINAL test must fail
deterministically); ≥3 distinct green Windows-CI attempts of the
release commit (attempts-API verified).

## Pass-2: codex-v11510-wfb-1 codex-rev-001 (BLOCKING) integrated

Pass-1: gemini `goal_satisfied=true`, 0 blocking; codex
`goal_satisfied=false`, blocking codex-rev-001 (the escalated
form of my flagged Q3): the mutant gate proved the deadlock by
waiting for `_drive` to hit the global `_CEILING` (~20s) before
`release_all()` — the proof DEPENDED on the safety timeout
(slow + a disguised hang), violating the deterministic-and-fast
goal. gemini cleared it (model-correct); codex traced that the
PROOF mechanism was the timeout — complementary priors (the
recurring v1.15.x pattern).

Integrated codex's exact recommendation: the mutant now proves
the deadlock by DETERMINISTICALLY OBSERVING the backpressure
state — `clock.wait_for(buffer saturated AND _writer_blocked)`,
woken by `write()`'s notify the instant it blocks (all stderr is
due at t=0 → first poll() blocks; no clock advance, no `_drive`,
no `_CEILING`-as-proof). `release_all()` is now pure TEARDOWN
with an explicit `< _CEILING` prompt-unwind assertion. A TDD
race (the runner creates the proc in its thread → `instances[0]`
was indexed too early; the old `_drive` had masked it) was
caught and fixed by guarding the index inside the predicate +
asserting `deadlocked` before indexing.

Result: mutant test ~50 ms (was ~20 s); streaming 8/8, 6/6
deterministic; ≥12 targeted runs all green ~50 ms (codex's
≥10-targeted bar passed, now genuinely fast). The flagged Q3
20-second concern is fully eliminated — the proof is the
observation, not the timeout. Pass-2 re-dispatched.

## Pass-3: codex pass-2 (1 blocking + 2 high) integrated — backpressure redesign

Pass-2: pass-1 blocking (timeout-driven proof) RESOLVED; gemini
clean. codex pass-2 raised 3 valid interrelated findings:
- **codex-rev-001 (blocking):** my pass-1 fix introduced a
  semantic inversion — the mutant "passed by OBSERVING the
  deadlock" instead of demonstrating the clean-exit contract
  FAILS deterministically under no-drain. The gate must prove the
  test has teeth (mutation → deterministic failure).
- **codex-rev-002 (high):** `deque(maxlen=)` is LINE-count
  bounded; the converged plan explicitly specified BYTE capacity
  (a real OS pipe is ~64 KB byte-bounded).
- **codex-rev-003 (high):** `write()` blocked INSIDE `poll()` →
  the runner parked inside poll(), so the NOMINAL path could
  still fall back to `_CEILING` if production drain regressed.

Integrated as one coherent redesign (codex-rev-003 is the root
enabling the others):
- `_FakePipeReader` is now BYTE-capacity (`_buffered_bytes`,
  capacity in bytes; `None`=unbounded → other 7 tests unchanged).
- `write()` is NON-BLOCKING — returns True (buffered) / False
  (would overflow → backpressure, no append, cursor not
  advanced). `_service_pipes()` breaks on False; `poll()` returns
  None while `_writer_blocked`; the runner returns from poll()
  and parks NORMALLY in clock.sleep — NEVER inside poll().
- The test is now parametrized `(drain=True→clean)` /
  `(drain=False→must-fail)`: ONE clean-exit contract asserted
  both ways. Mutant fast-detects byte-backpressure via a
  notify-driven `wait_for` (ms, not `_CEILING`/`_drive`), then
  ASSERTS the contract FAILED (`"out" not in holder`, proc not
  exited) — failure is the assertion, not a deadlock observation;
  `release_all()` is teardown with a `< _CEILING` prompt-unwind
  proof. Gate has teeth both directions (a drain regression fails
  the nominal case).

Verified: parses; streaming 9/9 (test split to 2 params), 5/5
deterministic, sub-0.25 s (no _CEILING dependence anywhere); full
suite re-run. Code changed substantially → BOTH peers
re-dispatched pass-3 (gemini's pass-2-clean was on the old
design; must re-confirm on the redesign).

## Audit questions
- Q1: Is the backpressure model deadlock-free — does EVERY
  `write()`/`readline()`/`poll()` wait have a guaranteed waker
  on every path (buffer-full, reader-consume, proc-exit,
  release_all, _CEILING)? Any path where the producer blocks
  with no waker?
- Q2: Is the `_drain_stderr` seam genuinely zero-behaviour-change
  for all real callers (grep; default True, keyword-only,
  guarded start+join+is_alive)? Is the v1.15.9 `_sleep=` parallel
  exact?
- Q3: The mutant Case 2 takes ~20s (release_all catches the
  deadlock at the global `_CEILING`=20s — bounded + deterministic
  + loud, the converged plan explicitly ratified the
  `_drive`-ceiling→release_all catch path; NOT a hang). Is the
  20s cost acceptable, or should the mutant scenario take a
  tighter per-scenario ceiling (harness-API scope)? State the
  trade-off; this is the one flagged design-quality call.
- Q4: Do the corrected DURABLE assertions actually prove the
  backpressure deadlock (buffer saturated vs nominal-empty + no
  output + release_all-fired), and would they FAIL on the
  obvious regressions (no backpressure / writer never blocks /
  proc exits anyway)?
- Q5: Blocking objections; state the differential/prior.
