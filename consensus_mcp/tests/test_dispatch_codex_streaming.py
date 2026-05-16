"""Regression tests for iter-0037 streaming/heartbeat/abort features of
`_invoke_codex` in `consensus_mcp/_dispatch_codex.py`.

v1.15.9 (iteration-v1159-deterministic-clock-harness, Workflow A
converged — claude+codex+gemini, weighted-synthesis): the harness is
now fully DETERMINISTIC. There are no real `time.sleep` waits on the
drive path and no "advance the clock + hope the daemon runner is
scheduled within a wall-clock budget + join(timeout)" — the exact
pattern that flaked on loaded Windows GitHub runners and forced the
v1.15.8 Q2(c) `@_FLAKY_WINDOWS_CI` interim skip (now DELETED).

Mechanism:
  - `_SyncClock` — a `threading.Condition`-backed virtual clock.
    `now()/advance()` are unchanged in spirit; `sleep(dt)` is
    injected into production `_invoke_codex` via its private,
    keyword-only `_sleep=` seam (defaults to `time.sleep`, so
    production behavior is unchanged) so the poll loop has an
    explicit happens-before with the test driver instead of racing
    real time. `wait_runner_parked()` gives the heartbeat-cadence
    test lockstep; `wait_for(predicate)` keys on the durable
    emitted `dispatch-log.jsonl` events the assertions already use;
    `release_all()` is a root-cause-INDEPENDENT teardown safeguard:
    any failed/timed-out wait releases every waiter, terminates the
    fake process, and fails LOUD with harness state — a misdiagnosed
    or deadlocked harness can never wedge a CI job.
  - `_FakePipeReader` waits on the same clock (no real 5 ms poll);
    `StreamingFakeCodexPopen` notifies the clock on exit/terminate/
    kill so reader threads are woken deterministically.
  - `test_stderr_drain_prevents_deadlock` keeps REAL stdout/stderr
    reader threads (the behavior it exists to prove).

Deadlock-free invariant: every blocking wait has a guaranteed
waker on EVERY path (advance, runner-parked, log-event, proc-exit,
exception, and the absolute safety ceiling -> release_all()).
"""
from __future__ import annotations

import collections
import json as _json
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _dispatch_codex  # noqa: E402


SCHEMA_PATH = REPO_ROOT / "consensus_mcp" / "dispatch_templates" / "codex_review_schema.json"

# Absolute real-time safety ceiling for any harness wait. Dozens x
# normal; under correct operation NO wait ever approaches it (every
# wakeup is Condition-notify driven). If one is hit, the handshake is
# broken -> release_all() fails the test LOUD and fast instead of
# hanging CI. This is the converged independent_safeguard, not a
# correctness-timing dependency.
_CEILING = 20.0

# Re-poll bound on every Condition.wait. The design is notify-driven
# (advance / sleep progress-beat / proc-exit / thread-death all
# notify_all), so under correct operation a waiter wakes in
# microseconds. _REPOLL is purely a lost-wakeup SAFETY NET: a missed
# notify self-heals in ~50 ms instead of stalling to _CEILING.
# Correctness NEVER depends on it — every wait re-checks an exact
# predicate; _REPOLL only bounds wakeup LATENCY, never pass/fail
# (this is what keeps the harness deterministic, not wall-clock-timed).
_REPOLL = 0.05


# --------------------------------------------------------------------------
# Test helpers
# --------------------------------------------------------------------------


class _SyncClock:
    """Deterministic virtual clock with an explicit test<->runner
    happens-before. Replaces the old real-sleep-polled
    `_ControllableClock`."""

    def __init__(self, start: float = 1000.0):
        self.t = float(start)
        self._cond = threading.Condition()
        self._epoch = 0          # bumped by every advance()
        self._park_epoch = -1    # max epoch a sleep() caller parked under
        self._sleepers = 0       # threads currently parked in sleep()
        self._released = False

    # -- production-facing (time_fn / _sleep injection) --------------

    def now(self) -> float:
        with self._cond:
            return self.t

    def sleep(self, _dt: float) -> None:
        """Injected as production `_invoke_codex(_sleep=...)`. Park the
        caller until virtual time passes its wake target OR the clock
        is released. Records the park epoch + sleeper count so the
        driver can detect "the runner consumed the latest advance".
        Emits a progress beat (notify) so `wait_for` re-checks its
        predicate immediately after each poll iteration."""
        deadline = time.monotonic() + _CEILING
        with self._cond:
            wake_at = self.t + float(_dt)
            while self.t < wake_at and not self._released:
                if time.monotonic() >= deadline:
                    return  # safety: never wedge the runner forever
                self._sleepers += 1
                self._park_epoch = max(self._park_epoch, self._epoch)
                self._cond.notify_all()
                self._cond.wait(_REPOLL)
                self._sleepers -= 1

    # -- driver-facing ----------------------------------------------

    def advance(self, dt: float) -> int:
        with self._cond:
            self.t += float(dt)
            self._epoch += 1
            self._cond.notify_all()
            return self._epoch

    def wait_runner_parked(self, after_epoch: int) -> bool:
        """Lockstep: block until a sleep() caller has parked having
        observed >= after_epoch (ran a full poll iteration after that
        advance). Guaranteed waker: sleep()'s notify, or the ceiling."""
        deadline = time.monotonic() + _CEILING
        with self._cond:
            while not self._released and not (
                self._sleepers > 0 and self._park_epoch >= after_epoch
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, _REPOLL))
            return not self._released

    def wait_for(self, predicate) -> bool:
        """Block until predicate() is true OR released OR ceiling.
        predicate reads external state (dispatch-log/holder) and does
        NOT take this lock. Re-checked on every notify — the runner
        emits a progress beat each poll iteration via sleep()."""
        deadline = time.monotonic() + _CEILING
        with self._cond:
            while not self._released:
                if predicate():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, _REPOLL))
            return predicate()

    def wait_byte_due(self, sched_rel: float, t0: float, parent_exited) -> bool:
        """Reader-side wait: True when (now - t0) >= sched_rel; False
        if the parent proc exited or the clock was released. Does NOT
        touch the sleeper/epoch state (that is the runner poll loop's
        alone). Woken by advance()/terminate()/kill()/release_all()."""
        deadline = time.monotonic() + _CEILING
        with self._cond:
            while not self._released:
                if (self.t - t0) >= sched_rel:
                    return True
                if parent_exited():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, _REPOLL))
            return (self.t - t0) >= sched_rel

    def notify(self) -> None:
        """Wake all waiters (used on proc exit / runner-thread death)."""
        with self._cond:
            self._cond.notify_all()

    def is_released(self) -> bool:
        with self._cond:
            return self._released

    def release_all(self) -> None:
        """Independent safeguard: free EVERY waiter immediately so a
        broken handshake fails fast/loud instead of wedging CI. The
        fake proc's poll() also observes is_released() and
        self-terminates, so the production runner loop actually EXITS
        (it does not just spin on a never-exiting proc) — without that,
        release_all only unblocked the driver while leaking a spinning
        daemon (codex-v1159-wfb-4 codex-rev-001, integrated)."""
        with self._cond:
            self._released = True
            self._cond.notify_all()


