# iter-0037 proposed patches — bidirectional dispatch (deferred half of Task #43)

Workflow #4 pre-review of the Popen-streaming + heartbeat + abort-signal-file
rewrite of `_invoke_codex` in `scripts/agent_loop_mcp/_dispatch_codex.py`.
Builds directly on the design approved by codex in iter-0036 design-plan.md
review (codex-iter0036-2). Operator preferences locked in 2026-05-11:
200-char truncation, 30s heartbeat, auto-abort ONLY on heartbeat-silence
(not wall-time), Popen+readline thread.

Per iter-0036 stall lesson: this doc is PROSE-FIRST with targeted diffs.
Embedded code excerpts kept tight. No full-file embeds.

---

## Patch 1 — replace `subprocess.run` with `Popen + reader thread`

**Defect**: current `_invoke_codex` uses blocking `subprocess.run(... capture_output=True, timeout=N)`. No visibility while codex runs; no recovery if subprocess.run itself hangs past timeout (iter-0036 codex-1 stall: 29 min with no terminal event despite 15-min internal timeout).

**Approach**:
1. Initialize: pass log_path + anchors (iteration_id/reviewer_id/pass_id) into `_invoke_codex` so it can emit per-line events.
2. Spawn codex via `subprocess.Popen(..., stdout=PIPE, stderr=PIPE, stdin=PIPE, bufsize=0)`.
3. Write the prompt to stdin and close stdin.
4. Reader thread reads stdout line-by-line, emits `dispatch_streamed_line` events (truncated to 200 chars), records raw bytes for final `output_sha256`.
5. Main loop ticks every 500ms: check `proc.poll()`, check abort-signal file, emit heartbeat every 30s, check heartbeat-silence threshold.
6. On exit: join reader thread, drain remaining bytes, return decoded output for downstream parsing.

**Diff (conceptual; ~120 LOC change at lines ~440-560 of `_dispatch_codex.py`)**:

```diff
--- a/scripts/agent_loop_mcp/_dispatch_codex.py
+++ b/scripts/agent_loop_mcp/_dispatch_codex.py
@@ -1,3 +1,4 @@
+import threading
 import argparse
 ...
@@ -440,~50 -def _invoke_codex(prompt: str, codex_bin: str, timeout_seconds: int,
-                  repo_root: Path, schema_path: Path) -> str:
-    """Run codex via subprocess.run; return stdout text."""
-    out_path = ... tempfile ...
-    proc = subprocess.run(
-        [codex_bin, "exec", "--output-schema", str(schema_path),
-         "--cd", str(repo_root), "-o", out_path],
-        input=prompt.encode("utf-8"),
-        capture_output=True,
-        timeout=timeout_seconds,
-        check=False,
-    )
-    if proc.returncode != 0:
-        raise CodexInvocationError(...)
-    return Path(out_path).read_text(encoding="utf-8")
+def _invoke_codex(prompt: str, codex_bin: str, timeout_seconds: int,
+                  repo_root: Path, schema_path: Path,
+                  log_path: Path, anchors: dict,
+                  heartbeat_interval: float = 30.0,
+                  stall_silence_seconds: float = 90.0,
+                  poll_interval: float = 0.5) -> str:
+    """Run codex via Popen + reader thread; emit streamed lines + heartbeats;
+    SIGTERM on operator-abort-signal or heartbeat-silence.
+    """
+    out_path = ... tempfile ...
+    abort_path = (repo_root / "agent-loop" / "state" /
+                  f"abort-dispatch-{anchors['pass_id']}.signal")
+    proc = subprocess.Popen(
+        [codex_bin, "exec", "--output-schema", str(schema_path),
+         "--cd", str(repo_root), "-o", out_path],
+        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
+        bufsize=0,
+    )
+    proc.stdin.write(prompt.encode("utf-8"))
+    proc.stdin.close()
+
+    start_ts = time.time()
+    last_streamed_ts = None
+    streamed_seq = 0
+    stdout_buf = []
+    state_lock = threading.Lock()
+
+    def reader():
+        nonlocal last_streamed_ts, streamed_seq
+        for raw_line in iter(proc.stdout.readline, b""):
+            stdout_buf.append(raw_line)
+            line_str = raw_line.decode("utf-8", errors="replace").rstrip("\n")
+            full_len = len(line_str)
+            with state_lock:
+                seq = streamed_seq
+                streamed_seq += 1
+                last_streamed_ts = time.time()
+            _log_dispatch(log_path, {
+                "event": "dispatch_streamed_line",
+                **anchors,
+                "stream": "stdout",
+                "line_truncated": line_str[:200],
+                "line_full_length": full_len,
+                "truncated": full_len > 200,
+                "seq": seq,
+            })
+
+    t = threading.Thread(target=reader, daemon=True)
+    t.start()
+
+    last_heartbeat = start_ts
+    while proc.poll() is None:
+        now = time.time()
+        # Operator abort?
+        if abort_path.exists():
+            try:
+                abort_reason = abort_path.read_text(encoding="utf-8").strip() or "operator_signal_file"
+            except OSError:
+                abort_reason = "operator_signal_file (unreadable)"
+            proc.terminate()
+            try: proc.wait(timeout=10)
+            except subprocess.TimeoutExpired: proc.kill()
+            with state_lock:
+                silence_age = (now - last_streamed_ts) if last_streamed_ts else None
+            _log_dispatch(log_path, {
+                "event": "dispatch_aborted",
+                **anchors,
+                "abort_source": "operator_signal_file",
+                "abort_reason": abort_reason,
+                "age_seconds": now - start_ts,
+                "last_streamed_line_age_seconds": silence_age,
+            })
+            try: abort_path.unlink()
+            except OSError: pass
+            raise CodexInvocationError(f"dispatch aborted by operator: {abort_reason}")
+
+        # Heartbeat tick?
+        if now - last_heartbeat >= heartbeat_interval:
+            with state_lock:
+                silence_age = (now - last_streamed_ts) if last_streamed_ts else (now - start_ts)
+                seq_snap = streamed_seq
+            _log_dispatch(log_path, {
+                "event": "dispatch_heartbeat",
+                **anchors,
+                "age_seconds": now - start_ts,
+                "last_streamed_line_age_seconds": silence_age,
+                "last_streamed_line_seq": seq_snap - 1 if seq_snap > 0 else None,
+            })
+            last_heartbeat = now
+
+            # Heartbeat-silence auto-abort?
+            if silence_age >= stall_silence_seconds:
+                proc.terminate()
+                try: proc.wait(timeout=10)
+                except subprocess.TimeoutExpired: proc.kill()
+                _log_dispatch(log_path, {
+                    "event": "dispatch_aborted",
+                    **anchors,
+                    "abort_source": "watchdog_silence",
+                    "abort_reason": f"no codex stdout for {silence_age:.0f}s",
+                    "age_seconds": now - start_ts,
+                    "last_streamed_line_age_seconds": silence_age,
+                })
+                raise CodexInvocationError(
+                    f"codex stuck: no output for {silence_age:.0f}s"
+                )
+
+        # Wall-time timeout still respected as final fallback.
+        if now - start_ts >= timeout_seconds:
+            proc.terminate()
+            try: proc.wait(timeout=10)
+            except subprocess.TimeoutExpired: proc.kill()
+            raise CodexInvocationError(f"codex exceeded {timeout_seconds}s wall timeout")
+
+        time.sleep(poll_interval)
+
+    # Codex exited; drain reader.
+    t.join(timeout=5)
+    full_output = b"".join(stdout_buf).decode("utf-8", errors="replace")
+    if proc.returncode != 0:
+        stderr_text = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
+        raise CodexInvocationError(
+            f"codex exit={proc.returncode}; stderr_tail={stderr_text[-2000:]!r}"
+        )
+    return Path(out_path).read_text(encoding="utf-8") if Path(out_path).exists() else full_output
```

