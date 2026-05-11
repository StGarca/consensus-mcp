# consensus_resume — MCP tool spec (v2)

**Status:** v2 (post-codex-iter0002-1) — addresses codex-rev-001..007
**Author:** claude (orchestrator) 2026-05-11
**Reviewer:** codex (dispatched via consensus_mcp._dispatch_codex)
**Iteration:** iter-0002-consensus-resume-spec, pass 2
**Motivation:** parity-with-guild bootstrap ergonomics — one MCP call returns the full operating-context snapshot of a consensus-mcp run so an orchestrator that just `/clear`-ed, compacted, or restarted can pick up exactly where it left off.

## Changes from v1

| Finding | Severity | Resolution location |
|---|---|---|
| codex-rev-001 | HIGH | §4 — `wait_for_dispatch` added to action enum with payload |
| codex-rev-002 | HIGH | §3 — single sort rule with deterministic tiebreaker; ambiguity always surfaced |
| codex-rev-003 | MEDIUM | §7 — every disk read in §5 has an explicit error policy |
| codex-rev-004 | MEDIUM | §4, §5 — `open_reviews[]` entries carry `classification` + closure source named |
| codex-rev-005 | MEDIUM | §4 — `bundle_mutation` (closure-relevant) split from `recent_activity` (audit) |
| codex-rev-006 | MEDIUM | §4, §8 — `snapshot_watermark` + revalidation rule replaces "tolerate transient inconsistency" |
| codex-rev-007 | LOW | §9 — all seven open questions answered |

---

## 1. Purpose

When an orchestrator (claude, codex, or any MCP client) attaches to a consensus-mcp instance, it currently has to inspect the filesystem under `consensus-state/` to figure out:

- Is there an iteration in flight?
- Has a dispatch been started? Is it still running?
- Are there sealed reviews waiting on the cross-family closure invariant?
- Is there a stuck dispatch the operator already aborted but didn't clean up?
- What did the last actor do, and what is the implicitly-expected next action?

This is N tool calls today (read goal_packet → list iteration dir → grep dispatch-log → read latest review-packet → check heartbeat freshness → …). It's also error-prone: an orchestrator that misreads the state can re-dispatch a still-running pass, or try to close an iteration whose mutation hasn't been hashed yet.

`consensus_resume` collapses all of that into one call. It returns a **read-only snapshot** of the world that the next actor needs to make a correct decision about what to do next.

The pattern is borrowed from `mathomhaus/guild`'s single-call session-bootstrap idiom ("one tool call returns project oath + parting scroll + highest-priority quest") — but adapted to consensus-mcp's verification-first semantics. Where guild returns "what to do," consensus_resume returns "what state the verification gate is in and what the invariants say must happen next."

## 2. Non-goals

