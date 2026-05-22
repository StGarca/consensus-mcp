---
title: Phase 1 MCP G1+G2 implementation + iteration-0006 completion summary
date: 2026-05-09
type: session-summary
status: active
note_on_provenance: "Authored inside a prior project before the consensus_mcp package was extracted. Retains references to that project's domain. Preserved as-is for fidelity to the original decision record."
reader_guidance: |
  This is a session-summary document from the implementation arc that
  produced the Phase 1 (G1+G2) tool surface in consensus-mcp. The
  TECHNICAL CONTENT (T2-T11 tool descriptions, smoke test counts, review-
  round findings) is what you want if you're tracing how a specific tool
  came to be. The DOMAIN PROSE (operator-pivot rationale, render-related
  scope discussions, project-specific milestone framing) references the
  upstream project; it's preserved for fidelity but isn't required
  reading to use consensus-mcp.
post_review_fixes_landed: 2026-05-09  # see Section 9 below + 2026-05-09-v1.9.x-third-party-review-findings.md: 38 review findings recorded across 6 rounds. Round 1+2 CLOSED after operator-supplied reviewer verification reruns; Source-MD + Codex follow-up + MD-set follow-up + Round 6 (self-dispatched on v1.9.2) all self-applied, independent re-review pending. Total counts in this comment update incrementally as review rounds land.
scope_disclaimer: |
  This summary documents the Phase 1 G1+G2 implementation arc (T2-T7 + iteration-0006 + v1.9.0/v1.9.1).
  Phase 2 G3+G4+G5 (T8-T11; v1.9.2; +10 smoke tests; smoke 47/47 at v1.9.2 land; current 51/51 post-Round-6 hardening) is recorded in:
    - parent spec revision_history v1.9.2 entry
    - consensus-state/state/disposition-ledger.yaml v1_9_2_application_log block
    - phase-1 design spec implementation_log block (g3/g4/g5_implemented_at fields)
    - 2026-05-09-v1.9.x-third-party-review-findings.md (Round 6: self-dispatched review on v1.9.2)
  Body text below SECTION 9 reflects the v1.9.0/v1.9.1 snapshot (smoke 35/35 -> 37/37; 6 tools).
  For current state, refer to the v1.9.2 sources listed above. (CFU/Round 6 staleness pattern
  acknowledged: this scope_disclaimer is the v1.9.2 supersession marker per Round 6 F4 fix.)
related_specs:
  - docs/architecture/orchestration-spec.md (v1.9.2 active)
  - docs/architecture/phase-1-completion.md (g1-through-g5-implemented status; implementation_log updated for v1.9.2)
  - docs/architecture/2026-05-09-v1.9.x-third-party-review-findings.md (consolidated 38-finding record across 6 rounds)
related_iterations:
  - consensus-state/active/iteration-0006/
related_archive:
  - consensus-state/archive/review-passes/2026-05-09-iteration-0006-consolidated_codex_plus_claude-pass.yaml (pass-22)
audience: ai-only-plus-operator-review
---

# Phase 1 MCP G1+G2 implementation + iteration-0006 completion summary

## 1. Progress

This session covered three discrete arcs:

### Arc A — Phase 1 MCP G1+G2 toolchain implementation (T1-T8)

Implemented the 6-tool MCP server stack via `subagent-driven-development` skill, dispatching fresh implementer subagents per task with two-stage review (spec compliance + code quality) between each.

| Task | Tool / scope | Outcome |
|---|---|---|
| T1 | MCP server skeleton (`consensus_mcp/server.py`, hand-rolled stdio JSON-RPC + `tool_registry.py`) | Done |
| T2 | `state.read_decision_ledger` (read-only ledger query with mtime cache) | Done |
| T3 | `audit.append_event` (canonical event-type vocabulary; atomic append) | Done |
| T4 | `patch.stage_and_dry_run` (canonical-006 dry-run gate) | Done |
| T5 | `patch.apply_consensus_patch` (atomic apply gated on T4) | Done |
| T6 | `review.write_and_seal` (sealed-review provenance with self-hash exception) | Done |
| T7 | `review.read_post_seal` (verifies seals; surfaces legacy unsealed packets) | Done |
| T8 | Close T5/T7 bounded gaps (mid-write IO failure, schema drift, lazy imports, BLOCKED-with-high-findings test) | Done |

