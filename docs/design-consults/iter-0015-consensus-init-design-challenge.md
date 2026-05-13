# Design challenge: iter-0015 — consensus init wizard + .consensus/config.yaml schema

**Workflow #4 BLIND DESIGN CHALLENGE.** You (the reading contributor — claude, codex, or gemini) are one of three independent contributors. Each of you will receive THIS document, with NO visibility into the other contributors' proposals. Author your own complete design proposal. After all three are sealed, the proposals will be revealed and you'll be asked to converge.

## Background

`consensus-mcp` orchestrates cross-AI code review. The current state has hardcoded assumptions: workflow #3 (apply-then-review) by default, codex always-on, claude as privileged orchestrator. The maintainer has determined these are **not universal** — every project should pick its own deliberation shape at initial setup.

The `docs/specs/consensus-onboarding-cli-spec.md` spec exists but was never implemented. iter-0015 must rectify both: build the missing init wizard AND make every workflow assumption operator-configurable.

## Problem statement

Design (do NOT implement yet — iter-0016+ implements per converged design):

1. **`consensus init` CLI wizard**: one-shot per-repo setup. Asks operator decisions across the dimensions below. Writes `.consensus/config.yaml` (committed) and updates `.gitignore` if needed.

2. **`.consensus/config.yaml` schema**: typed, versioned, declarative. Covers every dimension below. Sensible defaults so the wizard can run non-interactively (`--non-interactive --workflow 4 --contributors claude,codex,gemini --convergence majority`).

3. **Workflow engine**: at iteration start, the engine reads config and dispatches per operator's choices. Existing `reviewer.dispatch_codex` / `reviewer.dispatch_gemini` become pluggable adapters under the engine.

## Configurability dimensions (the operator must be able to pick each)

| Dimension | Options to support |
|---|---|
| **Workflow mode** | `#3` (post-review: claude implements, others audit) \| `#4` (propose-converge: blind-first proposals + reveal-and-converge) \| `advisory` (proposals are informational; claude decides) |
| **Contributor pool** | enabled set: any subset of {claude, codex, gemini}; minimum 1 (claude alone allowed) |
| **Independence model** | `blind-first-reveal` (canonical workflow #4) \| `visible-from-start` (all contributors see all inputs) \| `sequential` (1st proposes, 2nd critiques, 3rd synthesizes) |
| **Convergence rule** | `unanimous` (all enabled must agree) \| `strict-majority` (>N/2) \| `inclusive-majority` (≥N/2, ties pass) \| `advisory` (claude decides regardless of others) |
| **Finding disposition granularity** | `all-or-nothing` (current: any blocking → reject whole packet) \| `per-finding` (claude accepts/rejects/defers each finding individually, sub-converges per finding) |
| **Snapshot trigger** | `manual-only` \| `on-iteration-close` (auto-snapshot when iteration seals) \| `periodic` (every N iterations or every M minutes) |
| **Patch authoring permissions** | `claude-only` (default — only claude emits `patch_proposal`) \| `any-contributor` (codex/gemini also authorized) \| `none` (no contributor proposes patches; claude implements solo) |
| **Init-wizard interactivity** | `interactive` (prompt for each dimension) \| `non-interactive` (all flags supplied, no prompts) \| `mixed` (operator can `--accept-defaults` and override specific flags) |

## Your design proposal — required output structure

Emit ONE finding only. severity: low (design + scoping).

The `recommendation` field MUST contain a structured design proposal covering:

**Section A — `consensus init` CLI surface**
- Subcommand structure (`init`, `init --reconfigure`, etc.)
- Flag set (one per configurability dimension above, plus any you propose)
- Interactive prompt flow (what questions, in what order, with what default highlights)
- Non-interactive invocation form (full one-liner)
- Idempotency semantics (re-running `init` on an existing `.consensus/config.yaml`)

**Section B — `.consensus/config.yaml` schema**
- Top-level keys + types
- Per-dimension key paths and value types
- Versioning (`schema_version: N`)
- Defaults document (what each dimension defaults to when omitted)
- Validation rules (which combinations are illegal, e.g., `workflow: 4` + `contributors: [claude]` only)

**Section C — Workflow engine integration**
- Where in the codebase the engine lives (new module, refactor of existing)
- How the engine reads config at iteration start
- How adapters (codex, gemini, future) are dispatched per config
- Backward compat with existing iter-0001..0014 (most lack `.consensus/config.yaml`)
- New artifact types (e.g., `claude-proposal.yaml` for workflow #4 blind phase) and their lifecycle

**Section D — Migration plan**
- Existing repos without `.consensus/config.yaml`: assume what default? Generate one? Refuse new iterations until init runs?
- Renaming: `reviewer.dispatch_codex` is review-vocabulary; workflow #4 needs "contributor" or "proposer" vocabulary. Do we rename, alias, or keep the legacy name forever?
- Tests: how to test the workflow engine without dispatching real codex/gemini

**Section E — Open questions you flag**
- Anything you'd want the maintainer (or other contributors) to weigh in on before implementation
- Edge cases that may need further specification

## Output format

Standard codex/gemini review output:

```json
{
  "findings": [
    {
      "id": "<prefix>-rev-001",
      "severity": "low",
      "summary": "Design proposal for iter-0015 consensus init wizard + config schema",
      "citation": "docs/design-consults/iter-0015-consensus-init-design-challenge.md:0",
      "risk": "<3-5 sentence rationale for the highest-impact design choices>",
      "recommendation": "<Sections A through E, as a single long string>",
      "patch_proposal": null,
      "patch_not_proposed_reason": null
    }
  ],
  "goal_satisfied": true,
  "goal_satisfied_rationale": "<one-sentence why this design meets the goal>",
  "blocking_objections": []
}
```

You may use Markdown formatting inside the `recommendation` string. Do NOT propose any code changes (no `patch_proposal`). Iter-0016+ implements after consensus converges.

## Reminder

You are ONE of THREE independent contributors. The other two are also writing their own proposals against THIS SAME problem statement, without seeing your work. After all three seal, the proposals will be revealed for the convergence phase. Be creative and complete in this round — your proposal is your unique contribution, not a critique of someone else's.
