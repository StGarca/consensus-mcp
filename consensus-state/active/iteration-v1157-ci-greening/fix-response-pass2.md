# Fix response — Workflow B pass-2 (CORRECTED PREMISE)

## Why this re-audit exists (disconfirming evidence)

Pass-1: gemini `goal_satisfied=true`, 0 blocking, adjudicated the
open windows-py3.10 item as **fix-shape (C)** (test-only reader
hardening). codex pass-1 STALLED (no output 600s — transient
cold-start, not a verdict; codex succeeded earlier this session
with identical invocation).

**gemini's (C) rationale rested on a load-bearing premise that
artifact-verification REFUTED:** gemini asserted production
dispatch-log writes are line-atomic (`_locked_append`), so the torn
line is test-only. The code says otherwise — `_dispatch_base.py`
`_log_dispatch` was a **bare unlocked append**:

```
with log_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(event_with_ts) + "\n")
```

No lock. The streaming `_invoke_codex` emits dispatch events from
the main thread AND stdout/stderr reader threads concurrently; an
abrupt wall-time-ceiling teardown can interrupt a write mid-line.
So a torn/interleaved `dispatch-log.jsonl` line can occur **in
production**, not just the test. Per the convergence-correctness
doctrine (verify the load-bearing premise; disconfirming evidence
IS the signal; don't ratify a 2-AI agreement built on a shared
false prior), (C)-alone was rejected as masking a real fragility.

Operator decision: **C now + fix A now (proper), re-audit
corrected.**

## What was implemented (the corrected converged decision: C + A)

- **(A) production root cause** — `_dispatch_base._log_dispatch`
  now appends via the codebase's existing OS-exclusive-lock
  primitive `_visibility_watchdog._locked_append` (msvcrt.locking
  on Windows / fcntl.flock on POSIX) — the SAME mechanism the
  audit log already uses. Concurrent emitters + abrupt teardown can
  no longer tear a dispatch-log line. Deferred import (acyclic;
  call site already I/O-bound).
- **(C) defensive reader** — the test-only `_read_log_events`
  helper now skips a line that fails `json.loads` (try/except
  JSONDecodeError) instead of crashing. Belt-and-suspenders: (A)
  prevents torn lines at the source; a telemetry reader must still
  never crash on a partial line.

## Landed v1.15.7 fixes (re-confirm, unchanged from pass-1)

1. 4 codex "smoke" tests `skipif(no codex)` — interim; named
   Popen-mock-rewrite follow-up.
2. `review_packet_known_good/input.yaml` parent-path → repo path
   (validator self-tests 21/21). **Now embedded in this packet**
   (gemini-rev-001 pass-1: it was described but not included).
3. `_FakePopen.pid` 0 → never-live; `tests/conftest.py` suite-wide
   `os.killpg`/`os.getpgid` neutralizer; `pytest-timeout` CI
   hardening; POSIX `signal.SIGTERM` stdlib ref.

## Audit questions (corrected facts)

- Q1: with the CORRECTED premise (dispatch-log append was
  unlocked; now `_locked_append`), is C+A the right resolution?
  Is the `_locked_append` reuse correct + sufficient for the
  concurrent-emitter + abrupt-teardown case (msvcrt byte-range /
  flock; auto-release on close)?
- Q2: does the deferred import introduce any cycle or
  import-order hazard? (`_visibility_watchdog` does not import
  `_dispatch_base`.)
- Q3: re-confirm the 5 prior fixes are root-cause not masks
  (esp. conftest guard = hermeticity).
- Q4: any blocking objection. State the differential/prior you
  reasoned from. (codex: this is your first substantive pass —
  prior attempt stalled.)