class _FakeStdin:
    """Bytes-capturing stdin replacement; closed flag used to gate proc exit."""

    def __init__(self):
        self.buf = bytearray()
        self.closed = False

    def write(self, data):
        if self.closed:
            raise BrokenPipeError("stdin closed")
        self.buf.extend(data)
        return len(data)

    def close(self):
        self.closed = True

    def flush(self):
        pass


class _FakePipeReader:
    """stdout/stderr replacement that models a finite-capacity OS pipe
    buffer to test backpressure-driven deadlocks.

    A writer (`StreamingFakeCodexPopen`) calls `write()`; if the
    buffer is full, it BLOCKS on a `_SyncClock` Condition until a
    reader (`_dispatch_codex`'s real reader thread) calls `readline()`
    and consumes. The fake Popen's `poll()` will NOT report exit
    while its writer is blocked, deterministically reproducing the
    deadlock if the reader thread stops consuming.

    `readline()` returns `b""` (EOF) when the parent proc has exited
    and the schedule/buffer are drained, so production's
    `iter(readline, b"")` reader threads terminate like real pipes.
    """

    def __init__(self, schedule, parent, *, capacity: int | None):
        # schedule: list[tuple[float, bytes]] — clock-relative time + payload
        # capacity: BYTE capacity (codex-v11510-wfb-2 codex-rev-002 —
        # a real OS pipe buffer is byte-bounded, ~64 KB, NOT line-count
        # bounded). None = unbounded (preserves the other 7 tests).
        self._schedule = list(schedule)
        self._schedule_idx = 0  # producer's cursor
        self._parent = parent
        self._capacity = capacity
        self._buffer = collections.deque()
        self._buffered_bytes = 0
        self._writer_blocked = False

    @property
    def _pending_schedule(self) -> bool:
        return self._schedule_idx < len(self._schedule)

    def _bytes_in_buffer(self) -> int:
        return self._buffered_bytes

    def write(self, data: bytes) -> bool:
        """NON-BLOCKING producer push (codex-v11510-wfb-2 codex-rev-003:
        write() must NOT block inside poll() — that parks the runner
        thread inside poll() so the nominal driver can fall back to
        _CEILING). Returns True if `data` fit within the BYTE capacity
        (buffered); False if it would overflow (backpressure — caller
        does NOT advance the schedule cursor; poll() returns None and
        the runner parks NORMALLY in clock.sleep). A reader consuming
        bytes frees space for a later retry."""
        with self._parent._clock._cond:
            if self._parent._clock.is_released():
                self._writer_blocked = False
                return True  # teardown: drop, never block
            if (
                self._capacity is None
                or self._buffered_bytes + len(data) <= self._capacity
            ):
                self._buffer.append(data)
                self._buffered_bytes += len(data)
                self._writer_blocked = False
                self._parent._clock.notify()
                return True
            # Would overflow the byte buffer → backpressure (no append).
            self._writer_blocked = True
            self._parent._clock.notify()
            return False

    def readline(self):
        # codex-v1159-wfb-4 codex-rev-003 (integrated): production's
        # reader thread calls readline() in a tight `iter(readline,
        # b"")` loop — so a call here happens-AFTER it processed the
        # previous line into a `dispatch_streamed_line` event. Notify
        # the clock so event-count waits (`_drive_streaming` /
        # `_drive_post_stream`) have a TRUE happens-before waker for
        # reader-thread-produced events, instead of falling back to
        # the _REPOLL safety net (the reader does not otherwise notify;
        # the runner's sleep()-beat is not ordered after this thread).
        self._parent._clock.notify()
        deadline = time.monotonic() + _CEILING
        with self._parent._clock._cond:
            while not self._parent._clock.is_released():
                if self._buffer:
                    line = self._buffer.popleft()
                    self._buffered_bytes -= len(line)
                    # Space freed → a backpressured writer can retry.
                    self._writer_blocked = False
                    self._parent._clock.notify()
                    return line
                # Buffer is empty. EOF?
                if self._parent._exited and not self._pending_schedule:
                    return b""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._parent._clock._cond.wait(min(remaining, _REPOLL))
            return b""  # released


