<!-- consensus-mcp:begin (managed - do not edit inside) -->
## consensus-mcp is active in this project

- Start with `consensus-mcp-start-consult --question "..." --scope-glob "..."`, then run its generated `consensus-mcp-consult --iteration <id> --tier <quick|standard|deep>` command.
- Read sealed reviews via `consensus.get_iteration_outcome`; as host, synthesize the ideas on their merits into `converged-plan.yaml`. Run `consensus-mcp-approve --iteration <id> --init-dispositions` to create the disposition table.
- Complete `dispositions.yaml`: mark each idea `accepted` with a reason, `rejected` with evidence proving it wrong, or `deferred` with why and what remains. Then run `consensus-mcp-approve --iteration <id> --scope-glob "..."`. Approval validates and seals the table; it arms an edit gate only in continuous governance mode. On-demand is the default and returns a sealed result without a gate.

The host AI is the approver by design. Failed reviewers do not count against a change. A family means a model, not a vendor. See [README.md](README.md#how-it-works) for the roster, tiers, scorecard and gold outcomes, diff-scoped packets and budgets, async jobs, fallback behavior, failure classes, and Windows notes.

---

<!--
Source: https://github.com/multica-ai/andrej-karpathy-skills/blob/main/CLAUDE.md
(vendored 2026-05-22). Shared consensus-mcp contributor operating guidelines.
The content below is the upstream file verbatim; do not edit in place - re-vendor
from the source URL to update.
-->

# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" -> "Write tests for invalid inputs, then make them pass"
- "Fix the bug" -> "Write a test that reproduces it, then make it pass"
- "Refactor X" -> "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] -> verify: [check]
2. [Step] -> verify: [check]
3. [Step] -> verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
<!-- consensus-mcp:end -->
