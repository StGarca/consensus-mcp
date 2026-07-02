"""Streaming + silence-watchdog integration tests for `_invoke_grok`.

These cover the DEFECT 1/2 fix (docs/grok-dispatch-streaming-watchdog-fix.md):
  - `--output-format streaming-json` emits thought/text event lines
    continuously, so the silence watchdog stays fed (where the old `plain`
    output buffered everything and starved it). The Gate-G5 regression test
    proves a streaming run survives a wall-window that silence kills.
  - The answer is reassembled from `text` events by `_assemble_grok_stream`.
  - grok runs from a fresh empty per-pass temp `--cwd` that is always
    removed afterwards (DEFECT 2).
  - A self-cancel (stopReason==Cancelled with zero text) surfaces as a
    `GrokStreamCancelledError` invocation failure.

The deterministic clock harness is the proven `_SyncClock`/`_FakePipeReader`
design from `test_dispatch_codex_streaming.py` (v1.15.9 - fully
Condition-driven, no real-sleep timing). It is injected into production via
`_invoke_grok`'s `_sleep=`/`time_fn=`/`popen_factory=` seams so the watchdog
tests are deterministic, NOT wall-clock-timed (the pattern that flaked on
loaded CI before the codex `_sleep` rewrite).
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
import sys  # noqa: E402

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _dispatch_grok  # noqa: E402


_CEILING = 100.0   # absolute real-time safety ceiling for any harness wait
_REPOLL = 0.05    # lost-wakeup safety net; correctness never depends on it


class _SyncClock:
    """Deterministic virtual clock with an explicit test<->runner
    happens-before (ported verbatim from the codex streaming harness)."""

    def __init__(self, start: float = 1000.0):
        self.t = float(start)
        self._cond = threading.Condition()
        self._epoch = 0
        self._park_epoch = -1
        self._sleepers = 0
        self._released = False

    def now(self) -> float:
        with self._cond:
            return self.t

    def sleep(self, _dt: float) -> None:
        """Injected as production `_invoke_grok(_sleep=...)`. Park until
        virtual time passes the wake target OR the clock is released."""
        deadline = time.monotonic() + _CEILING
        with self._cond:
            wake_at = self.t + float(_dt)
            while self.t < wake_at and not self._released:
                if time.monotonic() >= deadline:
                    return
                self._sleepers += 1
                self._park_epoch = max(self._park_epoch, self._epoch)
                self._cond.notify_all()
                self._cond.wait(_REPOLL)
                self._sleepers -= 1

    def advance(self, dt: float) -> int:
        with self._cond:
            self.t += float(dt)
            self._epoch += 1
            self._cond.notify_all()
            return self._epoch

    def wait_for(self, predicate) -> bool:
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

    def notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def is_released(self) -> bool:
        with self._cond:
            return self._released

    def release_all(self) -> None:
        with self._cond:
            self._released = True
            self._cond.notify_all()


class _FakePipeReader:
    """stdout/stderr replacement. The producer (`StreamingFakeGrokPopen`)
    writes due scheduled lines; the production reader thread consumes via
    `readline()`, which returns b"" (EOF) once the proc exited and the
    schedule/buffer are drained. (Unbounded capacity; grok needs no
    backpressure model.)"""

    def __init__(self, schedule, parent, *, capacity=None):
        self._schedule = list(schedule)
        self._schedule_idx = 0
        self._parent = parent
        self._capacity = capacity
        self._buffer = collections.deque()
        self._buffered_bytes = 0
        self._writer_blocked = False

    @property
    def _pending_schedule(self) -> bool:
        return self._schedule_idx < len(self._schedule)

    def write(self, data: bytes) -> bool:
        with self._parent._clock._cond:
            if self._parent._clock.is_released():
                return True
            if (
                self._capacity is None
                or self._buffered_bytes + len(data) <= self._capacity
            ):
                self._buffer.append(data)
                self._buffered_bytes += len(data)
                self._parent._clock.notify()
                return True
            self._writer_blocked = True
            self._parent._clock.notify()
            return False

    def readline(self):
        self._parent._clock.notify()
        deadline = time.monotonic() + _CEILING
        with self._parent._clock._cond:
            while not self._parent._clock.is_released():
                if self._buffer:
                    line = self._buffer.popleft()
                    self._buffered_bytes -= len(line)
                    self._writer_blocked = False
                    self._parent._clock.notify()
                    return line
                if self._parent._exited and not self._pending_schedule:
                    return b""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._parent._clock._cond.wait(min(remaining, _REPOLL))
            return b""


class StreamingFakeGrokPopen:
    """Popen-shaped fake for grok: stdout/stderr only (grok passes no
    stdin, no `-o` output file). Records the `cwd` kwarg so a test can
    assert grok ran from the fresh per-pass temp dir. Exit transitions
    notify the clock so reader threads + driver wake deterministically."""

    def __init__(
        self,
        cmd,
        stdout=None,
        stderr=None,
        bufsize=0,
        cwd=None,
        *,
        _scheduled_stdout=None,
        _scheduled_stderr=None,
        _returncode: int = 0,
        _exit_at: float = 1.0,
        _clock: _SyncClock,
        **_popen_kwargs,  # absorb start_new_session / creationflags
    ):
        self._cmd = cmd
        self.cwd = cwd
        self._clock = _clock
        self._t0 = _clock.now()
        self._exit_at = float(_exit_at)
        self._returncode_on_exit = int(_returncode)
        self._exited = False
        self._terminated = False
        self.returncode = None
        self.stdout = _FakePipeReader(_scheduled_stdout or [], self)
        self.stderr = _FakePipeReader(_scheduled_stderr or [], self)

    def _service_pipes(self):
        now_rel = self._clock.now() - self._t0
        for pipe in (self.stdout, self.stderr):
            while pipe._pending_schedule:
                sched_time, payload = pipe._schedule[pipe._schedule_idx]
                if now_rel < sched_time:
                    break
                if not pipe.write(payload):
                    break
                pipe._schedule_idx += 1

    def poll(self):
        if self._exited:
            return self.returncode
        if self._clock.is_released():
            self._terminated = True
            self._exited = True
            self.returncode = -15
            return self.returncode
        self._service_pipes()
        now_rel = self._clock.now() - self._t0
        if now_rel >= self._exit_at:
            self.returncode = self._returncode_on_exit
            self._exited = True
            self._clock.notify()
            return self.returncode
        return None

    def terminate(self):
        self._terminated = True
        if not self._exited:
            self.returncode = -15
            self._exited = True
            self._clock.notify()

    def kill(self):
        self._terminated = True
        if not self._exited:
            self.returncode = -9
            self._exited = True
            self._clock.notify()

    def send_signal(self, sig):
        self.terminate()

    @property
    def pid(self):
        return 2_147_483_647  # never-live PID -> os.getpgid raises -> fallback

    def wait(self, timeout=None):
        return self.returncode


def _make_factory(clock, *, scheduled_stdout=None, scheduled_stderr=None,
                  returncode=0, exit_at=1.0):
    instances: list = []

    def factory(cmd, **kwargs):
        p = StreamingFakeGrokPopen(
            cmd,
            _scheduled_stdout=scheduled_stdout,
            _scheduled_stderr=scheduled_stderr,
            _returncode=returncode,
            _exit_at=exit_at,
            _clock=clock,
            **kwargs,
        )
        instances.append(p)
        return p

    factory.instances = instances
    return factory


def _run_invoke_in_thread(clock, **invoke_kwargs):
    holder: dict = {}

    def runner():
        try:
            holder["out"] = _dispatch_grok._invoke_grok(
                _sleep=clock.sleep, time_fn=clock.now, **invoke_kwargs
            )
        except Exception as e:  # noqa: BLE001
            holder["e"] = e
        finally:
            clock.notify()

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    return th, holder


def _drive(clock, th, *, chunk: float = 50.0, max_rounds: int = 64):
    """Advance virtual time in chunks until the runner thread dies
    (clean exit or raise). Loud-fail via release_all on any wedge."""
    if not clock.wait_for(lambda: (not th.is_alive()) or clock._sleepers > 0):
        clock.release_all()
        th.join(timeout=5)
        raise AssertionError("_drive: runner neither parked nor exited at startup")
    rounds = 0
    while th.is_alive() and rounds < max_rounds:
        e = clock.advance(chunk)
        if not clock.wait_for(
            lambda e=e: (not th.is_alive())
            or (clock._sleepers > 0 and clock._park_epoch >= e)
        ):
            clock.release_all()
            th.join(timeout=5)
            raise AssertionError("_drive: per-round wait hit _CEILING (wedge)")
        rounds += 1
    exhausted = th.is_alive() and rounds >= max_rounds
    clock.release_all()
    th.join(timeout=5)
    if th.is_alive() or exhausted:
        raise AssertionError(f"_drive: runner did not finish within {max_rounds} rounds")


def _drive_streaming(clock, th, log_path, *, step: float, until, max_steps: int = 128):
    """Advance `step` (< stall_silence) at a time; after each advance wait
    until production PROCESSED a new streamed line (so last_streamed_ts is
    provably fresh at every silence check) OR `until` fired OR the runner
    died. Proves streaming keeps the watchdog fed."""
    if not clock.wait_for(lambda: (not th.is_alive()) or clock._sleepers > 0):
        clock.release_all()
        th.join(timeout=5)
        raise AssertionError("_drive_streaming: runner never started")
    steps = 0
    while th.is_alive() and not until() and steps < max_steps:
        prev = len(_events(log_path, "dispatch_streamed_line"))
        clock.advance(step)
        # Block until production has actually PROCESSED a new streamed line
        # (so last_streamed_ts is provably fresh before the next advance) OR
        # the run reached its terminal state. NOTE: do NOT also return on
        # `clock._sleepers > 0` - the runner parks in `_sleep` every poll
        # iteration, so that would let the driver advance the virtual clock
        # again before the reader thread refreshes last_streamed_ts. Under
        # reader-thread lag (loaded CI) the clock then outruns the last line
        # and the silence watchdog false-fires. This mirrors the codex
        # _drive_streaming, whose per-step wait keys ONLY on processed-line
        # progress / death / until(), with _CEILING as the sole wedge net.
        if not clock.wait_for(
            lambda prev=prev: (not th.is_alive())
            or until()
            or len(_events(log_path, "dispatch_streamed_line")) > prev
        ):
            clock.release_all()
            th.join(timeout=5)
            raise AssertionError("_drive_streaming: per-step wait hit _CEILING (wedge)")
        steps += 1
    exhausted = th.is_alive() and not until() and steps >= max_steps
    clock.release_all()
    th.join(timeout=5)
    if th.is_alive() or exhausted:
        raise AssertionError(f"_drive_streaming: did not reach terminal within {max_steps} steps")


def _read_log_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _events(log_path, name):
    return [e for e in _read_log_events(log_path) if e.get("event") == name]


def _setup_repo_root(tmp_path: Path) -> Path:
    (tmp_path / "consensus-state" / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _anchors():
    return {
        "iteration_id": "iter-test",
        "reviewer_id": "grok-test",
        "pass_id": "grok-test-pass1",
    }


@pytest.fixture(autouse=True)
def _no_auth_preflight(monkeypatch):
    """The streaming/watchdog behavior under test is independent of the
    ~/.grok/auth.json pre-flight (covered separately in test_dispatch_grok)."""
    monkeypatch.setattr(_dispatch_grok, "_check_grok_auth", lambda: None)


def _stream_lines(*events):
    return [(t, (json.dumps(e) + "\n").encode("utf-8")) for (t, e) in events]


# --------------------------------------------------------------------------
# I1 - streaming run assembles the answer, runs from a fresh temp cwd, cleans up
# --------------------------------------------------------------------------

def test_streaming_assembles_answer_and_uses_clean_temp_cwd(tmp_path):
    repo_root = _setup_repo_root(tmp_path)
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"
    clock = _SyncClock()

    scheduled = _stream_lines(
        (0.0, {"type": "thought", "data": "reasoning"}),
        (0.0, {"type": "text", "data": '{"goal_satisfied":'}),
        (0.0, {"type": "text", "data": ' true, "findings": []}'}),
        (0.0, {"type": "end", "stopReason": "EndTurn"}),
    )
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=0.5)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="hi", grok_bin="grok", model=None,
        timeout_seconds=60, iter_dir=tmp_path, pass_id="grok-test-pass1",
        repo_root=repo_root, log_path=log_path, anchors=_anchors(),
        heartbeat_interval=30.0, stall_silence_seconds=90.0, poll_interval=0.01,
        popen_factory=factory,
    )
    _drive(clock, th)

    assert not th.is_alive()
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"
    answer, prompt_path = holder["out"]
    assert answer == '{"goal_satisfied": true, "findings": []}'
    assert json.loads(answer)["goal_satisfied"] is True

    # grok ran from a fresh per-pass temp dir (NOT the repo root) ...
    run_cwd = factory.instances[0].cwd
    assert "grok-run-" in os.path.basename(run_cwd)
    assert os.path.realpath(run_cwd) != os.path.realpath(str(repo_root))
    # ... and that temp dir was removed afterwards (no leak on any path).
    assert not os.path.exists(run_cwd), "per-pass grok temp cwd was not cleaned up"


# --------------------------------------------------------------------------
# I2 - streaming keeps the watchdog FED across a window that silence kills
#      (Gate G5: the case that would have tripped the old silence watchdog)
# --------------------------------------------------------------------------

def test_streaming_keeps_watchdog_fed(tmp_path):
    repo_root = _setup_repo_root(tmp_path)
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"
    clock = _SyncClock()

    # Thought lines every 2s out to 16s, then the answer + end at 16s. The
    # 16s total span FAR exceeds stall_silence_seconds=5 - under the old
    # `plain` behavior (one buffered blob at the end) the watchdog would
    # have fired at 5s. Because each streamed line refreshes last_streamed_ts
    # and the lines are 2s apart (< 5s), the watchdog never trips.
    events = [(float(i), {"type": "thought", "data": f"t{i}"}) for i in range(0, 16, 2)]
    events.append((16.0, {"type": "text", "data": "ANSWER-OK"}))
    events.append((16.0, {"type": "end", "stopReason": "EndTurn"}))
    scheduled = _stream_lines(*events)
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=17.0)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="hi", grok_bin="grok", model=None,
        timeout_seconds=300, iter_dir=tmp_path, pass_id="grok-test-pass1",
        repo_root=repo_root, log_path=log_path, anchors=_anchors(),
        heartbeat_interval=1000.0, stall_silence_seconds=5.0, poll_interval=0.01,
        popen_factory=factory,
    )
    _drive_streaming(clock, th, log_path, step=2.0, until=lambda: not th.is_alive())

    assert not th.is_alive()
    assert "e" not in holder, f"streaming run aborted unexpectedly: {holder.get('e')}"
    answer, _ = holder["out"]
    assert answer == "ANSWER-OK"
    # The watchdog never fired despite a 16s wall span vs a 5s silence window.
    assert _events(log_path, "dispatch_aborted") == [], "watchdog tripped on a fed stream"


# --------------------------------------------------------------------------
# I3 - the failure mode streaming fixes: a silent run trips the watchdog
# --------------------------------------------------------------------------

def test_silent_run_trips_watchdog(tmp_path):
    """A run that produces NO stdout lines (the old `plain` failure shape:
    grok thinks past the window with everything buffered) trips the silence
    watchdog and raises - the bug DEFECT 1 fixes by streaming."""
    repo_root = _setup_repo_root(tmp_path)
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"
    clock = _SyncClock()

    factory = _make_factory(clock, scheduled_stdout=[], exit_at=10000.0)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="hi", grok_bin="grok", model=None,
        timeout_seconds=10, iter_dir=tmp_path, pass_id="grok-test-pass1",
        repo_root=repo_root, log_path=log_path, anchors=_anchors(),
        heartbeat_interval=1000.0, stall_silence_seconds=180.0, poll_interval=0.01,
        popen_factory=factory,
    )
    _drive(clock, th)

    assert not th.is_alive()
    assert "e" in holder, "expected a GrokInvocationError for the silent run"
    assert isinstance(holder["e"], _dispatch_grok.GrokInvocationError)
    assert "grok stuck" in str(holder["e"])
    aborts = _events(log_path, "dispatch_aborted")
    assert len(aborts) == 1
    assert aborts[0]["abort_source"] == "watchdog_silence"


# --------------------------------------------------------------------------
# I4 - self-cancel (Cancelled with zero text) surfaces as an invocation error
# --------------------------------------------------------------------------

def test_self_cancel_raises_invocation_error_and_cleans_cwd(tmp_path):
    repo_root = _setup_repo_root(tmp_path)
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"
    clock = _SyncClock()

    scheduled = _stream_lines(
        (0.0, {"type": "thought", "data": "thinking..."}),
        (0.0, {"type": "thought", "data": "still thinking..."}),
        (0.0, {"type": "end", "stopReason": "Cancelled"}),
    )
    # grok exits 0 even on a Cancelled stream -> the returncode gate cannot
    # catch it; the assembler must.
    factory = _make_factory(clock, scheduled_stdout=scheduled, returncode=0, exit_at=0.5)

    th, holder = _run_invoke_in_thread(
        clock,
        prompt="hi", grok_bin="grok", model=None,
        timeout_seconds=60, iter_dir=tmp_path, pass_id="grok-test-pass1",
        repo_root=repo_root, log_path=log_path, anchors=_anchors(),
        heartbeat_interval=30.0, stall_silence_seconds=90.0, poll_interval=0.01,
        popen_factory=factory,
    )
    _drive(clock, th)

    assert not th.is_alive()
    assert "e" in holder, "expected GrokStreamCancelledError"
    assert isinstance(holder["e"], _dispatch_grok.GrokStreamCancelledError)
    # Cleanup still happens on the raise path.
    assert not os.path.exists(factory.instances[0].cwd), "temp cwd leaked on cancel"