Smoke coverage: **35/35** in `consensus_mcp/_smoke_test.py` (was 34/34 pre-iteration-0006; iteration-0006 added `test_patch_stage_and_dry_run_new_file_creation`).

### Arc B — iteration-0006 real-iteration smoke (T11)

Routed an actual consensus-loop iteration end-to-end through the MCP toolchain to prove G1+G2 work on real workload.

| Sub-task | Outcome |
|---|---|
| T11.1 | Built `consensus-state/active/iteration-0006/` with input.yaml + review-packet.yaml; T3 recorded sealed_inputs_recorded |
| T11.2 | Dispatched dual reviewers (codex implementation_realist + claude methodology_critic, model=opus, parallel) |
| T11.3 | T6 sealed consolidated review-pass to `consensus-state/archive/review-passes/2026-05-09-iteration-0006-consolidated_codex_plus_claude-pass.yaml`; T7 verified=True |
| T11.4 | Synthesized consensus.yaml + finalized independence-audit.yaml |
| T11.5 | T4+T5 applied iteration-outcome.yaml + verification.yaml (canonical-006 gate APPROVED with 0 high findings; apply_step_landed audit recorded) |
| T11.6 | Out-of-MCP direct edits: parent spec md frontmatter v1.8.2 -> v1.9.0 + revision_history v1.9.0 entry; sibling Phase 1 design spec status flip; ledger spec_version + v1_9_0_application_log + iteration_0006_dispositions |
| T11.7 | All 4 validators clean on iter-0006 (validate_iteration, validate_consensus, validate_review × 2); 35/35 + 21/21 + 0 disposition findings; iteration_closed audit event recorded |

Reviewer outcome: **unanimous bump_approved=true**, 0 blocking objections, 13 non-blocking suggestions, 1 methodological disagreement (phase vocabulary; codex preserved-ENDED vs claude phase-distinction) resolved by synthesizer preserving both readings.

### Arc C — iteration-0006 self-test discoveries (mid-iteration fixes)

The iteration revealed THREE real toolchain bugs that pre-iteration smoke did not cover:

| Defect | Root cause | Fix |
|---|---|---|
| **canonical-iter0006-009** — T4 rejected new-file patches | T4 `patch_stage_and_dry_run.py` line 207 raised `"file not found"` when `real_path` didn't exist, even when `old_string=""` (new-file creation case) | Added new-file branch; smoke test added (count 34 -> 35) |
| (uncatalogued) — T4 staged_iter_dir name was `"iteration-staged"` | Validators' ITERATION_ID_MISMATCH fired on every file because dir name didn't equal the iteration_id field | Changed to `staging_path / iteration_id` |
| (uncatalogued) — T3 wrote `event_type:` field key | Legacy `validate_iteration` reads `entry.get("event")`; T3 diverged silently when first authored | Changed T3 to write `event:`; updated 2 smoke test assertions |

Plus one **canonical-iter0006-008** finding noted-not-blocking: codex reviewer used self-hash-exception form for `reviewed_packet_sha256` cross-artifact reference; spec section 7 says canonical-full for cross-artifact. Root cause: orchestrator-dispatch prompt (T11.2 codex brief) mislabeled the packet field value as canonical-full. Codex review content unaffected; flag for orchestrator-prompt review at next iteration boot.

## 2. Changes (file-by-file)

### New files