class StreamingFakeCodexPopen:
    """Popen-shaped fake. Constructor uses Popen's positional shape so
    production `popen_factory(cmd, stdin=..., stdout=..., stderr=...,
    bufsize=...)` works unchanged. Exit state transitions notify the
    `_SyncClock` so reader threads + the driver wake deterministically."""

    def __init__(
        self,
        cmd,
        stdin=None,
        stdout=None,
        stderr=None,
        bufsize=0,
        *,
        _scheduled_stdout=None,
        _scheduled_stderr=None,
        _stdout_capacity: int | None = None,
        _stderr_capacity: int | None = None,
        _returncode: int = 0,
        _exit_at: float = 1.0,
        _clock: _SyncClock,
        _output_payload: str = '{"ok": true}',
        **_popen_kwargs,  # iter-0039: absorb creationflags / start_new_session
    ):
        self._cmd = cmd
        self._clock = _clock
        self._t0 = _clock.now()
        self._exit_at = float(_exit_at)
        self._returncode_on_exit = int(_returncode)
        self._exited = False
        # codex-v1159-wfb-3 codex-rev-001 (BLOCKING): record the
        # SIGTERM/terminate side-effect so the operator-abort test can
        # assert _terminate_process_tree actually ran (H2 names SIGTERM
        # as required coverage — a regression that stopped killing the
        # process must FAIL, not silently pass).
        self._terminated = False
        self.returncode = None
        self._output_payload = _output_payload
        self._out_file = None
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                self._out_file = cmd[i + 1]
                break
        self.stdin = _FakeStdin()
        self.stdout = _FakePipeReader(
            _scheduled_stdout or [], self, capacity=_stdout_capacity
        )
        self.stderr = _FakePipeReader(
            _scheduled_stderr or [], self, capacity=_stderr_capacity
        )

    def _maybe_write_output(self):
        if self._out_file and self._returncode_on_exit == 0:
            try:
                Path(self._out_file).write_text(self._output_payload, encoding="utf-8")
            except OSError:
                pass

    def _service_pipes(self):
        """Write any due scheduled lines to the pipe buffers. This is
        the producer side of the backpressure model."""
        now_rel = self._clock.now() - self._t0
        for pipe in (self.stdout, self.stderr):
            while pipe._pending_schedule:
                sched_time, payload = pipe._schedule[pipe._schedule_idx]
                if now_rel < sched_time:
                    break  # this pipe's next line is not due yet
                # NON-BLOCKING (codex-v11510-wfb-2 codex-rev-003): if
                # the byte buffer would overflow, write() returns False
                # WITHOUT consuming the schedule cursor. Stop pumping
                # this pipe; poll() returns None (proc still running,
                # backpressured), the runner parks NORMALLY in
                # clock.sleep, and a later poll retries after a reader
                # has freed space. The runner NEVER parks inside poll().
                if not pipe.write(payload):
                    break
                pipe._schedule_idx += 1

    def poll(self):
        if self._exited:
            return self.returncode
        # codex-v1159-wfb-4 codex-rev-001 (integrated): when the clock
        # is released (a harness-timeout teardown), the fake proc MUST
        # exit so the production poll loop terminates and the daemon
        # runner thread actually dies — instead of _sleep returning
        # immediately while proc.poll() stays None forever (a spinning
        # leak after the driver raised). release_all() owns the single
        # teardown signal; the fake observes it here (poll() is called
        # every production iteration), so no second coordination
        # surface / registration list is introduced.
        if self._clock.is_released():
            self._terminated = True
            self._exited = True
            self.returncode = -15
            return self.returncode

        self._service_pipes()

        is_blocked = self.stdout._writer_blocked or self.stderr._writer_blocked
        now_rel = self._clock.now() - self._t0
        if now_rel >= self._exit_at and not is_blocked:
            self.returncode = self._returncode_on_exit
            self._exited = True
            self._maybe_write_output()
            self._clock.notify()  # wake reader threads (EOF) + driver
            return self.returncode
        return None

    def terminate(self):
        self._terminated = True
        if not self._exited:
            self.returncode = -15  # SIGTERM
            self._exited = True
            self._clock.notify()
            # Do NOT stage output payload on termination — matches real codex.

    def kill(self):
        self._terminated = True
        if not self._exited:
            self.returncode = -9
            self._exited = True
            self._clock.notify()

    def send_signal(self, sig):
        """iter-0039: _terminate_process_tree calls send_signal(CTRL_BREAK_EVENT)
        on Windows and os.killpg on POSIX. The fake accepts any signal and
        treats it as terminate (the test cares about the exit path, not the
        signal-delivery mechanism)."""
        self.terminate()

    @property
    def pid(self):
        """`_terminate_process_tree` does os.killpg(os.getpgid(proc.pid))
        on POSIX. Return a synthetic, never-live PID so os.getpgid raises
        ProcessLookupError and the production OSError-fallback
        (proc.terminate()) runs. (pid 0 == the caller's own process group
        — that previously made the abort path SIGTERM the pytest job
        itself. The suite-wide conftest guard also neutralizes
        os.killpg/os.getpgid.)"""
        return 2_147_483_647

    def wait(self, timeout=None):
        # Exit is controlled via clock + terminate/kill; production only
        # calls wait() after terminate/kill, by which point _exited is set.
        return self.returncode


def _make_factory(
    clock: _SyncClock,
    *,
    scheduled_stdout=None,
    scheduled_stderr=None,
    stdout_capacity: int | None = None,
    stderr_capacity: int | None = None,
    returncode: int = 0,
    exit_at: float = 1.0,
    output_payload: str = '{"ok": true}',
):
    """Build a popen_factory closure for injection into `_invoke_codex`.

    `factory.instances` records every `StreamingFakeCodexPopen`
    created so a test can assert process-side-effects (codex-v1159-
    wfb-3 codex-rev-001: the operator-abort test must verify the
    real SIGTERM/terminate happened, not just the log/signal-file)."""

    instances: list = []

    def factory(cmd, **kwargs):
        p = StreamingFakeCodexPopen(
            cmd,
            _scheduled_stdout=scheduled_stdout,
            _scheduled_stderr=scheduled_stderr,
            _stdout_capacity=stdout_capacity,
            _stderr_capacity=stderr_capacity,
            _returncode=returncode,
            _exit_at=exit_at,
            _clock=clock,
            _output_payload=output_payload,
            **kwargs,
        )
        instances.append(p)
        return p

    factory.instances = instances
    return factory


def _run_invoke_in_thread(
    clock: _SyncClock, *, drain_stderr: bool = True, **invoke_kwargs
):
    """Run `_invoke_codex` in a daemon thread; return (thread, holder).

    Injects `_sleep=clock.sleep` (the converged DI seam) so the
    production poll loop is virtual-time-gated. The `finally` notifies
    the clock so the driver's `wait_for` is woken the instant the
    runner thread dies — on EVERY path incl. exceptions (the universal
    escape that makes the design deadlock-free).
    """
    holder: dict = {}

    def runner():
        try:
            # The _drain_stderr kwarg is not part of the public contract,
            # but is a pass-through seam used by the mutant-gate test.
            holder["out"] = _dispatch_codex._invoke_codex(
                _sleep=clock.sleep, _drain_stderr=drain_stderr, **invoke_kwargs
            )
        except Exception as e:  # noqa: BLE001
            holder["e"] = e
        finally:
            clock.notify()

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    return th, holder


