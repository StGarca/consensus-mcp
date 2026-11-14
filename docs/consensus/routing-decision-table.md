# Consensus routing & tiers - decision table (B4)

Canonical, single-place reference for **which workflow/tier a change gets**, codified
from the two 2026-05 consults (`sp-consensus-optimization` tiers + `weighted-consensus`
weighting). The doctrine is scoped by **workflow mode**, never by contributor count.

Implemented by: `consensus_mcp/_tier_router.py` (classify + cost estimate + gate-lock),
`consensus_mcp/_contributor_weights.py` (advisory weights), `consensus_mcp/_outcome_ledger.py`
(external credit), `consensus_mcp/_iteration_telemetry.py` (cost/outcome), and
`consensus_mcp/validators/validate_interaction_surface.py` (interaction-surface check).

## 1. Routing rule (top-down, first match wins)

| # | Trigger | Workflow | Path | Panel | Rounds |
|---|---------|----------|------|-------|--------|
| R0 | Trivial: typo, doc format, single log line, version bump | none | - | - | - |
| R1 | Pure execution of an already-converged plan | A consult when requested | B | all enabled, minimum 2 independent | 1 |
| R2 | Hot-patch for a single blocking failure-mode | A consult when requested | B | all enabled, minimum 2 independent | 1 |
| R3 | Real design surface **or** cross-cutting/interaction surface **or** security/data-loss/irreversible | A (propose-converge) | B (A only for genuine multi-round host re-convergence) | all enabled, minimum 2 independent | 1, multi only on split/objection |

R2 escalates to R3 if it touches the **interaction surface** (hooks/gates/dispatch/sealing/distribution).
"Listing 2+ design choices in one response" => R3.

## 2. Tiers (presets over the routing knobs)

| Tier | Use | Workflow / Panel / Path | Pre-commit cost band |
|------|-----|-------------------------|----------------------|
| **quick** | bounded work | A / all enabled / B | low compute; ~1 round |
| **standard** | multi-file bounded feature | A / all enabled / B | medium compute; ~1 round |
| **deep** | architectural / governance / security / irreversible | A / all enabled / A | high compute; ~2 rounds |

The cost estimate is **shown before any dispatch** (`_tier_router.estimate_cost`), as a
band - never a false-precision token promise. The engine requires an explicit
operator-declared tier; suggestions are advisory. The standalone
`consensus-mcp-consult` defaults to standard, uses exactly two quick seats, and
adds available `opencode-deepseek` for deep; see the hosting runbook.

## 3. Gate-consistency hard rule (non-negotiable)

Tiers set **how much rigor above the floor**, never **whether the cross-family gate
applies**. A change touching governance machinery (`.consensus` config / hooks / gates /
dispatchers / the engine) or that is security/irreversible is **auto-upgraded to DEEP and
LOCKED** - the operator may upgrade further but can **never downgrade** it
(`_tier_router.is_downgrade_allowed` refuses it). The host AI is the approver by
design in weighted-synthesis / proposal mode;
the independent-model evidence floor holds regardless of tier or weights.

## 4. Conditional brainstorming

`brainstorming` is **not** default-on. Run it only when the *problem* (not the solution)
is genuinely ambiguous - a novel feature, an unclear ask, or 2+ rival framings. For a
bounded change with a clear problem, route straight to the consult. Any contributor that
surfaces 2+ rival framings re-opens brainstorming.

## 5. Advisory weights (do not gate)

Contributor weights are **advisory**: they only re-ORDER proposals/findings for synthesis
reading-prominence (`order_proposal_paths`, a stable permutation). They never feed the
host approval or the independent-model gate - **weights-off equivalence**: delete the weights
and the gate's pass-set is byte-identical. Weights are **learned** per-(contributor, domain)
from an **external** outcome ledger (objective test/falsification = gold; audited operator
disposition = secondary; commit-landing never standalone; **no AI writes its own/peer
credit**), discount-only, with a hard floor, decay, and min-sample cold-start. The static
"codex/kimi high, gemini/host low" seed was **rejected** - the evidence didn't support it.

## 6. Robustness guard (the cheap, causal one)

For any change touching the interaction surface: declare `interaction_surface` in the
goal_packet (path heuristic flags a reflexive "none"), and run the **governed-project
integration smoke** (init/--repair in a temp governed project) - an external, O(1) check
that catches the self-referential integration class (the v1.29.4 gate<->init miss) that no
amount of AI agreement caught. The host seals design approval with an
evidence-backed disposition table; the smoke checks behavior.

## 7. Dispositions and per-model outcomes

Collect blind reviews, then record each idea in `dispositions.yaml` as accepted
with reasons, rejected with refuting evidence, or deferred. `consensus-mcp-approve`
validates and seals the table with the host-authored plan. It checks model
results, not vendor identities or majority/unanimous votes. Failed reviewers do
not count against a change. `consensus results` / `consensus.results` expose
per-model host dispositions; those weak signals remain distinct from externally
verified gold outcomes. Gold requires evidence tied to the exact sealed idea and
model; accepting an idea does not make it gold.
