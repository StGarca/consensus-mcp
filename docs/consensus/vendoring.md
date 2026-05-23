# Vendoring Superpowers Skills into consensus-mcp

consensus-mcp ships a self-contained workflow that requires **no Superpowers
prerequisite**. To do that, it vendors (copies + adapts) ten skills from the
[Superpowers](https://github.com/obra/superpowers) project (MIT, (c) 2025 Jesse
Vincent), pinned at **v5.1.0 @ commit `f2cbfbe`**.

This is a **snapshot, not a live fork**. The vendored copies live under
`consensus_mcp/claude_extensions/skills/consensus-<name>/SKILL.md` and are
installed onto a machine via the existing
`consensus-init --install-claude-code` (`_install_claude_extensions`) path — the
same mechanism that installs the native `consensus` and `consensus-workflow`
skills. A fresh machine with **only consensus-mcp** installed therefore gets the
full design → plan → execute → review → verify → finish workflow.

Provenance for every vendored skill is recorded in
`consensus_mcp/claude_extensions/VENDORED.md`; the MIT license text is in
`consensus_mcp/claude_extensions/NOTICE`.

## The 10 vendored skills and their adaptations

Every vendored skill receives the same mechanical treatment:

- **MIT attribution** comment prepended to the top of each `SKILL.md`.
- **Frontmatter `name`** rewritten to `consensus-<name>`; description prefixed
  with `Consensus-adapted: `.
- **Skill cross-references** rewritten: every `superpowers:<x>` → `consensus:<x>`.
- **Save paths** repointed: `docs/superpowers/...` → `docs/consensus/...`.
- **Precedence header** added near the top of the body:
  `> Consensus has precedence at decision gates (see the consensus bootstrap / consensus-workflow).`

Six skills get **only** that mechanical adaptation (verbatim + ref-rewrite), to
keep the re-sync diff tiny. Four "spine" skills get additional consensus
hand-off logic.

### Spine skills (4) — consensus hand-off adaptations

| skill | consensus adaptation |
|-------|----------------------|
| **consensus-brainstorming** | Terminal "present the design and get USER approval" → **hand off to a consensus Workflow A consult**. The CONVERGED-PLAN is the approval — consensus is the approver, not the user. On convergence, mint the `.consensus/design-approved` marker (`iteration_id`, `scope_glob`, `converged_plan_sha256`, `sealed_at_utc`), then invoke `consensus:writing-plans`. (The marker is consumed by a PreToolUse hook built separately.) |
| **consensus-requesting-code-review** | Single-Claude reviewer-subagent dispatch → **consensus Workflow B**: dispatch cross-family reviewers (`reviewer_dispatch_codex`, plus gemini/kimi per panel size). The sealed cross-family audit IS the review, not a single-Claude pass. Git-range diff preparation kept. |
| **consensus-receiving-code-review** | "Evaluate review feedback" → **weigh the SEALED consensus panel findings on merit**; record any dismissal with empirical evidence (grep the cited content first). |
| **consensus-verification-before-completion** | Gate extended: before any completion claim, **mint/verify a delivery-readiness token** (`consensus_mcp/_delivery_readiness.py`) and run `gate_evaluate_production_with_scope_match`. That token is what the consensus Stop hook checks. The "evidence before claims" iron law is preserved. |

### Near-verbatim skills (6) — ref-rewrite + precedence header only

- **consensus-writing-plans**
- **consensus-executing-plans**
- **consensus-subagent-driven-development**
- **consensus-test-driven-development**
- **consensus-finishing-a-development-branch**
- **consensus-using-git-worktrees**

The complete per-skill ledger (source path, version, SHA, exact adaptation) is in
`consensus_mcp/claude_extensions/VENDORED.md`.

## Skills deliberately NOT vendored (SKIP set)

The convergence (4-way Workflow A: claude + codex + gemini + kimi) landed on a
**minimal 10-skill spine** — vendor only what completes
design → plan → execute → review → verify → finish. The following Superpowers
skills are deliberately skipped:

| skipped skill | rationale |
|---------------|-----------|
| **dispatching-parallel-agents** | consensus-mcp already has native cross-AI dispatch plus its own "maximize parallelism" doctrine. The Superpowers version is single-Claude sharding, which has no consensus-gate intersection. Recorded as an **optional** later-add (claude + codex saw it as arguably distinct; gemini + kimi voted skip). |
| **writing-skills** | Meta-documentation about authoring skills, not part of the workflow spine. No consensus gate touches it. (codex + gemini + kimi.) |
| **systematic-debugging** | No consensus-gate intersection. Marked **OPTIONAL phase-2** vendor (near-verbatim, not in the initial set). claude + codex wanted it; gemini + kimi voted skip — resolved toward the minimal spine, revisit if users ask. |

Also **write-own, not vendored**: `using-superpowers`. There is no vendored
bootstrap skill body. The existing `consensus` skill +
`consensus-init --install-claude-code` + the SessionStart precedence hook ARE the
bootstrap; the hook layer already does bootstrap/precedence, so no new bootstrap
skill file is needed.

## Coexistence with upstream Superpowers

The vendored skills use a distinct directory (`consensus-<name>/`) and distinct
skill IDs (`consensus:<name>`), orthogonal to upstream `superpowers:*`. Both can
be installed at once **without ID collision**.

When both are installed, the **SessionStart precedence hook wins**: it injects a
precedence mapping so the consensus-adapted behavior takes effect at decision
gates (brainstorming → Workflow A; requesting/receiving-code-review → Workflow B;
verification → sealed gate + delivery token; Edit/Write blocked until
`.consensus/design-approved`). The vendored brainstorming writes the
`.consensus/design-approved` marker the PreToolUse hook validates, and the
vendored verification mints the delivery token the Stop hook checks — the skills
do the consensus work, the hooks enforce it deterministically.

## Manual re-sync procedure

Re-sync is **manual and selective** — this is a pinned snapshot, not a live
fork. To re-sync against a newer Superpowers release:

1. **Check the pin.** `VENDORED.md` records the pinned version (5.1.0) and SHA
   (`f2cbfbe`). Compare against the upstream release you want to move to.
2. **Diff upstream.** For each of the 10 skills, diff the new upstream
   `SKILL.md` against the pinned source. Most changes will be in upstream prose;
   the six near-verbatim skills should re-apply with only the mechanical
   ref-rewrite + precedence header.
3. **Re-apply the adaptation contract** to any changed skill:
   - MIT attribution comment at top
   - `name: consensus-<name>` in frontmatter
   - every `superpowers:<x>` → `consensus:<x>` (grep to confirm ZERO residual
     `superpowers:` outside the attribution line)
   - `docs/superpowers/...` → `docs/consensus/...`
   - precedence header line
   - for the four spine skills, re-apply the hand-off adaptations from the table
     above (do not lose them in a re-sync).
4. **Update `VENDORED.md`** with the new version + SHA + any changed adaptation.
5. **Health check.** When installed Superpowers version != the pinned version,
   the SessionStart hook should warn (snapshot-drift risk). After re-sync, run
   the lightweight self-checks: each `SKILL.md` frontmatter parses as YAML, and
   `grep -c 'superpowers:'` returns 0 outside the attribution line for each file.

**Keep adaptation creep out of the re-sync diff:** non-spine edits must stay at
ref-rewrite + the one-line precedence header. Concentrate all behavioral edits on
the four spine skills so the other six remain a near-trivial re-sync.
