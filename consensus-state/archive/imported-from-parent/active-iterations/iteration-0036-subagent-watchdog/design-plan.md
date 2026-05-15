# iter-0036 design — bidirectional dispatch monitoring + watchdog

Operator-driven design for true monitoring + control of codex dispatches.
One-shot fire-and-forget was empirically inadequate (iter-0036 first
pre-review dispatch hung 29 minutes past its 15-min internal timeout
with zero visibility). This design adds:
- Real-time streaming of codex stdout into the audit log
- Periodic heartbeat events while codex runs
- Operator abort signal via file
- Watchdog with auto-abort triggered by heartbeat silence (not wall time)

Operator preferences locked in (2026-05-11):
1. **Streamed lines truncated to 200 chars** — every line, full structure
   visible, log not bloated.
2. **30-second heartbeat interval** — 2 missed heartbeats = ~1 min stall
   detection.
3. **Auto-abort ONLY on heartbeat-silence** (not wall-time) — genuinely
   slow codex with streaming output is left alone; truly stuck codex
   gets killed.
4. **Popen pipe + readline** for stdout capture (standard pattern).

## Bidirectional limitations to be explicit about

codex-cli has no mid-run input channel. Once invoked, codex consumes the
prompt and emits output; there is no protocol for "send more input to
codex while it thinks." So "bidirectional" here means:

- **Codex → us**: real-time stdout streaming (NEW; one-way info flow)
- **Us → codex**: SIGTERM via abort-signal file (NEW; one-way control)

True conversational bidirectional (multiple sub-prompts within one
codex session) would require either a different codex invocation pattern
(short codex calls in a loop with our orchestration in between) OR a
codex feature we don't have. That's an iter-0037+ architectural question
out of scope here.

## Event schema additions

All new events share the existing `pass_id` / `iteration_id` /
`reviewer_id` anchor convention.

```jsonl
{"event":"dispatch_streamed_line","pass_id":"<id>","iteration_id":"<id>",
 "reviewer_id":"<id>","stream":"stdout","line_truncated":"<≤200 chars>",
 "line_full_length":<int>,"truncated":<bool>,"timestamp_utc":"..."}

{"event":"dispatch_heartbeat","pass_id":"<id>","iteration_id":"<id>",
 "reviewer_id":"<id>","age_seconds":<float>,
 "last_streamed_line_age_seconds":<float|null>,
 "last_streamed_line_seq":<int|null>,
 "timestamp_utc":"..."}

{"event":"dispatch_aborted","pass_id":"<id>","iteration_id":"<id>",
 "reviewer_id":"<id>","abort_source":"operator_signal_file"|"watchdog_silence",
 "abort_reason":"<string>","age_seconds":<float>,
 "last_streamed_line_age_seconds":<float|null>,"timestamp_utc":"..."}

{"event":"dispatch_stalled","pass_id":"<id>","iteration_id":"<id>",
 "reviewer_id":"<id>","stall_reason":"watchdog_orphan_detected",
 "age_seconds":<float>,"timestamp_utc":"..."}
```

`dispatch_streamed_line` is the highest-volume event. We bound it via
`line_truncated` (≤200 chars) + `line_full_length` (so the consumer
knows when truncation lost info). Long lines (codex's structured JSON
output) get truncated visibly.

`dispatch_heartbeat` carries `last_streamed_line_age_seconds` so the
watchdog can detect silence: if `now - last_heartbeat.last_streamed_line_age_seconds`
exceeds the silence threshold, codex is stuck.

`dispatch_aborted` and `dispatch_stalled` are both terminal events (pair
with the dispatch_start by pass_id like dispatch_done/dispatch_failed).
Aborted = active intervention; stalled = post-hoc detection.

## `_dispatch_codex.py` changes (sketch)

Replace `subprocess.run(...)` with `Popen + threaded readline loop`:

