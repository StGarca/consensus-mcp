# Tool-Defect Bypass - Procedure for Legitimate consensus-mcp Work

The PreToolUse hook at `.claude/hooks/tool-defect-gate.py` blocks Edit/Write/MultiEdit/Bash on protected paths unless a valid proof artifact exists. This document defines the legitimate bypass: how to produce a proof artifact when a real consensus-mcp defect actually requires a source edit.

## Protected paths (gate triggers on writes to these)

- `**/consensus_mcp/**/*.py`
- `**/_dispatch_*.py`
- `.claude/agents/**`

If the orchestrator (Claude) tries to Edit/Write to one of these without a proof artifact, the gate blocks with stderr pointing back to this document.

## When to use the bypass

Use it ONLY when an INDEPENDENT investigation has produced reproducible evidence that the consensus-mcp surface is defective. Not when:

- Claude observed an unexpected behavior but hasn't reproduced it from a known-good input.
- Claude is "fairly sure" it's a bug.
- Claude's own input/yaml/goal_packet might be malformed.
- Consensus has worked fine on the same surface in hundreds of prior iterations.

If any of those apply, the gate is doing its job. Stop.

## Procedure

1. **Observe.** Capture the exact command run, exact observed output (stdout + stderr + exit code), and the consensus-mcp version under test.

2. **Find a known-good input.** From a prior clean iteration (git log of `consensus-state/archive/review-passes/`), identify an input that previously worked. Capture it exactly.

3. **Dispatch the Tooling Defect Diagnoser subagent.** Use Claude Code's `Agent` tool with `subagent_type: "tooling-defect-diagnoser"`. Pass in the dispatching prompt:
   - The observed unexpected behavior (exact command + outputs).
   - The known-good input (exact).
   - The consensus-mcp version (`consensus-mcp --version` output).
   - The candidate target file path (the consensus-mcp source file you want to edit).

4. **The subagent independently decides.** It runs reproductions with both inputs, reads the candidate file, and returns one of:
   - `STATUS: confirmed-defect; ARTIFACT: <path>` - the subagent wrote a schema-valid proof artifact at the deterministic path (`./tmp/tool-defect-proof-<sha256(target)>.yaml`).
   - `STATUS: no-defect-likely-self-inflicted; REBUILD: <what to re-examine>` - no defect found. The orchestrator must rebuild assumptions; the gate stays locked.
   - `STATUS: cannot-diagnose; missing inputs: <list>` - the dispatching prompt was incomplete.

5. **If CONFIRMED DEFECT:** the proof artifact now exists. Future Edit/Write attempts to the cited target will pass the gate. Make the edit. After the edit lands, the proof artifact may be deleted (it was for that specific target file at that specific sha256).

6. **If NO DEFECT:** do NOT manually fabricate a proof artifact. The orchestrator must rebuild its assumptions and re-examine its own input.

## Anti-patterns the gate is designed to catch

- Claude reasoning "this looks like a dispatcher bug" without reproducing from a known-good input.
- Claude diving into consensus-mcp source after a single failed run and proposing a fix.
- Claude blaming `.mcp.json`, MCP-tool-name regex, dispatcher flag handling, etc., without independent reproduction.
- Claude claiming "wrapper asymmetry is a defect" and proposing source changes before verifying the asymmetry causes operational failure (vs. cosmetic UX gap).

In every one of those cases, the gate blocks and the orchestrator must produce an independent proof artifact OR rebuild assumptions.

## Decisive experiment

The gate's correctness is tested by the cases in `consensus-state/active/iteration-claude-screwup-prevention-meta-2026-05-27/decisive-experiment.md` (Tests A-D).

## What this gate is NOT (honesty about the trust model)

The Tooling Defect Diagnoser subagent is a fresh-context Claude instance running inside the same Claude Code session. Same model family, same training, same blind spots - only the conversation context differs. **The diagnoser is NOT an independent trust root.** The schema field `independent_observer_id` is a label, not an enforcement: nothing in the proof-artifact pipeline cryptographically distinguishes orchestrator-Claude from subagent-Claude.

This is by design and is acknowledged here so the gate is not over-trusted:

- **What this gate IS:** operator-controlled containment. The operator decides when to engage the gate-stack engagement marker (default OFF). When ON, the gate forces a procedure break - observe, find a known-good input, dispatch the diagnoser, wait for an independent context window to disagree or agree - before the orchestrator can edit protected paths. The friction is the point; it interrupts the failure mode where Claude rationalizes a source edit after a single failed run.
- **What this gate is NOT:** third-party attestation. The diagnoser cannot prove independence to a skeptical reviewer; it can only provide a second look. If a hostile orchestrator wanted to fabricate a proof, the diagnoser-subagent path is the same circular trust loop the v1.22 trust kernel had - proven untrustworthy and ripped out.

If you need actual independence, the proof artifact should be co-signed by a non-Claude reviewer (codex/gemini/kimi/grok) via a Workflow B sealed packet, OR carry an explicit `operator_ack` field countersigned by the human. Neither is enforced today; both are tracked as follow-ups.

The human operator remains the trust root.

## Cross-references

- Validator: `.claude/hooks/tool-defect-gate.py`
- Schema: `.consensus/schemas/tool-defect-proof.schema.yaml`
- Subagent: `.claude/agents/tooling-defect-diagnoser.md`
- Origin iteration: `consensus-state/active/iteration-claude-screwup-prevention-meta-2026-05-27/`
- Companion memory rule: `feedback_claude_degradation_hallucination_containment` (per-project auto-memory)
- Self-trust-root limitation surfaced in: iter-0045 cross-family review (single-Claude reviewer Critical #2; not flagged by the codex/gemini/kimi panel).
