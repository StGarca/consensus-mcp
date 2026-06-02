# Path A: converging on a synthesized plan

A propose-converge consult whose deliverable is ONE merged plan cannot converge in the
autonomous engine (Path B / `run_iteration`) - there is no host in the loop to author/revise
the plan, so the engine would only bundle proposals and vote on the pile. Declare it and use
Path A:

1. In the goal_packet: `convergence: { requires_synthesis: true }`. (Path B now fails loud
   pointing here.)
2. Host authors ONE `converged-plan.yaml` in the iteration dir (the real plan: resolves each
   goal question; includes the feasibility matrix + concrete DoD the goal_packet requires).
3. Dispatch each contributor to review THAT plan (blind round 1):
   `consensus-mcp-dispatch-<codex|gemini|kimi> --goal-packet <gp> --iteration-dir <iter>
   --reviewer-id <id> --pass-id <id>-pass1 --mode review --review-target <iter>/converged-plan.yaml`
   (the v1.30.5 review_target_content embed makes the plan visible to the sandbox).
4. Load the sealed review artifacts and call
   `engine.evaluate_plan_convergence(review_artifacts, outcome)`.
5. If `conv.converged`: `engine.seal_plan_iteration(iter, plan_path, conv, round_number)` ->
   writes `iteration-outcome.yaml` (sealed closing_state). With the plan + >=2 cross-family
   review YAMLs present, `mint_design_approval` can now point at this iteration.
6. If not converged: REVISE `converged-plan.yaml` (fold in the round's findings) and
   re-dispatch - the next round's review target is the REVISED plan.
