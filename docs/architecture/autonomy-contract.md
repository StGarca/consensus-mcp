---
title: Bounded Quorum Self-Driving Loop -- Autonomy Contract
date: 2026-05-09
type: autonomy-spec
status: active
related_specs:
 - docs/architecture/orchestration-spec.md (v1.9.3-rc parent spec)
 - docs/architecture/phase-1-completion.md (G1-G5 implementation)
 - docs/architecture/2026-05-09-v1.9.x-third-party-review-findings.md (consolidated review record)
audience: ai-only-plus-operator-review
phase: 4
authorization: operator_directive_2026_05_09_bounded_quorum_self_drive
---

# Bounded Quorum Self-Driving Loop -- Autonomy Contract

Per operator directive 2026-05-09:

> "Our system should support a bounded self-driving loop when it receives a clear goal and a detailed plan. The quorum is the safety mechanism. The mistake would be letting the loop invent goals, expand scope, or self-certify outside the packet it was given."

This document is the AUTHORITY on what the loop may and may not do under bounded self-drive. Phase 4 v1.0 = Level 2 (bounded self-driving against explicit goal packets); Level 3 (queued unattended) and beyond are out of scope.

## 1. The autonomy contract

Every self-driving run starts with a sealed `goal_packet`. The loop may act ONLY inside this contract. If it needs to change the goal, widen scope, touch forbidden files, or reinterpret acceptance, it MUST stop.

### 1.1 goal_packet schema

```yaml
schema_version: 1
pilot_id: <iteration-NNNN-pilot-N>
goal:
 summary: <one-sentence what>
 desired_end_state: <observable / verifiable>
 non_goals: [<list of things this is NOT>]
allowed_files: [<absolute or repo-relative paths; explicit list>]
allowed_sections: [<for spec-md edits via T9/T10; format "<file_alias>/section_<N>" or "<file_alias>/frontmatter">]
forbidden_files: [<explicit list; trumps allowed if conflict>]
max_iterations: <int; hard cap>
max_patch_size: <int LOC; hard cap per applied patch>
validators_required: [<validator names that must pass>]
acceptance_gates: [<measurable conditions; not "looks good">]
stop_conditions: [<see Section 6>]
operator_escalation_triggers: [<conditions that mandate operator intervention>]
authorization:
 authorized_by: operator
 authorized_at_utc: <iso8601>
 scope_signature: <sha256 over (goal + allowed_files + acceptance_gates) for tamper detection>
```

Additional fields permitted when measurable; never required.

### 1.2 Sealing

The `goal_packet` is sealed (canonical_yaml_sha256) before the loop begins. The loop reads the sealed packet on every state transition; if its sha drifts, the loop stops.

## 2. Quorum is required for movement

A self-driving cycle CANNOT advance on one model's judgment.

### 2.1 Minimum quorum

- **Claude reviewer**: methodology, consistency, scope discipline, process risk
- **Codex reviewer**: implementation, tests, runtime behavior, patch safety
- **Synthesizer**: merges findings, resolves conflicts, produces the next patch plan

### 2.2 Rules

- Reviewers work INDEPENDENTLY before seeing each other (G4 forced post-exposure carried; same as the v1.9.x dual-reviewer pattern)
- Both must seal findings before synthesis (G1 enforcement via T6 + T7)
- ANY blocking finding stops apply
- ANY disagreement on scope stops apply unless the original `goal_packet` resolves it
- **Same-model self-review is evidence, not quorum** (per CFU F3 self-correction principle)
- Cross-vendor not required for Phase 4 v1.0; cross-MODEL is the bar (claude-iter0007-2 distinction)

## 3. Loop states

The loop has 11 explicit states. Transitions are recorded in the iteration's `independence-audit.yaml` via T3 (audit.append_event).

```
goal_received
 |
 v
packet_built
 |
 v
reviews_dispatched
 |
 v
reviews_sealed
 |
 v
consensus_ready
 |
 v
patch_planned
 |
 v
validator_dry_run_passed (T4 canonical-006 gate)
 |
 v
patch_applied (T5 atomic apply)
 |
 v
verification_passed
 |
 v
quorum_close_passed (TERMINAL: clean stop)

(any state) ----> blocked_needs_operator (TERMINAL: escalation)
```

