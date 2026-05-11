---
title: Recent iteration failures (iter-0019 → iter-0031)
type: consensus pipeline-postmortem
created: 2026-05-10
updated: 2026-05-11
scope: consensus pipeline iterations 0019 through 0031
tags: [consensus pipeline, postmortem, iter-0019-0031, qa]
---

# Recent iteration failures (iter-0019 → iter-0031)

This is a failure-focused readout of the consensus pipeline iterations run during the 2026-05-10 session. The wins are documented in `2026-05-10-codex-fix-author-roadmap-results.md` and per-iteration `iteration-outcome.yaml`. **This doc only catalogs what went wrong, what surfaced it, and what landed as the fix.**

## Summary

**13 iterations attempted, 12 documented failure events across 5 recurring patterns, 1 stalled iteration abandoned.**

| Iter | Failure event | Surfaced by | Resolution |
|---|---|---|---|
| 0019 | schema strict-output incompatibility | codex CLI rejecting dispatch | inline schema fix |
| 0019 | sandbox-cannot-compute patch_id | codex empirical fail | iter-0020 redesign |
| 0020 | sandbox-cannot-read source files | observed in emitted diff | iter-0021 redesign |
| 0021 | sandbox-cannot-compute base_sha | observed in emitted patch_proposal | iter-0022 helper-stamp |
| 0023 | merge-direction integrity bug | claude implementation_critic | iter-0024 F1 |
| 0023 | ISO-8601 lex-string freshness | claude/codex overlap | iter-0024 F2 |
| 0025 | EMITTER gap for canonical mutation fields | codex caught claude's miss | iter-0026 codex-rev-002 |
| 0026 | CRLF base_sha mismatch on Windows | claude empirical fail at apply-time | iter-0026 F1 |
| 0027 | MCP wrapper drops helper return code | codex | iter-0028 F3 |
| 0027 | scope check bypass via unified_diff body | codex | iter-0028 F4 |
| 0029 | blocked_closure_invariant_failed has no positive test | codex + claude overlap | iter-0030 F2 |
| 0031 | stalled subagent dispatch | operator observation | abandoned + cleaned up |

## Current live verification (2026-05-10, post schema/validator alignment)

After all 13 iterations + iter-0031 cleanup + schema/validator alignment fix:

- pytest: **344 passed** (was 343; +1 regression test `test_patch_proposal_missing_expected_tests_rejected`)
- smoke: **60/60 tests passed**
- release gates: **10/11 gates passed** pre-commit (G_unstaged FAILs by design while there is a working-tree diff); **11/11** after commit

Verification commands:
```
python_env/python.exe -m pytest consensus_mcp/tests/ -q
python_env/python.exe -m consensus_mcp._smoke_test
python_env/python.exe consensus_mcp/_release_gate_check.py
```

The doc below is a historical postmortem. The live state is materially stronger than any individual failure event below.

## Status matrix

| Class | Items | Notes |
|---|---|---|
| **Resolved (in-code fix landed + tests)** | iter-0019 schema, iter-0020 patch_id, iter-0021 contents-embed, iter-0022 base_sha stamp, iter-0023 merge-direction, iter-0023 freshness, iter-0025 EMITTER gap, iter-0026 CRLF, iter-0027 wrapper rc, iter-0027 scope bypass, iter-0029 test-gap | landed across iter-0019 → iter-0030; pytest/smoke/gates green |
| **Resolved this session** | schema/validator mismatch on `patch_proposal.expected_tests` (validator listed it optional; schema required it) | `_PATCH_PROPOSAL_REQUIRED` updated + regression test added; pytest 344 / smoke 60/60 / gates 10/11 (G_unstaged clears on commit) |
| **Needs follow-up (no acceptance criteria yet)** | iter-0031 subagent-stall watchdog | tracked as new task |
| **Abandoned (intentional, not a defect)** | iter-0031 self_drive Flavor B review | re-run from a fresh session with external codex-cli |

---

## iter-0019 — schema strict-output incompatibility

**closing_state**: `blocked_needs_operator`