**Callsite update** (~line 1340, in `main()`):

```diff
-        codex_output = _invoke_codex(
-            prompt=prompt,
-            codex_bin=ns.codex_bin,
-            timeout_seconds=ns.timeout_seconds,
-            repo_root=repo_root,
-            schema_path=schema_path,
-        )
+        codex_output = _invoke_codex(
+            prompt=prompt,
+            codex_bin=ns.codex_bin,
+            timeout_seconds=ns.timeout_seconds,
+            repo_root=repo_root,
+            schema_path=schema_path,
+            log_path=log_path,
+            anchors={
+                "iteration_id": iteration_id,
+                "reviewer_id": reviewer_id,
+                "pass_id": pass_id,
+            },
+        )
```

---

## Patch 2 — `_visibility_tui.py` show live-dispatch state

`_assemble_dispatches` already pairs by (iteration_id, pass_id). Extend the `active` dispatch render to peek at the last `dispatch_streamed_line` + `dispatch_heartbeat` for each active pass.

**Approach**:
1. In `_assemble_dispatches`, while walking events, capture the most recent `dispatch_streamed_line` and `dispatch_heartbeat` per (iteration_id, pass_id) key. Attach to the entry dict.
2. In `_render`'s active-dispatches loop, if last_line / last_heartbeat present: display them under the dispatch.

**Diff sketch (~30 LOC change)**:

```diff
@@ in _assemble_dispatches:
         elif kind in ("dispatch_done", ..., "dispatch_stalled", "dispatch_aborted"):
             entry["end"] = ev
+        elif kind == "dispatch_streamed_line":
+            entry["last_line"] = ev
+        elif kind == "dispatch_heartbeat":
+            entry["last_heartbeat"] = ev

@@ in _render's active-dispatch block (after existing age/timeout/target lines):
+            last_line = ...  # peek from entry
+            if last_line:
+                txt = last_line.get("line_truncated", "")
+                lines.append(f"        last_line: {txt!r}")
+            last_hb = ...  # peek from entry
+            if last_hb:
+                hb_silence = last_hb.get("last_streamed_line_age_seconds")
+                ...colorize based on silence...
+                lines.append(f"        last_heartbeat: {_humanize_age(...)} ago; silence: {hb_silence:.0f}s")
```