def _drive(clock: _SyncClock, th: threading.Thread, *, until=None, chunk: float = 50.0,
           max_rounds: int = 64):
    """Deterministic driver for terminal scenarios (stream/truncate/
    clean-exit/stderr-drain/silence-abort/operator-signal).

    The terminal condition is ALWAYS runner-thread death (proc exited
    cleanly, or aborted/raised). It is intentionally NOT a mid-stream
    observable: stopping early on e.g. "a line was streamed" while the
    fake proc has not yet been advanced to its exit_at leaves the
    production loop spinning on a proc that never exits → wedged. The
    event assertions are checked by the caller AFTER thread death.

    Fully park-synced (no t0-capture race, no real-sleep budget):
    1. Wait for the runner's first park (prologue + the fake's
       `_t0 = now()` capture complete) OR the runner already died.
    2. Each round: advance one logical chunk, then wait until the
       runner parked again having observed this advance (still running)
       OR the runner thread died. Both are Condition-notified —
       guaranteed waker on every path; ceiling → release_all() → loud
       fast fail, never a wedged CI job. `until` is accepted for
       call-site readability only and does not gate termination."""
    # Startup: wait until the runner has parked once (prologue + the
    # fake's _t0=now() capture complete) OR the runner thread already
    # died — an abort can fire on iteration 1 (e.g. operator-signal
    # written before drive) BEFORE the loop ever reaches _sleep, so
    # "parked" is not guaranteed; thread-death is the universal escape.
    if not clock.wait_for(lambda: (not th.is_alive()) or clock._sleepers > 0):
        clock.release_all()
        th.join(timeout=5)
        raise AssertionError("_drive: runner neither parked nor exited at startup (release_all fired)")

    rounds = 0
    while th.is_alive() and rounds < max_rounds:
        e = clock.advance(chunk)
        # codex-v1159-wfb-2 codex-rev-001 (BLOCKING, integrated): the
        # per-round wait return MUST be acted on. A False return means
        # _CEILING elapsed with neither runner re-park (epoch>=e) nor
        # thread death — under correct notify-driven operation that
        # never happens (wakeups are sub-ms), so False == a genuine
        # wedge. Fire the release_all() safeguard and fail FAST/LOUD
        # here, not after max_rounds×_CEILING. This is what makes the
        # claimed deadlock-free invariant actually true.
        if not clock.wait_for(
            lambda e=e: (not th.is_alive())
            or (clock._sleepers > 0 and clock._park_epoch >= e)
        ):
            clock.release_all()
            th.join(timeout=5)
            raise AssertionError(
                "_drive: per-round wait hit _CEILING with no runner "
                "progress — release_all fired. Deadlock/handshake or "
                "product logic broken; NOT a timing flake."
            )
        rounds += 1

    exhausted = th.is_alive() and rounds >= max_rounds
    clock.release_all()
    th.join(timeout=5)
    if th.is_alive() or exhausted:
        raise AssertionError(
            f"_drive: runner did not finish within max_rounds={max_rounds} "
            "(release_all fired — deterministic handshake broke; NOT a "
            "timing flake). max_rounds exhaustion is a HARD fail even if "
            "release_all later lets the thread limp out."
        )


def _drive_heartbeats(
    clock: _SyncClock,
    th: threading.Thread,
    log_path: Path,
    *,
    interval: float,
    steps: int,
    final_advance: float,
    poll_interval: float,
):
    """Lockstep driver that PROVES the heartbeat interval gate
    (codex-v1159-wfb-5 codex-rev-001, integrated). Advancing exactly
    `interval` per step with one poll/step only checks "one per step"
    — a regression that emits EVERY poll (or uses a tiny threshold)
    would still produce one-per-step and pass. So each round we:
      (1) advance a SUB-interval amount, sync, and assert NO new
          heartbeat (the interval gate held — premature emit fails);
      (2) advance the remainder to cross the boundary, sync, and
          assert EXACTLY ONE new heartbeat.
    This restores the cadence coverage the pre-rewrite test had via
    its many sub-interval polls + loose count window."""

    def _hb():
        return len(_events(log_path, "dispatch_heartbeat"))

    def _sync(e, where):
        if not clock.wait_runner_parked(after_epoch=e):
            clock.release_all()
            th.join(timeout=5)
            raise AssertionError(
                f"_drive_heartbeats: runner did not consume advance "
                f"({where}) — release_all fired; handshake broken, NOT "
                "a timing flake"
            )

    # codex-v1159-wfb-7 codex-rev-001 (BLOCKING, integrated): startup
    # wait must use the SAME release_all()+join loud-fail path as the
    # other drivers — a bare assert here bypasses the synthetic
    # clock/process teardown (the teardown-bypass class of pass-2/4).
    if not clock.wait_runner_parked(after_epoch=0):
        clock.release_all()
        th.join(timeout=5)
        raise AssertionError(
            "_drive_heartbeats: runner never parked (startup) — "
            "release_all fired; handshake broken, NOT a timing flake"
        )
    # codex-v1159-wfb-7 codex-rev-001 (BLOCKING, integrated): check
    # no-emit JUST BEFORE the boundary, not at half-interval — a
    # regression emitting at e.g. 20s for a 30s interval slips past a
    # half-interval (15s) check but is caught at interval-epsilon.
    # Float-robustness refinement on codex's exact-boundary epsilon:
    # the boundary advance overshoots by epsilon (total interval+eps)
    # so `now - last_heartbeat >= interval` cannot be flipped False by
    # ~1e-10 double error from landing EXACTLY on the interval.
    # `gap` must exceed `poll_interval`: the production loop sleeps
    # `poll_interval` between polls, so an advance < poll_interval
    # never triggers another poll iteration (the runner would not emit
    # — the bug a 2*epsilon=0.002 advance vs poll_interval=0.01 hit).
    # gap stays << interval so the no-emit check remains a TIGHT gate
    # (catches emit-every-poll and wrong-threshold regressions like
    # "emit at 20s for a 30s interval": 20 < interval-gap → caught).
    # Float margin = gap ≫ ~1e-10 double error, so the boundary
    # comparison cannot be flipped by rounding.
    gap = max(interval / 1000.0, poll_interval * 5.0)
    pre_boundary = interval - gap
    for k in range(steps):
        before = _hb()
        # (1) advance to interval-gap: the gate MUST suppress emission.
        e1 = clock.advance(pre_boundary)
        _sync(e1, f"round {k} pre-boundary")
        mid = _hb()
        assert mid == before, (
            f"round {k}: heartbeat emitted at +{pre_boundary}s — BEFORE "
            f"the {interval}s interval elapsed: count {before}->{mid}. "
            "The interval gate is broken (regression: emit-every-poll / "
            "wrong threshold)."
        )
        # (2) cross the boundary by 2*gap (now-last_hb = interval+gap,
        # unambiguously >= interval AND > poll_interval so the runner
        # actually polls): EXACTLY one new heartbeat.
        e2 = clock.advance(2.0 * gap)
        _sync(e2, f"round {k} boundary")
        after = _hb()
        assert after == before + 1, (
            f"round {k}: expected exactly 1 heartbeat at the {interval}s "
            f"boundary, got {after - before} (count {before}->{after})"
        )
    clock.advance(final_advance)
    ok = clock.wait_for(lambda: not th.is_alive())
    clock.release_all()
    th.join(timeout=5)
    assert ok and not th.is_alive(), "_drive_heartbeats: runner did not finish"


