# Consult: verify the cross-project metrics + write a layman README section

You are one of several AI contributors in a Workflow A (propose-converge) consult.
Two deliverables in your proposal:

1. **VERIFY** the orchestrator's cross-project metrics methodology + counts below.
   State plainly whether you agree, and where you think the numbers are wrong or
   over/under-stated. You CANNOT re-scan the filesystem (you only have what's
   embedded here) — so review the METHOD (the extraction script) and the
   reported results for soundness, bias, and likely error, and say where you'd
   trust vs distrust each number.
2. **PROPOSE** a concise, layman-readable replacement for the README's "Does it
   actually work?" cross-project paragraph (constraints at the bottom).

## What was measured

The orchestrator (claude) used consensus-mcp on three separate codebases and
tried to aggregate the sealed run history into one scorecard. Each project has
its own `consensus-state/` directory (project-specific) whose run artifacts are
gitignored. The extractor deduped sealed passes by content hash and counted ONLY
structured review findings that carry a `severity` field (the codex/gemini/kimi
code-review schema), excluding design-consult/proposal items.

### Extraction method (the script that produced the numbers)

```python
# dedup passes by packet_sha256 or (reviewer_id,pass_id); count findings objects
# that HAVE a `severity` key, from sealed passes only (dispatch_provenance or
# reviewer_id+pass_id present); classify selected_target/proposal as proposal passes.
# apply/fix events counted from independence-audit.yaml audit_log where
# event in {apply_step_landed,patch_applied,codex_patch_apply,apply_consensus_patch}.
```

### Results (per project + total)

| Project | Iterations | Review passes | Bug findings (severitied) | Blocking/critical | fixes logged | Reliability (orchestrator's own call) |
|---|---|---|---|---|---|---|
| Project A (the tool itself) | 76 | 220 | 230 (5 critical, 55 blocking, 41 high, 82 medium, 47 low) | 60 | 0 | RELIABLE — modern sealed format; matches an independent count |
| Project B (firmware/hardware UI) | 15 | 46 | 60 (14 high, 17 medium, 26 blocking) | 26 | 0 | APPROXIMATE — single date, no converged-plans → older format |
| Project C (large ML pipeline) | 66 | 4 | 15 | 1 | 0 | UNDERCOUNT — its reviews use a non-sealed `*-out.yaml` format the pass-filter skips; true count is far higher (>=88 pass-like files) |
| TOTAL | 157 | 270 | 305 | 87 | 0 | FLOOR ONLY — dominated by Project A; not a clean aggregate |

### The orchestrator's own conclusions (verify or refute these)
- Project A's 230 findings / 60 blocking-or-critical is trustworthy (two independent counts agree).
- A single trustworthy cross-project total CANNOT be produced today: the three
  projects use different log formats/eras; `fixes_applied` is 0 everywhere
  because corrections are not logged as events; findings vs design-consult items
  vs prose summaries are mixed. So 305 is a floor, not a real number.
- Therefore the README must NOT lean on the cross-project total; it should rest on
  Project A's reliable numbers + an honest "used on other real codebases too"
  statement, without fabricated aggregates.

## Your task 1 — verdict on the metrics
Do you agree the methodology is sound and the conclusions hold? Where do the
counts likely deviate (over or under)? Is 230/60 for Project A defensible? Is it
honest to present 305 as a floor rather than a total? Name any methodology flaw
(dedup, severity-only filter, the `is_pass` filter that undercounts Project C,
fixes=0).

## Your task 2 — propose the README section
Write a concise (roughly 6-12 lines), plain-language "Does it actually work?"
section a non-expert understands. Put your proposed markdown in
`rationale_vs_alternatives` (or deliverable_scope), clearly delimited.

CONSTRAINTS (hard):
- Layman-readable; no jargon.
- Ground claims ONLY in defensible numbers (Project A's 230 findings / 60
  blocking across 76 iterations; the system is sealed/auditable; it's also used
  on other real, separate codebases). Do NOT cite the unreliable cross-project
  total or Project B/C bug counts as if precise.
- Do NOT name any project, and do NOT use project-specific terms (e.g. NO
  "emotion engine" or any feature codename), and do NOT use the words ebook,
  audiobook, or narration.
- It's fine to say the per-run results aren't yet aggregated into one number
  (honest), or to omit a cross-project number entirely — your call, argue it.