```python
def _invoke_codex(prompt, codex_bin, timeout_seconds, repo_root, schema_path,
                  log_path, anchors):
    """Run codex via Popen; stream stdout into dispatch-log as it arrives;
    emit periodic heartbeats; watch for abort-signal file; SIGTERM on
    silence (no streamed line in last STALL_SILENCE_SECONDS).
    """
    proc = subprocess.Popen([codex_bin, "exec", ...],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            cwd=str(repo_root),
                            text=False,  # binary; we decode per line
                            bufsize=1)
    proc.stdin.write(prompt.encode("utf-8"))
    proc.stdin.close()

    start_ts = time.time()
    last_streamed_ts = None
    streamed_seq = 0
    stdout_buf = []  # collect raw bytes for final output_sha256
    abort_path = repo_root / "agent-loop" / "state" / f"abort-dispatch-{anchors['pass_id']}.signal"

    # Read stdout in a thread (or async); main loop emits heartbeats and
    # checks abort signal.
    def reader():
        nonlocal last_streamed_ts, streamed_seq
        for raw_line in proc.stdout:
            stdout_buf.append(raw_line)
            try:
                line_str = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            except Exception:
                continue
            full_len = len(line_str)
            truncated = full_len > 200
            line_event = line_str[:200]
            _log_dispatch(log_path, {
                "event": "dispatch_streamed_line",
                **anchors,
                "stream": "stdout",
                "line_truncated": line_event,
                "line_full_length": full_len,
                "truncated": truncated,
                "seq": streamed_seq,
            })
            streamed_seq += 1
            last_streamed_ts = time.time()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    HEARTBEAT_INTERVAL = 30.0
    STALL_SILENCE_SECONDS = 90.0  # 3 heartbeats with no output → kill
    last_heartbeat = start_ts

    while proc.poll() is None:
        now = time.time()
        # Operator abort?
        if abort_path.exists():
            try:
                abort_reason = abort_path.read_text(encoding="utf-8").strip() or "operator_signal_file"
            except OSError:
                abort_reason = "operator_signal_file (unreadable)"
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            _log_dispatch(log_path, {
                "event": "dispatch_aborted",
                **anchors,
                "abort_source": "operator_signal_file",
                "abort_reason": abort_reason,
                "age_seconds": now - start_ts,
                "last_streamed_line_age_seconds": (now - last_streamed_ts) if last_streamed_ts else None,
            })
            try:
                abort_path.unlink()
            except OSError:
                pass
            raise CodexInvocationError(f"dispatch aborted by operator: {abort_reason}")

        # Heartbeat tick?
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            silence_age = (now - last_streamed_ts) if last_streamed_ts else (now - start_ts)
            _log_dispatch(log_path, {
                "event": "dispatch_heartbeat",
                **anchors,
                "age_seconds": now - start_ts,
                "last_streamed_line_age_seconds": silence_age,
                "last_streamed_line_seq": streamed_seq - 1 if streamed_seq > 0 else None,
            })
            last_heartbeat = now

            # Heartbeat-silence auto-abort: if codex has been silent for
            # STALL_SILENCE_SECONDS, assume stuck.
            if silence_age >= STALL_SILENCE_SECONDS:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                _log_dispatch(log_path, {
                    "event": "dispatch_aborted",
                    **anchors,
                    "abort_source": "watchdog_silence",
                    "abort_reason": f"no codex stdout for {silence_age:.0f}s (threshold {STALL_SILENCE_SECONDS}s)",
                    "age_seconds": now - start_ts,
                    "last_streamed_line_age_seconds": silence_age,
                })
                raise CodexInvocationError(
                    f"codex stuck: no output for {silence_age:.0f}s"
                )

        # Wall-time timeout still respected as final fallback.
        if now - start_ts >= timeout_seconds:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise CodexInvocationError(f"codex exceeded {timeout_seconds}s wall timeout")

        time.sleep(0.5)

    # Codex exited; drain remaining stdout via thread join.
    t.join(timeout=5)
    full_output = b"".join(stdout_buf).decode("utf-8", errors="replace")
    if proc.returncode != 0:
        stderr_text = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        raise CodexInvocationError(
            f"codex exit={proc.returncode}; stderr_tail={stderr_text[-2000:]!r}"
        )
    return full_output
```

## `_visibility_watchdog.py` (companion, sweeps stale orphans)

Same as the original proposal but tightened:
- Recognizes 4 terminal events: dispatch_done, dispatch_failed,
  dispatch_aborted, dispatch_stalled.
- Default `--stall-after 600s` (10 min) — used for retroactive cleanup
  of legacy orphans that existed before this dispatch refactor.
- `--action mark` appends `dispatch_stalled` for orphans (audit hygiene
  only; the live auto-abort happens inside `_dispatch_codex` now).