def _drive_streaming(
    clock: _SyncClock,
    th: threading.Thread,
    log_path: Path,
    *,
    step: float,
    until,
    max_steps: int = 64,
):
    """Lockstep driver for the wall-ceiling scenario: codex streams
    continuously so silence-abort must NOT fire while virtual time
    accumulates past the hard ceiling. `step` < stall_silence_seconds;
    after each advance, wait until production has actually PROCESSED a
    new streamed line (the durable `dispatch_streamed_line` count rose)
    — so `last_streamed_ts` is provably fresh at every silence check —
    OR `until` (the wall abort) fired OR the runner died. This removes
    the reader/processing race that a coarse jump would lose to
    pre-first-line silence."""
    if not clock.wait_for(lambda: (not th.is_alive()) or clock._sleepers > 0):
        clock.release_all()
        th.join(timeout=5)
        raise AssertionError("_drive_streaming: runner never started (release_all fired)")

    steps = 0
    while th.is_alive() and not until() and steps < max_steps:
        prev = len(_events(log_path, "dispatch_streamed_line"))
        clock.advance(step)
        # codex-v1159-wfb-2 codex-rev-001 (BLOCKING, integrated):
        # act on the per-step wait return — False == _CEILING elapsed
        # with no new processed line / abort / death = a genuine wedge;
        # fire release_all() and fail FAST/LOUD, not after
        # max_steps×_CEILING.
        if not clock.wait_for(
            lambda prev=prev: (not th.is_alive())
            or until()
            or len(_events(log_path, "dispatch_streamed_line")) > prev
        ):
            clock.release_all()
            th.join(timeout=5)
            raise AssertionError(
                "_drive_streaming: per-step wait hit _CEILING with no "
                "progress — release_all fired. Deadlock/handshake or "
                "product logic broken; NOT a timing flake."
            )
        steps += 1

    exhausted = th.is_alive() and not until() and steps >= max_steps
    clock.release_all()
    th.join(timeout=5)
    if th.is_alive() or exhausted:
        raise AssertionError(
            f"_drive_streaming: did not reach terminal within "
            f"max_steps={max_steps} (release_all fired — deterministic "
            "handshake broke; NOT a timing flake). max_steps exhaustion "
            "is a HARD fail even if release_all later frees the thread."
        )


def _advance_until_streamed(
    clock: _SyncClock,
    th: threading.Thread,
    log_path: Path,
    *,
    min_streamed: int,
    step: float,
    max_steps: int = 64,
):
    """Lockstep small advances (`step` < stall_silence_seconds) until
    `min_streamed` `dispatch_streamed_line` events are PROCESSED and
    the runner is back parked mid-run. Establishes a DETERMINISTIC
    mid-run state: callers can then assert post-stream behaviour
    (silence path) or inject a mid-run operator-abort signal — never
    the startup/pre-first-line path (codex-v1159-wfb-3 codex-rev-002
    silence; codex-v1159-wfb-4 codex-rev-002 operator). Returns with
    the runner alive + parked + ≥min_streamed events; raises LOUD
    (release_all) on any wedge or shortfall."""
    if not clock.wait_for(lambda: (not th.is_alive()) or clock._sleepers > 0):
        clock.release_all()
        th.join(timeout=5)
        raise AssertionError("_advance_until_streamed: runner never started (release_all fired)")

    steps = 0
    while (
        th.is_alive()
        and len(_events(log_path, "dispatch_streamed_line")) < min_streamed
        and steps < max_steps
    ):
        e = clock.advance(step)
        if not clock.wait_for(
            lambda e=e: (not th.is_alive())
            or len(_events(log_path, "dispatch_streamed_line")) >= min_streamed
            or (clock._sleepers > 0 and clock._park_epoch >= e)
        ):
            clock.release_all()
            th.join(timeout=5)
            raise AssertionError(
                "_advance_until_streamed: per-step wait hit _CEILING "
                "(release_all fired; NOT a timing flake)"
            )
        steps += 1

    if len(_events(log_path, "dispatch_streamed_line")) < min_streamed:
        clock.release_all()
        th.join(timeout=5)
        raise AssertionError(
            f"_advance_until_streamed: only "
            f"{len(_events(log_path, 'dispatch_streamed_line'))} of "
            f"{min_streamed} lines processed — deterministic mid-run "
            "state NOT established (release_all fired)"
        )