ONLY `quorum_close_passed` means the loop can stop cleanly without operator intervention. Any other terminal state is escalation.

## 4. Execution cycle

For each round:

1. Build review-packet from `goal_packet` + current state
2. Dispatch claude + codex independently (parallel; G4 forced post-exposure carried)
3. Seal both reviews via T6
4. Synthesize canonical findings (severity gate per spec section 13)
5. If blocking/high findings exist, create a bounded patch plan within `allowed_files` + `allowed_sections`
6. Run validator-first dry-run via T4 (canonical-006 gate)
7. Apply only if validators pass (T5 atomic apply)
8. Rerun full acceptance gates (per `acceptance_gates` field)
9. Redispatch reviewers on the post-apply state
10. Close ONLY if quorum agrees the original goal is satisfied

Each round increments the iteration counter; capped at `max_iterations`.

## 5. What quorum may close

### 5.1 Quorum MAY close (Level 2 authorized)

- Bounded code fixes
- Bounded doc consistency fixes
- Validator/test hardening
- Planned mechanical refactors
- Single-purpose repo decisions with clear acceptance gates

### 5.2 Quorum MAY NOT silently close (operator approval required)

- Goal changes
- New architecture phases
- Release/shipping claims
- Production-impacting actions
- Scope expansion beyond `goal_packet`
- Any task where acceptance depends on operator taste, hearing, product judgment, or business decision

If the loop encounters one of these, it MUST escalate via `blocked_needs_operator`.

## 6. Self-driving stop rules

The loop MUST stop when ANY of these fire:

1. `max_iteration` count reached
2. Same class of finding repeats twice (recursion-pattern detected)
3. A fix creates new cross-document drift
4. Validators disagree with reviewer claims
5. Patch would touch forbidden files
6. Required acceptance gate is missing
7. Claude and Codex disagree on whether the goal is satisfied
8. The only remaining work is deciding what the goal should mean

