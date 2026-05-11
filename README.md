# consensus-mcp

**Peer review for AI code, automated.** Two AI agents check each other's work before any code lands. Like the four-eyes principle at a software company, except both sets of eyes belong to LLMs — and they have to agree before the change ships.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)

---

## The problem

AI coding assistants are good. They're also confident liars. They hallucinate functions that don't exist, invent file shapes they never read, and skip past assumptions they should have stated. When a single AI reviews its own work, it has the same blind spots that caused the bug in the first place.

Cross-AI review fixes that. Different models trained on different data with different optimizers fail in different ways. When two of them disagree about what the code is doing, one of them is wrong — and that disagreement surfaces bugs that single-agent review reliably misses.

consensus-mcp is the infrastructure that makes that automatic.

## What it does (in plain English)

When you ask an AI to write code, fix a bug, or review a change, consensus-mcp:

1. **Captures the request as a sealed contract** — what's being changed, what files are touched, what success looks like, who authorized it. This is the `goal_packet`.
2. **Dispatches a second AI to review** — typically OpenAI Codex via its CLI. It produces a structured findings document with severity-graded defects, citations to specific file:line locations, and proposed patches.
3. **The first AI (typically Claude) reviews the reviewer** — verifies findings against the actual code, agrees, disagrees, or adds findings of its own.
4. **Both agents must reach consensus** before the change is allowed to land.
5. **Every step is cryptographically sealed** with content hashes so you can prove later what was reviewed by whom and when.
6. **A separate watchdog process catches stuck reviews** — real-time output streaming, heartbeats, and a kill-switch file the operator can write to abort.

The end result: changes that pass two AIs aren't just "looks good to one model." They're "looks good to two models that habitually disagree."

## Quick start

```bash
pip install consensus-mcp
```

Then in your repo:

```bash
# 1. Make sure you have codex-cli installed
# (https://github.com/openai/codex-cli or your preferred MCP-compatible model)

# 2. Author a goal packet describing what you want done
# (template: docs/templates/goal_packet.yaml)

# 3. Dispatch the review
python -m consensus_mcp._dispatch_codex \
    --goal-packet path/to/goal.yaml \
    --iteration-dir path/to/iteration/ \
    --reviewer-id codex-review-1 \
    --review-target path/to/code-or-diff.yaml

# 4. (Optional) Watch it live in another terminal
python -m consensus_mcp._visibility_tui
```

For real-time stall detection:

```bash
# In one terminal: the dispatch above
# In another terminal: live observability
python -m consensus_mcp._visibility_tui

# If a review hangs, write the abort signal:
echo "abort: this looks stuck" > consensus-state/abort-dispatch-codex-review-1-pass1.signal
```

## How it actually works

### The four-step consensus cycle

1. **Author** — claude (or any orchestrator) produces a `goal_packet.yaml` describing the change. The packet enforces a scope signature: a sha256 over all fields that constrain safety (allowed files, forbidden files, max patch size, validators required, acceptance gates, stop conditions). Any mutation to those fields invalidates the signature.

2. **Dispatch** — `_dispatch_codex` spawns codex as a subprocess with `--sandbox read-only --output-schema codex_review_schema.json`. The schema is strict: codex MUST emit a JSON document with `findings[]`, `goal_satisfied`, `blocking_objections`. No prose, no preamble.

3. **Seal** — the response is captured, hashed, and written to an append-only audit log alongside the prompt hash, schema hash, scope signature, and a sealed packet sha256. Re-running the same review produces a byte-identical packet (modulo timestamp).

4. **Verify** — the orchestrator reads the sealed packet, runs a containment check against the goal packet's scope, and either applies any proposed patches OR records a `blocked_needs_operator` state.

### The closure invariant

A close-attempt review (claude-review.yaml or codex-review.yaml) is only valid if:

- `closer.actor.model_family ≠ last_mutation.actor.model_family` (**cross-family** — same author can't be reviewer)
- `closer.review_target_hash == bundle_sha256(last_mutation.files)` (**hash match** — reviewer looked at the actual changed state)
- `closer.created_at_utc > last_mutation.timestamp_utc` (**freshness** — review came after the mutation)

All three must hold. Any miss raises `blocked_closure_invariant_failed` and the iteration cannot close.

### Bidirectional dispatch monitoring

Every codex call streams output in real-time via `subprocess.Popen` + reader threads:

- Each stdout line becomes a `dispatch_streamed_line` event in the audit log (truncated to 200 chars)
- Every 30s a `dispatch_heartbeat` event records how long the dispatch has been alive and how long since the last line
- If codex is silent for more than `stall_silence_seconds` (default 45s, operator-tunable), the wrapper kills the entire process group and emits `dispatch_aborted` with `abort_source: watchdog_silence`
- The operator can write `consensus-state/abort-dispatch-<pass_id>.signal` at any time; the wrapper picks it up within 500ms and SIGTERMs the codex tree (CTRL_BREAK_EVENT on Windows; killpg/SIGTERM on POSIX)

Wall-time timeouts are a soft ceiling — they raise but don't auto-kill, because a long but actively-streaming codex call is healthier than the wrapper's blind-spot used to assume.

## Real-world results

consensus-mcp was developed using itself ("self-hosted"). Six subsystems of the codebase were peer-reviewed via the four-step cycle. Numbers from the bootstrap deployment:

| Metric | Value |
|---|---|
| Cross-AI findings caught pre-commit | **38** |
| False positives | **0** |
| Findings that turned out to be real defects | **38 / 38 (100%)** |
| Post-commit fixup iterations required | **0** |
| Subsystems peer-reviewed end-to-end | **6** |
| Parallel agent dispatches with zero file conflicts | **7 simultaneous** |
| Time from stall detection to auto-abort | **~45s** (was: 29 minutes before bidirectional monitoring) |

Concrete examples of the kind of defect cross-AI review surfaces that single-AI review missed:

**Race condition in a previously-clean module.** A reader thread mutated shared state without lock protection; main thread joined with a timeout and then read the same state. Single-AI review didn't notice the field was used in two places. Cross-AI flagged it immediately.

**Silent fail-open in a safety gate.** An exception handler caught `Exception` and set a critical check to "skipped" with no operator signal. Static contract said the gate had 9 rules; runtime had 8 with no visible difference. Cross-AI flagged it in the first review pass; the bug had been live for 14 prior iterations.

**Drift-bypass via path resolution.** A reference-path resolver fell back to a basename match when the explicit path didn't exist. Same-name file collisions could satisfy drift hash checks that were intended for a different location. Cross-AI flagged it; nobody had thought to test "what if a different file with the same basename matches the hash?"

**Pre-commit catch of an unrelated regression.** During a planned fix for an unrelated finding, cross-AI flagged that the proposed patch caught `OutsideRepoPathError` but missed the `OSError` from `mkdir()` in the same preflight block. Same-class failure mode, missed by the human-style mental model that grouped errors by symptom rather than by code path.

## Common hurdles consensus-mcp overcomes

**AI sandboxes can't compute hashes.** When the codex CLI is invoked with `--sandbox read-only`, it has no Python eval; it can't compute sha256 or read files mid-prompt. consensus-mcp solves this by **helper-stamping** every hash post-parse: codex returns its findings, the helper validates them, computes the canonical hashes, and writes them into the sealed packet. The agent never has to "compute" anything.

**AI sandboxes can't always read files.** Same constraint. Solution: `_author_review_packet` embeds the full contents of touched files directly into the review-packet YAML before dispatching codex. The model sees the code as part of its prompt, not as a path it has to read.

**AI processes stall silently.** Before bidirectional monitoring, a stuck codex dispatch could hang for the full 15-minute internal timeout — and sometimes longer if the wrapper itself wedged. Now: streamed output + 30s heartbeats + 45s silence threshold = stuck dispatches die in under a minute.

**Cross-AI authorship-vs-reviewer collapse.** A naive "two-AI review" architecture can have both AIs be Claude (one instance reviews, another implements). That's not cross-AI; that's same-model bias laundered through state. consensus-mcp enforces `model_family` as the cross-AI axis — two different families (claude/codex) must touch any closing review.

**Pre-commit vs post-commit catch.** Workflow #4 (claude proposes patches, codex pre-reviews the proposed diff, claude integrates feedback, then implements) catches defects before they hit the working tree. Workflow #3 (claude implements, codex reviews after) catches them post-commit and requires fix iterations. The bootstrap deployment used both; workflow #4 had a 100% catch-fix rate; workflow #3 required 6 followup iterations.

**Audit-log shape under concurrent writers.** JSONL append-only logs can interleave bytes on Windows. consensus-mcp's `_locked_append` wraps every audit-event write in an OS-appropriate exclusive lock (`msvcrt.locking` on Windows; `fcntl.flock` on POSIX). 50-thread concurrent write tests pass.

**Cross-platform process termination.** On Windows, `subprocess.terminate()` only kills the immediate process; node-based subprocesses (codex-cli is one) leave orphans. consensus-mcp creates dispatches in their own process group (`CREATE_NEW_PROCESS_GROUP` on Windows; `start_new_session=True` on POSIX) and signals the whole group on abort.

## Architecture overview

```
consensus_mcp/
├── _dispatch_codex.py          # External codex-CLI dispatcher (Popen + streaming + heartbeats + abort)
├── _self_drive.py              # Stop-rule evaluator + goal-packet validator
├── _author_review_packet.py    # Embed file contents into review-packets
├── _closure_invariant.py       # Cross-family + hash-match + freshness gate
├── _visibility_tui.py          # Real-time event-stream TUI
├── _visibility_watchdog.py     # Post-hoc orphan-dispatch cleanup
├── _release_gate_check.py      # 11-gate release readiness check
├── server.py                   # MCP server entry point
├── tools/                      # 14 MCP tools (dispatch, seal, verify, apply, etc.)
├── dispatch_templates/         # Codex review template + JSON schema
├── validators/                 # Disposition index + scope check + run_validator_tests
└── tests/                      # 420+ regression tests

consensus-state/                # Runtime state (gitignored except .gitkeep)
├── active/                     # Per-iteration working dirs
├── archive/                    # Sealed review-pass archive
└── state/                      # Dispatch log + audit log + ledger
```

## Workflow taxonomy

The bootstrap deployment surfaced four distinct workflows. Pick the one that matches your situation:

| # | Name | Authoring AI | Reviewing AI | When to use |
|---|---|---|---|---|
| **#1** | codex-fix-author | codex | claude | codex found a defect AND wants to fix it; claude verifies |
| **#2** | Flavor B subsystem review | both (parallel) | both (cross-validate) | reviewing existing code with no pending change |
| **#3** | design-then-claude-implements | claude | codex (post-commit) | bidirectional design conversation, then claude implements unilaterally |
| **#4** | claude-as-fix-author with codex pre-review | claude | codex (pre-implementation) | claude is the fix-author; codex reviews proposed diffs BEFORE they land |

Workflow #4 has the strongest pre-commit catch rate in the bootstrap deployment. See `docs/workflows/workflow-4-preferred.md`.

## Documentation

- [`docs/architecture/orchestration-spec.md`](docs/architecture/orchestration-spec.md) — the full multi-agent consensus orchestration design
- [`docs/architecture/autonomy-contract.md`](docs/architecture/autonomy-contract.md) — what the AI is allowed to do without operator approval
- [`docs/architecture/visibility-tui-design.md`](docs/architecture/visibility-tui-design.md) — the visibility TUI + watchdog design
- [`docs/workflows/workflow-4-preferred.md`](docs/workflows/workflow-4-preferred.md) — when and how to use workflow #4
- [`docs/workflows/external-process-fallback.md`](docs/workflows/external-process-fallback.md) — recovery policy for stalled dispatches
- [`docs/postmortems/iter-0019-0036-failures.md`](docs/postmortems/iter-0019-0036-failures.md) — failure modes encountered during bootstrap, with how each was caught

## Status

**2.0.0** — initial standalone release (2026-05-11). Extracted from the project that produced and stress-tested it. 420+ regression tests passing. The bootstrap deployment is the test corpus; new users can build on a stable surface.

## Requirements

- Python 3.10+
- An MCP-compatible LLM (the bootstrap uses OpenAI Codex CLI; the architecture is model-agnostic as long as the model can produce JSON-schema-conformant output)
- PyYAML

## License

MIT — see [LICENSE](LICENSE).

## Contributing

The project is self-hosted: consensus-mcp uses itself for its own review process. Pull requests go through the four-step cycle. If you contribute, expect cross-AI review feedback on your change.

When in doubt about a finding, the closure invariant is the tiebreaker: did two different model families agree on the same code state? If yes, the change can land. If no, the iteration stays open until they agree.
