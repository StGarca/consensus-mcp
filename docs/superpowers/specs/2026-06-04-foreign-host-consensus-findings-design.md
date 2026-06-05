# Foreign-Host Consensus Findings - Design

Date: 2026-06-04
Branch: `fix/dispatch-log-payload-cap`
Origin: Review of a Codex-hosted consult in a consuming project
(`external-project`), report `wiki/audiobook-project/reports/codex-host-consensus-findings-2026-06-04.md`.

## Context

A non-Claude AI (Codex) successfully hosted a full 5-contributor propose-converge
consult in a consuming project. The run itself was genuine (5/5 responsive,
converged strict-majority, sealed provenance, a substantive codex dissent). But
reviewing the actual run artifacts surfaced three issues. One was a real
consensus-mcp defect with an unambiguous fix (already shipped, below). Two are
workflow/onboarding gaps that this design takes to a consensus consult for
approval.

## Already shipped (Phase A - pure bug fix, no consult)

**Dispatch-log payload bloat.** A kimi workdir copytree failure raised a
`shutil.Error` whose `str()` embedded the entire copied-file manifest (~188 MB).
`_log_dispatch` wrote it verbatim, producing a single 188 MB JSON line and a
702 MB append-only `dispatch-log.jsonl` accreted across iterations.

Root fix at the one writer every adapter shares: `_log_dispatch`
(`_dispatch_base.py`) now caps each string field at `_MAX_DISPATCH_FIELD_CHARS`
(16 KB) with a `...[truncated N chars]` marker that preserves the original
length. TDD: `consensus_mcp/tests/test_dispatch_log_cap.py` (failing-first), then
the fix. 244 adapter-suite tests pass, no regressions.

Dropped as speculative: changing kimi's *copy behavior* on a non-ENOSPC copytree
error. The copytree fallback only triggers in a no-`.git`/clone-failed repo;
Claude-hosted consensus in this repo never hits it (it uses `git clone --local`).
The root log cap already neutralizes the real harm regardless of copy outcome.

## Proposed for the consult

### Component 1 - Embed the review target in the convergence packet

**Problem.** In propose-converge, `_build_convergence_packet`
(`workflow_engine.py:442`) bundles the per-contributor proposal YAMLs into
`defect_target.touched_files_contents`, but never reads or embeds the actual
**target document** under review. Converge-round reviewers therefore see every
proposal but not the file being edited, so they cannot author line-accurate
patches. In the field run all three codex findings were `patch_proposal: null`
with the reason "the actual Markdown is not embedded in this review target."

**Fix.** `_build_convergence_packet` also reads `target_path` and embeds its
content so reviewers see the real file. Size-guarded with the same
truncation-marker discipline as the log cap (large targets get an explicit
`...[truncated]` marker, never silent omission).

**Open questions for the panel.**
- Dedicated `review_target_document` key vs. folding the target into the
  existing `touched_files_contents` map.
- Does embedding the target change any independence/provenance property? (The
  target is already visible to reviewers in the propose round, so likely
  neutral - panel to confirm.)
- Truncation threshold for large targets.

### Component 2 - Official `consensus-mcp-run-iteration` CLI + foreign-host doc

**Problem.** Entry points exist (`consensus-mcp-start-consult` CLI, the
`consensus_run_iteration` MCP tool) but **no console script runs a full
iteration end-to-end**. A non-Claude host (Codex) hand-rolled
`run/_consensus_launch.py` calling `consensus_run_iteration.handle()` directly,
bypassing the supported surface.

**Fix.**
- New console script `consensus-mcp-run-iteration` (`consensus_mcp/_run_iteration_cli.py`)
  wrapping `handle()` with the args the shim proved out: `--iteration-dir
  --goal-packet --target --config --claude-proposal --repo-root --outcome`.
- A docs page "Hosting a consult as a non-Claude AI" covering the writable-`$HOME`
  / unsandboxed-execution requirement that the field run documented.

**Open questions for the panel.**
- Should the CLI write a run-outcome JSON by default (the shim did)?
- Naming: `consensus-mcp-run-iteration` vs. another convention.

## Testing

- Component 1: failing-first test that a convergence packet built for a known
  target embeds the target's content (and truncates an oversized target with a
  marker).
- Component 2: failing-first test that the CLI invokes `handle()` with parsed
  args and writes the outcome JSON; console-script entry registered in
  `pyproject.toml`.

## Out of scope

- kimi copy-behavior change (covered above).
- Any versioning/release action (operator-controlled).