def _drive_post_stream(
    clock: _SyncClock,
    th: threading.Thread,
    log_path: Path,
    *,
    min_streamed: int,
    step: float,
):
    """codex-v1159-wfb-3 codex-rev-002 (integrated): process
    `min_streamed` lines first so `last_streamed_ts` is set and the
    subsequent silence-abort exercises the POST-stream path (not
    pre-first-line startup silence — the coverage the original test's
    now-removed `time.sleep(0.1)` guaranteed), then drive to terminal."""
    _advance_until_streamed(clock, th, log_path, min_streamed=min_streamed, step=step)
    _drive(clock, th)


def _read_log_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(_json.loads(line))
        except _json.JSONDecodeError:
            # Defensive JSONL parse (v1.15.7 C): a telemetry-log reader
            # must never crash on a partial line. v1.15.8 made the
            # production append OS-lock-atomic so torn lines should not
            # occur; drop any anyway — the asserted events are whole.
            continue
    return out


def _setup_repo_root(tmp_path: Path) -> Path:
    (tmp_path / "consensus-state" / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _anchors():
    return {
        "iteration_id": "iter-test",
        "reviewer_id": "codex-test",
        "pass_id": "codex-test-pass1",
    }


def _events(log_path, name):
    return [e for e in _read_log_events(log_path) if e["event"] == name]


# --------------------------------------------------------------------------
# Test 1 — streamed lines appear in dispatch-log with correct seq + content
# --------------------------------------------------------------------------


def test_streamed_lines_appear_in_dispatch_log(tmp_path):
    repo_root = _setup_repo_root(tmp_path)
    clock = _SyncClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    scheduled = [
        (0.0, b"line1\n"),
        (0.0, b"line2\n"),
        (0.0, b"line3\n"),
        (0.0, b"line4\n"),
        (0.0, b"line5\n"),
    ]
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=0.5)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="hi",
        codex_bin="codex",
        timeout_seconds=60,
        repo_root=repo_root,
        schema_path=SCHEMA_PATH,
        log_path=log_path,
        anchors=_anchors(),
        heartbeat_interval=30.0,
        stall_silence_seconds=90.0,
        poll_interval=0.01,
        time_fn=clock.now,
        popen_factory=factory,
    )
    _drive(clock, th, until=lambda: len(_events(log_path, "dispatch_streamed_line")) >= 5)
    assert not th.is_alive(), "runner did not finish"
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"
    assert holder["out"] == '{"ok": true}'

    stream_events = _events(log_path, "dispatch_streamed_line")
    assert len(stream_events) == 5, f"expected 5 stream events, got {len(stream_events)}"
    for i, ev in enumerate(stream_events):
        assert ev["seq"] == i, f"event {i} seq={ev['seq']}"
        assert ev["line_truncated"] == f"line{i + 1}"
        assert ev["truncated"] is False
        assert ev["stream"] == "stdout"
        assert ev["pass_id"] == "codex-test-pass1"


# --------------------------------------------------------------------------
# Test 2 — long lines truncated to 200 chars; full length recorded
# --------------------------------------------------------------------------


def test_long_lines_are_truncated_to_200_chars(tmp_path):
    repo_root = _setup_repo_root(tmp_path)
    clock = _SyncClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    big = ("A" * 1000) + "\n"
    scheduled = [(0.0, big.encode("utf-8"))]
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=0.2)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="x",
        codex_bin="codex",
        timeout_seconds=60,
        repo_root=repo_root,
        schema_path=SCHEMA_PATH,
        log_path=log_path,
        anchors=_anchors(),
        heartbeat_interval=30.0,
        stall_silence_seconds=90.0,
        poll_interval=0.01,
        time_fn=clock.now,
        popen_factory=factory,
    )
    _drive(clock, th, until=lambda: len(_events(log_path, "dispatch_streamed_line")) >= 1)
    assert not th.is_alive()
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"

    stream = _events(log_path, "dispatch_streamed_line")
    assert len(stream) == 1
    ev = stream[0]
    assert len(ev["line_truncated"]) == 200
    assert ev["line_full_length"] == 1000
    assert ev["truncated"] is True


# --------------------------------------------------------------------------
# Test 3 — heartbeat fires at heartbeat_interval cadence
# --------------------------------------------------------------------------


def test_heartbeat_fires_at_interval(tmp_path):
    """Advance virtual time so codex appears to run ~95s with no stdout;
    stall_silence_seconds=200 so silence-abort never trips; with
    heartbeat_interval=30 we deterministically see exactly 3 heartbeats
    (at ~30, ~60, ~90) via lockstep advances."""
    repo_root = _setup_repo_root(tmp_path)
    clock = _SyncClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    factory = _make_factory(clock, scheduled_stdout=[], exit_at=95.0)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="x",
        codex_bin="codex",
        timeout_seconds=300,
        repo_root=repo_root,
        schema_path=SCHEMA_PATH,
        log_path=log_path,
        anchors=_anchors(),
        heartbeat_interval=30.0,
        stall_silence_seconds=200.0,
        poll_interval=0.01,
        time_fn=clock.now,
        popen_factory=factory,
    )
    # 3 lockstep interval rounds — each PROVES the gate: no heartbeat
    # at +15s, exactly one at +30s (codex-v1159-wfb-5 codex-rev-001).
    _drive_heartbeats(
        clock, th, log_path,
        interval=30.0, steps=3, final_advance=20.0, poll_interval=0.01,
    )
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"

    hb = _events(log_path, "dispatch_heartbeat")
    assert len(hb) == 3, f"expected exactly 3 heartbeats, got {len(hb)}: {hb}"
    ages = [ev["age_seconds"] for ev in hb]
    assert ages == sorted(ages), f"heartbeat ages not monotonic: {ages}"


# --------------------------------------------------------------------------
# Test 4 — heartbeat silence triggers abort
# --------------------------------------------------------------------------