(The TUI's `_assemble_dispatches` currently returns `{"active": [start_event, ...], "recent": [end_event, ...]}`. To attach last_line/heartbeat to active entries, change the shape to `{"active": [{"start": ev, "last_line": ev, "last_heartbeat": ev}, ...], ...}` and adjust the render loop.)

---

## Patch 3 — test migration: `subprocess.run` → `Popen` mocks

The new `_invoke_codex` uses `Popen` not `run`. Existing tests mock `subprocess.run` via `monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)`. They will break.

**Strategy**: rather than rewriting each fake_run, provide a `_mock_popen_for_codex(stdout_text, returncode=0, version_text=...)` helper in a new `tests/_popen_mock_helper.py` (or top of test_dispatch_codex.py if simpler) that returns a fake Popen object with `.stdout.readline()` yielding the stdout text one line at a time, then b"" sentinel.

Test files to update (approximate fixture counts; verify with grep):
1. `tests/test_dispatch_codex.py` — ~10 fixtures
2. `tests/test_capstone_full_fix_loop.py` — ~2 fixtures
3. `tests/test_iter_0022_base_sha_stamp.py` — ~1 fixture
4. `tests/test_iter_0024_per_patch_base_sha.py` — ~1 fixture
5. `tests/test_iter_0026_crlf_base_sha.py` — ~1 fixture

**Parallel-agent strategy**: dispatch ONE agent per test file (5 parallel agents), each:
- Reads its file
- Identifies every place that mocks `subprocess.run` for codex purposes
- Replaces with the Popen mock helper
- Runs `pytest <its file>` to verify
- Reports back

---

## Patch 4 — NEW file `tests/test_dispatch_codex_streaming.py`

New regression tests for the streaming/heartbeat/abort behavior:

1. `test_streamed_lines_appear_in_dispatch_log` — synthetic codex emits 5 lines; dispatch-log gets 5 dispatch_streamed_line events with correct seq/truncation.
2. `test_long_lines_are_truncated_to_200_chars` — synthetic codex emits a 1000-char line; dispatch_streamed_line records line_truncated len=200, line_full_length=1000, truncated=True.
3. `test_heartbeat_fires_at_interval` — patch time + run synthetic codex for 90s elapsed; ~3 heartbeats fire.
4. `test_heartbeat_silence_triggers_abort` — synthetic codex starts streaming, then goes silent for 100s; dispatch_aborted with abort_source="watchdog_silence".
5. `test_operator_abort_signal_file_triggers_abort` — synthetic codex runs; abort-signal file written mid-run; dispatch_aborted with abort_source="operator_signal_file"; signal file deleted afterward.
6. `test_wall_time_timeout_still_respected` — synthetic codex streams a line every 30s (no silence trigger) but runs past timeout_seconds; CodexInvocationError raised; dispatch_failed event emitted.
7. `test_clean_exit_returns_output` — synthetic codex streams + exits 0; full output returned for downstream parsing.

---

## Acceptance & verification

After all patches:
- pytest: +7-10 new tests in test_dispatch_codex_streaming.py. ~388 → ~395+.
- smoke: 60/60 unchanged
- gates: 11/11 after staging + commit
- `G_pytest_dispatch_codex` baseline: bumped if test_dispatch_codex.py count grew (it shouldn't — migrations don't add tests, just change mocks)
- Empirical: dispatch a real codex call (e.g., a tiny smoke goal_packet); inspect dispatch-log.jsonl to confirm dispatch_streamed_line + dispatch_heartbeat events appear

## Out of scope (deferred)

- Stderr streaming as dispatch_streamed_line — only stdout for now. Stderr captured in toto and used in failure messages.
- TUI color tiering for heartbeat-silence levels (yellow at 60s, red at 90s). Land basic display in iter-0037; refine in a followup.
- Async/asyncio refactor — using threads to match existing subprocess patterns.

## Reviewer questions for codex pre-review

1. **Reader thread + main loop synchronization**: the state_lock protects last_streamed_ts/streamed_seq writes from the reader thread + reads from main. Any race conditions I'm missing?
2. **bufsize=0 vs bufsize=1**: with bufsize=0 + binary mode + iter(readline, b""), are we guaranteed line-at-a-time delivery without buffering surprises on Windows?
3. **Wall-time fallback**: when wall timeout fires we raise CodexInvocationError but don't emit a dedicated event (the calling `_failed_event` path handles it). Should we emit a `dispatch_aborted` with abort_source="wall_timeout" for symmetry?
4. **Abort-signal-file race**: operator writes file at T+0; main loop polls 500ms later. Acceptable, or fast-path the poll on every iteration regardless of poll_interval?
5. **Output retrieval**: I return `Path(out_path).read_text()` if the file exists (codex writes its output there via `-o`), else the streamed-buf decoded. Is the file always written before proc.exit? If not, the streamed buf is the fallback truth.
6. **Cleanup on exception path**: if the reader thread is still reading when an abort fires, does proc.terminate() reliably close the stdout pipe so the readline loop exits? Windows vs Linux behavior differences?