These prevent infinite process churn (the v1.9.x recursive-review-rounds pattern is exactly what stop-rule #2 + #3 prevent).

## 7. Required evidence for closure

A self-driving run closes (`quorum_close_passed`) ONLY with:

- Both reviews sealed (T6 verified via T7)
- No blocking/high unresolved findings
- All required validators pass
- Dry-run passed before apply (T4 canonical-006)
- Post-apply verification passed
- Changed files match `allowed_files` + `allowed_sections` scope (verified via git diff scoped to those paths)
- Ledger updated (T8 if state-affecting)
- Review findings written to MD (per `feedback_review_writeback_full` rule)
- Quorum EXPLICITLY says original goal satisfied (both reviewers' `goal_satisfied: true`)

No "looks good" closure. No silent close.

### 7.1 Pilot/prototype exception (added iter-0009 stabilization 2026-05-09 per Round 9 F2)

A `goal_packet` may declare itself a **pilot/prototype** by setting
`closure_class: prototype` in the `authorization` block. Pilot/prototype packets
MAY close on gate evidence alone (acceptance_gates pass + scope verified via
`_self_drive verify_scope` + no stop rules fired) WITHOUT the full Section 7
ceremony, on the explicit condition that:

- The packet is operator-pre-authorized and named in the parent iteration's input
- Pilot/prototype evidence MUST NOT be cited as proof of contract enforcement
 for any production self-drive iteration
- Pilot/prototype evidence MUST NOT be cited as "release-ready" or
 "shippable self-drive proof"
- Spec/ledger updates MAY be deferred outside the iteration close, but the
 iteration-outcome.yaml MUST record the deferral explicitly

Production self-drive iterations REQUIRE the full Section 7 ceremony (fresh
dual-reviewer dispatch + post-apply verification + ledger update + MD writeback +
both reviewers' `goal_satisfied: true`). The pilot exception exists to support
contract bring-up; once a pilot has closed cleanly, future packets in the same
class graduate to full ceremony.

iteration-0008-pilot's three pilots used this exception (recorded retroactively in
iter-0009 stabilization); their evidence is therefore prototype-class, not
shippable-self-drive proof.

## 8. Self-drive levels

- **Level 1**: supervised quorum loop (orchestrator-LLM dispatches reviewers + drives state machine; the autonomy contract enforces discipline)
- **Level 2**: bounded self-driving loop with clear goal packet (CURRENT TARGET; operator authorized 2026-05-09)
- **Level 3**: queued unattended loop (out of scope for Phase 4 v1.0)

Phase 4 v1.0 implements Level 2 via the supervised-quorum-orchestrator pattern: an orchestrator-LLM follows this contract on a sealed `goal_packet`, with stop rules that force halt on any contract violation. True autonomous execution (Level 3) requires API-driven reviewer dispatch + a long-running daemon; not in v1.0 scope.

## 9. Phase 4 v1.0 deliverables

Built once; reused by every subsequent self-drive iteration:

- `docs/architecture/autonomy-contract.md` -- THIS DOC
- `consensus_mcp/_self_drive.py` -- goal_packet validator + state-machine recorder + stop-rule enforcer (NOT an autonomous API executor; the orchestrator-LLM follows the contract; this script enforces it via state-tracking + stop checks)
- `consensus_mcp/goal_packet_schema.yaml` -- canonical schema example

## 10. Pilot iteration plan

**iteration-0008-pilot**: Phase 4 launch + 3 sequential pilots in increasing complexity:

- **Pilot 1**: memory promotion (claude-iter0006-006 deferred to operator) -- 2-file write; smallest bounded target
- **Pilot 2**: codex sha-form drift recurrence fix (canonical-iter0006-008 + canonical-iter0007-006) -- memory entry encoding the canonical-full convention
- **Pilot 3**: consolidated review-findings MD backfill -- post-v1.9.3-rc + Round 8 closure agreement

Each pilot gets its own `goal_packet`, runs through the loop, closes (or stops). Iteration-0008 closes only if all 3 pilots cleanly close OR if any pilot stops, the iteration documents which stop rule fired and escalates.

## 11. What this contract does NOT do

Per Phase 3 anti-scope rule (still in effect):

- No new MCP tools (G6+ blocked)
- No new validators
- No modifications to existing tool source beyond what pilot goal_packets explicitly authorize
- No spec sections 1-23 architectural changes

The autonomy contract is process design, not capability expansion.

## 12. Memory entries promoted as part of Phase 4 launch

- `feedback_phase_4_self_drive_authorized.md` -- operator memory entry recording Phase 4 v1.0 authorization + Level 2 target + iteration-0008-pilot scope
- (deferred to Pilot 1) `feedback_no_process_polishing.md` -- claude-iter0006-006
- (deferred to Pilot 2) `feedback_codex_dispatch_use_canonical_full.md` -- canonical-iter0006-008 + canonical-iter0007-006

## 13. Acceptance for Phase 4 v1.0

iteration-0008-pilot closes implementation_ready iff:

- All 3 pilots cleanly close OR document-clean-stop
- Autonomy contract held throughout (no scope drift; no goal expansion)
- v1.9.4 spec entry records Phase 4 launch + pilot results
- Ledger v1_9_4_application_log block records same
- 9 release gates remain green (Phase 3 RC integrity preserved)

## See also

Downstream subsystem-review pages that built on this autonomy contract during the iter-0014..iter-0036 self-drive cycle:

- [[2026-05-10-recent-iteration-failures]] — failure postmortem covering iter-0019..iter-0031
- [[2026-05-10-visibility-tui-design]] — visibility TUI + v1.10.5 hardening design
- [[2026-05-10-codex-fix-author-roadmap-results]] — codex-fix-author cycle results (iter-0014..iter-0022)
- [[2026-05-10-v1.10.4-hardening-expert-review]] — v1.10.4 hardening expert review
