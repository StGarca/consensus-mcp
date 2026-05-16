# Advisories

Standing channel for "shipped artifact has known doctrine drift"
notices. Each advisory names the affected versions, the issue, the
correct version to upgrade to, and any user action required.

Format: append-only, newest first. Older advisories may be marked
**resolved** when no users remain on the affected versions, but
the entry stays for the historical record.

---

## Advisory 2026-05-16: v1.15.9 named follow-up — OS pipe-buffer backpressure modeling

**Affected versions:** `v1.15.9` (forward, until addressed).

**Severity:** Tracked engineering follow-up — NOT a defect in
shipped behavior, NOT a regression. The v1.15.9 deterministic
streaming-harness rework (Workflow A, claude+codex+gemini; 8
audit defects caught + integrated over 6 Workflow-B passes; the
4 `@_FLAKY_WINDOWS_CI` skips deleted) is sound and proven.

During pass-6, codex (`codex-v1159-wfb-6 codex-rev-001`) noted
`test_stderr_drain_prevents_deadlock` does not model OS
pipe-buffer **backpressure**: the fake proc exits on virtual
time regardless of stderr consumption, so it would not, by
itself, prove real codex cannot deadlock on a full stderr pipe.
A focused **scope-adjudication consult** (claude+codex+gemini,
weighted-synthesis; `iteration-v1159-stderr-scope`) verified
**unanimously** — each independently checking `git show
da62d54^:...` — that this weakness is **PRE-EXISTING and
identical in the v1.15.8 baseline** (`exit_at=0.3`,
assert-only-not-alive), i.e. NOT a regression introduced by the
rewrite, and therefore **not a valid v1.15.9 blocker**.

**Resolved-in-part in v1.15.9** (zero-determinism-risk, shipped):
the test docstring no longer overclaims, and it now asserts
production's REAL reader threads drained EVERY scheduled
stdout+stderr line (`_FakePipeReader._idx == len(scheduled)`,
post-run state) — so a regression that removes/breaks the stderr
reader **fails the test deterministically**, satisfying codex's
stated requirement without backpressure modeling.

**Deferred (this follow-up):** true OS-pipe-buffer backpressure
modeling. Concrete blocker for deferral: it is its own design
surface with real Windows-CI determinism risk (the exact flake
class v1.15.9 eliminated). Converged design seed for the future
iteration (codex + gemini): finite-capacity `_FakePipeReader`;
`StreamingFakeCodexPopen.poll()` does NOT exit while a pipe is
"blocked"; `_SyncClock` deterministically unblocks on reader
consumption; **MUST** integrate `release_all()`; **MUST** ship a
mutant gate (normal passes / stderr-drain-removed fails
deterministically / never hangs).

**User action:** none. Recorded for the maintainer.

---

## Advisory 2026-05-15: v1.15.8 named follow-up — deterministic clock harness (Q2(a))

**RESOLVED in v1.15.9** (pending the provisional-until-proven
≥3-consecutive-green-Windows-CI gate on the v1.15.9 tag commit).
The Q2(a) deterministic-harness rework landed via Workflow A
consult `iteration-v1159-deterministic-clock-harness`: a private
keyword-only `_invoke_codex(_sleep=)` seam (default `time.sleep`
— zero production behavior change) + a `threading.Condition`
`_SyncClock`; ALL real sleep removed from the drive path;
`@_FLAKY_WINDOWS_CI` DELETED; all 8 clock-driven tests migrated;
`release_all()` independent safeguard. Both codex pass-2
non-blocking findings below (skip breadth + interim-not-
deterministic) are thereby retired — the breadth is moot (no
skip) and the deterministic rework IS the fix. See CHANGELOG
1.15.9. The text below is retained as the historical record of
the v1.15.8 interim.

**Affected versions:** `v1.15.8` only (v1.15.9+ resolved).

**Severity:** Tracked engineering follow-up — NOT a defect in
shipped behavior. The v1.15.8 load-bearing fix (`_visibility_
watchdog._locked_append` intra-process `threading.Lock` +
fixed-byte-0 cross-process Windows lock + fail-loud `OSError`)
was confirmed RESOLVED by **both** Workflow B pass-2 reviewers
(gemini `goal_satisfied=true`, 0 findings; codex 0 blocking
objections — the pass-1 blocking `codex-rev-001` cross-process
byte-range defect is fixed). This advisory records the two
**non-blocking** codex pass-2 findings on the Q2(c) interim,
integrated here rather than dismissed.

1. **Q2(c) Windows-CI skip is the converged-plan-sanctioned
   interim, not the Q2(a) deterministic rework (codex-rev-002,
   medium).** The converged plan's unanimous *preferred* answer is
   Q2(a): replace `_ControllableClock` + `_FakePipeReader`'s real
   `time.sleep` poll ticks (`test_dispatch_codex_streaming.py:126,
   137`) + `th.join(timeout=...)` (`:304,476,540,597,663`) with a
   test↔runner synchronizing handshake (Condition/Event/queue) so
   the reader/runner wake deterministically on `clock.advance()`
   with no real sleeps and no scheduling-budget dependence. This
   is a multi-thread harness redesign with genuine deadlock risk
   (the precise stall mode this release line repeatedly hit) — an
   open concurrency-design question, NOT a bounded mechanical edit.
   The converged plan explicitly pre-sanctioned the Q2(c) interim
   "if Q2(a) overruns the iteration bound, with a concrete named
   follow-up." It does. This is that follow-up. **Proper fix:**
   the synchronizing-clock redesign, scoped as its own iteration
   (Workflow A — it has real design surface).

