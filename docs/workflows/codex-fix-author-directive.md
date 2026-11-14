---
name: Codex fix-author directive (operator 2026-05-10)
description: Operator-locked architectural commitment - codex should fix problems it finds; claude verifies the fix per CLAUDE.md; correction sub-loop if claude finds issues; codex re-reviews; consensus required for close.
type: project
originSessionId: 3dc1e744-0c21-449b-80ee-09dff754acb7
---
Operator directive 2026-05-10 (immediately following codex 2026-05-10 v2 verdict on the
consensus pipeline tooling sweep):

> "codex should fix problems it finds. The caveat to that is claude MUST verify
> correction, identify new problems (if any), if no problems with new code/logic =
> close issue, if problems found = correct issues, verify correct per claude.md
> THEN pass to codex for final code review. Repeat as necessary until consensus."

**This locks the architectural model** for the Phase 4.x autonomy expansion:

```
codex finds problem -> codex emits fix
 |
 v
claude verifies codex's fix per CLAUDE.md
 |-- no new problems -> close (consensus)
 \-- problems found -> claude corrects -> claude verifies -> re-submit to codex
 |
 v
 codex re-reviews
 |
 v
 (loop until consensus)
```

**Why:** The original loop goal (codex's expert verdict bar) was: "codex finds
blocker -> claude fixes -> codex re-reviews -> close on goal_satisfied=true." The
operator's directive expands that: codex doesn't just emit findings narratively
for claude to act on; codex emits the fix itself. Claude's role becomes
verifier-and-corrector rather than primary fixer. The cross-vendor independence
property is preserved because each agent reviews the OTHER's contribution.

**Implementation roadmap (Tasks #24-#27, blocking-chained):**

- **#22 iter-0013** - codex prompt-template hardening. PREREQUISITE; codex output
 reliability must be proven before trusting it to emit unified-diff patch text.
 (DONE 2026-05-10.)

- **#24 iter-0014** - extend `codex_review_schema.json` with optional
 `patch_proposal: {diff, files_touched, rationale}`. Validator extension:
 parse unified diff; require touched files in `goal_packet.allowed_files`;
 reject any forbidden_files paths. Codex still emits findings as primary;
 patch_proposal is per-finding opt-in. Does NOT apply patches yet.

- **#25 iter-0015** - `loop.verify_codex_patch` MCP tool. Dispatches a claude
 verifier subagent with (codex finding + proposed diff + CLAUDE.md project
 rules). Subagent emits structured verdict: `approved` OR
 `corrected_resubmit` (with claude's corrections embedded). New state-machine
 states in `loop.run_goal`: `codex_patch_proposed` ->
 `claude_verifying_patch` -> `patch_verified_ready_for_codex_resubmit` |
 `patch_corrected_by_claude_ready_for_codex_resubmit`.

- **#26 iter-0016** - staged-apply of verified codex patches via existing T4/T5
 tools (`patch.stage_and_dry_run`, `patch.apply_consensus_patch`). Refuse-by-
 default; requires BOTH `goal_packet.authorization.codex_patch_apply_authorized:
 true` AND env `CONSENSUS_MCP_CODEX_PATCH_APPLY=1`. Patches without claude
 verification approval refuse to apply.

- **#27 iter-0017** - capstone: full autonomous demo of the full cycle on a real
 small defect. Validates the automation-completion path end-to-end.

**Operating rule for claude during verification (key):** When claude verifies
a codex-emitted patch, it MUST consult `CLAUDE.md` (especially the Karpathy
Four Principles + the project-specific golden rules). The verifier-subagent's
prompt must explicitly include CLAUDE.md as the authoritative standard, not
just generic correctness review.

**Independence property to preserve:** codex emits patch -> claude verifies.
Claude must not have authored the original implementation that codex found
the defect in (or at least, must not be the same dispatch). The supervisor
already enforces this via subagent independence; ensure the patch-verification
subagent receives ONLY the patch + CLAUDE.md + the original codex finding,
not the reasoning of any prior dispatch.

**Authoritative status:** This is operator-locked architecture. Future
iterations CANNOT pivot away from this cycle without explicit operator
re-authorization. Treat as anchor for Phase 4.x scope.

## Closure-cross-verification-and-freshness invariant (locked 2026-05-10 v3)

Per codex 2026-05-10 v3 directive: enforce in supervisor what the directive
above currently only documents. "Prompt says another AI verifies" is too
weak; must be supervisor-enforced invariant.

### `last_mutation` is an event object, not a string

Stored as event derived from `apply_step_landed` audit events (NOT mutable
state text):

```yaml
last_mutation:
 actor: codex # or claude
 role: fix_author # or correction_author
 pass_id: codex-iter0017-1-pass1
 patch_id: <id from codex output>
 files_touched: [paths]
 base_sha: <pre-patch git sha>
 post_sha: <post-patch git sha>
 timestamp: 2026-05-10T...Z
```

The supervisor reads the most-recent `apply_step_landed` event to determine
`last_mutation`. Event-derived = cannot drift from reality.

### Closer must satisfy ALL THREE conditions

Not just "different AI" - must also verify the POST-MUTATION artifact AT
A FRESHER TIMESTAMP:

```python
assert closer.actor != last_mutation.actor # cross-AI
assert closer.review_target_hash == last_mutation.post_sha # post-mutation
assert closer.created_at_utc > last_mutation.timestamp # fresh
```

Failing any of these refuses close. Stale review of the right post-mutation
hash is still blocked because the timestamp check fires.

### Codex patch schema binding fields

Codex's `patch_proposal` MUST include (not just narrative):

```yaml
patch_proposal:
 patch_id: <unique>
 applies_to_findings: [codex-rev-NNN, ...]
 base_sha: <repo state codex saw>
 unified_diff: <text>
 files_touched: [paths]
 expected_tests: [test_name, ...] # tests codex expects to pass post-patch
```

base_sha binds the patch to the repo state codex reviewed; claude verifier
detects drift. applies_to_findings maps patch to specific findings. expected_tests
lets the verifier check codex's reasoning empirically.

REJECT extra fields like `verified` / `self_verified` / `correct` /
`approved` - anti-self-verification claims.

### Claude verifier inputs

Claude verifier subagent receives FACTS, not REASONING:

- goal_packet.yaml
- acceptance_gates
- patch unified_diff
- touched-file snippets or full files
- test output (post-patch run)
- relevant constraints from CLAUDE.md

EXCLUDE codex's reasoning trail and prior reviewer conclusions. Independence
preserved by source-segregating facts vs reasoning.

### Multi-layer enforcement (defense-in-depth)

Codex 2026-05-10 v3: "Defense-in-depth matters because one bypass path will
eventually appear." Three independent enforcement points:

1. `loop.run_goal` transition guard - refuses `ready_to_close` when invariant fails
2. `_self_drive.cmd_check_stop_rules` new stop rule `closer_stale_or_self`
3. T6 `audit_append_event` `iteration_closed` event - refuses to record close
 when invariant fails

Any single layer can fail-closed; bypass requires defeating all three.

### Forbidden state transitions

- `claude_verifying_patch.corrected_resubmit -> ready_to_close`: BLOCKED. Must
 route through `codex_re_reviewing_after_claude_correction` first.
- Any path where closer is the same actor as `last_mutation.actor`: BLOCKED.
- Any path where closer.review_target_hash != last_mutation.post_sha: BLOCKED.
- Any path where closer.created_at_utc < last_mutation.timestamp: BLOCKED.

### Acceptance tests for the invariant (#28 minimum bar)

1. Codex patch then Codex close: BLOCKED (cross_actor)
2. Codex patch then Claude approve: ALLOWED
3. Codex patch, Claude correction, Claude close: BLOCKED (cross_actor)
4. Codex patch, Claude correction, Codex review of post-correction hash: ALLOWED
5. Codex re-fix then Codex re-review: BLOCKED (cross_actor)
6. Stale Claude review from before Codex re-fix: BLOCKED (hash_match)
7. Operator override marking quorum_close_passed: REJECTED at close_state taxonomy layer (must use close_with_caveat)
8. Re-dispatch with reused pass_id: REJECTED at audit_append_event layer
9. Closer's review_scope_hash differs from claimed = inputs drifted: REJECTED

## v4 refinements (codex 2026-05-10 v4)

### Sequencing: #28 codifies BEFORE #24

Invariant skeleton must land before codex patching capability. Otherwise codex
authors patches in a window where close rules aren't fully hardened - risk
of normalizing unsafe closure patterns. Updated task chain: #22 -> #28 -> #24.

### Stop rule renamed: `closure_cross_verification_failed`

Was: `closer_is_last_modifier`. Renamed to cover all 3 failure modes in one
rule: self-closer + stale verdict + hash mismatch. Single rule = single
diagnostic surface; covers what the invariant actually prevents.

### Identity refined: actor is structured, not a string

```yaml
actor:
 id: <unique-per-attempt> # codex-iter0017-1
 model_family: codex|claude
 role: fix_author|correction_author
 pass_id: <unique per attempt>
```

Two codex runs no longer blur into one identity. pass_id mandatory unique
per attempt (re-dispatch increments: `<reviewer_id>-attempt-<N>`).

### Canonical hash: patch target file bundle hash

```python
def bundle_sha(repo_root, files_touched) -> str:
 parts = []
 for p in sorted(files_touched):
 content = (repo_root / p).read_bytes() if (repo_root / p).exists() else b""
 parts.append(f"{p}\0{hashlib.sha256(content).hexdigest()}")
 return hashlib.sha256("\n".join(parts).encode()).hexdigest()
```

NOT git HEAD (no-auto-commit conflict). NOT review-packet sha (covers
metadata not patched files). Bundle hash: reproducible, bound to exactly
the touched files' content, doesn't change with unrelated commits.

### Hash binding PRIMARY, timestamp SECONDARY

Codex 2026-05-10 v4: "Timestamp freshness is useful but secondary. Hash
binding should be primary because clocks and generated timestamps can drift."

```python
# PRIMARY (cannot drift)
assert closer.actor.id != last_mutation.actor.id
assert closer.review_target_hash == last_mutation.post_sha
# SECONDARY (defense-in-depth against re-played stale verdicts)
assert closer.created_at_utc > last_mutation.timestamp
```

### patch_id globally unique, content-bound

```python
patch_id = f"patch-{base_sha[:12]}-{unified_diff_sha256[:12]}"
```

Tied to `base_sha + unified_diff_sha256`. No patch can be relabeled after
authorship; collisions = identical patches. Codex emitting same patch_id
twice with different diffs = REJECTED.

### iter-0020 ergonomics fix (supersedes content-binding above)

Per iter-0019 empirical finding: codex's read-only sandbox cannot execute
arbitrary code, so it cannot compute `sha256(unified_diff)`. The
content-bound patch_id formula above is therefore producible only by
helpers, not by codex itself. iter-0019's strict-mode demo correctly
showed codex refusing to guess and using `patch_not_proposed_reason`.

iter-0020 relaxes patch_id to a codex-producible form:

```python
# Codex emits this:
patch_id = f"{finding_id}-patch" # e.g., "codex-rev-001-patch"
```

The regex moves from `^patch-[0-9a-f]{12}-[0-9a-f]{12}$` to
`^codex-rev-\d+-patch$`. The validator enforces `patch_id == f"{finding_id}-patch"`.
The `unified_diff_sha256` field is no longer implicit (in patch_id) - it
becomes explicit:

- `_dispatch_codex._validate_patch_proposal` computes
 `sha256(unified_diff)` and stamps it on the validated patch_proposal as
 `unified_diff_sha256` (helper does the work codex can't).
- `apply.codex_patch.handle` reads that field (or recomputes defensively
 if absent on older patches) and stamps it on `last_mutation.unified_diff_sha256`
 in the apply_step_landed audit event.

Drift detection is unchanged: it works via `base_sha` (recomputed at
apply time and compared against `patch_proposal.base_sha`) plus `post_sha`
(recomputed at apply time) plus `unified_diff_sha256` (separately stamped).
The patch_id is now a label, not a hash; relabeling is detectable
downstream via the explicit hash field.

### review_scope_hash (verifier input bounding)

Claude verifier emits `review_scope_hash`: sha256 of the EXACT inputs
received. Proves verifier was reproducibility-bounded - not minimal-only,
not unbounded.

### "Reproducibility-bounded" not "facts-only minimal"

Codex 2026-05-10 v4: "Verifier needs enough context to reproduce the result,
not just inspect the diff."

Inputs to claude verifier:
- goal_packet.yaml
- acceptance_gates
- unified_diff
- **touched-file FULL contents (post-patch)** - not just snippets
- test output (post-patch run)
- CLAUDE.md constraints
- base_sha + post_sha (drift detection)
- codex finding TEXT (the WHAT not the WHY)

EXCLUDE: codex's reasoning trail, prior reviewer conclusions, orchestrator
analysis. Independence by source-segregating reasoning, reproducibility by
including all evidence needed to re-derive the verdict.

### Closure certificate artifact

Single compact proof per close attempt at
`iteration_dir/closure-certificate.yaml`:

```yaml
schema_version: 1
iteration_id: <id>
last_mutation: <event object>
closing_verdict:
 actor: {id, model_family, pass_id}
 review_target_hash: <sha>
 created_at_utc: <ts>
 review_scope_hash: <sha>
invariant_checks:
 cross_actor: PASS|FAIL
 hash_match: PASS|FAIL
 freshness: PASS|FAIL
gate_outputs: {gate_name: pass_status}
overall: PASS|FAIL
```

Operator reviews ONE artifact for close validity, not the full audit log.

### Operator override = distinct close state

`close_with_caveat` (already in taxonomy) is NEVER `quorum_close_passed`.
The invariant only applies to `quorum_close_passed`. Operator overrides
land as `close_with_caveat` and CANNOT be cited as evidence the loop closed
cleanly. This is the existing close-state taxonomy enforced as architectural
invariant.

### T6 bypass concern: every close path MUST go through T6

Codex 2026-05-10 v4: "T6 audit_append_event refusing close is good, but
only if every close path must go through T6. Any manual artifact write path
remains a bypass."

Architectural commitment: NO close path may write `closing_state` to
iteration-outcome.yaml without first appending an `iteration_closed` event
through T6. T6's refusal is the LAST gate; any path that bypasses it is a
bug to be fixed, not a workaround to be tolerated.

### Historical validator

`consensus_mcp/_validate_closure_invariant.py` - scans existing
iterations (iter-0009..iter-0012 historically; all future automatically)
for invariant compliance. Catches retroactive drift; proves rule is not
just future-facing.