- **Not a mutation tool.** `consensus_resume` is strictly read-only. It must never write to `consensus-state/`, never send signals, never start a dispatch.
- **Not a planner.** It reports what the closure invariant *requires* next based on the current sealed state. It does not pick which workflow (#1/#2/#3/#4) to use, doesn't author goal packets, doesn't propose patches.
- **Not a watchdog substitute.** `_visibility_watchdog.py` handles orphan cleanup; `consensus_resume` only *reports* whether an orphan-dispatch condition exists.
- **No cross-iteration history.** Returns the current iteration plus an optional immediate-predecessor pointer when an archived `closure-certificate.yaml` exists.
- **No new persistence.** Reads existing on-disk files (`goal_packet.yaml`, `dispatch-log.jsonl`, `*-review.yaml`, `iteration-outcome.yaml`, `closure-certificate.yaml`); does not introduce a new DB or cache layer.

## 3. Input

```jsonc
{
  "iteration_id": "iter-0002-consensus-resume-spec",   // optional; auto-detect if absent
  "include_streamed_lines": false,                      // optional; default false
  "max_streamed_lines": 50,                             // optional; only used if above is true
  "prior_snapshot_watermark": "sha256..."               // optional; see §8
}
```

**Auto-detection rule when `iteration_id` is absent (codex-rev-002 resolution):**

1. List directories under `consensus-state/active/` whose `goal_packet.yaml` exists and parses.
2. **Primary sort:** `authorization.authorized_at_utc` descending. Iterations with missing or unparseable `authorized_at_utc` sort last.
3. **Tiebreaker:** directory name lexicographic descending (POSIX byte order, not locale-aware).
4. `selected_iteration_id` is the first element of the sorted list (or `null` if the list is empty).
5. `multiple_active_iterations[]` is populated **whenever** the sorted list has length > 1, regardless of whether the operator passed `iteration_id` explicitly. (Surfacing ambiguity is always cheap; suppressing it can be misleading.)

Lex-greatest naming is NOT used as a primary key under any condition. Iteration directory names are operator-authored and not guaranteed numerically prefixed.

## 4. Output (schema)

```jsonc
{
  "schema_version": 2,
  "snapshot_taken_at_utc": "2026-05-11T17:42:11Z",
  "snapshot_watermark": "sha256...",                   // §8 — content-bound stale-snapshot detector
  "selected_iteration_id": "iter-0002-consensus-resume-spec",
  "iteration_state": "open" | "closed_passed" | "closed_failed" | "blocked_operator" | "unknown",

  "goal": {
    "summary": "...",
    "scope_signature": "sha256...",
    "scope_signature_valid": true,                     // recomputed and compared
    "authorized_by": "operator",
    "authorized_at_utc": "...",
    "max_iterations": 1,
    "iterations_used": 0
  } | null,                                            // null when goal_packet.yaml missing/malformed

  // codex-rev-005 resolution: bundle_mutation = closure-relevant only.
  // Only events that change review_target_hash (= bundle_sha of touched files)
  // are eligible to populate this field. Other audit events go to recent_activity.
  "bundle_mutation": {
    "actor": {"id": "claude-author-1", "model_family": "claude"},
    "kind": "patch_applied" | "review_packet_rebundled" | "operator_force_bundle_rewrite",
    "timestamp_utc": "...",
    "bundle_sha256": "..."                             // canonical review_target_hash after this event
  } | null,

  // codex-rev-005 resolution: separate field for non-closure audit context.
  // Bounded — most recent N=10 events, newest-first, deterministic order.
  "recent_activity": [
    {
      "event_id": "...",
      "kind": "dispatch_started" | "dispatch_heartbeat" | "dispatch_streamed_line"
            | "dispatch_completed" | "dispatch_aborted"
            | "review_packet_authored" | "review_sealed"
            | "goal_packet_authored" | "operator_abort_signaled",
      "actor_id": "...",
      "timestamp_utc": "..."
    }
  ],

  // codex-rev-004 resolution: each entry classified; closure source named.
  "open_reviews": [
    {
      "pass_id": "codex-iter0002-1-pass1",
      "reviewer_id": "codex-iter0002-1",
      "model_family": "codex",
      "sealed_at_utc": "...",
      "blocking_objections": ["codex-rev-001"],
      "goal_satisfied": false,
      "review_target_hash": "...",

      // Classification source (§5 step 4):
      //   "open"        — sealed; review_target_hash matches current bundle; not referenced by any closure cert
      //   "closing"     — referenced by an existing closure-certificate.yaml as a closer
      //   "consumed"    — referenced by closure cert; iteration is closed_passed/closed_failed
      //   "superseded"  — review_target_hash != current bundle (bundle_mutation occurred after seal)
      //   "invalid"     — parses but fails closure-invariant freshness/hash-match self-check
      "classification": "open",
      "superseded_by_pass_id": null,
      "closure_source": null                           // path to closure-certificate.yaml when classification ∈ {closing, consumed}
    }
  ],

  "in_flight_dispatches": [
    {
      "pass_id": "codex-iter0002-1-pass1",
      "reviewer_id": "codex-iter0002-1",
      "started_at_utc": "...",
      "last_heartbeat_utc": "...",
      "seconds_since_last_line": 12,
      "stall_silence_threshold_seconds": 45,           // imported from _dispatch_codex (codex-rev-007 Q1)
      "abort_signal_path": "consensus-state/abort-dispatch-codex-iter0002-1-pass1.signal",
      "abort_signal_present": false,
      "looks_stuck": false
    }
  ],

  "closure_invariant_status": {
    "satisfiable_now": false,
    "blockers": ["needs_cross_family_reviewer"],
    "last_bundle_mutation_family": "claude",
    "valid_closer_families": ["codex"],
    "valid_closer_review_target_hash": "..."
  },

  // codex-rev-001 resolution: full enum with payload per kind.
  "expected_next_action": {
    "kind":
        "dispatch_cross_family_reviewer"
      | "wait_for_dispatch"                           // ADDED v2 — pass_id + freshness payload
      | "apply_proposed_patch"
      | "close_iteration"
      | "operator_decision_required",
    "rationale": "human-readable string",
    "suggested_command": "python -m consensus_mcp._dispatch_codex ..." | null,
    // Per-kind payload (only the field for the selected kind is populated):
    "wait_for_dispatch_payload": {
      "pass_id": "codex-iter0002-1-pass1",
      "seconds_since_last_line": 12,
      "stall_silence_threshold_seconds": 45,
      "looks_stuck": false
    } | null,
    "apply_proposed_patch_payload": {
      "finding_id": "codex-rev-001",
      "patch_id": "codex-rev-001-patch"
    } | null,
    "close_iteration_payload": {
      "closing_review_pass_id": "codex-iter0002-1-pass1"
    } | null
  },

  // codex-rev-007 Q4: optional, only populated when prior iteration's
  // closure-certificate.yaml exists in consensus-state/archive/.
  "previous_iteration_summary": {
    "iteration_id": "iter-0001-extraction-review",
    "iteration_state": "closed_passed",
    "closed_at_utc": "...",
    "closure_certificate_path": "consensus-state/archive/.../closure-certificate.yaml"
  } | null,

  "warnings": [],                                      // soft signals; not blocking
  "multiple_active_iterations": []                    // populated when len > 1
}
```

All timestamps are RFC 3339 UTC with `Z` suffix. All hashes are sha256 hex (64 chars).
All array fields use deterministic ordering: `recent_activity` newest-first by `timestamp_utc`; `open_reviews` ascending by `pass_id`; `in_flight_dispatches` ascending by `pass_id`; `warnings` ascending by string content; `multiple_active_iterations` per §3 sort order.

## 5. Behavior

1. **Resolve iteration** per §3.
2. **Load `goal_packet.yaml`.** Recompute `scope_signature` via `_self_drive._scope_signature` and compare to recorded value. Surface `scope_signature_valid: false` as a flag if mismatch (do NOT raise — per codex-rev-007 Q3, downstream apply will block tampered packets; consensus_resume is a *reporter*).
3. **Walk `consensus-state/state/dispatch-log.jsonl`** (filter by iteration_id). Build `recent_activity[]` (newest 10) and per-pass `in_flight_dispatches[]` (entries with `dispatch_started` but no `dispatch_completed` / `dispatch_aborted`).
4. **Glob `<iter_dir>/*-review.yaml`.** For each:
   - Parse `payload.findings`, `payload.goal_satisfied`, `payload.blocking_objections`, `dispatch_provenance.review_target_hash`, `actor.model_family`, `pass_id`, `sealed_at_utc`.
   - Classify per §4:
     - If `<iter_dir>/closure-certificate.yaml` exists AND references this `pass_id` as a closer: classification = `closing` (if `iteration_state` is still `open`) or `consumed` (if closed).
     - Else if `dispatch_provenance.review_target_hash` ≠ current bundle sha (= `bundle_mutation.bundle_sha256`): classification = `superseded`; populate `superseded_by_pass_id` with the newest review whose hash *does* match.
     - Else if closure-invariant freshness/hash self-check fails: classification = `invalid`.
     - Else: classification = `open`.
5. **Determine `bundle_mutation`.** Read `<iter_dir>/iteration-outcome.yaml` and `<iter_dir>/review-packet.yaml`. The latest event of kind `patch_applied`, `review_packet_rebundled`, or `operator_force_bundle_rewrite` (per dispatch-log) wins. If none exists, `bundle_mutation: null`.
6. **Compute `iteration_state`.**
   - `closed_passed` ⇔ `<iter_dir>/closure-certificate.yaml` exists AND its `verdict` is `quorum_close_passed`.
   - `closed_failed` ⇔ closure-certificate exists AND verdict is `quorum_close_failed`.
   - `blocked_operator` ⇔ `<iter_dir>/iteration-outcome.yaml` exists AND its `state` is `blocked_needs_operator`. (codex-rev-007 Q6: derive from existing files; no new on-disk marker introduced.)
   - `unknown` ⇔ `goal_packet.yaml` missing or unparseable.
   - `open` ⇔ none of the above.
7. **Compute `closure_invariant_status`** via `_closure_invariant.py` semantics against `bundle_mutation` + classified reviews.
8. **Determine `expected_next_action`.** Exhaustive decision tree (codex-rev-001 resolution):

   ```
   if iteration_state == "closed_passed" or "closed_failed":
     → operator_decision_required ("iteration already closed; start a new iteration")
   elif iteration_state == "blocked_operator":
     → operator_decision_required ("iteration is blocked; consult iteration-outcome.yaml")
   elif iteration_state == "unknown":
     → operator_decision_required ("goal_packet missing or unparseable")
   elif closure_invariant_status.satisfiable_now:
     → close_iteration (closing_review_pass_id from the satisfying open review)
   elif any open_review.classification == "open" has a patch_proposal that hasn't been applied:
     → apply_proposed_patch (finding_id + patch_id from that review)
   elif any in_flight_dispatch.looks_stuck == False:
     → wait_for_dispatch (oldest healthy in-flight pass_id)
   elif any in_flight_dispatch.looks_stuck == True:
     → operator_decision_required ("dispatch <id> stalled; let watchdog auto-abort or write abort signal")
   else:
     → dispatch_cross_family_reviewer (suggested_command populated)
   ```

   Every branch returns; no fall-through `else: null`. This is acceptance-criterion-2 in §10.

9. **Compute `snapshot_watermark`** per §8.
10. **Populate `previous_iteration_summary` (optional).** If `consensus-state/archive/` contains a `closure-certificate.yaml` for an iteration with `authorized_at_utc < current.authorized_at_utc`, surface the most recent one. Otherwise `null`.

## 6. Where the implementation lives

- **New file:** `consensus_mcp/_resume.py` — pure read-only helper that imports from `_self_drive`, `_closure_invariant`, `_dispatch_codex` (for the `stall_silence_seconds` constant, per codex-rev-007 Q1), and a small log-walker.
- **New MCP tool registration:** `consensus_mcp/tools/resume.py` — wraps `_resume.snapshot()` as MCP tool `consensus_resume`.
- **No changes** to `_dispatch_codex.py`, `_visibility_tui.py`, `_visibility_watchdog.py`, `_closure_invariant.py`, `_self_drive.py`. Those are read by `_resume.py` only as imports.

The `stall_silence_seconds` constant is imported **directly** from `_dispatch_codex` (not duplicated). Drift between dispatcher and reporter is impossible by construction. (codex-rev-007 Q1 resolution.)

## 7. Error handling (codex-rev-003 resolution — every read in §5 covered)

| §5 step | Disk read | Failure mode | Policy |
|---|---|---|---|
| 1 | `consensus-state/active/` listing | Directory missing | Return `iteration_state: "unknown"`, `selected_iteration_id: null`, warning `"consensus-state/active/ does not exist"`. Do not raise. |
| 1 | `iteration_id` passed but dir absent | Bad operator input | Raise `ValueError("iteration_id not found")`. Programming error, not a state condition. |
| 2 | `goal_packet.yaml` | Missing | `goal: null`, `iteration_state: "unknown"`, warning. |
| 2 | `goal_packet.yaml` | Malformed YAML | `goal: null`, `iteration_state: "unknown"`, warning with parse-error excerpt. |
| 2 | `scope_signature` | Mismatch | Flag `scope_signature_valid: false`, do not raise. |
| 3 | `dispatch-log.jsonl` | Missing | `recent_activity: []`, `in_flight_dispatches: []`, warning `"dispatch-log.jsonl not found"`. |
| 3 | `dispatch-log.jsonl` line | Malformed JSON | Skip line, append to warnings (line number only, no content). |
| 4 | `*-review.yaml` glob | No matches | `open_reviews: []`, no warning (legitimate state for a freshly-authored iteration). |
| 4 | A specific review YAML | Malformed | Skip review, warning `"review file <name> failed to parse"`. Do NOT include in `open_reviews[]`. |
| 5 | `review-packet.yaml` | Missing | `bundle_mutation: null`, warning. |
| 5 | `iteration-outcome.yaml` | Missing | Treated as "no outcome recorded yet" — `iteration_state` falls through to other rules. No warning. |
| 5 | `iteration-outcome.yaml` | Malformed | Warning; `iteration_state` falls through. Do not raise. |
| 6 | `closure-certificate.yaml` | Missing | Treated as "not yet closed" — `iteration_state ∈ {open, blocked_operator, unknown}` per other rules. No warning. |
| 6 | `closure-certificate.yaml` | Malformed | Warning; classify all reviews as `open` or `superseded`; `iteration_state: open`. |
| 10 | `consensus-state/archive/` | Missing | `previous_iteration_summary: null`. No warning. |

Every disk read in §5 has a row above. (Acceptance-criterion-5.)

## 8. Concurrency and snapshot consistency (codex-rev-006 resolution)

`consensus_resume` is read-only and lock-free **for individual file reads**, but emits a `snapshot_watermark` that lets clients detect torn reads and retry.

**Watermark computation.** At snapshot time, compute:

```
snapshot_watermark = sha256(
    str(iteration_dir.stat().st_mtime_ns) + "|" +
    str(dispatch_log_path.stat().st_mtime_ns) + "|" +
    "|".join(sorted(f"{p.name}:{p.stat().st_mtime_ns}"
                    for p in iteration_dir.glob("*.yaml")))
)
```

**Watermark check (when `prior_snapshot_watermark` is passed).** Re-compute watermark BEFORE doing any disk read. If it equals `prior_snapshot_watermark`, return a cheap response:

```jsonc
{
  "schema_version": 2,
  "snapshot_watermark": "...",
  "watermark_unchanged_since_prior": true,
  "snapshot_taken_at_utc": "...",
  // all other fields omitted
}
```

The orchestrator MUST treat `watermark_unchanged_since_prior: true` as a successful no-op refresh — the prior snapshot it cached is still valid.

**Torn-read protection.** After computing the snapshot, re-compute the watermark. If it changed during the snapshot construction, set `warnings += ["watermark_drifted_during_snapshot; recommend retry"]` and include both the start and end watermarks in the response under `snapshot_watermark_drift: {start: ..., end: ...}`. The client SHOULD retry. The response is still well-formed and safe to consume — just possibly stale on one or more sub-objects.

This replaces v1's "tolerate transient inconsistency" claim with a content-bound stale-snapshot detector that requires no lock contention.

## 9. Open design questions — resolutions (codex-rev-007)

| # | Question | v2 answer | Rationale |
|---|---|---|---|
| 1 | `looks_stuck` threshold sourcing | Import from `_dispatch_codex` directly. | Single source of truth. Drift impossible by construction. |
| 2 | `closure_invariant_status` vs. `expected_next_action` redundancy | Keep split. | Machine-checkable invariant status and human-readable next action serve different consumer surfaces. A linter wants the former; an orchestrator wants the latter. |
| 3 | `scope_signature_valid: false` policy | Report only; do not refuse. | The downstream `apply.codex_patch` tool already validates scope-signature integrity. consensus_resume is a *reporter*; refusing to report on a tampered packet would hide a real adversarial signal. |
| 4 | Cross-iteration handoff | Surface `previous_iteration_summary` only when archive's `closure-certificate.yaml` exists. | Cheap, passive, no new disk writes. Bias toward "explicit reference in next iteration's goal_packet" remains correct for hard dependencies; this is a soft hint only. |
| 5 | Auto-detection ambiguity | Resolved in §3. | `authorized_at_utc` primary, dir-name lex tiebreaker, ambiguity always surfaced. |
| 6 | `blocked_needs_operator` derivation vs. explicit marker | Derive from `iteration-outcome.yaml.state` per §5 step 6. | An explicit marker file would be a new on-disk artifact and contradict §2 "no new persistence." The orchestrator that *creates* a blocked state already writes iteration-outcome.yaml; consensus_resume reads it. |
| 7 | Output stability across reads | All arrays use deterministic ordering (§4). Dict serialization uses `sort_keys=True` on any JSON path that gets hashed. | Byte-identical snapshots are required for the watermark to be meaningful and for testing. |

## 10. Acceptance criteria

The spec is *implementable* if a reviewer can answer "yes" to each:

- [ ] Every field in the §4 output schema has an unambiguous source (file or computation) named in §5.
- [ ] The `expected_next_action` decision tree (§5 step 8) is exhaustive — every branch returns; no `else: null`. (codex-rev-001)
- [ ] No mutation to `consensus-state/` is described anywhere in §5.
- [ ] All seven open questions in §9 have explicit answers with rationale. (codex-rev-007)
- [ ] Error paths in §7 cover every disk read in §5 — verifiable by row-count vs. read-count audit. (codex-rev-003)
- [ ] `expected_next_action.kind` enum (§4) is a superset of every branch's return value in §5 step 8. (codex-rev-001)
- [ ] `open_reviews[].classification` enum is exhaustive over the cases in §5 step 4. (codex-rev-004)
- [ ] `bundle_mutation.kind` is restricted to events that change `review_target_hash`; `recent_activity.kind` covers everything else. (codex-rev-005)
- [ ] `snapshot_watermark` is content-bound (sha256 over deterministic inputs) and the §8 watermark-check fast path is specified. (codex-rev-006)
- [ ] All output arrays have a defined sort key. (codex-rev-007 Q7)
