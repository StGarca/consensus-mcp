# Dispatcher hardening - parallel dispatch, codified consult + 2 robustness fixes

**Date:** 2026-06-02
**Status:** Draft -> pending consensus consult ratification (anchored) -> implementation (TDD)
**Author:** Claude (Opus 4.8) with project operator
**Consult framing:** ANCHORED. Operator contribution (Findings A/B below) added first
and integrated; panel reviews this whole package.
**Scope of this consult:** (1) parallel dispatch rewrite, (2) codified uniform consult
process, (3) grok stale-PATH resolution [Finding A], (4) kimi cp1252 stdin decode
[Finding B].

## Problem

A consult on a fresh install runs "incredibly slow" and varies session-to-session.
Two layers cause this:

1. **Engine dispatches reviewers serially.** `workflow_engine.py` walks contributors
   in blocking `for c in enabled:` loops in BOTH workflows:
   - `_run_workflow_3` (post-review): `adapter.review(...)` one at a time.
   - `_run_workflow_4` (propose-converge): Phase-1 blind `adapter.propose(...)` one at
     a time, AND every convergence round `adapter.converge(...)` one at a time.
   The code comments admit the contributors are independent ("the contributors
   themselves don't see each other's outputs"). Four reviewers at ~60-120s each run
   in ~sum(t) (~4-8 min) instead of ~max(t) (~1-2 min).

2. **Orchestrator improvises Path A.** The new-machine session hand-rolled per-reviewer
   `reviewer_dispatch_*` calls, invented a "validate plumbing with one codex dispatch
   first" probe, and hand-created containment-marker dirs. None of that is codified, so
   every session differs.

## Goal

Every install runs the SAME, fast process: **write goal_packet -> fan out to all enabled
reviewers in parallel -> wait for all -> converge.** No improvised probe, no per-session
variance.

## Decision summary

- **Unconditional parallel.** No `sequential` mode, no config flag. The serial dispatch
  path is removed entirely. (Operator decision 2026-06-02.)
- Scope: **engine parallelization + skill codification** (both layers).

## Design

### Component 1 - Engine: concurrent fan-out within a phase

Replace each blocking per-contributor loop with a `concurrent.futures.ThreadPoolExecutor`
(`max_workers = len(enabled)`), submitting every contributor's `adapter.review` /
`adapter.propose` / `adapter.converge` at once and collecting results as futures complete.

- **Threads, not asyncio.** Each adapter call is a blocking subprocess (CLI dispatch) -
  thread pool is the right tool for I/O-bound subprocess waits; no event-loop rewrite of
  the synchronous adapters.
- **Rounds stay sequential.** Convergence round N+1 consumes round N's artifacts; only the
  contributors *within* a single phase/round fan out. The round loop is unchanged.

Affected functions: `_run_workflow_3`, `_run_workflow_4` (Phase-1 loop + per-round loop).
A shared private helper - e.g. `_dispatch_phase_parallel(enabled, fn) -> list[SealedArtifact]`
- encapsulates the executor, ordering, and error collection so both workflows share one
implementation and cannot drift.

### Component 2 - Determinism & error handling

Concurrency must not change outcomes, only wall-clock.

- **Deterministic ordering.** Collect futures concurrently, then re-sort the resulting
  artifacts into `enabled` (config) order before sealing, audit-logging, and convergence
  evaluation. Seal ordering and convergence input become byte-reproducible regardless of
  which CLI finishes first.
- **Collect-all, not fail-fast.** A reviewer that raises `DispatchError` (timeout, crash)
  is recorded via the existing per-contributor `timeout_policy` (`_record_timed_out`); the
  remaining reviewers still complete. One bad CLI never aborts the panel. No future's
  exception propagates out of the helper - each is caught and mapped to the existing
  per-contributor failure path.
- **Per-contributor timeout** is already per-call; parallel dispatch does not change it.

### Component 3 - Skill codification (uniform every install)

Tighten `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`:

- Codify the single path: write goal_packet -> ONE fan-out call (the engine /
  `run_iteration` path) -> wait for all -> converge.
- **Explicitly forbid** the improvised "validate plumbing with one reviewer first" probe.
- Reference containment-marker dirs being auto-created at init (see separate TODO
  `todo-containment-marker-dirs`) - no hand-creation step in the consult flow.

## kimi-integrity note

Prior guidance: kimi's integrity check false-positives on concurrent **repo source edits**
during a dispatch. Reviewer dispatch writes **sealed artifacts to the consensus-state dir**,
not repo source. The new-machine run already executed kimi concurrently with gemini+grok
under Path A without tripping it. Action: before/at implementation, confirm reviewer artifact
writes do not land where kimi's integrity snapshot watches (expected: safe). If they did, the
fix is to ensure the state dir is outside the integrity scope - NOT to serialize.

## Testing strategy (TDD)

- **Parallelism is observable, not just faster.** Assert concurrency directly rather than by
  timing: inject fake adapters whose `review/propose/converge` block on a shared barrier that
  only releases once N have entered - proving all N ran concurrently (a sequential loop would
  deadlock the barrier and time out the test). No wall-clock flakiness.
- **Determinism.** Fake adapters that complete in shuffled order must yield artifacts in
  `enabled` order; assert the sealed/eval order is identical across runs.
- **Collect-all.** One adapter raises `DispatchError`; assert the others' artifacts are still
  collected and the failure is recorded via the timeout path (not propagated).
- Existing workflow_3 / workflow_4 convergence and seal tests must stay green (behavior parity
  except ordering-by-completion, which Component 2 normalizes away).

## Dispatcher hardening - operator-contributed findings (2026-06-02)

Anchoring contribution from the operator, diagnosed live on the Windows install.
Two independent dispatcher robustness bugs surfaced while running a real consult.
Folded into this consult so the panel ratifies the fixes alongside the parallel
rewrite (they touch the same dispatch path). Neither undermined the codex+gemini
findings on that run - the payload was sound; these are clean hardening candidates.

### Finding A - grok "binary not found" from a stale server PATH snapshot

Symptom: `grok binary not found: grok` even though `grok.exe` exists at
`C:\Users\example\.grok\bin\grok.exe` and is on PATH (current process + user
registry).

- The dispatcher resolves grok by bare name via `shutil.which("grok")`
  (`_dispatch_grok.py:172`) because `.consensus/config.yaml` enables grok but sets
  no `adapters.grok.command`. On `FileNotFoundError` it raises this exact error
  (`:380`).
- **Root cause:** stale PATH snapshot in the long-lived consensus-mcp MCP server
  process. `shutil.which` resolves against the env the server was launched with;
  that env predated `~/.grok/bin` becoming visible, so `which` returned None. Fresh
  diagnostic shells see it; the server didn't.
- **Operator-proposed fix (deterministic):** pin the absolute path in config -
  `contributors.adapters.grok.command: C:\Users\example\.grok\bin\grok.exe`.
  `_resolve_grok_bin` returns a drive-prefixed path verbatim (`:170`), bypassing
  PATH. Or fully restart Claude Code so the server re-inherits PATH.
- **Hardening question for the panel:** should the dispatcher re-resolve binaries
  against the *current* environment (or a configurable search path) rather than the
  server's launch-time PATH, so a long-lived server doesn't go stale? Applies to ALL
  bare-name adapter resolution, not just grok.

### Finding B - kimi `'\udc9d' ... surrogates not allowed` (cp1252 stdin decode)

Symptom: kimi dispatch crashes with a lone-surrogate error. A kimi-cli 1.44.0
Windows bug, not a dispatcher or payload defect.

- Crash is inside kimi-cli's own pydantic `model_dump_json()` serializing the user
  Message (its request to the model API). The dispatcher sends correct UTF-8 over
  stdin (`_dispatch_kimi.py:803` `prompt.encode("utf-8")`).
- `\udc9d` is the surrogateescape of byte `0x9D` - the middle byte of the [x] emoji's
  UTF-8 (`E2 9D 8C`). That [x] came from the consuming project's GUI status icons
  ([x] [ok] [warn]) in `appointment_processor_gui.py`; byte scan confirmed exactly one `0x9D`.
- **Mechanism:** kimi-cli reads its UTF-8 stdin under the Windows locale (cp1252)
  with `surrogateescape`, so [x] decodes to a lone `\udc9d`; its strict-UTF-8 JSON
  serializer then refuses it. Deterministic (same byte position every run ->
  non-retryable). codex/gemini ate the same bytes fine.
- The kimi subprocess env (`_kimi_subprocess_env`, `:264-280`) scrubs API keys but
  does not set `PYTHONUTF8`.
- **Operator-proposed fixes:** (1) inject `PYTHONUTF8=1` into `_kimi_subprocess_env()`
  - a one-line consensus-mcp fix since kimi-cli is a Python app; (2) user-side, launch
  Claude Code with `PYTHONUTF8=1`; (3) real fix is upstream - kimi-cli should decode
  stdin as UTF-8 regardless of locale. NOT recommended: stripping the emoji from the
  source. This is the same cp1252 class as v1.33.4 (see `windows-console-portability`).
- **Hardening question for the panel:** is forcing `PYTHONUTF8=1` for the kimi
  subprocess the right scope, or should ALL Python-app adapter subprocesses get a
  forced-UTF-8 env to immunize the panel against locale-dependent stdin decode?

### Finding C - post-consult seal + approval-marker minting is uncodified

Symptom (observed on the new-project install): after the panel returns sealed
review packets and the orchestrator synthesizes, the agent does NOT know how to
turn that into an accepted `.consensus/design-approved` marker that unblocks
`src/` edits. It reverse-engineers `_design_approval` internals over many calls -
discovering `SEALED_CLOSING_STATES`, `resolve_consensus_ref`,
`compute_artifact_hash`, the >=2-non-claude-reviewer rule, scope_glob confinement,
and the prepare->scaffold-outcome->mint sequence - just to legitimately approve a
consult it already ran.

- The trust model is CORRECT and must NOT be weakened: the marker is a pointer
  re-validated against the live cross-family seal (no self-approval; >=2 non-claude
  reviewers; `converged_plan_sha256` must match the iteration's converged-plan;
  scope-confined; fail-closed). `mint_design_approval()` exists.
- The GAP is purely ergonomic: there is no single codified "consult converged ->
  seal iteration -> mint approval marker -> unblock" command, and the
  consensus-workflow skill does not spell out the exact post-convergence steps. So
  every fresh agent re-derives the internals.
- Related sub-gap: how the orchestrator CONSUMES the returned sealed packets
  (read + synthesize) is also improvised per session.
- Field-confirmed sub-friction (the seal sequence itself): `prepare` scaffolds an
  outcome with `EDIT_ME` / `EDIT_ME_TO_A_SEALED_STATE` placeholders the agent must
  hand-fill, then `mint`. And a FOOTGUN: `mint --converged-plan` wants a BARE
  filename (joined to the iteration dir), so passing a full path fails with a
  misleading `missing_converged_plan` "not found" error even though the file is on
  disk. The codified approve-flow should accept either form (or default it) and
  never emit a "not found" error for a file that exists.
- **Operator-proposed fix:** a single composed command/flow - e.g.
  `consensus approve --iteration <dir>` (or an MCP tool) that runs the existing
  prepare/seal + `mint_design_approval` primitives end-to-end, validates the
  >=2-non-claude + hash + scope preconditions, and emits a clear actionable error
  when a precondition is unmet (e.g. "only 1 non-claude review sealed; need >=2").
  Plus a codified "consume returned packets -> synthesize converged-plan" step in
  the skill. Trust model unchanged; only the UX is codified.
- **Hardening question for the panel:** should the codified approve-flow be a CLI
  binary, an MCP tool, or both, and where should the consensus-workflow skill draw
  the line between "engine does it" vs "documented operator steps"?

## Out of scope (tracked separately)

- Auto-creating containment-marker dirs at init (`todo-containment-marker-dirs`).
- Propagating the v1.33.4 cp1252/isatty fix to `server.py` / `_dispatch_*`
  (`todo-cp1252-other-entrypoints`).

## Open questions for the consult

1. Is unconditional parallel (no sequential fallback) acceptable for a core-engine change,
   or does the panel want a hidden kill-switch env var as insurance?
2. Is the deterministic re-sort sufficient for reproducibility, or are there audit-log
   side effects (timestamps, event ordering) that also need normalization?
3. Any concurrency hazard in the adapters themselves (shared mutable state, temp-file/path
   collisions, audit-log append races) that the fan-out would expose?
4. **[Finding A]** Should adapter binary resolution re-resolve against the current
   environment / a configurable search path instead of the server's launch-time PATH, so a
   long-lived MCP server doesn't go stale? (Pinning `adapters.grok.command` fixes grok now;
   the class fix covers all bare-name resolution.)
5. **[Finding B]** Is forcing `PYTHONUTF8=1` for the kimi subprocess the right scope, or
   should ALL Python-app adapter subprocesses get a forced-UTF-8 env to immunize the panel
   against locale-dependent stdin decode? (Same cp1252 class as v1.33.4.)
6. **[Finding C]** Codified post-consult approve-flow: CLI binary, MCP tool, or both? Where
   is the line between engine-automated sealing/minting and documented operator steps in the
   consensus-workflow skill? (Trust model stays as-is; only the UX is codified.)