**What failed**: First real-codex dispatch was rejected by the codex CLI's strict-output validator. OpenAI strict-output requires every property to be in the `required` array, but the iter-0014 schema had `expected_tests` optional inside `patch_proposal`, and `patch_not_proposed_reason` optional at finding level.

**Surfaced by**: codex CLI itself refusing to emit any content. Visible in the dispatch error.

**Resolution (inline)**:
- `patch_proposal.required` now includes `expected_tests`
- finding.required now includes both `patch_proposal` AND `patch_not_proposed_reason`, both nullable (`[T, null]`)
- mutual exclusivity moved out of JSON Schema and into `_dispatch_codex._validate_patch_proposal` (python parse-time), since OpenAI strict-output rejects `oneOf`

**Evidence**: `consensus_mcp/dispatch_templates/codex_review_schema.json:12,24` (required arrays); `consensus-state/active/iteration-0019-real-codex-fix-demo/iteration-outcome.yaml`.

**Caveat (live divergence — see "Open finding" below)**: the strict-output JSON schema requires `expected_tests` inside `patch_proposal`, but the python-side `_validate_patch_proposal` lists it as optional. Codex CLI rejects missing-field emissions, so production traffic never sees the divergence, but synthetic / non-CLI callers can construct a `patch_proposal` lacking `expected_tests` and pass the local validator.

**Lesson**: don't assume tooling vendors support standard JSON Schema features. Strict-output is a real constraint that drives schema shape.

---

## iter-0019 — sandbox cannot compute content-bound patch_id

**What failed**: After the schema fix, codex still couldn't produce a valid `patch_proposal.patch_id`. The iter-0014 formula was:

```
patch_id = patch-{base_sha[:12]}-{sha256(unified_diff)[:12]}
```

Codex's sandbox does NOT execute arbitrary code. It cannot compute `sha256(unified_diff)` mid-reasoning. The formula was authored assuming a tool-using agent; codex is text-only.

**Surfaced by**: codex emitting `patch_id` strings that failed the helper's regex match against the binding formula.

**Resolution**: deferred to iter-0020 (relax to `^codex-rev-\d+-patch$` — finding-id-derived form codex CAN produce; helper stamps the content-bound `unified_diff_sha256` separately).

**Evidence**: `consensus-state/active/iteration-0020-patch-id-ergonomics/iteration-outcome.yaml` (SUCCESS section documents codex-iter0020-3 pass with `patch_id: codex-rev-001-patch` and helper-stamped `unified_diff_sha256`).

**Lesson**: any "compute this hash" requirement placed on a read-only sandbox agent will fail. Helper-stamp pattern is the answer.

---

## iter-0020 — sandbox cannot read source files

**closing_state**: `blocked_needs_operator`

**What failed**: With patch_id ergonomics fixed, codex emitted a valid `patch_proposal`, BUT the `unified_diff` body was structurally wrong. The diff hallucinated `def handle(args):` for a function whose real signature in `tools/apply_codex_patch.py` is `def handle(iteration_dir: str, patch_id: str, actor: dict)`. Codex's sandbox apparently could not read the actual source file; it reconstructed the diff from imagined code shape.

**Surfaced by**: visual inspection of the emitted `unified_diff` against the real file.

**Resolution**: deferred to iter-0021 (embed touched-file full contents inline in the review-packet via `_author_review_packet`, so codex sees them in the prompt without needing filesystem reads).

**Evidence**: `consensus-state/active/iteration-0020-patch-id-ergonomics/iteration-outcome.yaml` (SECONDARY OBSERVATION section); `consensus_mcp/_author_review_packet.py`.

**Lesson**: codex's read-only sandbox can read files when invoked via codex-cli, but inside our dispatch pipeline the file-reading guarantee is absent. Treat codex as a pure text-in/text-out agent and embed everything it needs.

---

## iter-0021 — sandbox cannot compute base_sha

**closing_state**: `blocked_needs_operator`