2. **Skip breadth: 4 tests, 1 with first-hand CI evidence
   (codex-rev-001-pass2, high) — shared-mechanism justification +
   recorded residual gap.** `@_FLAKY_WINDOWS_CI` skips
   `test_heartbeat_fires_at_interval` (the F2-verified flake) plus
   `test_heartbeat_silence_triggers_abort`,
   `test_operator_abort_signal_file_triggers_abort`,
   `test_wall_time_hard_ceiling`. codex correctly noted only the
   first has first-hand Windows-CI failure evidence. **Why the
   breadth is justified (verifiable from the code, not loose
   similarity):** all four starve on the *same* harness path — the
   `_FakePipeReader.readline()` real `time.sleep(self._real_tick)`
   poll loop (`:126,137`) feeding a daemon runner thread the test
   then `th.join(timeout=...)`s. The flake is structural to that
   reader/runner-starvation-under-loaded-Windows-scheduling
   mechanism, *not* to heartbeat logic — so the same root cause
   makes all four nondeterministic on loaded Windows runners. **The
   residual coverage gap codex named, recorded explicitly:** while
   skipped on Windows GitHub Actions, Windows-only regressions in
   (i) silence-triggered process termination, (ii) operator
   abort-signal-file detection/cleanup, and (iii) wall-time
   hard-ceiling abort are covered ONLY by Linux CI (every push) +
   local Windows dev — NOT Windows CI. The logic under test is
   driven by the injected `time_fn`, so it is not a Windows
   product code path; the gap is loss of the *Windows-runner*
   environment for those three behaviors until Q2(a) lands. The
   Q2(a) rework eliminates both the breadth and the gap by making
   all four run everywhere.

**User action:** none required. Recorded for the maintainer; no
upgrade implication. The shipped integrity fix is complete and
peer-confirmed; these are deferred test-harness quality items.

---

## Advisory 2026-05-15: v1.15.7 named follow-ups (non-blocking)

**Affected versions:** `v1.15.7` (forward, until addressed).

**Severity:** Tracked engineering follow-ups — NOT defects in
shipped behavior. CI is fully green (all 6 legs) and the full
suite is 968/0; these are scoped, deliberately-deferred items
from the v1.15.7 CI-greening Workflow B audit.

1. **Cross-process dispatch-log serialization.** v1.15.7 made
   `_dispatch_base._log_dispatch` intra-process atomic (a
   module-level `threading.Lock`) — the verified root cause of the
   observed torn line (main + reader threads of one dispatcher).
   It does **not** serialize a shared `dispatch-log.jsonl` across
   *parallel dispatcher processes* (e.g. Workflow-A round-1
   codex+gemini). This is **unobserved** and was explicitly
   excluded (a blocking cross-process OS lock was tried and
   regressed windows-py3.12 — see CHANGELOG v1.15.7). Revisit only
   if parallel-dispatcher torn lines are ever observed.
2. **4 codex-dispatch "smoke" tests are integration tests.**
   `test_main_smoke_with_mocked_codex` + 3 siblings mock
   `subprocess.run` but not the iter-0037 `Popen` path, so they
   `skipif` when no real `codex` binary (CI runners). Proper fix:
   rewrite onto the existing `make_fake_codex_popen_factory` /
   `popen_factory=` pattern so they run hermetically everywhere,
   then drop the skip.

**User action:** none required. Recorded for the maintainer; no
upgrade implication.

---

## Advisory 2026-05-15: gemini dispatch fails on gemini CLI ≥ 0.43.0-preview.0

**RESOLVED in `v1.15.2`** — `_dispatch_gemini.py` now injects
`GEMINI_CLI_TRUST_WORKSPACE=true` into the gemini subprocess env
(`_gemini_subprocess_env()`). Users on `v1.15.1` or earlier with
gemini CLI ≥ 0.43.0-preview.0 must upgrade to `v1.15.2` (or apply
the env-var workaround below until they do). Entry retained for the
historical record.

**Affected versions:** all consensus-mcp versions through `v1.15.1`
(the gemini dispatcher behavior is unchanged across this range).

**Severity:** Functional — gemini peer reviews silently fail to
seal when the installed `gemini` CLI enforces workspace trust.
codex + claude consensus is unaffected; the cross-AI safety net
degrades from 3-AI to 2-AI until worked around.

