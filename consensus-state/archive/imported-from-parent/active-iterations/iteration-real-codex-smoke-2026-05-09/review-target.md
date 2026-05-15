# Smoke review target (2026-05-09)

This is a tiny ASCII-only review target for the first real-codex smoke of
`_dispatch_codex`. Codex reviews this content; the actual code-review value is
secondary to validating the helper's pipeline (subprocess + JSON parse + T6
seal + audit log).

## Context

- Helper: `scripts/agent_loop_mcp/_dispatch_codex.py`
- v1.10.2 (committed b5ad344d) hardened against 5 codex-review findings on v1.10.1
- v1.10.3 (uncommitted; this smoke run is verifying it) addresses 3 Windows-specific
  findings: PATHEXT/.cmd resolution via shutil.which; .ps1-vs-.cmd preference;
  binary-mode UTF-8 stdin to bypass Windows text-mode CRLF translation that was
  corrupting multibyte UTF-8 (specifically em-dash U+2014 in the prompt template).

## What you (codex) should review

There is nothing meaningful to review here; this is a pipeline smoke test. Emit a
single low-severity finding noting "smoke target; no code change to review" or
similar, plus `goal_satisfied: true`. The point is that the pipeline produces a
sealed `codex-review.yaml` end-to-end, not that the review content is useful.
