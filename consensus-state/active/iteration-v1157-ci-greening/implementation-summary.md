# Workflow B audit target — v1.15.7 CI greening (test-infra only)

Re-enabling CI in v1.15.4 (dormant v1.13.0→v1.15.3, main-only
trigger) exposed three independent, pre-existing test-hermeticity
debts. No production code changed — diffs are confined to
`consensus_mcp/tests/`, `.github/workflows/test.yml`, and one
tracked test fixture.

## The 3 root causes + fixes

1. **4 codex-dispatch "smoke" tests need a real `codex` binary.**
   `test_dispatch_codex.py` (`test_main_smoke_with_mocked_codex`,
   `_smoke_flag_with_env_proceeds`,
   `_sealed_packet_embeds_dispatch_provenance`,
   `_dispatch_done_includes_archive_path_and_audit_id`) predate the
   iter-0037 move to `subprocess.Popen`; they mock only
   `subprocess.run` (now just the `--version` probe), so the real
   dispatch runs real codex. Green on dev (codex on PATH); on
   runners → `CodexInvocationError`. Fix: `_REQUIRES_REAL_CODEX =
   skipif(shutil.which("codex") is None)` with a reason naming the
   proper-Popen-mock rewrite as a tracked follow-up. Verified both
   ways (present: 4 run+pass; absent: 4 skip, 0 fail).

2. **Validator self-test 20/21.**
   `review_packet_known_good/input.yaml` `target_files` pointed at
   `agent-loop/tests/fixtures/prompt_injection_doc.md` — a
   parent-project path never rewritten at standalone extraction.
   `build_review_packet` resolves `target_files` repo-relative
   (`REPO_ROOT / rel`); the file didn't exist → empty
   `sanitization_log` → `>=7` check failed. Repointed to
   `consensus-state/tests/fixtures/prompt_injection_doc.md` (verified
   to contain all 7 `SANITIZE_PATTERNS`). Now 21/21. Reproduced
   locally; not OS-specific; latent since iter-0001.

3. **ubuntu job-kill (exit 143 / "operation canceled" ~25%).**
   `test_dispatch_codex_streaming._FakePopen.pid` returned `0`.
   POSIX `_dispatch_base._terminate_process_tree` does
   `os.killpg(os.getpgid(proc.pid), SIG*)`; `os.getpgid(0)` =
   **the caller's own process group** → abort/watchdog tests
   SIGTERM'd the pytest/CI job itself (instant — pytest-timeout
   never fired). Windows uses send_signal/taskkill (masked); latent
   since iter-0039. Fix (defense in depth): (a) NEW
   `tests/conftest.py` autouse guard neutralizing
   `os.killpg`/`os.getpgid` suite-wide (raise `ProcessLookupError`
   → production's documented `proc.terminate()` fallback runs;
   every `dispatch_aborted` assertion unchanged); (b) `_FakePopen.
   pid` 0 → 2_147_483_647 (never-live → `os.getpgid` raises →
   safe fallback); (c) `pytest-timeout --timeout=120` retained as
   permanent hardening against future hangs.

## Verification (CI run 25940449887, commit 41304aa)

- **5 of 6 legs GREEN:** ubuntu py3.10/3.11/3.12 ✓; windows
  py3.11/3.12 ✓. validator self-tests 21/21. full suite local
  968 passed / 1 skipped / 0 regressions.
- **1 OPEN failure — windows-py3.10 ONLY:**
  `test_dispatch_codex_streaming.py::test_wall_time_hard_ceiling`
  → `json.decoder.JSONDecodeError: Expecting value: line 1
  column 1` at the test helper `_read_log_events` (line 290:
  `_json.loads(line)`). The helper ALREADY skips blank lines
  (`line=line.strip(); if line:`), so a **non-empty line in
  `dispatch-log.jsonl` is not valid JSON**, occurring during the
  wall-time-ceiling abort + reader-thread teardown, only on
  py3.10 + Windows (py3.11/3.12 + all Linux pass). Signature
  ⇒ likely an interleaved/partial concurrent write to the JSONL
  log during thread teardown under py3.10's threading on Windows,
  OR a py3.10-specific flake.

## ADJUDICATION REQUESTED (the divergent fix-shape)

Decide the correct fix for the open item — these genuinely differ:
- **(A) Real defect:** dispatch-log writes are not atomic under
  concurrent reader-thread teardown → fix the writer
  (`_dispatch_base`/`_locked_append`/log append) for line-atomic
  JSONL. Highest value if real; touches production code.
- **(B) Test-harness artifact:** the controllable-clock/fake
  teardown in `test_dispatch_codex_streaming` races the log
  flush; fix the test/fake teardown ordering. Test-only.
- **(C) Reader-helper hardening:** `_read_log_events` should
  tolerate a torn final line (defensive JSONL parse). Risk:
  could mask (A) if (A) is real.
- **(D) py3.10-flake:** xfail/skip on windows-py3.10 + tracked
  follow-up; or a supported-version policy call (matrix).
Which, and why? Argue from the trace + the writer/teardown code.
State the differential/prior you reasoned from.

## Audit questions

- Q1 goal_satisfied: are these the correct ROOT-CAUSE fixes, not
  masks? Specifically Q2 below.
- Q2 (key): does the `conftest` guard neutralizing
  `os.killpg`/`os.getpgid` hide a PRODUCTION defect? Argue from
  the code: production uses real `Popen(start_new_session=True)`
  (POSIX) so `os.getpgid(real_child_pid)` targets the child's own
  session/group — correct; the bug was solely the fake's `pid=0`.
  Confirm or refute that the guard is test-hermeticity, not a
  prod-bug mask.
- Q3: do the 4 `skipif` codex tests lose meaningful coverage that
  the named Popen-mock-rewrite follow-up must restore? Is "skip +
  tracked follow-up" the right interim vs. rewriting now?
- Q4: any blocking objection. State the differential/prior you
  reasoned from.