- The watchdog is now SECONDARY (cleanup); the primary stall defense is
  the in-dispatch silence-watcher.

## `_visibility_tui.py` updates

- Treat `dispatch_streamed_line` and `dispatch_heartbeat` as
  non-terminal (ignore for pairing); show summary instead.
- Treat `dispatch_aborted` and `dispatch_stalled` as terminal.
- For active dispatches, show:
  - "last_line: <truncated content>" (most recent dispatch_streamed_line)
  - "last_heartbeat: <age>s ago" (most recent heartbeat)
  - "stream_silence: <silence_age>s" (when concerning)
- Color: silence ≥ 60s = yellow; silence ≥ 90s = red.

## Open architectural questions

1. **Threading vs asyncio**: reader thread is simpler but has graceful-
   shutdown subtleties on Windows. Asyncio is cleaner conceptually but
   adds complexity to a previously-synchronous function. Recommend
   thread (matches existing subprocess patterns elsewhere).

2. **`STALL_SILENCE_SECONDS` default**: 90s gives 3 missed heartbeats at
   30s cadence — feels right but is a guess. Alternative: 60s (faster
   detection, more false positives on slow LLMs).

3. **Wall-time fallback**: still respected as a hard ceiling. Should
   `dispatch_failed` event for wall-timeout vs heartbeat-silence carry
   different `error_type` so the operator can tell them apart? Recommend
   yes: `WallTimeExceeded` vs `HeartbeatSilenceStall`.

4. **Test scaffold**: mocking Popen + thread + time is significantly
   harder than mocking subprocess.run. Plan: extract `_invoke_codex` into
   a class with injectable time source and injectable subprocess factory
   so tests can drive timing deterministically.

5. **Backward compatibility risk**: every existing test that mocks
   `subprocess.run` for `_dispatch_codex` invocations (there are ~10 of
   them across test_dispatch_codex.py + test_capstone_full_fix_loop.py)
   will need updating to mock `Popen` instead. Non-trivial migration.

## Implementation phases

Phase A (~150 LOC change):
- Add streamed-line + heartbeat emission via Popen+thread in _dispatch_codex
- Update existing tests to mock Popen
- Verify pytest stays green

Phase B (~80 LOC):
- Add abort-signal-file polling + dispatch_aborted emission
- Heartbeat-silence auto-abort logic + dispatch_aborted emission
- New regression tests

Phase C (~100 LOC):
- _visibility_watchdog.py (retroactive cleanup; same shape as original
  proposal but recognizes dispatch_aborted as terminal)
- New regression tests

Phase D (~50 LOC):
- _visibility_tui.py updates to show streamed/heartbeat state
- New regression tests

All phases land in iter-0036 together (one commit) since they're tightly
coupled in event schema.

## Reviewer questions (for codex pre-review)

1. **Architecture sanity**: does the Popen+reader-thread pattern + main-
   loop polling pattern have any obvious Windows-specific failure modes
   that would re-create the iter-0036 stall? (E.g., codex-cli not
   line-flushing → reader thread sees no lines → heartbeat-silence
   incorrectly fires.)
2. **`bufsize=1` semantics**: in Python 3 binary mode, `bufsize=1` is
   deprecated. Should we use `bufsize=0` (unbuffered) or set explicitly
   via os.set_blocking?
3. **Signal handling on Windows**: `proc.terminate()` on Windows calls
   TerminateProcess; codex's children (if any) become orphans. Should
   we use a process group or just accept the leak?
4. **Event volume bounds**: 200-char-truncated stream events × thousands
   of codex output lines per dispatch can push the JSONL log to MB-scale.
   Acceptable, or should we cap stream events at N per dispatch?
5. **Abort-signal-file race**: operator writes file at T+0; wrapper polls
   at T+0.5 (every 500ms). Worst case: 500ms latency before SIGTERM
   fires. Acceptable, or should we use OS file-watching (inotify on
   Linux, ReadDirectoryChangesW on Windows)?
6. **Threading vs subprocess.run with stdout=PIPE + read() in a loop**:
   could we avoid threads entirely by doing non-blocking reads on
   the pipe from the main loop? Cleaner if it works; the only complexity
   is whether Python's pipe support handles non-blocking on Windows.