**What failed**: With contents embedded, codex emitted a content-correct diff (hunks matched real source line-by-line). BUT `patch_proposal.base_sha` was a different value than the operator-stamped `defect_target.base_sha`. Codex was producing some hash, but not the canonical `bundle_sha(repo_root, files)` over disk bytes.

**Surfaced by**: helper validation comparing emitted `base_sha` to `review_packet.defect_target.base_sha`.

**Resolution**: deferred to iter-0022 (helper OVERWRITES `patch_proposal.base_sha` with `defect_target.base_sha` post-validation; codex's emitted value is informational only).

**Evidence**: `consensus-state/active/iteration-0021-real-codex-fix-end-to-end/iteration-outcome.yaml` (PRIMARY EMPIRICAL PROOF); `consensus-state/active/iteration-0022-end-to-end-fix-loop/iteration-outcome.yaml` (closing_state `quorum_close_passed` confirms the end-to-end loop closes); `consensus_mcp/_dispatch_codex.py` (base_sha overwrite logic).

**Lesson**: same root cause as iter-0019/0020 — sandbox can't compute hashes. Helper-stamp the canonical value rather than asking codex to produce it.

---

## iter-0023 — merge-direction integrity bug in last_mutation hoist

**closing_state**: `blocked_closure_invariant_failed`

**What failed**: `_closure_invariant.last_mutation_from_audit` was hoisting both a top-level legacy actor (string) and a nested structured actor (dict), then merging them with the top-level value WINNING. Result: the legacy string shadowed the structured dict, breaking `actor.model_family` extraction, which broke the cross-family check downstream.

**Surfaced by**: claude implementation_critic pre-codex pass (claude-iter0023-001 HIGH). Codex's `codex-rev-001` addressed only the outer read-fallback issue and MISSED the inner merge-direction bug.

**Resolution**: iter-0024 F1 — inverted the hoist merge so nested structured dict wins over legacy top-level string.

**Verified in current code (2026-05-10)**: `_closure_invariant.check_closure_invariant` now keys the cross-AI gate on `actor.model_family`, not `actor.id`. A prior version of the closure invariant compared only `actor.id`, which would have silently passed Codex-A → Codex-B (same family, different ids) as cross-AI. **The model_family check corrects that overclaim**: if either side lacks `model_family`, the gate fails closed (see `_closure_invariant.py:197-233`).

**Evidence**: `consensus-state/active/iteration-0023-closure-invariant-flavor-b-review/iteration-outcome.yaml` (`blocked_closure_invariant_failed`), `consensus-state/active/iteration-0024-closure-invariant-followup-fixes/iteration-outcome.yaml` (`implementation_ready_followup_fix_landed`), `consensus_mcp/_closure_invariant.py:140-160` (hoist-merge fix) and `:212-260` (cross_family gate).

**Lesson**: codex-only review is NOT sufficient for integrity-critical invariants. The implementation_critic pre-pass catches things codex misses.

---

## iter-0023 — ISO-8601 lex-string freshness compare

**What failed**: freshness check in `check_closure_invariant` compared timestamps as raw lex strings. Works for fixed-format ISO-8601, breaks the moment any timestamp uses a different tz suffix or precision (e.g. `2026-05-10T22:00:00Z` vs `2026-05-10T22:00:00+00:00`).

**Surfaced by**: BOTH claude-iter0023-002 AND codex-rev-002 (direct match — only finding both reviewers independently caught).

**Resolution**: iter-0024 F2 — UTC-normalized datetime parse instead of lex compare.

**Evidence**: `consensus-state/active/iteration-0023-closure-invariant-flavor-b-review/codex-review.yaml` (codex-rev-002); `consensus-state/active/iteration-0023-closure-invariant-flavor-b-review/claude-review-implementation-critic.yaml` (claude-iter0023-002); `consensus_mcp/_closure_invariant.py:_parse_utc_timestamp`.

**Lesson**: confirmation by independent overlap. When both reviewers flag the same thing, the priority is unambiguous.

---

## iter-0025 — EMITTER gap for canonical mutation fields

**closing_state**: `quorum_close_passed` (but only after codex caught a gap claude missed)

**What failed**: `audit.append_event` for `apply_step_landed` emitted `last_mutation` ONLY in a nested object, not at top-level canonical fields. iter-0023/0024 fixed the READER side (hoist merge); the EMITTER side was still asymmetric. New code reading the audit would see only nested fields and miss the top-level convention.

**Surfaced by**: codex-rev-002. Claude reviewers (including the implementation_critic) MISSED this asymmetry.

**Resolution**: iter-0026 codex-rev-002 — `apply_step_landed` now emits canonical top-level fields (actor, timestamp, post_sha, files_touched) in addition to nested `last_mutation`.

**Evidence**: `consensus-state/active/iteration-0025-apply-pipeline-flavor-b-review/codex-review.yaml` (codex-rev-002); `consensus-state/active/iteration-0026-apply-pipeline-followup/iteration-outcome.yaml` (closing_state `flavor_a_followup_closed`); `consensus_mcp/tools/audit_append_event.py`.

**Lesson**: codex catches EMITTER-side defects that claude tends to miss because claude's mental model focuses on the reader. Cross-AI review is empirically asymmetric.

---

## iter-0026 — CRLF base_sha mismatch on Windows

**What failed**: `bundle_sha` was reading file contents as text strings and hashing them. On Windows, git's autocrlf settings caused the hash to differ between in-memory (LF) and on-disk (CRLF) representations. Apply-time `base_sha` check refused with `base_sha_drift` even though the diff was correct.

**Surfaced by**: empirical apply failure during iter-0025 cleanup mutation. Reproduced reliably on Windows.

**Resolution**: iter-0026 F1 — `_compute_per_patch_base_sha` reads file as disk bytes (binary mode) for hashing. CRLF/LF differences become part of the canonical hash, matching what apply-time will see.

**Evidence**: `consensus_mcp/_dispatch_codex.py:_compute_per_patch_base_sha`; `consensus-state/active/iteration-0026-apply-pipeline-followup/iteration-outcome.yaml` (F1 line item).

**Lesson**: any cross-platform tool that hashes file contents must use bytes, not strings. Windows CRLF is silent until it bites.

---

## iter-0027 — MCP wrapper drops helper return code

**closing_state**: `quorum_close_passed_no_mutation`

**What failed**: `reviewer_dispatch_codex.handle()` did not capture the int return code from `_dispatch_codex.main(argv)`. If the helper printed success-shaped JSON to stdout but returned non-zero, the wrapper would report `ok=true` because it only inspected stdout content.

**Surfaced by**: codex-rev-001 (HIGH). Claude reviewers missed this — the failure mode requires reasoning about MCP-tool result-shaping semantics, not pure code review.

**Resolution**: iter-0028 F3 — `reviewer_dispatch_codex` now captures `_dispatch_codex.main()` rc and forces `ok=False` on rc != 0.

**Evidence**: `consensus-state/active/iteration-0027-codex-dispatch-flavor-b-review/codex-review.yaml` (codex-rev-001); `consensus_mcp/tools/reviewer_dispatch_codex.py` (rc capture); `consensus-state/active/iteration-0028-codex-dispatch-followup/iteration-outcome.yaml`.

**Lesson**: codex is strong on contract violations between layers (helper-vs-wrapper). Claude is weaker here because claude tends to trust the helper's stdout shape.

---

## iter-0027 — scope check bypass via unified_diff body paths

**What failed**: `_validate_patch_proposal` scope-checked `patch_proposal.files_touched` against `allowed_paths` / `forbidden_paths` but never checked the actual `+++ b/<path>` headers inside `patch_proposal.unified_diff`. A malicious or buggy codex emission could declare `files_touched=[ok_file]` while the diff body targeted a forbidden path.

**Surfaced by**: codex-rev-002 (HIGH). Critical for sandbox enforcement.

**Resolution**: iter-0028 F4 — `_dispatch_codex._validate_patch_proposal` now parses `+++ b/<path>` headers via `_DIFF_FILE_HEADER_RE` and scope-checks each against allowed/forbidden lists.

**Evidence**: `consensus-state/active/iteration-0027-codex-dispatch-flavor-b-review/codex-review.yaml` (codex-rev-002); `consensus_mcp/_dispatch_codex.py:_DIFF_FILE_HEADER_RE`; `consensus-state/active/iteration-0028-codex-dispatch-followup/iteration-outcome.yaml`.

**Lesson**: declarative scope fields (`files_touched`) and operational scope (diff body) must BOTH be enforced. Trust nothing the agent declares; verify against the actual artifact.

---

## iter-0029 — blocked_closure_invariant_failed has no positive test coverage

**closing_state**: `quorum_close_passed_no_mutation`

**What failed**: `loop_run_goal.py` mapped `closure_cross_verification_failed` stop rule to `blocked_closure_invariant_failed` state, but no test exercised the transition. A regression that broke this safety gate would land silently.

**Surfaced by**: codex-rev-001 (HIGH) AND claude-rev-003 (MEDIUM) — direct overlap.

**Resolution**: iter-0030 F2 — added positive test coverage for the transition.

**Evidence**: `consensus-state/active/iteration-0029-supervisor-flavor-b-review/codex-review.yaml` (codex-rev-001) + `claude-review.yaml` (claude-rev-003); `consensus-state/active/iteration-0030-supervisor-followup/iteration-outcome.yaml`; `consensus_mcp/tests/test_loop_run_goal.py`.

**Lesson**: defense-in-depth states need positive test coverage, not just "code path exists". Codex flagged it as HIGH because untested safety states are silent-failure waiting to happen.

---

## iter-0031 — stalled subagent dispatch (abandoned)

**closing_state**: never reached (cleaned up via `chore(consensus pipeline): clean up iter-0031 partial state`)

**What failed**: iter-0031 was the FIFTH planned Flavor B subsystem review, targeting `_self_drive.py` stop-rule subsystem. After codex emitted a v1 review, claude (in-process subagent) authored a re-dispatch follow-up. The subagent reported "Waiting for codex re-dispatch" and never returned. Two stale index entries in `consensus-state/archive/review-passes/index.yaml` referenced archive yaml files that were never written to disk.

**Surfaced by**: operator observation (TaskList showed iter-0031 as in_progress for an unusually long time; no completion signal).

**Resolution**: per operator direction "Clean up iter-0031 partial state + stop here":
- removed dangling index entries (codex-iter0031-1-pass1 + claude-iter0031-1-pass1)
- deleted partial `consensus-state/active/iteration-0031-self-drive-flavor-b-review/` directory
- task marked deleted in TaskList
- subsystem 5 (`_self_drive.py`) remains for a future session

**Evidence**: cleanup commit `a2a329b2` ("chore(consensus pipeline): clean up iter-0031 partial state"); `consensus-state/archive/review-passes/index.yaml` (post-cleanup state, iter-0031 entries removed).

**Lesson**: in-process subagent dispatch is NOT reliable for long-running codex re-dispatches. The supervisor needs a watchdog timeout on subagent execution, or the dispatch should be reified through the same external-process pipeline that iter-0019 → iter-0030 used successfully.

---

## Cross-cutting patterns

**Pattern 1 — Sandbox limitation cascade** (iter-0019 → iter-0022):
Four consecutive iterations to discover what a read-only text-only sandbox CANNOT do. Each iteration removed one assumption (codex can compute hashes / codex can read files / codex can compute bundle_sha) by pushing the work into helper-stamps and inline-content embedding. Final shape: codex is a pure reasoner over text; the helper does all I/O and crypto.

**Pattern 2 — Reader/emitter asymmetry** (iter-0023 → iter-0026):
Bugs cluster on whichever side of a contract a given reviewer last looked at. Claude reviewed the closure-invariant READER in iter-0023 and missed the EMITTER gap that codex caught in iter-0025. Defense: dual-reviewer with one bias toward consumer-side and one toward producer-side.

**Pattern 3 — Codex catches MCP-tooling defects claude misses** (iter-0027):
Two HIGH findings in iter-0027 (rc drop, scope bypass) were both contract-boundary defects in the MCP wrapper / validator layer. Claude's review missed both. Hypothesis: claude tends to trust wrapper layers it has authored; codex has no such bias.

**Pattern 4 — Overlap means the bug is real** (iter-0023, iter-0029):
Cases where both reviewers independently flag the same defect: iter-0023 freshness compare, iter-0029 untested safety state. Zero false-positive overlap so far this session.

**Pattern 5 — In-process subagent dispatch is fragile** (iter-0031):
Out of 13 iterations, the only failed-to-complete one was the only one that relied on in-process subagent re-dispatch rather than external codex-cli. Reify subagent dispatch through the same external-process pipeline.

---

---

## Resolved finding — schema / local-validator alignment on `expected_tests`

**State**: RESOLVED in this session.

**Where (pre-fix)**:
- `consensus_mcp/dispatch_templates/codex_review_schema.json:24` — `patch_proposal.required` includes `expected_tests`.
- `consensus_mcp/_dispatch_codex.py:60` (pre-fix) — `_PATCH_PROPOSAL_OPTIONAL = ("expected_tests",)`; the local validator iterated only `_PATCH_PROPOSAL_REQUIRED` and tolerated missing `expected_tests`.

**Effect (pre-fix)**: codex CLI strict-output never emitted a `patch_proposal` without `expected_tests` (the schema blocked it upstream), but synthetic or non-CLI callers could construct one and pass the python-side validator. The two layers disagreed on what a valid patch_proposal looked like.

**Fix landed**:
- `_dispatch_codex.py:_PATCH_PROPOSAL_REQUIRED` now includes `"expected_tests"`; `_PATCH_PROPOSAL_OPTIONAL` is now empty.
- Regression test `test_patch_proposal_missing_expected_tests_rejected` in `test_dispatch_codex.py` asserts a `patch_proposal` missing `expected_tests` raises `CodexOutputParseError` naming the field.
- Five test fixtures across `test_iter_0018_cross_ai_invariant.py`, `test_iter_0022_base_sha_stamp.py`, `test_iter_0024_per_patch_base_sha.py`, `test_iter_0026_crlf_base_sha.py`, `test_capstone_full_fix_loop.py` updated to include `expected_tests` in synthetic patch_proposals.
- `_release_gate_check.py` `G_pytest_dispatch_codex` baseline bumped 81→82 to match the new regression test count.

**Verified**: pytest 344 / smoke 60/60 / gates 10/11 (G_unstaged clears on commit).

---

## Suggested next-session priorities

1. ~~**Schema / local-validator alignment**~~ — DONE this session (see Resolved finding above).
2. **Subagent watchdog + external-process fallback** — promote the iter-0031 lesson to a tracked task. Acceptance: a stalled subagent dispatch gets a timeout event, a cleanup event, and recoverable state rather than silent in-progress drift.
3. **Subsystem 5 review (external codex-cli only)** — review `_self_drive.py` stop-rule subsystem. NO in-process subagent re-dispatch. All review outputs archived under `consensus-state/archive/review-passes/`.
4. **Subsystem 6**: `tools/audit_append_event.py` T6 archive + closure-invariant gate.
5. **Subsystems 7–9**: meta tooling, author/sync helpers, legacy T2–T11.
6. **Pattern-3 mitigation**: when a review targets MCP wrapper code, require explicit reviewer-rotation (codex must review claude-authored wrappers, since same-author-as-reviewer is the failure pattern).

## Verification commands after any correction

```
python_env/python.exe -m pytest consensus_mcp/tests/ -q
python_env/python.exe -m consensus_mcp._smoke_test
python_env/python.exe consensus_mcp/_release_gate_check.py
```

Expected outcome at green: 344 passed / 60/60 smoke / 11/11 gates at the time this doc was authored. Live baseline as of 2026-05-11 has advanced to 388 passed (iter-0034/0035/0036 added regression tests); the verification commands should always emit a number >= the historical baseline.
