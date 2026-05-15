"""Regression tests for iter-0037 streaming/heartbeat/abort features of
`_invoke_codex` in `consensus_mcp/_dispatch_codex.py`.

These tests use:
  - `_ControllableClock` — manual `time_fn` substitute; tests advance the
    clock to drive heartbeat / silence / wall-time logic without real waits.
  - `StreamingFakeCodexPopen` — Popen-shaped fake; `stdout.readline()` returns
    scheduled bytes when the controllable clock has reached the scheduled
    time, else blocks briefly (real time, not mocked) until either the next
    schedule time is reached or the proc is told to exit.

Real `time.sleep(poll_interval)` is left as actual time. Tests use a tiny
poll_interval (0.01s) plus short real sleeps between clock-advances so the
reader thread can drain. This is acceptable: the *decision* logic in
_invoke_codex is driven entirely by `time_fn`; we only need a few ms of
real time for thread scheduling.
"""
from __future__ import annotations

import json as _json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _dispatch_codex  # noqa: E402


SCHEMA_PATH = REPO_ROOT / "consensus_mcp" / "dispatch_templates" / "codex_review_schema.json"


# --------------------------------------------------------------------------
# Test helpers
# --------------------------------------------------------------------------


class _ControllableClock:
    """Manually-advanced monotonic clock for `time_fn=` injection."""

    def __init__(self, start: float = 1000.0):
        self.t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self.t

    def advance(self, dt: float) -> None:
        with self._lock:
            self.t += float(dt)


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
    """stdout/stderr replacement.

    `readline()` returns the next scheduled `bytes` when the controllable
    clock has reached the scheduled time. Until then it sleeps (real time) in
    short ticks. When the parent fake proc is marked exited and the schedule
    is drained, `readline()` returns `b""` so `iter(readline, b"")` exits the
    reader thread (matching real Popen pipe behavior).
    """

    def __init__(self, schedule, parent):
        # schedule: list[tuple[float, bytes]] — clock-relative-to-start time + payload
        # parent: StreamingFakeCodexPopen — for clock + exit state.
        self._schedule = list(schedule)
        self._idx = 0
        self._parent = parent
        self._real_tick = 0.005

    def readline(self):
        while True:
            # If proc has exited and we've drained the schedule, EOF.
            if self._idx >= len(self._schedule):
                if self._parent._exited:
                    return b""
                # Wait for either more schedule entries (none coming) or exit.
                time.sleep(self._real_tick)
                continue
            sched_time, payload = self._schedule[self._idx]
            now_rel = self._parent._clock.now() - self._parent._t0
            if now_rel >= sched_time:
                self._idx += 1
                return payload
            # Not yet time; brief real sleep, then re-check.
            # Also bail out if proc was terminated mid-wait.
            if self._parent._exited:
                return b""
            time.sleep(self._real_tick)


