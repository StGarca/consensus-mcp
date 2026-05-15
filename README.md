# consensus-mcp

**Peer review for AI code, automated.** A pool of AI contributors (Claude + Codex + Gemini in the default configuration; the pool is operator-configurable) check each other's work before any code lands. Like the four-eyes principle at a software company, except every set of eyes belongs to a different model family — and they have to converge before the change ships.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)

---

## The problem

AI coding assistants are good. They're also confident liars. They hallucinate functions that don't exist, invent file shapes they never read, and skip past assumptions they should have stated. When a single AI reviews its own work, it has the same blind spots that caused the bug in the first place.

Cross-AI review fixes that. Different models trained on different data with different optimizers fail in different ways. When two of them disagree about what the code is doing, one of them is wrong — and that disagreement surfaces bugs that single-agent review reliably misses.

consensus-mcp is the infrastructure that makes that automatic.

## What it does (in plain English)

When you ask the contributor pool to write code, fix a bug, or review a change, consensus-mcp:

1. **Captures the request as a sealed contract** — what's being changed, what files are touched, what success looks like, who authorized it. This is the `goal_packet`.
2. **Routes through the operator-chosen workflow** — four modes (operator vocabulary updated to letter aliases in v1.14.4; numeric aliases 3/4 deprecated for one cycle): **Workflow A** (`propose-converge`, default — all contributors propose blindly then converge across rounds), **Workflow B** (`post-review`, lightweight — one AI implements, the others review), **Workflow C** (`autonomous-execute`, NEW in v1.14.4 contract; engine deferred to v1.15.0 — runs to completion overnight without operator-in-the-loop, auto-approving emergent scope items within an operator-pre-declared `autonomy_contract`), and **advisory** (recommendations only; orchestrator decides).
3. **Each contributor produces a sealed artifact** — structured findings with severity-graded defects, citations to specific file:line locations, and proposed patches (where applicable). Codex via the codex CLI, Gemini via the gemini CLI, Claude as the in-process orchestrator.
4. **The configured convergence rule decides** — unanimous, strict-majority, inclusive-majority, or advisory. Workflow A hides each contributor's proposal from the others until reveal phase, then runs convergence rounds until the rule is satisfied or the round limit is hit.
5. **Every step is cryptographically sealed** with content hashes so you can prove later what was reviewed by whom and when.
6. **State snapshots into an orphan git branch** — `consensus-state-snapshots` carries point-in-time captures of the gitignored iteration tree, so a `git clean -fdX` can't lose work.
7. **A separate watchdog process catches stuck reviews** — real-time output streaming, heartbeats, and a kill-switch file the operator can write to abort.

The end result: changes that pass three independent model families aren't "looks good to one model" — they're "looks good to three models that each fail in different ways."

## Quick start

**Install once per machine, use in any project:**

```bash
# Install via pipx — isolated venv, console scripts on PATH, no
# polluting individual project venvs:
pipx install git+https://github.com/stgarca/consensus-mcp.git@v1.14.6

# (Optional but recommended) install the Claude Code bootstrap pack —
# a tiny skill + slash command so you can run `consensus init` from
# inside Claude Code chat in any project:
consensus-init --install-claude-code
```

(If you prefer pip-in-venv: `pip install git+https://github.com/stgarca/consensus-mcp.git@v1.14.6` — but pipx is the recommended pattern for cross-project use.)

`--install-claude-code` is a **standalone global operation** — it copies three files into your Claude Code config and exits:

