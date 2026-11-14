# Path A: synthesizing a plan

For weighted-synthesis / proposal consults, the host AI is the approver by
design. Collect N independent blind reviews before synthesizing; the result
is one host-authored plan, not a majority vote over bundled proposals.

1. Scaffold with `consensus-mcp-start-consult --question "<question>" --scope-glob '<scope>'`.
2. Finalize the goal and review packet before dispatch, then run
   `consensus-mcp-consult --iteration <iteration> --tier standard`.
3. Read the sealed reviews and `consult-summary.yaml`. Failed reviewers do not
   count against the change; approval still requires two distinct non-Claude
   model results. Family means model, not vendor.
4. Author `converged-plan.yaml`. Run
   `consensus-mcp-approve --iteration <iteration> --init-dispositions`, then fill
   every idea's disposition: accepted with reasons, rejected with refuting
   evidence, or deferred with rationale.
5. Run `consensus-mcp-approve --iteration <iteration> --scope-glob '<scope>'`.
   It validates and binds the disposition table's hash into the sealed outcome.
   Enforcing governance modes additionally mint the scoped edit marker.

The autonomous `consensus.run_iteration` engine cannot author a synthesized
host plan. A goal declaring `convergence: { requires_synthesis: true }` routes
here. Further rounds are useful only when new evidence or unresolved design
questions require them; review disagreement alone does not decide host approval.
