# iter-0005 verdict qualifier - incomplete premise

## What

iter-0005-main-spec-scrub-design-consult's sealed codex review (`codex-iter0005-1-pass1`, sealed `2026-05-12T03:25:22Z`, packet_sha256 `ac8a82eb8549e2add718d3937d84488ec526b09b834798927886ac874f6eca74`) carries `recommendation: A` and `goal_satisfied: true`. The recommendation aligned with the maintainer's eventual choice and the action was carried out. **However, the sealed packet's premise is incomplete:** codex never saw the design document text it was nominally reviewing.

## Why

The dispatch was invoked with:

```
--review-target docs/design-consults/main-spec-scrub-stale-archive-refs.md
```

Per `_dispatch_codex.py:1746-1762`, the dispatcher only parses `touched_files_contents` into the prompt when `--review-target` points at a `.yaml`. A raw `.md` is silently treated as opaque review-target text - its `path` and `sha256` are computed and substituted into the prompt, but the `{touched_files_contents_block}` substitution receives an empty dict.

Codex thus received the prompt template with:
- `goal_packet.desired_end_state` (which contains substantial context about the scrub proposal)
- `review_target_path` and `review_target_hash` (the .md path and its hash)
- **An empty `# Touched-file contents` block** - no actual design doc text

Codex's verdict was a plausible inference from `desired_end_state` alone. The fact that it landed on Option A is consistent with the goal_packet's framing, not evidence of cross-AI review of the canonical design document.

## How we know

Compare `dispatch_provenance.review_target_path` across the iter-0005 -> iter-0007 series:

| Pass | review_target_path | touched-files-block populated? | Verdict source |
|---|---|---|---|
| codex-iter0005-1-pass1 | `docs/design-consults/main-spec-scrub-stale-archive-refs.md` | NO (.md path) | goal_packet.desired_end_state only |
| codex-iter0006-1-pass1 | `docs/design-consults/multi-project-resolution-v1.13.0.md` | NO (.md path) | rejected as unverifiable |
| codex-iter0006-3-pass1 | `docs/design-consults/multi-project-resolution-v1.13.0.md` | NO (.md path) | rejected as unverifiable |
| codex-iter0006-4-pass1 | `consensus-state/active/iteration-0006-.../review-packet.yaml` | YES (.yaml path) | full design doc reviewed |
| codex-iter0007-1-pass1 | `consensus-state/active/iteration-0007-.../review-packet.yaml` | YES (.yaml path) | full design doc reviewed |

iter-0005-1's `.md` review_target_path proves the same code path was hit. Either codex made a lucky guess from `desired_end_state` (most likely), or it never read past the path/hash anyway.

## What this changes operationally

- **The iter-0005 scrub decision stands.** The maintainer ratified the scrub independently; the cross-AI review was not load-bearing on the outcome.
- **The sealed packet's audit value is downgraded.** Future audits of "was this change peer-reviewed cross-family?" must treat `codex-iter0005-1-pass1` as "reasoned from goal_packet alone," NOT as "codex reviewed the canonical design doc."
- **No retroactive sealed update.** The packet is immutable (cryptographically sealed). This qualifier doc serves as the operator-facing note instead.

## Going forward

See `docs/operations/dispatching-codex-correctly.md` for the prevention rule. v1.14.0 F3 (dispatcher auto-discovery of `review-packet.yaml`) closes this failure mode at the code level.
