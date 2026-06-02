# Parallel dispatch + codified uniform consult — design

**Date:** 2026-06-02
**Status:** Draft → pending consensus consult ratification → implementation (TDD)
**Author:** Claude (Opus 4.8) with operator (StGarcia)

## Problem

A consult on a fresh install runs "incredibly slow" and varies session-to-session.
Two layers cause this:

1. **Engine dispatches reviewers serially.** `workflow_engine.py` walks contributors
   in blocking `for c in enabled:` loops in BOTH workflows:
   - `_run_workflow_3` (post-review): `adapter.review(...)` one at a time.
   - `_run_workflow_4` (propose-converge): Phase-1 blind `adapter.propose(...)` one at
     a time, AND every convergence round `adapter.converge(...)` one at a time.
   The code comments admit the contributors are independent ("the contributors
   themselves don't see each other's outputs"). Four reviewers at ~60–120s each run
   in ~sum(t) (~4–8 min) instead of ~max(t) (~1–2 min).

2. **Orchestrator improvises Path A.** The new-machine session hand-rolled per-reviewer
   `reviewer_dispatch_*` calls, invented a "validate plumbing with one codex dispatch
   first" probe, and hand-created containment-marker dirs. None of that is codified, so
   every session differs.

## Goal

Every install runs the SAME, fast process: **write goal_packet → fan out to all enabled
reviewers in parallel → wait for all → converge.** No improvised probe, no per-session
variance.

## Decision summary

- **Unconditional parallel.** No `sequential` mode, no config flag. The serial dispatch
  path is removed entirely. (Operator decision 2026-06-02.)
- Scope: **engine parallelization + skill codification** (both layers).

## Design

### Component 1 — Engine: concurrent fan-out within a phase

Replace each blocking per-contributor loop with a `concurrent.futures.ThreadPoolExecutor`
(`max_workers = len(enabled)`), submitting every contributor's `adapter.review` /
`adapter.propose` / `adapter.converge` at once and collecting results as futures complete.

- **Threads, not asyncio.** Each adapter call is a blocking subprocess (CLI dispatch) —
  thread pool is the right tool for I/O-bound subprocess waits; no event-loop rewrite of
  the synchronous adapters.
- **Rounds stay sequential.** Convergence round N+1 consumes round N's artifacts; only the
  contributors *within* a single phase/round fan out. The round loop is unchanged.

Affected functions: `_run_workflow_3`, `_run_workflow_4` (Phase-1 loop + per-round loop).
A shared private helper — e.g. `_dispatch_phase_parallel(enabled, fn) -> list[SealedArtifact]`
— encapsulates the executor, ordering, and error collection so both workflows share one
implementation and cannot drift.

### Component 2 — Determinism & error handling

Concurrency must not change outcomes, only wall-clock.

- **Deterministic ordering.** Collect futures concurrently, then re-sort the resulting
  artifacts into `enabled` (config) order before sealing, audit-logging, and convergence
  evaluation. Seal ordering and convergence input become byte-reproducible regardless of
  which CLI finishes first.
- **Collect-all, not fail-fast.** A reviewer that raises `DispatchError` (timeout, crash)
  is recorded via the existing per-contributor `timeout_policy` (`_record_timed_out`); the
  remaining reviewers still complete. One bad CLI never aborts the panel. No future's
  exception propagates out of the helper — each is caught and mapped to the existing
  per-contributor failure path.
- **Per-contributor timeout** is already per-call; parallel dispatch does not change it.

### Component 3 — Skill codification (uniform every install)

Tighten `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`:

- Codify the single path: write goal_packet → ONE fan-out call (the engine /
  `run_iteration` path) → wait for all → converge.
- **Explicitly forbid** the improvised "validate plumbing with one reviewer first" probe.
- Reference containment-marker dirs being auto-created at init (see separate TODO
  `todo-containment-marker-dirs`) — no hand-creation step in the consult flow.

## kimi-integrity note

Prior guidance: kimi's integrity check false-positives on concurrent **repo source edits**
during a dispatch. Reviewer dispatch writes **sealed artifacts to the consensus-state dir**,
not repo source. The new-machine run already executed kimi concurrently with gemini+grok
under Path A without tripping it. Action: before/at implementation, confirm reviewer artifact
writes do not land where kimi's integrity snapshot watches (expected: safe). If they did, the
fix is to ensure the state dir is outside the integrity scope — NOT to serialize.

## Testing strategy (TDD)

- **Parallelism is observable, not just faster.** Assert concurrency directly rather than by
  timing: inject fake adapters whose `review/propose/converge` block on a shared barrier that
  only releases once N have entered — proving all N ran concurrently (a sequential loop would
  deadlock the barrier and time out the test). No wall-clock flakiness.
- **Determinism.** Fake adapters that complete in shuffled order must yield artifacts in
  `enabled` order; assert the sealed/eval order is identical across runs.
- **Collect-all.** One adapter raises `DispatchError`; assert the others' artifacts are still
  collected and the failure is recorded via the timeout path (not propagated).
- Existing workflow_3 / workflow_4 convergence and seal tests must stay green (behavior parity
  except ordering-by-completion, which Component 2 normalizes away).

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
