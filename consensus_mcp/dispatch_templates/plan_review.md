Review the host's converged plan, not a new ideas proposal. Check the complete
plan against the goal, every idea disposition, and earlier plan objections and
host responses. Pay particular attention to rejected and deferred ideas and the
host's recorded evidence. Raise an objection if the host rejected, deferred, or
accepted an idea wrongly, introduced a conflict, or left a gap.

Use the existing reviewer output schema without adding fields. Prefix each
objection's existing summary with exactly one tag: [wrong_rejection],
[wrong_deferral], [wrong_acceptance], [conflict], [gap], or [other]. Put every
objection in findings. If no objection remains, return no findings and
goal_satisfied: true. Reconsider earlier rebuttals explicitly; repeat any
objection that remains open. Failed seats do not count against the plan.

The host may change the plan to accept an objection, but may never overrule a
reviewer objection by itself. The user decides unresolved holdouts. Do not
implement changes. Review the exact converged_plan_sha256 in this packet.
