# v1.30.6 - synthesis-aware propose-converge (design)

Date: 2026-05-24
Status: approved (brainstorming) - pending spec review -> writing-plans -> TDD build
Source spec: `ebook2audiobook/consensus-state/CONSENSUS-MCP-BUGFIX-SPEC-1.30.6-synthesis-step.md`
(panel-found: codex-rev-001 + gemini-rev-001, r6, unanimous)

## Problem (verified against the engine)

A propose-converge (Workflow A) consult whose deliverable is a single *synthesized* artifact
(a plan) can never converge. `workflow_engine._run_workflow_4` (verified `:269-313`):

- `proposal_paths = [proposals]`; each round `_build_convergence_packet(proposal_paths)` bundles
  them into `defect_target.touched_files_contents`; on no-converge
  `proposal_paths.extend(convergence_artifacts)` - the review target GROWS into a pile of
  proposals + every prior round's converge artifacts.
- `_build_convergence_packet` (`:386-395`) makes the review target that bundle.
- **There is no step that merges the proposals into ONE canonical plan.** Contributors are
  asked "is the goal (one converged plan) satisfied?" while looking at a pile - they correctly
  answer no. Structural, not a model issue.

This is the last blocker after the 1.30.1-1.30.5 stack (1.30.5 fixed the `:393` embed so a
review-target's content is actually visible; this fixes *what* the target should be).

## Decision

Synthesis - merging N proposals into one plan, then revising it against each round's findings -
is inherently a **host/orchestrator judgment act**. The autonomous engine (Path B,
`run_iteration`) dispatches subprocess contributors with **no host in the loop**, so it must not
pretend to synthesize. Therefore:

- **Path B fails loud** for a synthesis-deliverable consult (no silent never-converge).
- **Plan/synthesis consults converge via Path A** (host-driven): the host authors/revises ONE
  synthesized plan and dispatches contributors to review *that plan* each round.

(Chosen over: a per-round synthesis callback bolted onto the autonomous engine - high surface,
puts judgment in the wrong place. And over a heuristic "looks like a plan" trigger - violates the
operator-declared-never-inferred doctrine.)

## Design

### 1. Detection - operator-declared, never inferred
A goal_packet field declares that convergence requires a single merged artifact:
```yaml
convergence:
  requires_synthesis: true   # deliverable is ONE merged plan, not agreement on the proposals
```
Absent/false -> today's behavior (consensus-style bundle-vote), unchanged. This matches the
tier-router doctrine (operator-DECLARED rigor; heuristics are a shared-prior trap).

### 2. Path B guard (`_run_workflow_4`)
When `convergence.requires_synthesis` is true, raise a clear `WorkflowError` at entry -
before the bundle-vote loop - naming the cause and pointing at the Path A flow:
> "this consult declares convergence.requires_synthesis: the deliverable is a single
> synthesized plan, which the autonomous engine cannot author (no host in the loop). Run it
> via the Path A flow: author converged-plan.yaml, then converge with
> evaluate_plan_convergence (see docs)."

**Undeclared safety net:** the existing "convergence not reached after N rounds" error gains a
hint - "if the deliverable is a single synthesized artifact, the bundle-vote cannot converge;
set convergence.requires_synthesis and use Path A." No behavior change otherwise.

### 3. Path A flow (the supported plan-convergence path) - a thin helper, not a new loop
The host drives the rounds; the only new code is reusable evaluation + plan-seal:

1. Host authors ONE candidate `converged-plan.yaml` (the synthesized plan - resolves each goal
   question; includes the feasibility matrix + concrete DoD the goal_packet requires).
2. Host dispatches each contributor to review THAT plan: `--review-target converged-plan.yaml`
   (the v1.30.5 `:393` path embeds the plan so the sandbox sees it). Blind first round.
3. **New helper `evaluate_plan_convergence(review_artifacts, ...)`** - reuses
   `_evaluate_convergence` to compute strict-majority-approve / no-blocking over the sealed
   review verdicts on the plan.
4. On block: the host REVISES `converged-plan.yaml` (folds in the round's findings) and
   re-dispatches; the next round's review target is the *revised* plan.
5. On converge: **seal the plan file itself** as the artifact (reuse `_seal_converged_plan`
   semantics) - NOT a `defect_target` bundle.

The helper keeps the flow repeatable (not hand-rolled per consult) without adding an autonomous
multi-round loop - the host's judgment stays in the loop between rounds, which is the point.

### 4. Acceptance test
- **Path B guard:** a propose-converge goal_packet with `convergence.requires_synthesis: true`
  through `run_iteration` raises the clear `WorkflowError` (assert it does NOT enter the
  bundle-vote loop / does not silently never-converge).
- **Path A converge:** given contributor review verdicts on a single plan file -
  - a clean set (strict-majority approve, no blocking) -> converged, and the sealed artifact is
    THE PLAN FILE (assert it's the plan, not a `defect_target` proposal bundle);
  - a blocked set -> not converged, and the next round's review target is the REVISED plan
    (assert the target content changed between rounds), not a re-bundle.

## Scope
- `consensus_mcp/workflow_engine.py` - the Path B guard + the max-rounds hint.
- A small Path A helper - `evaluate_plan_convergence(...)` + a plan-seal wrapper (new function;
  module TBD in the plan, likely alongside the engine).
- goal_packet schema - the `convergence.requires_synthesis` field (+ validation).
- docs - the Path A plan-convergence flow.
- tests - the acceptance test above.

Out of scope (untouched): `_design_approval.py`, the PreToolUse gate, the dispatchers, the
autonomous-execute path.

## Then (the payoff - separate from this build)
Re-run the emotion-engine consult as Path A: the enriched `CONVERGED-PLAN.md` already IS the
synthesized plan (codex+gemini r6 feedback folded in) -> review-of-the-plan -> converge -> seal ->
mint `.consensus/design-approved` -> `writing-plans` -> the real work.
