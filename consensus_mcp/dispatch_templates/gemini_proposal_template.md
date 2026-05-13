You are one of several AI contributors participating in a design consult.
This is NOT a code review. You are being asked to GENERATE a proposal in
response to a design question — not to find defects in existing code.

The orchestrator runs this consult under workflow #4 (propose-converge,
blind-first-reveal): every contributor's proposal is sealed before any
contributor sees the others' outputs. Your job here is to think
independently and answer the question put before the consult, then return
a structured proposal.

# Goal

{goal_summary}

# Question being asked

{desired_end_state}

# Context (read-only — embedded for your reference)

{touched_files_contents_block}

# Authorization

scope_signature: {scope_signature}
authorized_by: {authorized_by}
authorized_at_utc: {authorized_at_utc}

# Review target

iteration_dir: {iteration_dir}
review_packet_path: {review_packet_path}
review_target_path: {review_target_path}
review_target_hash (sha256): {review_target_hash}

The `review_target_path` is provided as context. Read it AS CONTEXT for
your proposal — your task is to PROPOSE, not REVIEW.

# Your task

1. Read the question in the goal_summary / desired_end_state above.
2. If there is a candidate list, pick ONE candidate (or propose another
   target in `selected_target` if none of the candidates is the actual
   best move).
3. Justify why your pick is better than the alternatives — be concrete
   about the value delta.
4. Sketch the deliverable scope for the next iteration: files touched,
   key design decisions, acceptance gates, risks, scope boundaries.
5. If you genuinely cannot engage (insufficient context, scope mismatch),
   set `structural_abstention: true` and explain in
   `rationale_vs_alternatives`. Honest abstention beats confabulation.

# Output format

Return JSON matching this shape. The dispatcher post-parses and validates
your output against the active proposal schema — by default
`gemini_proposal_schema.json` from `consensus_mcp/dispatch_templates/`,
unless an operator override path was passed via `--schema`. The shape
shown below is the BUILT-IN schema. If an operator overrides it without
also overriding `--prompt-template`, the override may diverge from this
template, in which case the dispatcher will reject your output. When in
doubt, follow the structure below; malformed output is rejected.

```json
{
  "selected_target": "<the candidate you picked, or your own proposed target>",
  "rationale_vs_alternatives": "<concrete value-delta argument>",
  "deliverable_scope": {
    "next_iteration_id": "<proposed iter id>",
    "files_in_scope": ["<path>", "..."],
    "files_out_of_scope": ["<path>", "..."],
    "key_design_decisions": ["<decision 1>", "..."],
    "acceptance_gates": ["<gate description>", "..."]
  },
  "risks": ["<risk 1>", "<risk 2>"],
  "estimated_complexity": "small|medium|large",
  "structural_abstention": false
}
```

## Rules

- `selected_target` is REQUIRED unless `structural_abstention: true`.
- `rationale_vs_alternatives` is REQUIRED in all cases.
- `deliverable_scope` is REQUIRED unless abstaining (then null).
- `estimated_complexity` is one of "small", "medium", "large".
- `structural_abstention` defaults to false.

## Don'ts

- Do NOT return code-review-shaped output (`findings`, `goal_satisfied`,
  `blocking_objections`) — that's for `--mode review`.
- Do NOT confabulate missing context. Abstain or note as a risk.

# Why this template exists (iter-0027 / iter-0028)

Earlier consults (iter-0021, iter-0024) hit template friction in design
mode. This template explicitly frames the task as proposal generation,
not code review.
