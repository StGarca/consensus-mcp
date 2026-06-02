# Dispatching codex correctly

Operational guide for invoking `consensus_mcp._dispatch_codex` so codex actually reviews what you think you're sending. Two non-obvious gotchas surfaced in iter-0005..0007 (sealed packets `ac8a82eb...`, `1e1eb592...`, `39f8b7a8...`).

## Gotcha 1 - `--review-target` MUST point at the review-packet `.yaml`, not the raw artifact

The dispatcher only loads `touched_files_contents` into codex's prompt when the file passed via `--review-target` parses as YAML (`_dispatch_codex.py:1756`). If you pass a raw `.md` design doc, a `.diff`, or any non-YAML file, the prompt's `{touched_files_contents_block}` ends up empty.

What codex does in that case:
- **Hallucinate plausibly from the goal_packet's `desired_end_state`** and return a sealed verdict that *looks* legitimate (this happened in iter-0005 - the recommendation aligned with reality by lucky inference, but codex never saw the canonical document).
- Or honestly report "review target content not embedded" and refuse to verdict (iter-0006-1, iter-0006-3).

**The correct invocation** is `--review-target <iteration_dir>/review-packet.yaml`. The review-packet is authored by `_author_review_packet` with the touched file contents embedded; pass it as the review target so the dispatcher parses it and substitutes the contents block.

Quick example:

```bash
# 1. Author the review-packet (embeds touched-file contents)
py -3.11 -m consensus_mcp._author_review_packet \
    --iteration-dir consensus-state/active/iteration-NNNN-<name> \
    --files path/to/design.md

# 2. Dispatch codex, pointing --review-target at the .yaml packet
py -3.11 -m consensus_mcp._dispatch_codex \
    --goal-packet consensus-state/active/iteration-NNNN-<name>/goal_packet.yaml \
    --iteration-dir consensus-state/active/iteration-NNNN-<name> \
    --reviewer-id codex-iterNNNN-1 \
    --review-target consensus-state/active/iteration-NNNN-<name>/review-packet.yaml
#                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#                   THE .yaml - NOT the .md you also passed to _author_review_packet
```

**How to spot bad invocations after the fact:** open the sealed `codex-review.yaml` and check `dispatch_provenance.review_target_path`. If it ends in `.md`, `.diff`, or `.patch`, the verdict's premise was incomplete - codex didn't see the touched files. Don't trust the recommendation.

v1.14.0 plans to add auto-discovery of `<iteration_dir>/review-packet.yaml` when `--review-target` is non-yaml or absent (see `docs/roadmap/v1.14.0-deferred.md`).

## Gotcha 2 - `stall_silence_seconds: 45.0` is too aggressive for cold-start on bigger prompts

The default 45-second silence threshold (`_dispatch_codex.py:613`) starts ticking from process launch. For bigger review-packets, codex-cli's pre-first-token LLM warmup can exceed 45s - the watchdog kills the process with `last_streamed_line_seq: null` (zero output yet emitted), and the dispatcher returns "codex stuck."

iter-0006-2 was killed at 45s with zero bytes streamed. Bumping to 300s lets larger prompts complete; iter-0006-4 and iter-0007-1 then succeeded cleanly.

**Workaround until v1.14.0**: temporarily edit `_dispatch_codex.py:613`:

```python
    stall_silence_seconds: float = 45.0,   # change to 300.0 for big-prompt dispatches
```

Revert after the dispatch returns. (No env var or CLI flag exposes this in v1.13.0 - splitting into pre/post-first-byte thresholds with operator overrides is v1.14.0 F4.)

**How to spot this failure mode**: search `consensus-state/state/dispatch-log.jsonl` for `dispatch_aborted` events where `abort_source: watchdog_silence` and `last_streamed_line_age_seconds: null`. That means codex hadn't emitted a single byte yet - it was loading, not stalled. Bump the threshold and retry.

## Gotcha 3 - sealed verdicts with incomplete premise

Combining Gotchas 1+2: a sealed packet can carry `goal_satisfied: true` and a recommendation that *looks* fine while the prompt that produced it had `{touched_files_contents_block}` empty. The seal proves codex was invoked with the prompt hash - not that the prompt contained what you intended.

When auditing past iterations:

1. Check `dispatch_provenance.review_target_path` - must end in `.yaml` for the touched-files block to have populated.
2. If `.md` or other: treat the verdict as "reasoned from goal_packet alone," not as cross-AI review of the artifact.
3. Add a `verdict_qualifier` to the iteration-outcome if you spot one retroactively. See `docs/operations/iter-0005-verdict-qualifier.md` for the iter-0005 example.