- `consensus_mcp/server.py` — stdio JSON-RPC MCP server skeleton; boot validator check (refuses to start if `validate_disposition_index` reports findings); audit log JSONL appends to `consensus-state/state/mcp-server-audit.yaml`
- `consensus_mcp/tool_registry.py` — `class ToolRegistry` with `register(name, schema, handler)`, `list_tools()`, `get_handler(name)`
- `consensus_mcp/tools/state_read_decision_ledger.py` — T2; mtime-based cache; canonical_yaml_sha256 formula
- `consensus_mcp/tools/audit_append_event.py` — T3; 12 canonical event types; first-class kwargs for event-specific fields; atomic append
- `consensus_mcp/tools/patch_stage_and_dry_run.py` — T4; tempfile-based staging; default validator set (4); supports new-file creation; `dry_run_isolation_caveats` documents bounded-isolation gap
- `consensus_mcp/tools/patch_apply_consensus_patch.py` — T5; gated on T4; atomic per-file write; partial_apply_failed branch + structured error returns; CONCURRENCY single-writer disclaimer; path-traversal guard via `.resolve() + relative_to()`
- `consensus_mcp/tools/review_write_and_seal.py` — T6; canonical_yaml_sha256 with self-hash exception; deterministic path; index update; refuses path collision
- `consensus_mcp/tools/review_read_post_seal.py` — T7; recomputes hash; surfaces `legacy_unsealed=True` for pre-T6 packets; path-safety check
- `consensus_mcp/_smoke_test.py` — 35 tests covering T1-T7 unit behavior + the 3 iteration-0006 fixes
- `consensus-state/active/iteration-0006/` — 8 ceremonial files (input, review-packet, codex-review, claude-review, independence-audit, consensus, verification, iteration-outcome)
- `consensus-state/archive/review-passes/2026-05-09-iteration-0006-consolidated_codex_plus_claude-pass.yaml` — sealed pass-22; packet_sha256 `fe2180352387a81765767e042103a6e60b1f3a478030a5d2791abc83cf287cef`
- `docs/architecture/phase-1-completion.md` — this file

### Modified files

- `docs/architecture/orchestration-spec.md`:
  - frontmatter: status `draft-v1.8.2-ai-contract` -> `draft-v1.9.0-ai-contract`; contract_version `v1.8.2` -> `v1.9.0`
  - frontmatter: + `v1_8_2_trigger_gate_overridden_by_operator: true` + override_rationale string
  - frontmatter: + `phase_1_mcp_g1_g2_implementation_recorded_at: v1.9.0`
  - revision_history: v1.8.2 status active -> superseded; v1.9.0 entry added (substantive: ~80 lines covering implementation evidence, bounded gaps, trigger-gate override, phase vocabulary distinction, last-process-polishing claim, what does NOT happen)
  - section 24 archived block: pass-22 entry added; status_counts.archived 21 -> 22
- `docs/architecture/phase-1-completion.md`:
  - frontmatter: status `shelved-pending-operational-signal-per-operator-2026-05-08` -> `g1-g2-implemented-2026-05-09-rest-still-shelved`
  - frontmatter: contract_version `v0.1` -> `v0.2-implementation-record`
  - + `implementation_log` block recording 6 tools, 35/35 smoke, bounded gaps, G3-G5 still shelved
  - + `operator_authorization_for_implementation` rationale string
- `consensus-state/state/disposition-ledger.yaml`:
  - `spec_version` `v1.8.2` -> `v1.9.0`
  - + `v1_9_0_application_log` block (mirror v1.8.0 shape; reviewer evidence + files modified + code modified during iteration self-test + bookkeeping-not-governance milestone framing)
  - + `iteration_0006_dispositions` block (9 canonical_findings, all dispositioned)

### Smoke + validator state

- `consensus_mcp/_smoke_test.py`: **35/35**
- `consensus_mcp/validators/run_validator_tests.py`: **21/21**
- `consensus_mcp/validators/validate_disposition_index.py`: **0 findings**
- `consensus_mcp/validators/validate_iteration.py` on iter-0006: **0 findings**
- `consensus_mcp/validators/validate_consensus.py` on iter-0006: **0 findings**
- `consensus_mcp/validators/validate_review.py` on codex-review + claude-review: **0 findings**

## 3. Current status

### Toolchain (Phase 1 MCP G1+G2)

**100% complete in-place.** 6 tools registered + tested + exercised on real workload. Bounded gaps documented but not yet smoke-covered:

- **gap_1**: T5 partial_apply_failed branch — documented at module-docstring level; NO smoke coverage of mid-write IO failure path
- **gap_2**: T5 single-writer concurrency assumed — documented prominently; NO smoke coverage of concurrent invocation (operator deferred filelock work to Phase 1.x)
- **gap_3**: T7 surfaces 21 legacy pre-T6 packets via `legacy_unsealed=True` — documented + smoke-covered

### Standalone extraction readiness

**Not ready.** Two pieces of work remain before the toolchain can be copied out as a standalone tool:

