# Design consult: iter-0009 - add gemini as third reviewer, generalize to N-reviewer pool

**Context for reviewer (codex):** maintainer has `gemini` CLI v0.43.0-preview.0 installed and authenticated. The pipeline today is hardcoded 2-AI (claude orchestrator + codex reviewer). The literal ask is "add gemini" - but the maintainer has surfaced a load-bearing constraint:

> We need flexibility. We won't always use three, won't always use two. Cost / rate-limits / preference may disable gemini (or even codex temporarily) without losing anything but another opinion.

So this isn't really "add gemini" - it's **"generalize the reviewer pool to N-of-M, where the enabled-set is dynamic"** and gemini is the first new instance.

Maintainer has already chosen two governance defaults:

- **Vote semantics (default)**: majority of enabled-and-responsive reviewers must agree `goal_satisfied=true` with no `blocking_objections`. With 1 enabled reviewer -> that one alone gates. 2 enabled -> both must agree (no majority of 2 is "1-of-2"). 3 enabled -> 2-of-3. N enabled -> [N/2]+1 if N is even, [N/2] if N is odd? Codex weigh in on the exact rule below.
- **Dispatch order**: parallel; all enabled reviewers fire simultaneously, none can see the others' output during dispatch.

This consult covers the architectural choices for making the reviewer pool **first-class configurable**, with gemini as the first beneficiary.

## Findings

### Finding 1 - Helper module architecture

- **F1a (fork)**: copy `_dispatch_codex.py` -> `_dispatch_gemini.py`, mutate CLI invocation. ~1800 lines duplicated. Locks in 2-helper assumption; adding a 3rd reviewer (e.g. claude-via-API, deepseek, local-llama) re-duplicates.
- **F1b (extract `_dispatch_base.py`)**: shared infrastructure (prompt build, stall detection, sealing, audit append, scope-signature check) becomes ~1200 lines; per-reviewer adapters (`codex`, `gemini`, ...) are thin ~150-line modules carrying CLI invocation + output parser + reviewer-id prefix. **Strongly preferred under the N-reviewer constraint** - adding reviewer N+1 becomes one ~150-line file.

Risk of F1b: touches the most-tested v1.10.0-LANDED + v1.10.4-HARDENED file in the codebase. Must preserve current codex behavior bit-for-bit; regression test the existing iter-0001..0008 dispatch invariants.

### Finding 2 - Output schema enforcement (gemini lacks `--schema`)

Codex CLI enforces JSON schema via `--schema codex_review_schema.json`. Gemini CLI has no equivalent.

- **F2a (prompt-only)**: prompt-engineer for JSON matching `codex_review_schema.json`. Defensive parser; on schema mismatch, mark pass `ok=False`, error_type=`schema_parse_failure`. Lean MVP.
- **F2b (validator-retry)**: same prompt-engineering, but on parse failure re-prompt gemini ONCE with the error appended ("your previous response failed schema validation at <field>; re-emit conforming JSON"). One retry max - guards against infinite-loop on a genuinely confused model. Higher reliability; +latency on failures only.

Generalization: this question applies to any future reviewer adapter that doesn't have native schema enforcement. The choice is the **default for adapters lacking native schema**.

### Finding 3 - Parallel dispatch mechanism

`concurrent.futures.ThreadPoolExecutor(max_workers=N)` where N = len(enabled_reviewers). Each adapter reads `goal_packet.yaml` and `review-packet.yaml` independently from disk - no shared in-process state. Subprocess stdout streams isolated.

Failure semantics generalize:

- All succeed -> seal all, gate evaluates majority of enabled
- K-of-N succeed, M-of-N fail (K + M = N) -> seal K passes, log M failures; gate evaluates majority of K (if K >= minimum_responsive_threshold) or refuses
- Zero succeed -> gate refuses (insufficient reviewers)

Independence preserved by construction. Subprocess isolation + sealed-after-completion ordering.

### Finding 4 - Gate semantics (majority of enabled-and-responsive)

Current `gate.evaluate_production_with_scope_match`: single sealed codex pass.

Proposed: parameterized quorum policy.