- `~/.claude/skills/consensus/SKILL.md` — bootstrap skill (triggers on "consensus init", "bootstrap consensus", etc.)
- `~/.claude/commands/consensus-init.md` — explicit `/consensus-init` slash command
- `~/.claude/skills/consensus-workflow/SKILL.md` — operating-procedure reference; triggers on workflow-execution intent ("consensus review", "run a consult", "dispatch codex", "workflow 3", "workflow 4") and primes Claude with the load-bearing rules (workflow #3 vs #4, round-1 parallel dispatch, dispatcher hazards, gemini 429 handling, snapshot/restore safety, peer-citation verification)

It does NOT run the per-project bootstrap; you run `consensus-init` (without the flag) inside each project for that. Honors `CLAUDE_HOME` env var for non-default locations. Idempotent on rerun; pass `--force` to overwrite user-edited copies.

**Then bootstrap any project with a single command:**

```bash
cd /path/to/your-project

# 1. (Optional) Install the contributor CLIs you want in the pool.
#    Codex (https://github.com/openai/codex-cli) and Gemini
#    (https://github.com/google-gemini/gemini-cli) are auto-detected.
#    Claude is always present as the in-process orchestrator.

# 2. Bootstrap the project — interactive prompts for all 9 governance
#    dimensions, and writes BOTH .consensus/config.yaml AND .mcp.json
#    so Claude Code auto-connects to consensus-mcp on next launch:
consensus-init                                  # from the shell, or
consensus init                                  # `consensus init` with a space also works
# Or non-interactive with sensible defaults:
consensus-init --non-interactive --accept-defaults
```

**Or — once `--install-claude-code` has been run — bootstrap from inside Claude Code itself:**

Type one of the following in the Claude Code chat at the project root:

- `consensus init` — the bundled skill recognizes this phrase and runs the shell binary for you
- `/consensus-init` — the bundled slash command does the same thing more explicitly

Both surface paths invoke `consensus-init --from-claude-code`, which prints Claude-Code-specific restart instructions after the bootstrap completes. The MCP server activates only after Claude Code reloads (Ctrl-C in the project terminal then `claude code` again, or `/mcp` reload if your build supports it).

That's it. `consensus-init` produces three artifacts:

- `.consensus/config.yaml` — governance choices (workflow, contributor pool, convergence rule, etc.)
- `.gitignore` managed block — three paths (`.consensus/tmp/`, `.consensus/cache/`, `.consensus/logs/`)
- `.mcp.json` — Claude Code MCP server registration with correct per-project env vars; merge mode if a `.mcp.json` already exists, so existing MCP servers (playwright, github, etc.) are preserved

Then open Claude Code at that project. The consensus-mcp tools (`consensus.run_iteration`, `reviewer.dispatch_codex`, `reviewer.dispatch_gemini`, plus 15+ others) become available. Ask Claude in natural language ("get consensus review on this change") or use the `consensus` trigger word — both work.

### Bootstrap flags

- `--no-mcp-json` — skip the `.mcp.json` write (you'll manage it manually)
- `--mcp-command STR` — override the command written to `.mcp.json` (e.g., `"py -3.11 -m consensus_mcp.server"` for dev installs)
- `--mcp-force` — replace existing consensus-mcp entry on divergence (other MCP servers preserved)
- `--install-claude-code` — copy the bootstrap skill + slash command into `$CLAUDE_HOME` or `~/.claude` (idempotent; pair with `--force` to overwrite user-edited copies)
- `--from-claude-code` — caller is a Claude Code skill/command; print contextual restart guidance
- `--reconfigure` — re-prompt with existing config as defaults; show unified diff before writing
- `--check` — validate existing `.consensus/config.yaml` and exit
- `--print-defaults` — emit the default config YAML to stdout

### Programmatic / escape-hatch entry points

The single-reviewer dispatchers are still available as console scripts (handy for CI bootstrap or one-off reviews):

```bash
consensus-mcp-dispatch-codex --goal-packet ...
consensus-mcp-dispatch-gemini --goal-packet ...
```

Or the full engine via Python:

```python
from consensus_mcp.tools.consensus_run_iteration import handle
result = handle(
    iteration_dir='consensus-state/active/iteration-xxxx',
    goal_packet_path='consensus-state/active/iteration-xxxx/goal_packet.yaml',
    target_path='path/to/problem-or-patch.yaml',
)
```

For real-time stall detection: every codex/gemini dispatch streams output and emits 30-second heartbeats. If the model stalls past `CONSENSUS_MCP_STALL_SILENCE_SECONDS` (default 180), the wrapper kills the process group and records `dispatch_aborted`. The operator can also write `consensus-state/abort-dispatch-<pass_id>.signal` to force an abort within 500ms.

## The 9 operator-configurable dimensions

`.consensus/config.yaml` (generated by `consensus-init`) is the single source of truth for project governance. Every dimension is a project-level choice, not a hardcoded default:

| Dimension | Choices |
|---|---|
| `workflow.mode` | `post-review` (#3), `propose-converge` (#4), `advisory` |
| `workflow.independence` | `blind-first-reveal`, `visible`, `sequential` |
| `convergence.rule` | `unanimous`, `strict-majority`, `inclusive-majority`, `advisory` |
| `convergence.finding_disposition` | `all-or-nothing`, `per-finding` |
| `contributors.enabled` | ordered list (claude always present; codex/gemini optional; future adapters pluggable) |
| `snapshots.trigger` | `manual-only`, `on-iteration-close`, `periodic` |
| `snapshots.periodic.every_iterations` | integer cadence |
| `patches.authoring` | `claude-only`, `any-contributor`, `none` |
| `workflow.timeout_policy` | `treat-as-no-vote`, `treat-as-blocking`, `shrink-quorum` |

`consensus-init --print-defaults` emits the full schema. `consensus-init --reconfigure` re-prompts with the existing config as the prompt defaults and prints a unified diff before writing.

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

**Cross-AI authorship-vs-reviewer collapse.** A naive "two-AI review" architecture can have both AIs be Claude (one instance reviews, another implements). That's not cross-AI; that's same-model bias laundered through state. consensus-mcp enforces `model_family` as the cross-AI axis — two different families (e.g., claude/codex, claude/gemini, codex/gemini) must touch any closing review. v1.14.0 extends this from 2 to N: with three contributors in the default pool, the convergence rule (strict-majority by default for N≥3) requires at least two-of-three cross-family agreement.

**Pre-commit vs post-commit catch.** Workflow A (`propose-converge`; was numbered #4 prior to v1.14.4 — claude proposes patches, codex pre-reviews the proposed diff, claude integrates feedback, then implements) catches defects before they hit the working tree. Workflow B (`post-review`; was #3 — claude implements, codex reviews after) catches them post-commit and requires fix iterations. The bootstrap deployment used both; Workflow A had a 100% catch-fix rate; Workflow B required 6 followup iterations.

**Audit-log shape under concurrent writers.** JSONL append-only logs can interleave bytes on Windows. consensus-mcp's `_locked_append` wraps every audit-event write in an OS-appropriate exclusive lock (`msvcrt.locking` on Windows; `fcntl.flock` on POSIX). 50-thread concurrent write tests pass.

**Cross-platform process termination.** On Windows, `subprocess.terminate()` only kills the immediate process; node-based subprocesses (codex-cli is one) leave orphans. consensus-mcp creates dispatches in their own process group (`CREATE_NEW_PROCESS_GROUP` on Windows; `start_new_session=True` on POSIX) and signals the whole group on abort.

## Architecture overview

```
consensus_mcp/
├── _init_wizard.py             # `consensus-init` CLI (9 dimensions, interactive + flags)
├── _dispatch_codex.py          # Codex-CLI dispatcher (Popen + streaming + heartbeats + abort)
├── _dispatch_gemini.py         # Gemini-CLI dispatcher (same dispatch-base helpers as codex)
├── _dispatch_base.py           # Shared dispatcher primitives (resolve, prompt, seal, log)
├── _engine_factory.py          # Build adapters + WorkflowEngine from config + repo_root
├── _snapshot_state.py          # Orphan-branch snapshot/list/restore/diff
├── _import_parent_history.py   # One-time mirror of upstream iteration history
├── workflow_engine.py          # Workflow #3/#4/advisory orchestrator
├── config.py                   # Schema + validate + load + legacy synthesis
├── contributors/               # Adapter layer (Claude, Codex, Gemini, Fake* for tests)
│   ├── base.py                 # ContributorAdapter ABC + DispatchPacket + SealedArtifact
│   ├── claude.py               # In-process orchestrator self
│   ├── codex.py                # subprocess wrapper around _dispatch_codex
│   └── gemini.py               # subprocess wrapper around _dispatch_gemini
├── server.py                   # MCP server entry point + tool registration
├── tools/                      # MCP tools
│   ├── consensus_run_iteration.py        # Drive one iteration via the engine
│   ├── consensus_get_iteration_outcome.py # Read-only inspector
│   ├── reviewer_dispatch_codex.py        # Single-reviewer escape hatch
│   ├── reviewer_dispatch_gemini.py       # Single-reviewer escape hatch
│   ├── review_write_and_seal.py          # T6 — cryptographic seal + archive
│   ├── apply_codex_patch.py              # Apply sealed patches under containment
│   └── ...                                # 17+ tools total
├── dispatch_templates/         # Codex + Gemini review templates + JSON schemas
├── validators/                 # Disposition index + scope check + validator runner
└── tests/                      # 773+ regression tests (1 skipped)

consensus-state/                # Runtime state (gitignored; recoverable via snapshot branch)
├── active/                     # Per-iteration working dirs
├── archive/                    # Sealed review-pass archive + parent-history mirror
└── state/                      # Dispatch log + audit log + ledger

consensus-state-snapshots/      # Orphan git branch (NOT a directory) — point-in-time
                                # captures of consensus-state/active/ for recovery
                                # against accidental `git clean -fdX`.
```

## Workflow taxonomy

v1.14.0 ships three orchestrator-supported workflow modes. The choice is per-project via `consensus-init` (or `consensus-init --workflow <mode>`):

| Mode | Independence | When to use |
|---|---|---|
| **`post-review` (#3)** | `visible` — reviewers see the implementation | Implementation work per a converged design. One AI implements; the others audit. Fast, low-overhead, good for execution-mode iterations. |
| **`propose-converge` (#4)** | `blind-first-reveal` — every contributor proposes against the problem statement **before** seeing any peer output, then convergence rounds reveal all proposals and iterate to a consensus | Open design questions, architectural choices, anywhere the value comes from independent reasoning. iter-0015 canonical reference run is in `consensus-state/archive/imported-from-parent/`. |
| **`advisory`** | `visible` | The orchestrator wants recommendations but reserves final decision-making. Useful when the contributors disagree on fundamentals and the operator needs all viewpoints surfaced rather than collapsed by a convergence rule. |

Earlier workflow taxonomy (#1 codex-fix-author, #2 Flavor B subsystem review) is preserved in the parent-project archive under `consensus-state/archive/imported-from-parent/` for provenance but is **not** part of the v1.14.0 orchestrator-supported surface — implement those via the single-reviewer escape hatches if you need them.

## Documentation

- [`docs/architecture/orchestration-spec.md`](docs/architecture/orchestration-spec.md) — the full multi-agent consensus orchestration design
- [`docs/architecture/autonomy-contract.md`](docs/architecture/autonomy-contract.md) — what the AI is allowed to do without operator approval
- [`docs/workflows/workflow-4-preferred.md`](docs/workflows/workflow-4-preferred.md) — when and how to use Workflow A (was workflow #4); filename preserved for stable cross-references
- [`docs/workflows/workflow-c-autonomous.md`](docs/workflows/workflow-c-autonomous.md) — Workflow C autonomous-execute usage (v1.14.4 contract; v1.15.0 engine)
- [`docs/workflows/external-process-fallback.md`](docs/workflows/external-process-fallback.md) — recovery policy for stalled dispatches
- [`docs/postmortems/iter-0019-0036-failures.md`](docs/postmortems/iter-0019-0036-failures.md) — failure modes encountered during bootstrap, with how each was caught

## Status

**1.14.0** — multi-AI contributor pool, blind-first-reveal workflow #4, configurable governance, snapshot/restore, proposal-mode codex/gemini dispatchers, `consensus-init` auto-bootstraps `.mcp.json`. Extracted from the project that produced and stress-tested it; restarted at iter-0001 as a standalone. 773+ regression tests passing (1 skipped, 0 failing under any ordering). The bootstrap deployment is the test corpus; new users can build on a stable surface.

See [`CHANGELOG.md`](CHANGELOG.md) for the v1.14.0 feature train (iter-0009 through iter-0022).

## Requirements

- Python 3.10+
- [`pipx`](https://pipx.pypa.io/) (recommended) for isolated cross-project install
- For multi-contributor pools: [`codex-cli`](https://github.com/openai/codex-cli) and/or [`gemini-cli`](https://github.com/google-gemini/gemini-cli) on PATH (auto-detected by `consensus-init`)
- Claude is always present as the in-process orchestrator
- PyYAML, jsonschema (pulled in by pipx)

## License

MIT — see [LICENSE](LICENSE).

## Contributing

The project is self-hosted: consensus-mcp uses itself for its own review process. Pull requests go through the four-step cycle. If you contribute, expect cross-AI review feedback on your change.

When in doubt about a finding, the closure invariant is the tiebreaker: did the configured convergence rule pass on the same code state with cross-family contributors? If yes, the change can land. If no, the iteration stays open until the configured rule is satisfied.