- **Decouple from host-project paths** (deferred): `REPO_ROOT` discovery is hardcoded `__file__.parent.parent.parent.parent` (4 levels up); `consensus-state/active|state|archive` paths are baked in. Operator deferred this until tool is otherwise complete.
- **Package as installable** (deferred): no `pyproject.toml` yet; no `console_scripts` entry points; bundle of validators (`consensus_mcp/validators/`) + MCP server (`consensus_mcp/`) is operator-stated scope.

### Spec / governance

**v1.9.1 active** (originally landed as v1.9.0 then hardened to v1.9.1 after the third-party review pass — see Section 9 + the consolidated findings doc). `active_contract_readiness: phase_0_validated` UNCHANGED from v1.8.0. `do_not_treat_as_phase_0_ready: false` UNCHANGED. `next_required_action: redirect_to_render_outcomes` UNCHANGED. iteration-0006 closing_state: `implementation_ready_apply_landed`. self-construction phase remains CLOSED at v1.8.2 (architectural-scope reading per codex); Phase 1 MCP maintenance phase OPENED at v1.9.0 by operator strategic pivot (vocabulary distinction per claude); v1.9.1 added the post-review hardening (35/35 -> 37/37 smoke + design spec invariants honesty pass). Both readings preserved.

### Render outcomes

**Untouched this session.** Per the operator-locked `next_required_action` + canonical-iter0006-005 + codex-iter0006-007 lock-redirect-at-closure: this is the immediate next operator-directed work.

## 4. Pushback

### From the operator (across the session)

| Pushback | Context | Resolution |
|---|---|---|
| "T8 (3-iteration synthetic replay) is process-polish for a toolchain whose unit tests already prove correctness." | After dispatching T8 implementer with synthetic 3-iteration replay test plan | Operator interrupted; deleted T8 task; agreed real validation comes from real iteration not synthetic replay |
| "Lets remove the decouple step. We'll do that after tool is complete." | After scoping decouple+package as part of "100% complete" | Deferred T9 (decouple) |
| "Lets remove t10 until completion" | After landing T8 (close bounded gaps) | Deferred T10 (package) |
| "Don't keep polishing process unless it directly improves render outcomes" | Repeated multiple times across the multi-session arc | Carried as governance principle in v1.8.2 + v1.9.0 entries; claude-iter0006-006 recommends promoting to operator memory |

### From claude reviewer (methodology_critic)

The review surfaced 6 methodological red flags. All recorded in `consensus-state/active/iteration-0006/claude-review.yaml`:

1. **Override-as-narrative-prose pattern**: input.yaml proposed recording the v1.8.2 trigger override only as revision_history text. Same pattern produced iter-0005 stale-closure-text bug class. Resolution: structural frontmatter field (canonical-iter0006-001 landed in v1.9.0).
2. **Self-construction-ended inconsistency**: v1.8.2 declared closure that v1.9.0 immediately reopens (under any reasonable scoping). Resolution: phase vocabulary distinction in v1.9.0 entry (canonical-iter0006-002).
3. **Bounded gaps without smoke coverage**: gap_1 + gap_2 are documented behaviors NOT exercised by any of the 35 smoke tests. The 35/35 figure validates happy-path, NOT bounded-gap branches. Resolution: v1.9.0 entry explicitly notes "documentation-only verified, no smoke coverage" (canonical-iter0006-006); carry as Phase 1.x test-coverage debt.
4. **Operator-pivot-precedent risk**: iteration-0006 establishes that an operator can authorize work whose strict-reading trigger conditions did not fire. Resolution: v1.9.0 entry explicitly bounds the precedent ("future Phase 1.x work that lacks render-friction trigger ALSO requires operator strategic pivot, NOT auto-permission inherited from v1.9.0").
5. **No-polishing guidance lives only in spec, not in operator memory**: if a future spec edit weakens the guidance, no durable counter-record exists. Resolution: deferred to operator memory promotion (claude-iter0006-006).
6. **34/34 + 21/21 not pinned to test-list SHA**: counts are point-in-time; could drift undetected. Resolution: v1.9.0 entry notes the 35/35 figure is pinned to the smoke-test file at iteration-0006 close (partial; SHA not actually recorded in revision_history; carried as debt).

### From codex reviewer (implementation_realist)

7 non-blocking suggestions, all accepted. Most landed in v1.9.0 directly:

