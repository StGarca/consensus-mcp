---
title: consensus pipeline MCP — codex fix-author roadmap results (Tasks #22 → #27)
type: session-results
created: 2026-05-10
updated: 2026-05-11
session_phase: codex-fix-author roadmap completion (iter-0013 → iter-0017)
iterations_landed: 5 (#22 iter-0013, #28 closure invariant, #24 iter-0014, #25 iter-0015, #26 iter-0016, #27 iter-0017 capstone)
status: roadmap chain landed mechanically; real-codex iter-0018 demo + operator commit are the remaining empirical proofs
predecessor_doc: docs/architecture/2026-05-10-mcp-tooling-sweep-results.md
tags: [consensus pipeline, codex-fix-author, roadmap, results]
---

# consensus pipeline MCP — codex fix-author roadmap results

## TL;DR

Following codex's 2026-05-10 v4 directive ("the system standard should be stricter than the writeup; 'other AI verified it' becomes a mechanically enforced close invariant, not a convention"), this push landed the full **codex fix-author roadmap** end-to-end — from prompt-template hardening through capstone integration test.

Operator's directive: *"codex should fix problems it finds. The caveat to that is claude MUST verify correction, identify new problems (if any), if no problems with new code/logic = close issue, if problems found = correct issues, verify correct per claude.md THEN pass to codex for final code review. Repeat as necessary until consensus."*

**The architecture is now mechanically enforceable end-to-end.** Closure invariant has 3 enforcement layers (defense-in-depth). Real-codex iter-0018 demo + operator commit are the remaining empirical proofs.

## v5 ARCHITECTURAL HARDENING APPENDED — iter-0018 (2026-05-10T17:05:06Z)

Codex's 2026-05-10 v5 review of this document caught 5 real defects in the iter-0014→iter-0017 chain. Operator authorized fix; iter-0018 landed all 5 with TDD discipline.

### What v5 review caught

1. **HIGH** — `cross_actor` check used `actor.id` only; codex-A → codex-B with different actor.ids passed. **Real cross-AI requires `model_family` differs**, not just actor identity.
2. **HIGH** — capstone happy-path was codex-applies → codex-different-actor closes (the WEAKER rule). Per operator's directive, the closer must be the OPPOSITE family from the last mutator.
3. **MEDIUM** — `last_mutation_from_audit` only sees `apply_step_landed` events. Manual edits / direct file writes outside `apply.codex_patch` are invisible to the invariant.
4. **MEDIUM** — `patch_proposal` is optional, not required. Operator's "codex should fix problems it finds" directive deserves a strict mode.
5. **LOW/MEDIUM** — T6 fails OPEN if invariant evaluator raises or returns None. Should fail closed when any mutation has occurred.

### iter-0018 fixes (all landed)

| # | Fix | Test names |
|---|---|---|
| 1 | `check_closure_invariant` now requires `closer.actor.model_family != last_mutation.actor.model_family` (renamed `cross_actor` → `cross_family`). Missing model_family on either side → FAIL (undeterminable). | `test_same_model_family_different_actor_ids_fails`, `test_different_model_family_passes`, `test_missing_model_family_on_closer_fails`, `test_missing_model_family_on_last_mutation_fails`, `test_codex_apply_then_codex_post_mutation_review_blocked` |
| 2 | Capstone rewritten: codex applies + **claude** (model_family=claude) post-mutation reviews + closes. NEW negative test `test_capstone_codex_apply_then_codex_post_review_blocked` proves codex-applier + codex-different-actor-id reviewer = T6 refuse. | (in `test_capstone_full_fix_loop.py`) |
| 3 | T6 `audit_append_event` for `iteration_closed` now refuses with `unaudited_mutation_detected` if working tree has changes NOT covered by any `apply_step_landed` event's `files_touched`. Conservative-fail-closed for manual edits. | `test_unaudited_working_tree_changes_block_close`, `test_audited_working_tree_changes_allowed_to_close` |
| 4 | `goal_packet.fix_author_policy` field with `permissive` (default) and `strict`. In strict mode, every finding must have either `patch_proposal` OR `patch_not_proposed_reason` (mutually exclusive). Schema extended; prompt template updated. | `test_strict_mode_finding_with_patch_passes`, `test_strict_mode_finding_with_reason_passes`, `test_strict_mode_finding_missing_both_rejected`, `test_strict_mode_finding_with_both_rejected`, `test_permissive_default_finding_without_patch_passes`, `test_permissive_explicit_finding_without_patch_passes`, `test_strict_mode_only_reason_field_validates_as_string` |
| 5 | T6 `iteration_closed` now refuses with `closure_invariant_evaluation_failed` when invariant evaluator raises OR returns None AND any `apply_step_landed` event exists. No-mutation case still allowed. | `test_t6_fails_closed_when_invariant_evaluator_raises`, `test_t6_fails_closed_when_invariant_evaluator_returns_none`, `test_t6_no_mutation_evaluator_none_still_allowed` |

### iter-0018 state delta

| Metric | Pre-v5 | Post-v5 | Δ |
|---|---:|---:|---:|
| pytest | 229 | **247** | +18 |
| smoke | 59 | **59** | 0 |
| Capstone happy-path closer | codex (different actor.id) | **claude (different family)** | architecturally corrected |
| Cross-AI semantics | actor.id-only (weak) | **model_family-required (strong)** | hardened |
| Mutation completeness | apply_step_landed only | **+ working-tree gap detection** | hardened |
| Patch-author policy | implicit permissive | **explicit permissive/strict modes** | option exposed |
| T6 fail-closed-on-eval-error | NO (failed open) | **YES** | hardened |

### Files modified in iter-0018

- `consensus_mcp/_closure_invariant.py` — `cross_actor` → `cross_family`; missing model_family fails undeterminable
- `consensus_mcp/_dispatch_codex.py` — `patch_not_proposed_reason` validation, `fix_author_policy` strict enforcement
- `consensus_mcp/_self_drive.py` — comment alignment with cross_family
- `consensus_mcp/_validate_closure_invariant.py` — heuristic family derivation + CONSENSUS_MCP_REPO_ROOT env support (was hardcoded to source-tree __file__ walk; broke under installed wheel)
- `consensus_mcp/tools/audit_append_event.py` — `_detect_unaudited_mutation`; T6 refuse-on-unaudited-mutation; fail-closed on evaluator None/raise
- `consensus_mcp/dispatch_templates/codex_review_schema.json` — added `patch_not_proposed_reason`
- `consensus_mcp/dispatch_templates/codex_review_template.md` — documents strict mode
- `consensus_mcp/tests/test_capstone_full_fix_loop.py` — happy-path rewritten claude-closer; codex→codex negative test
- `consensus_mcp/tests/test_iter_0018_cross_ai_invariant.py` — NEW; 17 tests (10+ originally RED before fixes)

Wheel rebuilt + reinstalled (1.11.0 with v5 hardening).

### Honest limits remaining post-v5

- **Mutation-completeness scope is repo-root-wide**, not iteration-scoped. Manual edits to unrelated files (e.g., code) when closing an iteration with `apply_step_landed` events will trigger `unaudited_mutation_detected`. Workaround: commit + clean tree before close, OR emit `apply_step_landed` events for direct edits. Tighter scoping would require operator intent (`goal_packet.mutation_scope_paths`) — out of v5 scope.
- **Strict mode is opt-in**, not default. Archived iter-0009/0010/0011 reviews don't break (default permissive); new goal_packets opt in via `fix_author_policy: strict`.
- **Historical validator uses heuristic family inference** for legacy reviews (reviewer_id prefix or filename). Reviews with non-conforming reviewer_id will report invariant FAIL (correctly flagged as undeterminable).

### v5 verdict (codex's own framing)

> Bottom line: the system is materially better and close to the intended loop, but I would not mark the original concept fully achieved until cross-verification is enforced by AI family/role, not actor ID, and all code mutations are captured as auditable mutation events.

**Both bars now met (mechanically):**
- ✅ Cross-verification enforced by AI **family** (not actor ID) — Finding 1 fix
- ✅ Code mutations captured via auditable events; non-audited mutations refuse close — Finding 3 fix

**The original concept is now fully achieved at the architectural layer.** Remaining empirical work is the same as before v5: real-codex iter-0019 demo + operator commit. Codex's `quorum_close_passed` bar is now mechanically expressible AND the cross-family invariant is mechanically enforced — same architectural strength + a stricter floor.

---

## Live verification evidence (2026-05-10T17:05:06Z)

```
$ REPO_ROOT="C:/Users/steve/Downloads/the source project"

# pytest
$ CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" python_env/python.exe -m pytest consensus_mcp/tests/ -q
247 passed in 4.49s

# smoke (from installed wheel)
$ CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" python_env/python.exe -m consensus_mcp._smoke_test
59/59 tests passed

# release gates
$ CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" python_env/python.exe consensus_mcp/_release_gate_check.py
9/11 gates passed

# historical closure-invariant scan (works from BOTH source and installed wheel post-v5)
$ CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" python_env/python.exe -m consensus_mcp._validate_closure_invariant
{"summary": {"total": 14, "compliant": 0, "non_compliant": 0, "n/a": 10, "in_flight": 4}} # exit 0
```

**The 2 failing release gates** are state-of-tree only (`G_unstaged` + `G_untracked_pkg`) — operator's commit decision. All 9 functional gates green including new `G_archive_section_24_synced`.

## State delta this push

| Metric | Pre-push | Post-push | Δ |
|---|---:|---:|---:|
| pytest | 152 | **229** | +77 |
| smoke | 56 | **59** | +3 |
| MCP tools registered | 12 | **14** | +2 |
| Stop rules codified | 8/8 | **9/9** | +1 (closure_cross_verification_failed) |
| Release gates | 8/10 | **9/11** | +1 gate added (section-24-sync) |
| Architectural invariants | docs only | **3-layer mechanical enforcement** | new |

## What landed (chronologically, by task)

### #22 iter-0013 — codex prompt-template hardening

**Problem (codex 2026-05-10 verdict):** codex emitted narrative text in `blocking_objections` instead of canonical `codex-rev-NNN` IDs across 5 dispatches in iter-0010/0011/0012. Helper validator correctly rejected, but the prompt template wasn't constraining codex enough.

**Fix:** rewrote `dispatch_templates/codex_review_template.md` with explicit STRICT REQUIREMENTS:
- Rule 1: `id` MUST match `^codex-rev-\d+$` (with CORRECT vs INCORRECT examples — narrative text in `id` is INCORRECT)
- Rule 2: `blocking_objections` MUST be a list of finding IDs equal to the set of blocking/critical-severity finding IDs (NOT a list of narrative descriptions)
- Rule 3: `goal_satisfied` coherence with findings
- Rule 4: `goal_satisfied_rationale` required (non-empty)
- Schema reference block at bottom

**Result:** No regressions; pytest unchanged. The empirical proof is whether iter-0014+ codex dispatches produce well-formed output more reliably; that's an operator-callable test (real codex CLI invocation).

### #23 — section-24 auto-sync helper + release gate + smoke probe

**Problem:** Section 24 of the spec md mirrors the archive review-pass index; manual updates required after every closed iteration. Drifted 4 times this session (iter-0009/0010/0011/0012); each drift broke smoke (server refused to boot).

**Fix:** new `consensus_mcp/_sync_section_24.py` CLI helper. Detects drift; `--apply` mode auto-fixes. New release gate `G_archive_section_24_synced` (gate 11). New smoke probe `test_archive_section_24_synced`. Smoke 56 → 57 → eventually 59.

**Result:** Recurring failure class is now self-detecting. Operator runs `--apply` once, gate stays green. Wheel rebuilt + reinstalled.

### #28 — closure-cross-verification-and-freshness invariant (codified FIRST, before #24)

**Problem (codex 2026-05-10 v4 directive):** "Other AI verified it" was convention, not invariant. The system standard must be stricter than the writeup.

**Fix (5 phases):**

#### Phase 1: helper module `consensus_mcp/_closure_invariant.py`

- `bundle_sha(repo_root, files_touched) -> str` — patch target file bundle hash. Sha256 of sorted (path, content-sha) pairs. Reproducible without commits.
- `last_mutation_from_audit(audit_log) -> dict | None` — derives event from `apply_step_landed` audit events. Most-recent. Returns None when no mutation. **Iter-0017 capstone surfaced + fixed an architectural seam:** real `apply_codex_patch` events nest the structured fields under `extra_fields.last_mutation`, so this function now hoists nested fields to top-level for downstream consumers.
- `check_closure_invariant(last_mutation, closing_verdict) -> dict` — 3-part gate: cross_actor + hash_match (PRIMARY) + freshness (SECONDARY).

#### Phase 2: stop rule `closure_cross_verification_failed`

`_self_drive.cmd_check_stop_rules` extended (8/8 → **9/9** full contract coverage). Reads audit log, derives `last_mutation`, finds fresh closer, runs invariant check. Asymmetric with T6 by design (documented in code): operational normal during in-flight iteration; only fires when a fresh review IS present and stale/self.

#### Phase 3: `loop.run_goal` transition guard

New state `blocked_closure_invariant_failed`. `_detect_state_from_files` runs the invariant check BEFORE returning `ready_to_close`; on fail, returns the new state with `_NEXT_ACTION` hint pointing at the closure certificate.

#### Phase 4: T6 `audit_append_event` `iteration_closed` refusal + closure certificate authoring

`audit_append_event.handle()` runs invariant check BEFORE writing `iteration_closed` events. On fail: refuses with `closure_cross_verification_failed` error. On PASS: authors `iteration_dir/closure-certificate.yaml` with structured proof (last_mutation event + closing verdict + 3 invariant_checks PASS/FAIL + overall PASS/FAIL). Single artifact = single proof for operator review.

**Important:** Cert-write failure now logs to stderr (was silent — caught by code reviewer).

#### Phase 5: historical validator `_validate_closure_invariant.py`

CLI helper that scans `consensus-state/active/iteration-*/` retroactively. Live result: 14 iterations, 0 non-compliant, 10 n/a (no mutation), 4 in-flight (pre-#28 mutations without fresh post-#28 reviews). Clean corpus. Exit 0.

#### 9 acceptance tests (codex's minimum bar, all PASS)

1. ✅ Codex patch then Codex close: BLOCKED
2. ✅ Codex patch then Claude approve: ALLOWED
3. ✅ Codex patch, Claude correction, Claude close: BLOCKED
4. ✅ Codex patch, Claude correction, Codex review of post-correction hash: ALLOWED
5. ✅ Codex re-fix then Codex re-review: BLOCKED
6. ✅ Stale Claude review from before Codex re-fix: BLOCKED (freshness)
7. ✅ Hash mismatch (closer reviewed wrong hash): BLOCKED
8. ✅ No mutation yet → close not gated: ALLOWED (trivial pass)
9. ✅ Closure certificate authored at `iteration_dir/closure-certificate.yaml` on PASS

### #24 iter-0014 — codex_review_schema.json `patch_proposal` extension

**Goal:** make codex a fix-author, not just a reviewer.

**Schema:**
```yaml
patch_proposal: # optional per finding
 patch_id: ^patch-[0-9a-f]{12}-[0-9a-f]{12}$ # content-bound
 applies_to_findings: [codex-rev-NNN]
 base_sha: <bundle_sha codex saw>
 unified_diff: <text>
 files_touched: [paths]
 expected_tests: [name] # optional
```

**Validator (`_dispatch_codex._validate_patch_proposal`):**
- patch_id content-bound: must equal `patch-{base_sha[:12]}-{sha256(unified_diff)[:12]}`
- applies_to_findings refs validated against finding IDs in this review
- files_touched in goal_packet.allowed_files; not in forbidden_files
- Anti-self-verification: rejects extra fields (`verified`, `self_verified`, `correct`, `approved`)

**Tests:** 12 new (185 → 197). Backward-compat: codex outputs without patch_proposal still parse normally.

### #25 iter-0015 — `loop.verify_codex_patch` + state machine extension

**Goal:** claude verifies codex-emitted patches per CLAUDE.md.

**Tool design (two modes):**
- `mode=build_inputs`: reads codex-review.yaml + patch_proposal + goal_packet + acceptance_gates + touched files (FULL contents) + CLAUDE.md → returns reproducibility-bounded verifier_inputs bundle + `review_scope_hash`. Excludes codex's reasoning trail (codex finding TEXT yes; reasoning narrative no).
- `mode=record_verdict`: takes subagent's structured verdict → writes `iteration_dir/codex-patch-verifications/<patch_id>.yaml`. Validates patch_id format (defense-in-depth path-traversal protection).

**State machine extension** (`loop.run_goal`):
- `codex_patch_proposed` (codex-review has patch_proposal, no verification yet)
- `patch_verified_ready_for_codex_resubmit` (verdict=approved; awaits codex post-correction re-review)
- `patch_corrected_by_claude_ready_for_codex_resubmit` (verdict=corrected_resubmit; MUST route through codex re-review)

**Forbidden transition:** `claude_verifying_patch.corrected_resubmit → ready_to_close` is BLOCKED. Tested.

**Tests:** 23 new (197 → 220 → 208 mid-push). Smoke 57 → 58.

### #26 iter-0016 — `apply.codex_patch` staged-apply with operator authorization

**Goal:** apply verified codex patches with explicit operator gate.

**Authorization (refuse-by-default):** requires BOTH `goal_packet.authorization.codex_patch_apply_authorized=true` AND env `CONSENSUS_MCP_CODEX_PATCH_APPLY=1`. Either missing → refuse.

**Refusal classes:**
- Authorization missing
- Claude verification missing or not-approved
- `base_sha` empty or mismatched (drift detection — defense-in-depth fixed during review: empty base_sha now refuses with `base_sha_missing`)
- Patch staging or dry-run failed

**On success:** stages + applies + computes post_sha (bundle_sha post-apply) + emits `apply_step_landed` audit event with structured `last_mutation` event object (actor, patch_id, files_touched, base_sha, post_sha, unified_diff_sha256, timestamp).

**Tests:** 11 new (208 → 219). Smoke 58 → 59.

### #27 iter-0017 — capstone full-fix-loop integration test

**Goal:** prove the full cycle works end-to-end.

**14-step integration test** in `tests/test_capstone_full_fix_loop.py`:

1. Scaffold iter-0017-capstone with goal_packet (authorized) + target source file with off-by-one defect
2. Synthesize codex output JSON with valid patch_proposal (content-bound patch_id)
3. Parse via real `_dispatch_codex._parse_codex_output` → exercises iter-0014 schema validator
4. Author fake codex-review.yaml with parsed payload
5. Call `loop.run_goal` → state=`codex_patch_proposed`
6. Call `loop.verify_codex_patch` build_inputs → returns verifier_inputs + review_scope_hash
7. Synthesize claude verifier verdict (approved)
8. Call `loop.verify_codex_patch` record_verdict → writes verification yaml
9. Set authorization env var
10. Call `apply.codex_patch` → applies; on-disk file mutated
11. Verify audit log has `apply_step_landed` event with full nested last_mutation
12. Synthesize codex post-correction review (different actor.id, post-mutation timestamp)
13. Call `audit.append_event iteration_closed` with structured closing_verdict
14. Assert `closure-certificate.yaml` authored with all 3 invariant_checks=PASS, overall=PASS

**Forbidden-transition test:** verdict=corrected_resubmit → apply refuses; same-actor closer → T6 refuses with `closure_cross_verification_failed`.

**Capstone surfaced one real architectural seam** (apply_codex_patch's nested last_mutation fields vs reader's top-level expectation). Fixed during the session.

**Tests:** 2 new (227 → 229). All green.

### #18 umbrella — closed when #27 landed

The codex fix-author umbrella task closes automatically: each component (#22, #28, #24, #25, #26, #27) landed; the architectural directive is mechanically enforceable; the capstone proves the full chain.

## What "mechanically enforceable" means in practice

For any close attempt to land cleanly, the system enforces (defense-in-depth, 3 layers):

| Layer | Enforces | What it catches |
|---|---|---|
| `_self_drive.cmd_check_stop_rules` | 9 contract stop rules including `closure_cross_verification_failed` | stale or self-closing reviews |
| `loop.run_goal._detect_state_from_files` | refuses `ready_to_close` if invariant fails | supervisor-driven flow |
| T6 `audit_append_event iteration_closed` | refuses to record close + skips cert authoring | direct audit-event bypass attempts |

**Plus**: `apply.codex_patch` won't apply without claude approval; codex's patch schema rejects self-verification claims; closure certificate is the operator's single proof artifact.

## Architectural diagram (the cycle as it now works)

```
operator authorizes goal_packet
 ↓
codex finds defects + (optionally) emits patch_proposal
 ↓
claude verifies patch per CLAUDE.md (loop.verify_codex_patch)
 ├─ approved → continue
 └─ corrected_resubmit → claude corrects → re-submit to codex
 ↓
 codex re-reviews
 ↓
apply.codex_patch (refuse-by-default; both auth conditions required)
 ↓
emits apply_step_landed audit event with structured last_mutation
 ↓
codex post-correction review (must match post-mutation hash, fresh timestamp)
 ↓
3-layer closure invariant check
 ├─ stop rule
 ├─ supervisor transition guard
 └─ T6 iteration_closed gate
 ↓
closure-certificate.yaml authored at iter_dir
 ↓
quorum_close_passed (only if 3 invariant_checks all PASS)
```

## Per-iteration artifacts in this push

| Iteration | dir | iteration-outcome | closing_state |
|---|---|---|---|
| iter-0017 (synthetic capstone) | (created in tmp_path during test) | tested via integration | n/a (test-only) |
| (no real iter-0018 yet) | — | — | — |

The capstone is **test-only** in this push — it proves architecture, not production codex output reliability. Real iter-0018 with codex CLI is operator-callable.

## Code-quality findings addressed during reviews

3 important findings caught + fixed during code-quality review of #26:
1. T5 reuse claim mismatch (docstring corrected to reflect bespoke apply step)
2. Stale `56/56` in 2 docstrings of `_release_gate_check.py` (updated to `59/59`)
3. base_sha empty falls open (now refuses with `base_sha_missing`)

1 architectural seam caught + fixed during #27 capstone:
- `last_mutation_from_audit` now hoists nested `extra_fields.last_mutation` fields to top level for downstream consumers

## Open questions (not yet empirically proven)

| Question | Status |
|---|---|
| Does codex actually emit valid `patch_proposal` blocks in production? | Unknown — depends on iter-0013 prompt template + iter-0014 schema effectiveness. Real-codex iter-0018 demo would prove. |
| Does the unified-diff applier in `apply.codex_patch` handle all real-world diff shapes? | Tests cover single-file fast-path; multi-file unified-diff path is untested (iter-0016 minor finding) |
| Does claude verifier produce reliable verdicts under reproducibility-bounded inputs? | Unit-tested; full-cycle real-claude verifier on a real codex patch hasn't been run |

## Suggested next steps (priority order)

### 1. (HIGH) Operator commit + iter-0018 real-codex demo

The 2 release-gate failures (`G_unstaged` + `G_untracked_pkg`) are operator commit decisions. After commit:

```bash
# iter-0018 real-codex test (no operator-time required; ~2 min wall time)
mkdir -p consensus-state/active/iteration-0018-real-codex-fix-demo
# (author goal_packet + review-packet for a small known defect, then:)
CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" python_env/python.exe -m consensus_mcp._dispatch_codex \
 --goal-packet consensus-state/active/iteration-0018-real-codex-fix-demo/goal_packet.yaml \
 --iteration-dir consensus-state/active/iteration-0018-real-codex-fix-demo \
 --reviewer-id codex-iter0018-1 \
 --pass-id codex-iter0018-1-pass1 \
 --review-target consensus-state/active/iteration-0018-real-codex-fix-demo/review-packet.yaml
```

Whether codex produces a valid `patch_proposal` is the empirical question. If yes: drive through claude verify → apply (with auth) → re-review → close. If no: codex prompt template needs another hardening round.

### 2. (MEDIUM) Multi-file unified-diff applier hardening

`apply.codex_patch._apply_unified_diff` (lines 347-471) lacks integration tests. Real codex patches will likely span multiple files. Tests should:
- Multi-file diff
- Diff with hunks (multiple change zones in one file)
- Patch with binary file mention (refuse cleanly)
- Patch with rename (decide: support or refuse)

### 3. (MEDIUM) Claude verifier identity check at apply time

iter-0015 minor #5: `apply.codex_patch` checks `verdict==approved` but not the `verifier` field of the verification yaml. A codex-authored verification under `codex-patch-verifications/` would currently pass. Filesystem path is the de-facto enforcement; explicit assertion would be defense-in-depth.

### 4. (LOW) `actor` shape re-validation in apply.codex_patch.handle

iter-0016 minor #7: `actor.{id, model_family, role, pass_id}` structure is enforced at MCP schema layer but not re-validated when handle() is called directly via Python import. Defense-in-depth.

### 5. (LOW) Multi-writer race in T6 closure-certificate authoring

Cert authoring is part of the same handle() call as the audit event append, which is already documented as single-writer-per-iteration. Worth a docstring note that cert authoring inherits the same constraint.

### 6. (LOW) Section-24 auto-update at T6 archive seal

Currently the section-24 sync is operator-triggered (`--apply`). Could be automated as part of T6 archive flow itself: when `audit_append_event` writes a `review_returned_and_sealed` event with archive_sealed_path, automatically append the section-24 entry. Eliminates the manual step entirely.

## Files inventory

### NEW files (untracked at session close)

| Path | LOC | Purpose |
|---|---:|---|
| `consensus_mcp/_closure_invariant.py` | 180 | Phase 1: bundle_sha + last_mutation_from_audit + check_closure_invariant |
| `consensus_mcp/_sync_section_24.py` | 270 | Section-24 auto-sync helper (#23) |
| `consensus_mcp/_validate_closure_invariant.py` | ~140 | Phase 5: historical validator |
| `consensus_mcp/tools/loop_verify_codex_patch.py` | ~370 | iter-0015 claude verifier MCP tool (#25) |
| `consensus_mcp/tools/apply_codex_patch.py` | ~470 | iter-0016 staged-apply MCP tool (#26) |
| `consensus_mcp/tests/test_closure_invariant.py` | ~480 | 21 tests for Phase 1-4 |
| `consensus_mcp/tests/test_validate_closure_invariant.py` | ~150 | 8 tests for historical validator |
| `consensus_mcp/tests/test_loop_verify_codex_patch.py` | ~470 | 19 tests for #25 |
| `consensus_mcp/tests/test_apply_codex_patch.py` | ~430 | 11 tests for #26 |
| `consensus_mcp/tests/test_capstone_full_fix_loop.py` | ~330 | 2 tests for #27 capstone |

### MODIFIED files (uncommitted at session close)

| Path | Change | Tasks |
|---|---|---|
| `consensus_mcp/_self_drive.py` | +closure_cross_verification_failed stop rule (8/8 → 9/9) + asymmetry comment | #28 |
| `consensus_mcp/_dispatch_codex.py` | +patch_proposal validator (~+170 LOC) | #24 |
| `consensus_mcp/_release_gate_check.py` | +G_archive_section_24_synced gate; smoke counts 56→59; G_pytest_dispatch_codex 52→64 | #23, #26, #27 |
| `consensus_mcp/_smoke_test.py` | +3 probes (section-24, loop_verify, apply_codex_patch); count 56→59 | #23, #25, #26 |
| `consensus_mcp/server.py` | +2 tool registrations (loop.verify_codex_patch, apply.codex_patch) | #25, #26 |
| `consensus_mcp/tools/loop_run_goal.py` | +blocked_closure_invariant_failed state + 3 patch-verification states + asymmetry comment | #28, #25 |
| `consensus_mcp/tools/audit_append_event.py` | +T6 closure-invariant gate + closure-certificate authoring + stderr log on cert failure | #28 |
| `consensus_mcp/dispatch_templates/codex_review_schema.json` | +patch_proposal block schema | #24 |
| `consensus_mcp/dispatch_templates/codex_review_template.md` | +Output format STRICT + Optional patch_proposal section | #22, #24 |
| `docs/architecture/orchestration-spec.md` | section 24 entries for iter-0010/0011/0012 codex passes; status_counts.archived 25→29 | #23 |
| `consensus-state/active/iteration-0011-fault-recovery-demo/iteration-outcome.yaml` | +status_caveat + fault_recovery_demo_status block | iter-0012 fix |

### Architectural memory (persists across sessions)

- `memory/project_codex_fix_author_directive.md` — full v4 directive with all hardening details
- `MEMORY.md` — index updated to reference the directive

## Reproduction commands

```bash
cd C:\Users\steve\Downloads\the source project

# Use Windows-form absolute path (NOT $(pwd) under Git Bash)
REPO_ROOT="C:/Users/steve/Downloads/the source project"

# 1. pytest
CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" python_env\python.exe -m pytest consensus_mcp/tests/ -q
# Expected: 229 passed

# 2. smoke (from installed wheel)
CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" python_env\python.exe -m consensus_mcp._smoke_test
# Expected: 59/59 tests passed

# 3. release gates
CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" python_env\python.exe consensus_mcp/_release_gate_check.py
# Expected: 9/11 gates passed (only G_unstaged + G_untracked_pkg fail; pre-commit)

# 4. historical closure-invariant scan
CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" PYTHONPATH=scripts python_env\python.exe -m consensus_mcp._validate_closure_invariant
# Expected: exit 0; 0 non_compliant

# 5. section-24 sync detector
CONSENSUS_MCP_REPO_ROOT="$REPO_ROOT" PYTHONPATH=scripts python_env\python.exe -m consensus_mcp._sync_section_24
# Expected: synced=true

# 6. verify the 14 registered tools
python_env\python.exe -c "import sys; sys.path.insert(0, 'scripts'); from consensus_mcp import server; print(sorted(t['name'] for t in server.registry.list_tools()))"
# Expected: ['apply.codex_patch', 'audit.append_event', ..., 'loop.run_goal', 'loop.verify_codex_patch', 'reviewer.dispatch_codex', ...]
```

## Honest limitations

This push proves the **architecture** works mechanically end-to-end. It does NOT prove:

1. **Real codex CLI reliably emits well-formed `patch_proposal` blocks.** The prompt template was hardened (#22) but empirical proof requires real-codex iter-0018 demo.
2. **Real-world unified-diff patches apply cleanly across multi-file scope.** Single-file fast-path is tested; the unified-diff applier isn't integration-tested on multi-file patches.
3. **The full claude-verifier-via-subagent loop produces correct verdicts in production.** Build_inputs/record_verdict mechanics are unit-tested with synthetic verdicts; real-claude verifier on a real codex patch hasn't been exercised.

These are NOT architectural gaps — they're empirical questions that need real-codex/real-claude runs. The mechanical scaffolding is now in place to actually exercise them.

## Bottom line

Pre-push: codex's expert verdict said "the system standard should be stricter than the writeup."

Post-push: the system standard IS stricter than the writeup. Closure invariant has 3-layer mechanical enforcement. The full codex-finds-fixes-its-own-defects cycle is mechanically proven via integration test. Codex's expert bar (codex finds → claude verifies → codex re-reviews → close on both goal_satisfied=true) is **mechanically expressible** in this architecture; whether codex's actual output reliability lets us run it in production is the remaining empirical question.

The original loop goal is now buildable. Operator commit + iter-0018 real-codex demo is the next step.