- **F4a (claude implicit)**: claude's vote is implicit - authoring goal_packet AND landing the patch counts as claude-attests. Claude is always-on, never disabled. Quorum = [(1 + enabled_external_reviewers) / 2] of responsive votes including claude. With 0 external enabled, claude alone gates (degraded but functional). With 1 external (e.g. codex only) and both responsive, [2/2] = 1 needed -> trivially passes if either agrees. Edge case: with 1 external, codex disagreeing means 1-of-2 (claude vs codex) - gate refuses (tie).
- **F4b (claude explicit)**: claude files explicit `claude-review.yaml`. All votes are first-class. New tool surface (`reviewer.attest_claude`?). More provenance-honest. Adds orchestrator complexity.

Specific quorum rule for N enabled (including claude under F4a, or excluding claude under F4b):

- Odd N: [N/2] agree-votes (majority)
- Even N: N/2 + 1 agree-votes (strict majority, no ties allowed)
- 1 responsive: that one alone gates

Non-vote (reviewer crashed/timeout) does NOT count as "no" - counts as absent. Quorum recomputed against responsive set; if responsive < minimum_responsive (default 1, configurable), gate refuses with `insufficient_reviewers`.

### Finding 5 - Enablement/disablement mechanism

How does the operator declare "gemini is enabled this run"?

- **F5a (env vars)**: `CONSENSUS_MCP_REVIEWERS=codex,gemini` (comma-list of enabled adapter names). Absent or empty -> default set (TBD: codex-only or all-installed-and-authed). Per-process scope; easy to script.
- **F5b (goal_packet field)**: `authorization.enabled_reviewers: [codex, gemini]` per iteration. Per-iteration scope; sealed into the goal_packet so the iteration record is self-describing. Operator changes via editing goal_packet before dispatch.
- **F5c (both, precedence: goal_packet > env)**: env var sets default; goal_packet field overrides per-iteration. Most flexible. Adds documentation burden.

Default-on/off for gemini specifically: codex weigh in.

### Finding 6 - Backward compatibility

Existing iter-0001..0008 have codex-only review history under the OLD single-reviewer gate.

- **F6a (grandfather + version stamp)**: gate semantics versioned. `authorization.gate_semantics_version: 1` (legacy) vs `2` (N-reviewer). Iterations stamped at creation; gate dispatches to the matching evaluator. Defensive.
- **F6b (read-only history)**: existing iterations are immutable history, not re-gated. New gate only applies to iterations dispatched at v1.14.0+. No version stamp needed if no path re-evaluates historical iterations.

F6b simpler IF no path re-evaluates. Codex confirm: does any current code path re-evaluate the gate against historical sealed reviews? (Spot-check `gate_evaluate_production_with_scope_match.py` and `_release_gate_check.py`.)

## Open scope questions

**Q1.** Helper module: **F1a fork** or **F1b extract base**? Given N-reviewer scope, F1b is the right answer - confirm or dissent.

**Q2.** Output schema for schemaless reviewers (gemini, future others): **F2a prompt-only** or **F2b validator-retry**?

**Q3.** Claude's vote: **F4a implicit** (orchestrator authorship attests) or **F4b explicit** (claude-review.yaml first-class)?

**Q4.** Enablement: **F5a env-only**, **F5b goal_packet-only**, or **F5c env+goal_packet with override precedence**?

**Q5.** Backward compat: **F6a version-stamp** or **F6b read-only-history**?

**Q6.** Quorum rule for even-N: strict majority (N/2+1, ties refuse) vs >=N/2 (ties pass). Default I propose: strict majority. Codex confirm or dissent.

**Q7.** Are there findings I missed? (e.g., reviewer-specific timeout overrides, model-pinning per adapter, sandbox policy for gemini's auto-edit modes, rate-limit awareness baked into adapter)

## Your task

Emit ONE finding only. severity: low (design + scoping).

- `recommendation`: a single string of the form:
  `"Q1: <F1a|F1b>; Q2: <F2a|F2b>; Q3: <F4a|F4b>; Q4: <F5a|F5b|F5c>; Q5: <F6a|F6b>; Q6: <strict|inclusive>; Q7: <none|added: ...>"`
- `risk`: short rationale (~4 sentences) covering the highest-impact picks
- No `patch_proposal` needed - maintainer implements after the verdict.

Empty findings is NOT acceptable.