1. `phase_1_mcp_g1_g2_implementation_recorded_at` frontmatter field — landed
2. `v1_8_2_trigger_gate_overridden_by_operator` field (converged with claude-001) — landed
3. Ledger `v1_9_0_application_log` block shape — landed
4. Phase 1 design spec status flip — landed
5. Ledger spec_version bump — landed
6. validator-report SHA drift on metadata-only re-run is expected, not a finding — noted
7. Lock next_iteration_recommendations to render-outcomes-first at iteration-0006 closure — landed

### From iteration-0006 self-test (real-workload pushback against the toolchain itself)

The iteration's apply path surfaced 3 toolchain bugs that the unit-test smoke didn't catch:

- T4 didn't support new-file creation (`old_string=""` for non-existent target → "file not found" error). Real-workload requirement; smoke didn't cover.
- T4 staged dir name was hardcoded `"iteration-staged"` causing ITERATION_ID_MISMATCH on every staged file. Real-workload requirement; smoke didn't cover.
- T3 wrote `event_type:` field but `validate_iteration` reads `event:` field. Cross-tool schema drift; smoke didn't cover.

All 3 fixed during iteration-0006 itself. The iteration is the proof that real-iteration testing catches what unit-smoke misses.

## 5. Open questions

### Q1 — Is iteration-0006 itself the kind of process-polish iteration the operator has been pushing back against?

claude-iter0006-005 flagged that iteration-0006's input.yaml is ~290 lines for a bookkeeping bump. The full iteration ran 8 ceremonial files + dual-reviewer dispatch + sealing + apply + spec md + design spec + ledger updates. That is substantial process work.

Mitigations:
- v1.9.0 entry explicitly names this the LAST process-polishing iteration before render redirect (claude-iter0006-003)
- iteration-0006 next_iteration_recommendations locks render-outcomes-first (codex-iter0006-007)
- v1.9.0 frontmatter `next_required_action: redirect_to_render_outcomes` UNCHANGED

But: the precedent question remains. If render work surfaces friction that triggers G3 implementation, will THAT iteration's apply ceremony be similarly heavy? The toolchain self-tested itself by being used; future render-affecting changes don't need that ceremony unless they touch consensus pipeline infrastructure.

**Open**: should the operator confirm before any future "consensus pipeline iteration" runs, or treat this iteration as closing the door on routine governance polish?

### Q2 — Should canonical-iter0006-008 (codex sha-form drift) get a pre-emptive orchestrator-prompt fix?

Root cause: T11.2 codex dispatch prompt mislabeled the self-hash-exception form as "canonical-full." Codex followed the prompt literally; claude correctly used canonical-full per spec section 7. Different reviewers, different sha values for the same artifact reference.

The drift will recur in any future iteration that uses similar dispatch prompts. The fix is one-line: update the orchestrator's reviewer-dispatch template to disambiguate "form: canonical-full" vs "form: self-hash-exception" + cite spec section 7.

**Open**: do this pre-emptively now, or wait until next iteration boot when the prompt would be regenerated?

### Q3 — Bounded gaps gap_1 + gap_2: smoke coverage ever, or accepted as documentation-only?

Both gaps document behaviors that are NOT exercised by the 35 smoke tests:
- gap_1 (T5 mid-write IO failure → partial_apply_failed): would need a test that simulates IO error mid-loop (e.g., monkeypatch `os.replace` to raise on the 3rd file)
- gap_2 (T5 single-writer concurrency): would need a multi-process test plus an actual filelock implementation (currently no filelock exists)

claude-iter0006-004 carried both as Phase 1.x test-coverage debt. But Phase 1.x is not scheduled; G3-G5 are shelved.

**Open**: write smoke tests for gap_1 (cheap, no new code; just monkeypatch + assert) before extracting? OR accept "documentation-only verified" as the v1.9.0 honest state and let the gaps surface if they bite real workload?

### Q4 — Standalone extraction: when does it happen, and does decouple come before or after package?

Per operator: "we will then copy it out of the host project so it can be a standalone tool" — operator-manual move. Two preceding tasks were deferred:
- T9 decouple from host-project paths (configurable REPO_ROOT + artifact dirs)
- T10 package as installable (pyproject.toml, console_scripts, bundle agent_loop validators + consensus_mcp server)