def test_heartbeat_silence_triggers_abort(tmp_path):
    """Codex emits 2 lines then goes silent; advancing virtual time past
    stall_silence_seconds → `dispatch_aborted` with
    `abort_source="watchdog_silence"` and CodexInvocationError raised."""
    repo_root = _setup_repo_root(tmp_path)
    clock = _SyncClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    scheduled = [(0.0, b"a\n"), (0.0, b"b\n")]
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=1000.0)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="x",
        codex_bin="codex",
        timeout_seconds=300,
        repo_root=repo_root,
        schema_path=SCHEMA_PATH,
        log_path=log_path,
        anchors=_anchors(),
        heartbeat_interval=1000.0,  # don't fire heartbeats
        stall_silence_seconds=10.0,
        poll_interval=0.01,
        time_fn=clock.now,
        popen_factory=factory,
    )
    # codex-v1159-wfb-3 codex-rev-002: process BOTH scheduled lines
    # first (step=1.0 < stall_silence_seconds=10) so this exercises
    # POST-stream silence, then advance past the threshold.
    _drive_post_stream(clock, th, log_path, min_streamed=2, step=1.0)
    assert not th.is_alive(), "runner did not abort"
    assert "e" in holder, "expected CodexInvocationError"
    assert isinstance(holder["e"], _dispatch_codex.CodexInvocationError)

    aborts = _events(log_path, "dispatch_aborted")
    assert len(aborts) == 1, f"expected 1 abort event, got {aborts}"
    assert aborts[0]["abort_source"] == "watchdog_silence"
    # Proves the POST-stream silence path (not pre-first-line startup
    # silence): the abort is tied to prior streamed output, so a
    # regression in last_streamed_ts handling is caught (codex-rev-002).
    assert aborts[0].get("last_streamed_line_age_seconds") is not None, (
        "silence abort was startup-silence, not post-stream — "
        "last_streamed_line_age_seconds is null (codex-rev-002 coverage)"
    )


# --------------------------------------------------------------------------
# Test 5 — operator abort-signal file triggers abort
# --------------------------------------------------------------------------


def test_operator_abort_signal_file_triggers_abort(tmp_path):
    """Mid-run `abort-dispatch-<pass_id>.signal` → wrapper SIGTERMs codex,
    emits `dispatch_aborted` (`operator_signal_file`), deletes the signal
    file, and raises."""
    repo_root = _setup_repo_root(tmp_path)
    clock = _SyncClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    factory = _make_factory(clock, scheduled_stdout=[(0.0, b"working\n")], exit_at=1000.0)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="x",
        codex_bin="codex",
        timeout_seconds=10000,
        repo_root=repo_root,
        schema_path=SCHEMA_PATH,
        log_path=log_path,
        anchors=_anchors(),
        heartbeat_interval=10000.0,
        stall_silence_seconds=10000.0,  # don't fire silence-abort
        poll_interval=0.01,
        time_fn=clock.now,
        popen_factory=factory,
    )
    # codex-v1159-wfb-4 codex-rev-002 (integrated): establish a
    # DETERMINISTIC mid-run state before writing the signal — wait
    # until the scheduled "working" line is processed and the runner
    # is parked mid-loop. Writing the signal before then could test
    # startup-present-signal handling instead of the named H2 live
    # mid-run operator-abort + termination path.
    _advance_until_streamed(clock, th, log_path, min_streamed=1, step=1.0)
    signal_path = repo_root / "consensus-state" / "state" / "abort-dispatch-codex-test-pass1.signal"
    signal_path.write_text("operator manual abort", encoding="utf-8")
    _drive(clock, th, until=lambda: len(_events(log_path, "dispatch_aborted")) >= 1)
    assert not th.is_alive(), "runner did not abort"
    assert "e" in holder, "expected CodexInvocationError"
    assert isinstance(holder["e"], _dispatch_codex.CodexInvocationError)

    aborts = _events(log_path, "dispatch_aborted")
    assert len(aborts) == 1, f"expected 1 abort event, got {aborts}"
    assert aborts[0]["abort_source"] == "operator_signal_file"
    assert "operator manual abort" in aborts[0].get("abort_reason", "")
    assert not signal_path.exists(), "abort signal file was not deleted"
    # codex-v1159-wfb-3 codex-rev-001 (BLOCKING): assert the actual
    # SIGTERM/terminate side-effect. A regression that removed or broke
    # _terminate_process_tree(proc) in the operator-signal branch would
    # still emit the event, delete the signal file, and raise — passing
    # every assertion above while leaving the real codex process ALIVE.
    # H2 names SIGTERM as required coverage; assert it explicitly.
    assert factory.instances, "no codex process was ever created"
    assert factory.instances[0]._terminated, (
        "operator abort did NOT terminate/SIGTERM the codex process "
        "(_terminate_process_tree regression would leak a live process)"
    )


# --------------------------------------------------------------------------
# Test 6 — wall-time hard ceiling
# --------------------------------------------------------------------------


def test_wall_time_hard_ceiling(tmp_path):
    """Codex streams continuously (silence-abort never fires) but runs
    past `timeout_seconds + stall_silence_seconds` → `dispatch_aborted`
    with `abort_source="wall_time_hard_ceiling"` + CodexInvocationError."""
    repo_root = _setup_repo_root(tmp_path)
    clock = _SyncClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    scheduled = [(float(i), f"keepalive{i}\n".encode("utf-8")) for i in range(0, 200)]
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=10000.0)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="x",
        codex_bin="codex",
        timeout_seconds=10,  # wall = 10
        repo_root=repo_root,
        schema_path=SCHEMA_PATH,
        log_path=log_path,
        anchors=_anchors(),
        heartbeat_interval=1000.0,
        stall_silence_seconds=5.0,  # grace = 5; hard ceiling at 15s
        poll_interval=0.01,
        time_fn=clock.now,
        popen_factory=factory,
    )
    # step (2.0) < stall_silence_seconds (5.0): each lockstep keepalive
    # is processed (lst fresh) before the next advance, so silence never
    # fires while virtual time accumulates to the 15s hard ceiling.
    _drive_streaming(
        clock, th, log_path, step=2.0,
        until=lambda: len(_events(log_path, "dispatch_aborted")) >= 1,
    )
    assert not th.is_alive(), "runner did not abort"
    assert "e" in holder, "expected CodexInvocationError"
    assert isinstance(holder["e"], _dispatch_codex.CodexInvocationError)

    aborts = _events(log_path, "dispatch_aborted")
    assert len(aborts) == 1, f"expected 1 abort, got {aborts}"
    assert aborts[0]["abort_source"] == "wall_time_hard_ceiling"


