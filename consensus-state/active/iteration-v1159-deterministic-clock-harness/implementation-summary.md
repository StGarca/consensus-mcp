# Workflow B audit target — v1.15.9: deterministic streaming-test harness

Executes `converged-plan.yaml` (Workflow A: claude+codex+gemini,
weighted-synthesis; shared-prior self-check PASSED — agreed
diagnosis, DISAGREED on mechanism; convention validator exit 0;
dogfoods the v1.15.1 convention incl. `release_all()`
independent_safeguard). Removes the v1.15.8 Q2(c)
`@_FLAKY_WINDOWS_CI` interim skip by making the harness
deterministic. Commit `da62d54` on branch `v1.15.9`.

## What changed (two files)

**`consensus_mcp/_dispatch_codex.py` — production seam (zero
default behavior change).** `_invoke_codex` gains a private,
keyword-only `*, _sleep=None`; `if _sleep is None: _sleep =
time.sleep`; the single poll-loop `time.sleep(poll_interval)`
(the only `time.sleep` in the module) is now `_sleep(...)`.
Default path is byte-identical to before. Chosen over
monkeypatching global `time.sleep` (gemini's proposal) because DI
has no global-state leak (project has a monkeypatch-pollution
memory); chosen over claude's "gate the clock on `now()`" because
the explicit seam is a robust happens-before, not a fragile
inference. Honors gemini's no-production-*behavior*-change in
substance.

**`consensus_mcp/tests/test_dispatch_codex_streaming.py` — harness
rewrite.** `_ControllableClock` → `_SyncClock` (one
`threading.Condition`; `now/advance/sleep/wait_runner_parked/
wait_for/wait_byte_due/notify/release_all`). `sleep()` is injected
as production `_sleep` so the poll loop has an explicit
happens-before with the driver. `_FakePipeReader` waits on the
clock (no real 5 ms poll); `StreamingFakeCodexPopen.poll/
terminate/kill` notify the clock so reader threads + driver wake
deterministically. Drivers: `_drive` (park-synced; terminal is
ALWAYS runner-thread death, never a mid-stream observable —
that wedges a not-yet-exited proc), `_drive_heartbeats` (lockstep
epoch handshake for cadence — production emits ≤1 hb/poll so the
runner must observe each boundary), `_drive_streaming` (wall
ceiling: per-step wait until a streamed line is PROCESSED so
`last_streamed_ts` is fresh and silence never pre-empts wall).
`_drive_clock_until_done` DELETED. `@_FLAKY_WINDOWS_CI` DELETED.
All 8 clock-driven tests migrated. `test_stderr_drain_prevents_
deadlock` keeps REAL stdout/stderr reader threads.

## Deadlock-free argument

One lock (the `Condition`). Every blocking wait re-checks an
exact predicate and has a guaranteed waker on every path:
advance → `notify_all`; runner re-park → `sleep()` `notify_all`
progress beat; proc exit/terminate/kill → `notify`; runner-thread
death (incl. exception) → wrapper `finally: clock.notify()`;
absolute `_CEILING` (20 s) → `release_all()` frees ALL waiters,
terminates the fake proc, fails LOUD with harness state (the
independent_safeguard — works even if the timing hypothesis is
wrong). `_REPOLL` (50 ms) bounds lost-wakeup recovery: a missed
notify self-heals in ~50 ms; correctness is the notify-exact
predicate re-check, so `_REPOLL` only bounds LATENCY and NEVER
affects pass/fail (this is the determinism criterion, not a
wall-clock budget).

## Verification (provisional-until-proven)

Streaming file 8/8 deterministic across 6 repeated runs
(0.14–0.36 s, no ceiling waits, no timeout). Full suite **968
passed / 1 skipped / 0 regressions** — identical to the v1.15.8
baseline; the 1 skip is the long-standing unrelated one (the four
`@_FLAKY_WINDOWS_CI` tests now RUN, no test lost). The
≥3-consecutive-green-Windows-CI gate (attempts-API verified) is
PENDING; v1.15.9 is NOT cut until it passes.

## TDD-caught + fixed during implementation (deterministic, not flakes)

1. `_FakePipeReader` returned EOF the instant `_exited` set,
   dropping already-due buffered lines (a real pipe yields them).
2. `_drive` early-out on a mid-stream observable left a
   not-yet-advanced proc spinning forever post-`release_all`.
3. operator-abort fires on iteration 1 (signal pre-written)
   BEFORE first park → startup wait must be parked-OR-dead.
4. wall-ceiling vs pre-first-line silence race on a coarse jump
   → dedicated `_drive_streaming` (per-step streamed-line sync).
5. lost-wakeup bound to `_CEILING` (20 s, compounded > 60 s)
   → `_REPOLL` 50 ms self-heal.

## Pass-2: codex-rev-001 integrated (governance/scope)

Pass-1: gemini `goal_satisfied=true`, 0 findings (explicitly
cleared Q1 seam-is-safe/zero-behavior-change, Q2 deterministic/
race-resolved, Q3 H3-consistency); codex 0 blocking,
`goal_satisfied=false` solely on **codex-rev-001 (high,
governance — NOT correctness; codex affirms "substantively
deterministic")**: the sealed `goal_packet` listed
`_dispatch_codex.py` in `forbidden_files` while the design needs
the `_sleep` seam there.

**Integrated, not dismissed.** Verified the authorization chain:
the goal_packet's own `non_goals` pre-authorized "a single
justified+bounded observability seam in `_invoke_codex` (Q1
decides)"; Q1 weighted-synthesis ADOPTED that seam and
`converged-plan.yaml deliverable.files` lists
`consensus_mcp/_dispatch_codex.py`. The round-1 `forbidden_files`
entry was an un-reconciled pre-convergence default that
contradicted the goal_packet's own carve-out. Fix:
`_dispatch_codex.py` moved `forbidden_files` → `allowed_files`
with an inline note citing the Q1 + deliverable authorization.
No code changed — this is a scope-record correction so the
machine-readable authorization matches the ratified converged
decision. Pass-2 re-dispatched on the corrected scope.

## Pass-3: codex-rev-001 (BLOCKING, correctness) integrated

Pass-2: codex-rev-001 governance (scope) RESOLVED; gemini clean
again (`goal_satisfied=true`, 0 findings). But codex pass-2
raised a NEW **blocking** `codex-rev-001` (correctness, Q2):
`_drive` and `_drive_streaming` IGNORED the per-round/per-step
`wait_for` return value — on a `_CEILING` timeout they looped to
`max_rounds`/`max_steps` (≤ 64 × 20 s ≈ 21 min) before
`release_all()`, **contradicting the claimed "ceiling → fast loud
fail" deadlock-free invariant**. Verified correct against the
code (returns were dropped at :432 / :522). gemini cleared Q2
both passes reasoning about design intent; codex traced actual
control flow — convergence ≠ correctness, the code-tracer caught
the claim-vs-implementation gap (same pattern as v1.15.8).

**Integrated (codex's recommendation):** every per-round /
per-step `wait_for` return is now checked; `False` →
`release_all()` + join + raise IMMEDIATELY (fast loud fail);
`max_rounds`/`max_steps` exhaustion is a HARD fail even if
`release_all()` later lets the thread limp out. The
independent_safeguard is now actually wired into every wait, so
the deadlock-free invariant is true (a genuine wedge fails in
≤ `_CEILING` = 20 s, not ≤ 21 min). `_drive_heartbeats` already
checked its returns — unchanged. Re-verified: streaming 8/8
deterministic ×6 (sub-0.3 s — the fast-fail never false-triggers
under correct notify-driven operation, wakeups are sub-ms);
full suite re-run. Pass-3 re-dispatched.

## Pass-4: codex pass-3 findings (coverage fidelity) integrated

Pass-3: codex confirmed the pass-2 deadlock-invariant blocking
RESOLVED (not re-raised); gemini clean a 3rd time. codex pass-3
raised two NEW valid findings reasoning from its "H2 coverage
must be preserved" prior (gemini cleared on determinism/design
all 3 passes — complementary, not redundant; convergence ≠
correctness):
- **codex-rev-001 (blocking):** `test_operator_abort_signal_
  file_triggers_abort` never asserted the SIGTERM/terminate
  side-effect — a `_terminate_process_tree` regression would
  pass while leaking a live process. Integrated:
  `StreamingFakeCodexPopen._terminated` flag (set in terminate/
  kill/send_signal), `factory.instances` records created procs,
  test now asserts `factory.instances[0]._terminated`.
- **codex-rev-002 (high):** my rewrite dropped the original's
  `time.sleep(0.1)` that guaranteed the 2 lines were consumed,
  so `test_heartbeat_silence_triggers_abort` could pass via
  pre-first-line startup silence instead of the intended
  post-stream path. Integrated: new `_drive_post_stream` helper
  (phase-1 lockstep until N lines PROCESSED, then phase-2
  `_drive`); test asserts `last_streamed_line_age_seconds is not
  None` (proves the post-stream branch).

Re-verified: streaming 8/8 deterministic ×6 (sub-0.3 s); full
suite re-run. Pass-4 re-dispatched (codex must confirm pass-3
codex-rev-001 resolved). The multi-pass audit has now caught 4
substantive defects (governance scope; deadlock invariant;
SIGTERM coverage; post-stream coverage) — none would have been
caught by self-certification; provisional-until-proven holds the
cut until codex returns 0 blocking AND the ≥3-green gate passes.

## Pass-5: codex pass-4 findings integrated (0 blocking; safeguard/coverage hardening)

Pass-4: codex 0 blocking (pass-3 codex-rev-001/002 RESOLVED);
gemini clean a 4th time. codex pass-4 raised three valid
non-blocking findings (H1–H3 prior; gemini cleared on
determinism all 4 — complementary):
- **codex-rev-001 (high):** `release_all()` only flips
  `_released`/notifies — it did NOT terminate the fake proc, so a
  pre-exit harness timeout left the daemon runner SPINNING after
  the driver raised (the CHANGELOG over-claimed "release_all
  terminates the fake proc" — claim-vs-code). Integrated:
  `_SyncClock.is_released()`; `StreamingFakeCodexPopen.poll()`
  self-terminates when released (single teardown signal owned by
  `release_all`, observed where poll() already runs — no second
  coordination surface). The runner loop now actually EXITS.
- **codex-rev-002 (high):** operator-abort test wrote the signal
  before the runner was provably mid-run (same class as pass-3's
  silence fix, for the operator path). Integrated: factored
  `_advance_until_streamed`; the test now processes the "working"
  line + parks mid-run BEFORE writing the signal.
- **codex-rev-003 (medium):** verified correct — the stdout
  reader thread writes the log event without notifying the clock
  and the runner's `sleep()`-beat is not ordered after it, so
  event-count waits fell back to `_REPOLL` (contradicting the
  "_REPOLL never on the nominal path" claim). Integrated:
  `_FakePipeReader.readline()` notifies the clock on entry —
  production's tight `iter(readline, b"")` loop calls it
  happens-AFTER processing the prior line into an event, giving a
  TRUE happens-before waker (so `_REPOLL` is again only a
  lost-wakeup net).

Re-verified: streaming 8/8 deterministic ×6 (sub-0.21 s); full
suite re-run. Pass-5 re-dispatched. Audit trajectory: blocking
(pass-2) → blocking (pass-3) → 0-blocking high/high/med (pass-4)
→ converging. 7 substantive defects caught + integrated across
4 passes — none would survive to a release; provisional-until-
proven holds the cut until codex `goal_satisfied=true` (or only
trivial) AND the ≥3-green Windows-CI gate passes.

## Pass-6: codex pass-5 codex-rev-001 (BLOCKING, heartbeat gate) integrated

Pass-5: codex 0 prior blocking re-raised (pass-4 all RESOLVED);
gemini clean 5th. codex pass-5 raised one NEW blocking
codex-rev-001 (verified correct): the lockstep `_drive_heartbeats`
advanced exactly `interval` per step with one poll/step, so it
only proved "one heartbeat per step" — a regression emitting a
heartbeat EVERY poll (or with a tiny threshold) still produced
3 and passed. The pre-rewrite test caught that via 100
sub-interval polls + a loose count window; the rewrite lost the
interval-GATE coverage. Integrated (codex's rec): each round now
advances a SUB-interval (½) first and asserts NO new heartbeat
(gate held), then crosses the boundary and asserts EXACTLY ONE —
so emit-every-poll / wrong-threshold regressions FAIL. Traced
against the production `now - last_heartbeat >= interval` math:
deterministic (no emit at +15s, one at +30s, ×3); streaming 8/8
×6 (sub-0.2 s). Pass-6 re-dispatched.

Audit ledger (8 substantive defects, all integrated): governance
scope; deadlock-invariant claim-vs-code; SIGTERM coverage;
post-stream silence coverage; release_all-doesn't-terminate;
operator mid-run determinism; reader-event waker; heartbeat
interval-gate coverage. Each is a real gap the deterministic
rewrite introduced vs. the old real-sleep test's implicit
coverage — exactly what a multi-pass audit on a concurrency
rewrite exists to surface. Provisional-until-proven holds the
cut until codex `goal_satisfied=true` (or only trivial) AND the
≥3-green Windows-CI gate.

## Pass-8: codex pass-7 codex-rev-001 (BLOCKING) integrated

Pass-7: codex-v1159-wfb-6 (stderr-drain) RESOLVED — the bounded
fix + scope-consult verdict accepted (not re-raised); gemini
clean 7th. codex pass-7 new blocking codex-rev-001 on
`_drive_heartbeats` (with a patch proposal): (a) startup
`wait_runner_parked` used a bare `assert`, not the
`release_all()`+join loud-fail path the other drivers use
(teardown-bypass class of pass-2/4); (b) the half-interval
no-emit check misses a "wrong threshold" regression (emit at
20s for a 30s interval slips a 15s check). Both verified valid.

Integrated codex's patch WITH a refinement: codex's exact-
boundary `epsilon` (advance to land EXACTLY on `interval`) had a
double-rounding edge (`now-last_hb >= interval` flippable by
~1e-10) AND — found via TDD — `epsilon` (0.001) was SMALLER than
`poll_interval` (0.01), so the boundary advance never triggered
another production poll (the runner sleeps `poll_interval`
between polls) → 0 heartbeats. Fix: startup now uses the
release_all()+join path; `gap = max(interval/1000,
poll_interval*5)` (poll-safe AND ≫ float error), check no-emit
at `interval-gap` (tight: catches emit-at-20s), cross by
`2*gap`. `_drive_heartbeats` now takes `poll_interval`.
Re-verified deterministic 8/8 ×6 (sub-0.2s). Pass-8 dispatched.

Audit ledger: 9 substantive defects integrated over 7 passes +
1 scope-adjudication consult. The heartbeat driver specifically
hardened across pass-5 (gate exists) → pass-7 (gate strength +
startup consistency + poll-safety). Convergence is on
increasingly narrow, increasingly rigorous test-fidelity points
— exactly what a multi-pass audit on a concurrency rewrite
exists to extract.

## Audit questions
- Q1: Is the `_sleep=` seam genuinely zero-behavior-change for
  every existing production caller (grep callers; it is
  keyword-only + defaulted)? Is DI-over-monkeypatch the right
  call vs gemini's global-`time.sleep` proposal?
- Q2: Is the harness provably deadlock-free? Find ANY path where
  a waiter has no guaranteed waker, or where `_REPOLL`/`_CEILING`
  masks a real hang into a pass, or where correctness depends on
  `_REPOLL`/`_CEILING` (it must not).
- Q3: Do the four formerly-skipped tests still assert the SAME
  durable outcomes (heartbeat cadence/monotonic age;
  watchdog_silence; operator_signal_file + file deletion +
  SIGTERM; wall_time_hard_ceiling)? Any weakened assertion?
- Q4: Is `release_all()` a true root-cause-independent safeguard
  (loud fast fail, never a silent pass / wedged CI)? Scope:
  v1.15.8 immutable tag still ships the skip; fix attributed
  only to the v1.15.9 commit/tag — correct?
- Q5: Blocking objections; state the differential/prior.