**Issue:**
- `gemini` CLI `0.43.0-preview.0`+ refuses headless/automated runs
  in an "untrusted" directory, emitting the trust error to stderr
  and **empty stdout**. `consensus_mcp/_dispatch_gemini.py` did
  not set `GEMINI_CLI_TRUST_WORKSPACE`; it already passed
  `--skip-trust`, but that flag is **not load-bearing** on this CLI
  version (empirically: with `--skip-trust` only, gemini bypassed
  trust but went autonomous and 429'd). So the dispatcher saw empty
  output and failed `GeminiOutputParseError` ("Expecting value:
  line 1 column 1").
- Observed first-hand 2026-05-15 during the v1.15.1 Workflow B
  audit: gemini pass-1 failed twice (initial + dispatcher
  auto-retry) for exactly this reason.

**Workaround (no upgrade needed):** export
`GEMINI_CLI_TRUST_WORKSPACE=true` in the environment that runs the
dispatcher (or the MCP server). The v1.15.1 audit was completed
this way; gemini returned clean approvals once the env var was set.

**Correct upgrade target:** `v1.15.2` — ships the dispatcher-level
fix (`_gemini_subprocess_env()` injects
`GEMINI_CLI_TRUST_WORKSPACE=true` into the gemini subprocess env).
`_dispatch_gemini.py` was in the v1.15.1 iteration's
`forbidden_files`, so the fix was correctly deferred to v1.15.2
rather than patched out-of-scope. The env-var workaround above
remains valid for anyone still on ≤ v1.15.1.

**Provenance:**
- Diagnosed during `iteration-converged-plan-machine-enforcement`
  (v1.15.1 Workflow B audit); recorded there as a v1.15.2 named
  blocker in `fix-response-pass2.md`.
- Fixed in `iteration-gemini-trust-env-fix` (v1.15.2; Workflow B
  audit clean — gemini approved end-to-end via the source fix with
  the env-var workaround explicitly unset; codex doc-accuracy
  findings integrated).

---

## Advisory 2026-05-15: v1.15.0 converged-plan convention is doctrine-only

**Affected versions:** `v1.15.0`

**Severity:** Scope clarification (no regression). The v1.15.0 tag
`4e81f9e` shipped the converged-plan convention
(`falsification` / `independent_safeguard` /
`decisive_experiment_before_next_iteration`) as an **authoring
convention enforced by doctrine only** — bundled skill + Workflow
B audit, **zero engine code**.

**Issue:** users on `v1.15.0` may assume the convention is
machine-validated. It is not. There is no schema, no validator, no
seal-time gate on `v1.15.0`.

**Correct upgrade target:** `v1.15.1` — adds the JSON Schema, the
structure/consequence validator, the fail-closed seal-time gate,
the `convergence.converged_plan_enforcement` knob (default
`graduated`), and read-time surfacing. Machine enforcement exists
only from the `v1.15.1` tag forward.

**Provenance:**
- `iteration-converged-plan-machine-enforcement` (Workflow A
  weighted-synthesis: claude + codex + gemini; shared-prior
  self-check PASSED; Workflow B audit clean: gemini ×2, codex
  pass-4b goal_satisfied=true, 0 blocking, 0 findings).

---

## Advisory 2026-05-14: v1.14.0 + v1.14.1 bundled-skill drift

**Affected versions:** `v1.14.0`, `v1.14.1`

**Severity:** Doctrine drift (no functional regression in
consensus-mcp itself). Affects the bundled `consensus-workflow`
SKILL.md content shipped in the wheel and installed by the
Claude Code bootstrap pack.

**Issue:**
- `v1.14.0` ships a bundled skill that incorrectly documents a
  PyPI publish step in the release cut sequence. consensus-mcp
  is NOT registered on PyPI; releases are git-tag-only via
  `pipx install git+https://github.com/.../@vX.Y.Z`. The PyPI
  step in the skill was added in error during the v1.14.0 cut
  and propagates misleading release-procedure documentation to
  every project that runs `consensus init --install-claude-code`
  against this version.
- `v1.14.1` ships a partially-corrected skill (PyPI step
  removed) but is missing the "Verify before invent" and
  "Artifact-scoped claims" doctrine sections that landed in
  v1.14.2.

**Correct version:** `v1.14.3` (or any later release).

**User action required:** Upgrade installs that pulled
`@v1.14.0` or `@v1.14.1`:

```
pipx install --force git+https://github.com/stgarca/consensus-mcp.git@v1.14.3
```

If you have run `consensus init --install-claude-code` against
v1.14.0 or v1.14.1, re-run it against v1.14.3 to refresh the
project-local skill copy (the bootstrap pack copies the bundled
skill into the project's `.claude/skills/` directory at install
time; old installs retain the stale copy).

**Provenance:**
- Originating audit: `iter-audit-2026-05-14-pypi-invention`
  (workflow #4 weighted-synthesis convergence; codex + gemini +
  claude all approved with no blocking objections).
- Doctrine fix landed: `v1.14.2` tag, commit `12eca6c`.
- Follow-up audit: `iter-audit-2026-05-14-three-followup-gaps`
  shipped this advisory mechanism + README install-URL bump in
  `v1.14.3`.

---
