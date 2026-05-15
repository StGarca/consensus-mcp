# Fix response — Workflow B pass-3 ((A) primitive corrected)

## Why pass-3 exists (CI refuted the pass-2-audited (A))

Pass-2 (corrected premise): codex + gemini BOTH goal_satisfied=true,
0 blocking, explicitly endorsed **A+C** with (A) =
`_log_dispatch` → `_visibility_watchdog._locked_append`.

The real multi-platform CI then refuted the *implementation*:
- windows-py3.10: ✓ FIXED (A+C worked there)
- ubuntu ×3, windows-py3.11: ✓
- **windows-py3.12: ✗ REGRESSED** — `test_heartbeat_fires_at_
  interval` "runner did not finish" (`th.is_alive()` True).

Root cause: `_locked_append` uses `msvcrt.locking(LK_LOCK)` /
`fcntl.flock` — a **BLOCKING, cross-PROCESS** lock. The streaming
dispatch concurrency is **intra-process**: main thread + stdout +
stderr reader threads of ONE dispatcher all call `_log_dispatch`.
Contending that blocking OS lock across the *same* process's
threads on Windows stalled the runner thread. The audit endorsed
the *concept* (atomic append); neither peer caught that the chosen
primitive deadlocks same-process threads — **CI-as-oracle did**,
which is exactly why audit+local-green is necessary but not
sufficient.

## Corrected (A) (this pass)

`_dispatch_base` now uses a module-level `threading.Lock`
(`_DISPATCH_LOG_LOCK`) guarding a plain text append — the correct,
deadlock-free primitive for intra-process main+reader-thread
concurrency. No blocking OS syscall. (C) unchanged (defensive
`_read_log_events` json skip — belt-and-suspenders).

**Scope honesty:** the observed defect (windows-py3.10 torn line)
and the regression (py3.12 stall) are both INTRA-process; the
`threading.Lock` resolves exactly that. True cross-PROCESS
serialization of a shared `dispatch-log.jsonl` under *parallel
dispatcher processes* (e.g. Workflow-A round-1 codex+gemini) is a
separate, **unobserved** concern — deliberately NOT solved here
with a blocking OS lock (that is precisely what just regressed).
Named explicitly in code + CHANGELOG rather than over-engineered.

## Audit questions (pass-3)

- Q1: is the `threading.Lock` the correct primitive for the
  verified intra-process concurrency, with no deadlock/stall
  hazard (single non-reentrant lock; no nested acquisition;
  released via `with`)?
- Q2: is naming cross-process serialization an explicit
  out-of-scope follow-up acceptable (it is unobserved; the
  blocking-OS-lock alternative demonstrably regressed), or is a
  blocking objection warranted?
- Q3: re-confirm the 5 prior fixes + (C) remain root-cause/no-mask.
- Q4: blocking objections? State the differential/prior. Note for
  codex/gemini: your pass-2 endorsed `_locked_append` which CI
  refuted — reason from THIS primitive, not the prior one.
