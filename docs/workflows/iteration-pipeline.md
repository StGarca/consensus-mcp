# Iteration pipeline: superpowers ↔ consensus (repeatable runbook)

A session-independent walkthrough of one iteration. The load-bearing detail lives
in the `consensus-workflow` skill; this is the linear checklist.

## Stages
1. **Brainstorm** (`consensus:brainstorming`) — explore intent → design.
2. **Consult = approval gate** — consensus is the approver. Author goal_packet +
   review-packet; run the panel (offer panel size 2/3/4 + framing anchored/open);
   converge (weighted-synthesis).
3. **Plan** (`consensus:writing-plans`) — TDD plan from the converged design.
4. **Implement** (`consensus:subagent-driven-development`) — implementer +
   spec-compliance review + code-quality review per task.
5. **Finish** (`consensus:finishing-a-development-branch`) — release cut.

## Choosing the consult path
- **Path B (`consensus.run_iteration`)** — execution / post-review / clear-design /
  hot-patches. Engine dispatches codex/gemini/kimi + claude/host_peer callbacks.
- **Path A (orchestrator-driven)** — high-design-surface propose-converge needing
  genuine multi-round host re-convergence. host_peer first-class in both.

## host_peer
Dispatch a blind Claude subagent (fresh context, no peer artifacts) with the
host_peer review template (`host_peer_review_template.md`, in the package's
`dispatch_templates/`); capture `findings/goal_satisfied/blocking_objections`
YAML; feed `run_iteration` (Path B) or seal it (Path A). One-shot.

## Release-currency (after tag + GitHub Release + main FF)
1. `pipx install --force git+…@vX.Y.Z` (global install is a non-editable COPY).
2. `consensus-init --install-claude-code --force` (refresh ~/.claude).
3. Smoke the INSTALLED binary and assert version == vX.Y.Z.

## Caveats
- `.consensus` enforcement gate is project-scoped (inactive without
  `.consensus/config.yaml`).
- Don't mutate the repo during a 4-AI `run_iteration` run (kimi integrity check).
- Known follow-up: per-round host re-convergence in Path B needs a `run_iteration`
  pause/resume API (static-echo limitation).