Both still pending. T9 logically precedes T10 (decoupling makes the package layout clean).

**Open**: trigger condition for T9+T10? Operator preference was "after tool is complete" — iteration-0006 closes that bar but the bounded-gap test coverage debt and Q3 above are also extraction-readiness questions.

### Q5 — Memory promotion (claude-iter0006-006): operator-only step

claude-iter0006-006 recommended promoting "do not keep polishing process unless it directly improves render outcomes" to durable operator memory at `~/.claude/projects/.../memory/feedback_no_process_polishing.md`. This is operator-scope action; flagged in iteration-0006/iteration-outcome.yaml.operator_actions_recommended.

**Open**: operator may choose to do this manually; agent does NOT auto-promote operator guidance to memory.

### Q6 — Was the T11 framing (route a real iteration through MCP) the right test?

Pro: surfaced 3 real bugs that unit-smoke missed. Real-workload integration is the correct validation.

Con: the iteration itself was substantial process work and required substantial mid-iteration toolchain fixes. Each fix is small; aggregate is not. Karpathy "Surgical Changes" is preserved (every fix traced to a specific finding) but the work pattern is process-heavy.

**Open**: was T11 worth it vs simpler integration coverage? Operator can answer; the fact that 3 real bugs were caught argues yes.

## 6. Future plan

### Immediate next operator-directed work

1. **Render outcomes (locked first)**: per `next_required_action: redirect_to_render_outcomes` + canonical-iter0006-005 + codex-iter0006-007. Specific candidates from iteration-0006 next_iteration_recommendations:
   - Pass 2 lessons-learned re-pass on Book 1 (Sweet Silver Blues) — first book in 14-book corpus; long-deferred
   - Next chapter render (whichever book is in queue)

### Operator-triggered (not auto-scheduled)

2. **Standalone extraction prep** (T9 + T10):
   - T9 decouple: env var `AGENT_LOOP_REPO_ROOT` + configurable subdir layout; preserve current defaults; touches all 6 tools + validators
   - T10 package: `pyproject.toml`; `console_scripts`; bundle validators + MCP server; README

3. **Bounded-gap smoke coverage** (Q3):
   - gap_1 partial_apply_failed test (monkeypatch + assert; ~30 LOC)
   - gap_2 concurrency test pending filelock implementation (Phase 1.x scope)

4. **Memory promotion** (Q5): operator-scope; agent does not auto-promote

5. **Orchestrator-prompt fix** (Q2 / canonical-iter0006-008): disambiguate canonical-full vs self-hash-exception in reviewer-dispatch template at next iteration boot

### Conditional / shelved

6. **Phase 1 MCP G3 / G4 / G5**: still `shelved-pending-operational-signal`. v1.9.0 explicitly bounds the operator-pivot precedent: future G3-G5 implementation requires operator strategic pivot, NOT auto-permission inherited from v1.9.0. Trigger conditions per v1.8.2 unchanged (render-work breach OR canonical-006 manual mechanism failure).

7. **iteration-0007 (if ever)**: claude-iter0006-005 flagged iteration input over-engineering. Future iterations should reference spec sections rather than restating them; ~120 LOC inputs, not ~290.

## 7. Reference points (HISTORICAL v1.9.0 / v1.9.1 snapshot — for current state see frontmatter `scope_disclaimer` + parent spec v1.9.2 revision_history + ledger v1_9_2_application_log)