# --------------------------------------------------------------------------
# Test 7 — clean exit returns codex output payload
# --------------------------------------------------------------------------


def test_clean_exit_returns_output(tmp_path):
    repo_root = _setup_repo_root(tmp_path)
    clock = _SyncClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    payload = '{"findings": [], "verdict": "PASS"}'
    scheduled = [(0.0, b"working...\n"), (0.0, b"done\n")]
    factory = _make_factory(
        clock,
        scheduled_stdout=scheduled,
        exit_at=0.3,
        returncode=0,
        output_payload=payload,
    )

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="x",
        codex_bin="codex",
        timeout_seconds=60,
        repo_root=repo_root,
        schema_path=SCHEMA_PATH,
        log_path=log_path,
        anchors=_anchors(),
        heartbeat_interval=30.0,
        stall_silence_seconds=90.0,
        poll_interval=0.01,
        time_fn=clock.now,
        popen_factory=factory,
    )
    _drive(clock, th, until=lambda: not th.is_alive())
    assert not th.is_alive()
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"
    assert holder["out"] == payload

    assert _events(log_path, "dispatch_aborted") == [], "clean exit produced abort events"
    assert len(_events(log_path, "dispatch_streamed_line")) == 2


# --------------------------------------------------------------------------
# Test 8 — stderr drain prevents deadlock (REAL reader threads retained)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "drain_stderr,expect_clean",
    [(True, True), (False, False)],
    ids=["nominal-drain", "mutant-no-drain"],
)
def test_stderr_drain_prevents_deadlock(tmp_path, drain_stderr, expect_clean):
    """Production MUST drain stderr or a real codex deadlocks on a full
    OS pipe buffer. ONE clean-exit contract, asserted both ways — this
    is the mutant gate (codex-v11510-wfb-2 codex-rev-001):

    - nominal-drain: the stderr reader relieves backpressure, the proc
      exits, `out=={"ok": true}`, the byte buffer fully drains → the
      clean-exit contract HOLDS.
    - mutant-no-drain: NO stderr reader → the BYTE-bounded pipe
      (codex-rev-002) saturates → the producer is perpetually
      backpressured → the proc can NEVER cleanly exit. The SAME
      clean-exit contract MUST FAIL, and that failure is detected
      FAST + DETERMINISTICALLY (codex-rev-003: write() is
      non-blocking so the runner parks normally in clock.sleep, never
      inside poll(); the writer-blocked state is observed via a
      notify-driven `wait_for`, NOT a `_CEILING`/`_drive` timeout).

    If a production regression removed/broke the stderr drain, the
    nominal case's clean-exit assertions would fail → the gate has
    teeth in both directions.
    """
    repo_root = _setup_repo_root(tmp_path)
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    big_line = b"E" * 100 + b"\n"  # 101 bytes/line
    scheduled_err = [(0.0, big_line) for _ in range(5)]  # 505 bytes total
    scheduled_out = [(0.0, b"ok\n")]
    # BYTE capacity (codex-rev-002) below the 505 B scheduled volume but
    # >= a couple lines, so the producer writes some, backpressures,
    # and a live reader relieves it progressively.
    STDERR_CAP = 250  # ~2 lines of 101 B

    clock = _SyncClock()
    factory = _make_factory(
        clock,
        scheduled_stdout=scheduled_out,
        scheduled_stderr=scheduled_err,
        stderr_capacity=STDERR_CAP,
        exit_at=0.3,
    )
    th, holder = _run_invoke_in_thread(
        clock,
        prompt="x",
        codex_bin="codex",
        timeout_seconds=60,
        repo_root=repo_root,
        schema_path=SCHEMA_PATH,
        log_path=log_path,
        anchors=_anchors(),
        poll_interval=0.01,
        time_fn=clock.now,
        popen_factory=factory,
        drain_stderr=drain_stderr,
    )

    if expect_clean:
        # Nominal: backpressure occurs but the live reader relieves it
        # → the clean-exit contract HOLDS. (If production drain
        # regressed, these assertions fail = the gate's teeth.)
        _drive(clock, th, until=lambda: not th.is_alive())
        assert not th.is_alive()
        assert "e" not in holder, f"unexpected exception: {holder.get('e')}"
        assert holder.get("out") == '{"ok": true}', (
            "clean-exit contract BROKEN under a live stderr reader — "
            "production stderr drain may have regressed"
        )
        proc = factory.instances[0]
        assert proc.stderr._schedule_idx == len(scheduled_err)
        assert not proc.stderr._buffer, "stderr buffer not fully drained"
        assert proc.stderr._bytes_in_buffer() == 0
        return

    # Mutant: NO stderr reader. Deterministically + FAST establish that
    # the producer is byte-backpressured (write() notify_all()s the
    # instant it blocks — ms, not _CEILING, no _drive). The proc is
    # created inside the runner thread → guard instances[0].
    def _byte_backpressured():
        if not factory.instances:
            return False
        s = factory.instances[0].stderr
        return s._writer_blocked and s._bytes_in_buffer() <= s._capacity

    blocked = clock.wait_for(_byte_backpressured)
    assert blocked, (
        "stderr writer never byte-backpressured with NO reader — the "
        "deadlock surface is not modeled, so this gate would NOT catch "
        "a stderr-drain regression"
    )
    # The dispatch is deadlocked → the clean-exit contract is BROKEN.
    # Assert that FAILURE (the gate's point), then release_all() as
    # TEARDOWN only, and prove the unwind is prompt (NOT _CEILING).
    proc = factory.instances[0]
    assert not proc._exited, "proc exited despite an undrained full stderr pipe"
    t0 = time.monotonic()
    clock.release_all()
    th.join(timeout=5)
    elapsed = time.monotonic() - t0
    assert not th.is_alive(), (
        "runner did not unwind within 5s of release_all() — teardown "
        "does not promptly bound the backpressure deadlock"
    )
    assert elapsed < _CEILING, (
        f"unwind took {elapsed:.1f}s >= _CEILING — failure was "
        "timeout-driven, not the deterministic observation"
    )
    assert "out" not in holder, (
        "clean-exit contract HELD under no stderr drain — the mutant "
        "gate has no teeth (a stderr-drain regression would slip)"
    )