class StreamingFakeCodexPopen:
    """Popen-shaped fake.

    On instantiation, captures the `-o <out_file>` arg so `.close_stdin_writes_output`
    can stage a payload there at exit time (mimicking real codex's output-file write).

    Constructor uses Popen's positional shape so the real production code's
    `popen_factory(cmd, stdin=..., stdout=..., stderr=..., bufsize=...)` call
    works unchanged.
    """

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
        _returncode: int = 0,
        _exit_at: float = 1.0,
        _clock: _ControllableClock,
        _output_payload: str = '{"ok": true}',
        **_popen_kwargs,  # iter-0039: absorb creationflags / start_new_session
    ):
        self._cmd = cmd
        self._clock = _clock
        self._t0 = _clock.now()
        self._exit_at = float(_exit_at)
        self._returncode_on_exit = int(_returncode)
        self._exited = False
        self.returncode = None
        self._output_payload = _output_payload
        # Find -o <out_file> in cmd.
        self._out_file = None
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                self._out_file = cmd[i + 1]
                break
        self.stdin = _FakeStdin()
        self.stdout = _FakePipeReader(_scheduled_stdout or [], self)
        self.stderr = _FakePipeReader(_scheduled_stderr or [], self)

    def _maybe_write_output(self):
        if self._out_file and self._returncode_on_exit == 0:
            try:
                Path(self._out_file).write_text(self._output_payload, encoding="utf-8")
            except OSError:
                pass

    def poll(self):
        if self._exited:
            return self.returncode
        now_rel = self._clock.now() - self._t0
        if now_rel >= self._exit_at:
            self.returncode = self._returncode_on_exit
            self._exited = True
            self._maybe_write_output()
            return self.returncode
        return None

    def terminate(self):
        if not self._exited:
            self.returncode = -15  # SIGTERM
            self._exited = True
            # Do NOT stage output payload on termination — matches real codex.

    def kill(self):
        if not self._exited:
            self.returncode = -9
            self._exited = True

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
        (proc.terminate()) runs. The previous value `0` was WRONG — pid 0
        means "the caller's own process group", so on Linux CI this fake
        made the abort path SIGTERM the pytest/runner job itself
        ("operation canceled"). Belt-and-suspenders: the suite-wide
        conftest guard also neutralizes os.killpg/os.getpgid."""
        return 2_147_483_647

    def wait(self, timeout=None):
        # We control exit via clock + terminate/kill; this is a no-op.
        # The production code only calls wait() after terminate/kill, by which
        # point _exited is already True. Return returncode.
        return self.returncode


def _make_factory(
    clock: _ControllableClock,
    *,
    scheduled_stdout=None,
    scheduled_stderr=None,
    returncode: int = 0,
    exit_at: float = 1.0,
    output_payload: str = '{"ok": true}',
):
    """Build a popen_factory closure for injection into `_invoke_codex`."""

    def factory(cmd, **kwargs):
        return StreamingFakeCodexPopen(
            cmd,
            _scheduled_stdout=scheduled_stdout,
            _scheduled_stderr=scheduled_stderr,
            _returncode=returncode,
            _exit_at=exit_at,
            _clock=clock,
            _output_payload=output_payload,
            **kwargs,
        )

    return factory


def _run_invoke_in_thread(**invoke_kwargs):
    """Run `_invoke_codex` in a background thread; return (thread, holder).

    `holder` is a dict that, after `thread.join()`, contains either:
      - `out`: the codex output (success), or
      - `e`: the exception raised (failure).
    """
    holder: dict = {}

    def runner():
        try:
            holder["out"] = _dispatch_codex._invoke_codex(**invoke_kwargs)
        except Exception as e:  # noqa: BLE001
            holder["e"] = e

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    return th, holder


def _drive_clock_until_done(
    clock: _ControllableClock,
    thread: threading.Thread,
    *,
    step: float = 0.05,
    real_sleep: float = 0.01,
    max_iterations: int = 2000,
):
    """Advance `clock` by `step` until `thread` finishes or `max_iterations` hit.

    A small real sleep between advances gives the reader threads + main poll
    loop scheduler time. Total real wall <= max_iterations * real_sleep.
    """
    i = 0
    while thread.is_alive() and i < max_iterations:
        clock.advance(step)
        time.sleep(real_sleep)
        i += 1
    thread.join(timeout=5)


def _read_log_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(_json.loads(line))
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


# --------------------------------------------------------------------------
# Test 1 — streamed lines appear in dispatch-log with correct seq + content
# --------------------------------------------------------------------------


def test_streamed_lines_appear_in_dispatch_log(tmp_path):
    repo_root = _setup_repo_root(tmp_path)
    clock = _ControllableClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    # 5 lines scheduled at t = 0.0 (all immediately available); proc exits at t=0.5.
    scheduled = [
        (0.0, b"line1\n"),
        (0.0, b"line2\n"),
        (0.0, b"line3\n"),
        (0.0, b"line4\n"),
        (0.0, b"line5\n"),
    ]
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=0.5)

    th, holder = _run_invoke_in_thread(
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
    _drive_clock_until_done(clock, th)
    assert not th.is_alive(), "runner did not finish"
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"
    assert holder["out"] == '{"ok": true}'

    events = _read_log_events(log_path)
    stream_events = [e for e in events if e["event"] == "dispatch_streamed_line"]
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
    clock = _ControllableClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    big = ("A" * 1000) + "\n"
    scheduled = [(0.0, big.encode("utf-8"))]
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=0.2)

    th, holder = _run_invoke_in_thread(
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
    _drive_clock_until_done(clock, th)
    assert not th.is_alive()
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"

    events = _read_log_events(log_path)
    stream = [e for e in events if e["event"] == "dispatch_streamed_line"]
    assert len(stream) == 1
    ev = stream[0]
    assert len(ev["line_truncated"]) == 200
    assert ev["line_full_length"] == 1000
    assert ev["truncated"] is True


# --------------------------------------------------------------------------
# Test 3 — heartbeat fires at heartbeat_interval cadence
# --------------------------------------------------------------------------


def test_heartbeat_fires_at_interval(tmp_path):
    """Advance time so codex appears to run for ~95s with no stdout; with
    stall_silence_seconds=200 we don't trip silence-abort; with
    heartbeat_interval=30 we should see ~3 heartbeats (at ~30, ~60, ~90).

    NOTE: pre-first-line silence falls back to `now - start_ts`. So we set
    `stall_silence_seconds` high enough (200s) to not trip silence-abort,
    and emit no stdout, just heartbeats.
    """
    repo_root = _setup_repo_root(tmp_path)
    clock = _ControllableClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    # No stdout; proc exits at clock_rel = 95s.
    factory = _make_factory(clock, scheduled_stdout=[], exit_at=95.0)

    th, holder = _run_invoke_in_thread(
        prompt="x",
        codex_bin="codex",
        timeout_seconds=300,  # so wall-time hard ceiling isn't hit
        repo_root=repo_root,
        schema_path=SCHEMA_PATH,
        log_path=log_path,
        anchors=_anchors(),
        heartbeat_interval=30.0,
        stall_silence_seconds=200.0,  # don't trip silence-abort
        poll_interval=0.01,
        time_fn=clock.now,
        popen_factory=factory,
    )

    # Advance the clock in 1s ticks up to ~100s (past exit_at=95s), with
    # short real sleeps so the main loop body can iterate, observe the
    # advanced clock, and emit heartbeats.
    for _ in range(100):
        clock.advance(1.0)
        time.sleep(0.01)
    th.join(timeout=10)
    assert not th.is_alive(), "runner did not finish"
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"

    events = _read_log_events(log_path)
    hb = [e for e in events if e["event"] == "dispatch_heartbeat"]
    # Expect heartbeats at ~30, 60, 90 → 3 events (could be 2 or 4 depending
    # on scheduler timing; assert in window).
    assert 2 <= len(hb) <= 4, f"expected ~3 heartbeats, got {len(hb)}: {hb}"
    # Ages should be monotonically increasing.
    ages = [ev["age_seconds"] for ev in hb]
    assert ages == sorted(ages), f"heartbeat ages not monotonic: {ages}"


# --------------------------------------------------------------------------
# Test 4 — heartbeat silence triggers abort
# --------------------------------------------------------------------------


def test_heartbeat_silence_triggers_abort(tmp_path):
    """Codex emits 2 lines at t=0, then goes silent; advance clock past
    stall_silence_seconds → expect `dispatch_aborted` with
    `abort_source="watchdog_silence"` and CodexInvocationError raised.
    """
    repo_root = _setup_repo_root(tmp_path)
    clock = _ControllableClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    scheduled = [
        (0.0, b"a\n"),
        (0.0, b"b\n"),
    ]
    # Proc would exit at 1000s but we'll terminate via silence-abort first.
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=1000.0)

    exc_holder = {}

    def runner():
        try:
            _dispatch_codex._invoke_codex(
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
        except Exception as e:  # noqa: BLE001
            exc_holder["e"] = e

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    # Let the reader thread consume the 2 scheduled lines.
    time.sleep(0.1)
    # Now advance the clock 15s — past the 10s stall_silence_seconds since
    # last_streamed_ts was set when those lines were read (at clock=1000.0).
    clock.advance(15.0)
    th.join(timeout=10)
    assert not th.is_alive(), "runner did not abort"

    assert "e" in exc_holder, "expected CodexInvocationError"
    assert isinstance(exc_holder["e"], _dispatch_codex.CodexInvocationError)

    events = _read_log_events(log_path)
    aborts = [e for e in events if e["event"] == "dispatch_aborted"]
    assert len(aborts) == 1, f"expected 1 abort event, got {aborts}"
    assert aborts[0]["abort_source"] == "watchdog_silence"


# --------------------------------------------------------------------------
# Test 5 — operator abort-signal file triggers abort
# --------------------------------------------------------------------------


def test_operator_abort_signal_file_triggers_abort(tmp_path):
    """Mid-run, write `consensus-state/state/abort-dispatch-<pass_id>.signal`;
    expect wrapper to SIGTERM codex, emit `dispatch_aborted` with
    `abort_source="operator_signal_file"`, delete the signal file, and raise.
    """
    repo_root = _setup_repo_root(tmp_path)
    clock = _ControllableClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    # Codex would run a long time; we'll abort externally.
    factory = _make_factory(clock, scheduled_stdout=[(0.0, b"working\n")], exit_at=1000.0)

    exc_holder = {}

    def runner():
        try:
            _dispatch_codex._invoke_codex(
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
        except Exception as e:  # noqa: BLE001
            exc_holder["e"] = e

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    # Wait a beat so the loop is running.
    time.sleep(0.1)
    signal_path = repo_root / "consensus-state" / "state" / "abort-dispatch-codex-test-pass1.signal"
    signal_path.write_text("operator manual abort", encoding="utf-8")
    th.join(timeout=10)
    assert not th.is_alive(), "runner did not abort"

    assert "e" in exc_holder, "expected CodexInvocationError"
    assert isinstance(exc_holder["e"], _dispatch_codex.CodexInvocationError)

    events = _read_log_events(log_path)
    aborts = [e for e in events if e["event"] == "dispatch_aborted"]
    assert len(aborts) == 1, f"expected 1 abort event, got {aborts}"
    assert aborts[0]["abort_source"] == "operator_signal_file"
    assert "operator manual abort" in aborts[0].get("abort_reason", "")

    # Signal file should be deleted.
    assert not signal_path.exists(), "abort signal file was not deleted"


# --------------------------------------------------------------------------
# Test 6 — wall-time hard ceiling
# --------------------------------------------------------------------------


def test_wall_time_hard_ceiling(tmp_path):
    """Codex streams continuously (so silence-abort never fires) but runs
    past `timeout_seconds + stall_silence_seconds` → expect dispatch_aborted
    with abort_source="wall_time_hard_ceiling" and CodexInvocationError.
    """
    repo_root = _setup_repo_root(tmp_path)
    clock = _ControllableClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    # Stream a line every 1s of clock-time forever, so silence never fires.
    # The reader thread reads scheduled bytes as clock advances past them.
    scheduled = [(float(i), f"keepalive{i}\n".encode("utf-8")) for i in range(0, 200)]
    factory = _make_factory(clock, scheduled_stdout=scheduled, exit_at=10000.0)

    exc_holder = {}

    def runner():
        try:
            _dispatch_codex._invoke_codex(
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
        except Exception as e:  # noqa: BLE001
            exc_holder["e"] = e

    th = threading.Thread(target=runner, daemon=True)
    th.start()

    # Advance clock in 1s steps; reader drains scheduled keepalives as we go,
    # so silence_age stays low. After ~16s of clock advance, wall-time hard
    # ceiling (10 + 5 = 15s) should trip.
    for _ in range(20):
        clock.advance(1.0)
        time.sleep(0.02)
    th.join(timeout=10)
    assert not th.is_alive(), "runner did not abort"

    assert "e" in exc_holder, "expected CodexInvocationError"
    assert isinstance(exc_holder["e"], _dispatch_codex.CodexInvocationError)

    events = _read_log_events(log_path)
    aborts = [e for e in events if e["event"] == "dispatch_aborted"]
    assert len(aborts) == 1, f"expected 1 abort, got {aborts}"
    assert aborts[0]["abort_source"] == "wall_time_hard_ceiling"


# --------------------------------------------------------------------------
# Test 7 — clean exit returns codex output payload
# --------------------------------------------------------------------------


def test_clean_exit_returns_output(tmp_path):
    repo_root = _setup_repo_root(tmp_path)
    clock = _ControllableClock()
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
    _drive_clock_until_done(clock, th)
    assert not th.is_alive()
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"
    assert holder["out"] == payload

    events = _read_log_events(log_path)
    aborts = [e for e in events if e["event"] == "dispatch_aborted"]
    assert aborts == [], f"clean exit produced abort events: {aborts}"
    stream = [e for e in events if e["event"] == "dispatch_streamed_line"]
    assert len(stream) == 2


# --------------------------------------------------------------------------
# Optional 8th — stderr drain prevents deadlock
# --------------------------------------------------------------------------


def test_stderr_drain_prevents_deadlock(tmp_path):
    """codex emits ~100KB to stderr; without the stderr-reader thread real
    codex would block on a full pipe buffer. With the drain in place,
    dispatch completes normally.

    In this fake we simulate by scheduling many stderr lines; the test
    asserts that _invoke_codex returns cleanly within the test budget.
    """
    repo_root = _setup_repo_root(tmp_path)
    clock = _ControllableClock()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    big_line = b"E" * 1000 + b"\n"
    # ~100 lines × 1001 bytes ≈ 100KB on stderr.
    scheduled_err = [(0.0, big_line) for _ in range(100)]
    scheduled_out = [(0.0, b"ok\n")]
    factory = _make_factory(
        clock,
        scheduled_stdout=scheduled_out,
        scheduled_stderr=scheduled_err,
        exit_at=0.3,
    )

    th, holder = _run_invoke_in_thread(
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
    _drive_clock_until_done(clock, th)
    assert not th.is_alive()
    assert "e" not in holder, f"unexpected exception: {holder.get('e')}"
    assert holder["out"] == '{"ok": true}'