| Topic | Path |
|---|---|
| Active spec (v1.9.0 at this row's write time; **CURRENT: v1.9.2** — see scope_disclaimer in frontmatter) | `docs/architecture/orchestration-spec.md` |
| Phase 1 design spec | `docs/architecture/phase-1-completion.md` |
| Disposition ledger | `consensus-state/state/disposition-ledger.yaml` (v1.9.0; v1_9_0_application_log + iteration_0006_dispositions) |
| Iteration-0006 archive | `consensus-state/active/iteration-0006/` |
| Sealed pass-22 | `consensus-state/archive/review-passes/2026-05-09-iteration-0006-consolidated_codex_plus_claude-pass.yaml` |
| Phase 1 MCP server | `consensus_mcp/server.py` + `tools/*` |
| Smoke test | `consensus_mcp/_smoke_test.py` (35/35) |
| Validator suite | `consensus_mcp/validators/run_validator_tests.py` (21/21) |
| Validator harness | `consensus_mcp/validators/validate_*.py` |

## 8. Acceptance gates at session close (HISTORICAL v1.9.0 / v1.9.1 snapshot — current gates are 51/51 + 21/21 + 0 post-Round-6; see consolidated review-findings MD Round 6 verification block for current state)

- [x] `python_env\python.exe consensus_mcp/_smoke_test.py` -> 37/37 (post-review +2)
- [x] `python_env\python.exe consensus_mcp/validators/run_validator_tests.py` -> 21/21
- [x] `python_env\python.exe consensus_mcp/validators/validate_disposition_index.py` -> 0 findings
- [x] `python_env\python.exe consensus_mcp/validators/validate_iteration.py --iteration-dir consensus-state/active/iteration-0006` -> 0 findings
- [x] `python_env\python.exe consensus_mcp/validators/validate_consensus.py --consensus consensus-state/active/iteration-0006/consensus.yaml` -> 0 findings
- [x] `python_env\python.exe consensus_mcp/validators/validate_review.py --review consensus-state/active/iteration-0006/codex-review.yaml` -> 0 findings
- [x] `python_env\python.exe consensus_mcp/validators/validate_review.py --review consensus-state/active/iteration-0006/claude-review.yaml` -> 0 findings
- [x] iteration-0006 audit log: 9 events in canonical sequence ending in iteration_closed
- [x] T6 sealed pass-22 in archive; T7 verified=True
- [x] v1.9.1 active in spec frontmatter at this bullet's original write time (was v1.9.0 at original land; v1.9.1 was the first post-review hardening — see Section 9). **CURRENT post-Phase-2: v1.9.2 active** (G3+G4+G5 implementation 2026-05-09 via second operator strategic pivot — see consolidated review-findings MD + parent spec v1.9.2 revision_history entry)
- [x] Helper scripts (_seal_iter0006.py, _apply_iter0006.py, _debug_t4_stage.py) cleaned up

## 9. Third-party post-review fixes (2026-05-09)

**See also**: [`2026-05-09-v1.9.x-third-party-review-findings.md`](2026-05-09-v1.9.x-third-party-review-findings.md) — single consolidated doc covering all SIX review rounds (Round 1: 9 findings post-v1.9.0 CLOSED; Round 2: 3 findings post-v1.9.1 CLOSED; Source-MD: 7 doc-consistency findings self-applied/re-review-pending; Codex follow-up: 5 findings self-applied/re-review-pending; MD-set follow-up: 5 findings self-applied/re-review-pending; Round 6: 9 findings from self-dispatched review on v1.9.2 self-applied/re-review-pending) with full citation + reproduction + resolution detail. Total: 38 review findings recorded across 6 rounds.

After the initial summary doc landed, an external code review surfaced 9 issues. All
real, all genuinely missed by the session's self-checks. Honest accounting of each:

### Blocking — fixed

- **MCP implementation was gitignored**. `.gitignore` had `scripts/*` deny-by-default
  with only `consensus_mcp/validators/**` re-allowlisted. `consensus_mcp/**` was
  silently ignored — a commit would have recorded docs claiming implementation while
  omitting it. **Fix**: added `!consensus_mcp/` + `!consensus_mcp/**`
  re-allowlist; pycache excluded. 11 MCP files now visible to git.

### High — fixed

- **`validators_to_run=[]` bypassed canonical-006 gate**. T4 accepted empty list and
  returned `gate_decision: APPROVED` with no findings. Reproduced. **Fix**: T4 refuses
  empty list with explicit error; `None` still defaults to all 4 validators. Smoke test
  added (test_patch_stage_and_dry_run_empty_validators_refused).
- **T7 did not enforce the G1 "both reviews sealed before serving" rule**. Original T7
  accepted single pass_id/path; design spec required iteration_id+reviewer with
  both-sealed audit-log check. **Fix**: T7 gained `iteration_id + reviewer` mode that
  reads independence-audit.yaml, refuses with `both_reviews_not_sealed` unless BOTH
  codex and claude have reviewer_invoked AND review_returned_and_sealed events. Per-
  reviewer review.yaml served only after both sealed. Smoke test added
  (test_review_read_post_seal_iteration_reviewer_g1_enforcement, 6 sub-assertions).
- **T3 audit append was not atomic; "atomic append" was overclaim**. Implementation is
  read-modify-write of full YAML file. **Fix**: docstring now documents read-modify-
  write semantics + single-writer assumption; design spec invariant_4 updated to
  honestly describe v1.0 behavior. Real filelock deferred to Phase 1.x.

### Medium — fixed

- **T7 path safety only applied to direct paths, not pass_id index lookup**. Bad index
  entry could make T7 read a YAML file outside ARCHIVE_DIR. **Fix**: pass_id mode now
  also runs the same `Path.resolve() + relative_to(ARCHIVE_DIR)` containment check;
  refuses with `path_outside_archive` on drift.
- **Design said apply is atomic; implementation documents partial_apply_failed**.
  Design spec invariant_3 overclaimed multi-file atomicity. **Fix**: design spec
  invariant_3 updated to per-file os.replace atomic with structured
  partial_apply_failed return on mid-loop OS error; full multi-file atomicity deferred.
- **`mcp-server-audit.yaml` was JSONL with .yaml extension**. File mixed legacy YAML
  head + JSONL tail (Frankenstein). **Fix**: converted to clean JSONL via the YAML
  head being re-emitted as JSONL records; renamed to `.jsonl`; server.py + smoke test
  updated. yaml.safe_load no longer falsely fails on it because the file no longer
  pretends to be YAML.
- **Smoke test mutated real `consensus-state/state/mcp-server-audit.yaml`**. Each smoke run
  appended real boot events. **Fix**: server.py reads `CONSENSUS_MCP_AUDIT_LOG` env
  var override; smoke test points it at a per-PID temp file. Real audit log no longer
  touched by smoke runs.

### Low — fixed

- **`pre_canonical_pin_marker` on new T6 packets**. Spec defines that marker for
  pre-v1.7.4 historical hashes; using it on modern packets is semantically wrong.
  **Fix**: T6 now stamps `sealed_at_utc` for new packets; preserves `pre_canonical_pin_marker`
  only when caller passes it in (historical-packet support).

### Files modified for post-review fixes

- `.gitignore` — re-allowlist `consensus_mcp/`
- `consensus_mcp/server.py` — `AUDIT_LOG` env-var override; rename .yaml -> .jsonl; docstring update
- `consensus_mcp/tools/patch_stage_and_dry_run.py` — refuse empty `validators_to_run=[]`
- `consensus_mcp/tools/review_read_post_seal.py` — `iteration_id + reviewer` mode + path-safety on pass_id lookup; `_finalize_verification` helper extracted
- `consensus_mcp/tools/audit_append_event.py` — honest CONCURRENCY docstring (read-modify-write, single-writer)
- `consensus_mcp/tools/review_write_and_seal.py` — `sealed_at_utc` for new packets; `pre_canonical_pin_marker` preserved only if passed in
- `consensus_mcp/_smoke_test.py` — temp audit sink + 2 new tests (35 -> 37)
- `docs/architecture/phase-1-completion.md` — invariant_3 + invariant_4 honest about v1.0 limitations
- `consensus-state/state/mcp-server-audit.yaml` -> `mcp-server-audit.jsonl` (file rename + content cleanup)

### Acceptance after post-review fixes

- 37/37 smoke (2 new tests)
- 21/21 validator suite unchanged
- 0 disposition / iteration / consensus findings
- All 4 third-party-flagged High issues closed; all 4 Medium closed; 1 Low closed; 1 Blocking closed

### Lesson for next session

The session's self-checks reported "100% complete in-place" prematurely. Specifically
missed:

1. `.gitignore` check for new directories (Karpathy "Think Before Coding" — should
   have grepped before claiming files would commit)
2. Empty-validator-list edge case (Karpathy "no assumptions" — should have tested the
   bypass surface)
3. T7 design-spec interface mismatch (assumed pass_id/path was the only mode without
   re-reading the design spec for required inputs)
4. "atomic append" terminology used loosely without verifying the actual implementation
   semantics

The third-party review caught all four. Memory takeaway: when claiming "100% complete"
on any toolchain, run a third-party review pass — the same way iteration-0006's dual
reviewer caught real defects in the iteration's content. Self-review is necessary but
not sufficient.
