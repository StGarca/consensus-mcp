# Consult: scope a per-run RESULT-LOGGING feature for consensus-mcp

You are one of several AI contributors in a Workflow A (propose-converge) consult.
PROPOSE the design for a new consensus-mcp capability: a machine-readable record
of the RESULTS of every consensus run, so that all findings — every run — can be
aggregated at the project level into a trustworthy scorecard.

## Why (the problem, demonstrated)
consensus-mcp seals every review pass with provenance (great for audit), but it
does NOT record run RESULTS in a consistent, queryable form. Evidence: across
three real projects, aggregating "bugs found / fixed / validated" failed —
- findings live as structured review items (with `severity`) in sealed passes,
  AND as synthesized items in converged-plans, AND as PROSE in iteration
  summaries — three incompatible shapes;
- design-consult/proposal outputs pollute naive `findings` counts;
- code corrections ("fixes applied") are NOT logged as events anywhere
  (apply-event scan returned 0 in all three projects);
- different projects/eras use different formats, so one extractor can't count
  them all (one project was undercounted 4 vs >=88 passes).
Net: the history exists but is not aggregatable. There is no per-run results
record and no project-level rollup.

## Verified constraints (must hold)
- **Project-specific:** each project already has its own `consensus-state/`
  (confirmed across 3 projects). The results record lives there, per project —
  NOT global, NOT cross-project-merged on disk.
- **Gitignored:** verified `.gitignore` already excludes the run artifacts —
  `consensus-state/active/iteration-*/`, `consensus-state/state/*`,
  `consensus-state/archive/review-passes/*.yaml`. Only `archive/index.yaml` +
  test fixtures are tracked. The new results record MUST be gitignored too
  (project-local, uncommitted) — match this pattern; do NOT commit run results
  into the project's git history.

## Current relevant surfaces (for your design)
- Sealed review passes already carry: `findings[]` (each: id, severity, summary,
  citation, risk, recommendation), `blocking_objections`, `goal_satisfied`,
  `dispatch_provenance`, `iteration_id`, `reviewer_id`, `pass_id`, `sealed_at_utc`.
- `tools/audit_append_event.py` appends sealed audit events (incl. apply/closure
  events) to an iteration's `independence-audit.yaml` — the natural hook for
  logging "fix applied" + disposition events.
- `converged-plan.yaml` synthesizes per-iteration decisions/dispositions.
- `_snapshot_state.py` snapshots `consensus-state/` to an orphan branch.

## Design questions to answer in your proposal
1. **Schema** of the per-run / per-iteration results record: what fields capture
   findings raised (by severity), each finding's DISPOSITION
   (validated/accepted-and-fixed vs dismissed/refuted-with-evidence vs deferred),
   fixes/corrections applied, convergence outcome, reviewers + families, dates.
2. **Where it lives** (path under `consensus-state/`) and confirmation it's
   gitignored + project-local.
3. **How it's written**: at seal time / close time, via which existing hook
   (audit_append_event? a new emitter?) so it's automatic + fail-closed, not
   manual prose.
4. **Project-level aggregation**: a rollup (tool/CLI, e.g. `consensus results`)
   that reads the per-run records and emits a project scorecard
   (total findings by severity, validated vs dismissed, fixes applied,
   iterations, convergence rate). Machine-readable + human summary.
5. **Disposition tracking**: how a finding's outcome (real-and-fixed vs
   refuted-with-evidence) gets recorded — this is the "validated/invalidated"
   signal that's currently prose-only.
6. **Backfill**: do we backfill existing history (best-effort) or only log going
   forward? Argue it.
7. **Cross-format**: how to avoid the current trap where different eras/projects
   are uncountable — versioned schema.

Keep it minimal, fail-closed, and consistent with the existing sealed-provenance
model. Put the design in your proposal's deliverable_scope (files to touch, key
decisions, acceptance gates, risks).
