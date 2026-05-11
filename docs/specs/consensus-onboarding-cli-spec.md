# consensus-mcp onboarding & headless-CLI spec

**Status:** draft (brainstorm-approved 2026-05-11; codex-design-consulted iter-0004; awaiting implementation plan)
**Author:** claude (orchestrator)
**Date:** 2026-05-11
**Iteration target:** iter-0005-onboarding-cli-implementation (proposed)

---

## 1. Context

consensus-mcp today is **orchestrator-driven**: a human runs an interactive AI session (typically Claude Code), that session calls the MCP tool surface to dispatch the secondary AI (codex) as a subprocess. The README points new users at hand-authored `goal_packet.yaml` templates and long `python -m consensus_mcp._dispatch_codex …` invocations. The only supported secondary AI is codex; the only supported primary mode is "an interactive session you're already running."

This spec adds a **standalone, headless `consensus` CLI** in which both AIs are subprocesses. The user runs one shell command; the CLI orchestrates the primary AI (proposes change) and secondary AI (cross-reviews), then prints a verdict. No interactive editor required. No hand-authored YAML.

Three problems this addresses:

1. **First-run friction.** Install → invoke → result. Currently this is three sequential setup steps with hand-authored YAML. Target: `pip install consensus-mcp` → `consensus init` → `consensus run "intent"` → verdict.
2. **AI lock-in.** Today only codex is supported as the reviewer. Different users have different AI preferences (claude/codex, gemini/claude, etc.). A pluggable adapter system removes the hardcoding.
3. **Orchestrator session startup performance.** Already addressed by `consensus_resume` (iter-0002/0003); this spec wires it into the new CLI as `consensus status`.

## 2. Goals

- One-command first-run: `consensus init` autodetects the project shape, locates AI CLIs on disk, asks which is primary vs. secondary, and writes a per-repo config.
- One-command daily use: `consensus run "intent"` spawns the primary AI, captures its diff, applies it, dispatches the secondary AI for cross-review, prints a verdict, and exits with a meaningful status code.
- Pluggable AIs: built-in adapters for `claude` and `codex`; user-defined plugins via `.consensus/adapters/<name>.yaml`.
- No regression: existing MCP tool surface, existing `_dispatch_codex` CLI path, and existing manual workflow all continue to work.

## 3. Non-goals

- Auto-iteration. When the secondary flags a blocking finding, the CLI halts and reports. The user explicitly runs `consensus run --continue` to iterate. (Decision: halt+report over auto-iterate — predictable, no surprise token burn.)
- Workflow #4 (pre-review-then-integrate). v1 ships workflow #3 only. `--workflow 4` is a follow-up.
- Pre-commit / git hook integration. v1 is shell-only; CI hooks come later.
- Cross-iteration learning index. The "guild-inspired memory layer" remains out of scope.
- Web UI / GUI surface.
- Auto-detection beyond CLI discovery — we do not attempt to download/install missing AI CLIs; we print install hints and exit.

## 4. CLI surface

```
consensus init                        # one-time per repo; writes .consensus/config.yaml
consensus run "<intent>" [options]    # one-shot cross-AI review of a code change
consensus status                      # calls _resume.snapshot(); prints to stdout
consensus close [--iteration <id>]    # close iteration if closure invariant is satisfiable
consensus history [--limit N]         # last N iterations from consensus-state/archive/
```

### `consensus run` flags

| Flag | Default | Effect |
|---|---|---|
| `--workflow {3,4}` | `3` | Pick post-review (3) or pre-review (4) workflow |
| `--primary <name>` | from config | Override primary adapter for this invocation |
| `--secondary <name>` | from config | Override secondary adapter |
| `--scope <glob>` | inferred from `git diff` of clean tree | Limit `allowed_files` |
| `--commit` / `--no-commit` | `--commit` (config: `defaults.auto_commit: true`) | Auto-commit after consensus reached |
| `--continue` | off | Re-run the primary with prior secondary findings as context |
| `--max-patch <N>` | from config (`safety.max_patch_size_default`) | Cap on diff size in lines |
| `--allow-truncate` | off | Pack as much of `--scope` as fits in budget; manifest skipped files |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Consensus reached (`goal_satisfied`, no blocking findings); diff applied |
| `1` | Cross-AI disagreement (blocking finding(s)); user can `--continue` to iterate |
| `2` | Operator decision required (stuck dispatch, scope violation, garbled diff) |
| `3` | Config or environment error (adapter not found, family-coherence violation, budget overflow) |

The MCP tool surface stays. `consensus run` internally calls the same `review.write_and_seal`, `_author_review_packet`, etc. that MCP already exposes. One pipeline, two faces.

## 5. `consensus init` flow

Goal: one command, no prerequisite reading.

```
$ consensus init

Scanning project...
  ✓ README.md found (138 lines)
  ✓ pyproject.toml detected (Python project, package: my-app)
  ✓ File tree: top-5 dirs by size: src/, tests/, docs/, scripts/, data/
  ✓ Git repository (branch: main, 12 commits)

Detecting AI CLIs on $PATH...
  ✓ claude     → C:\Users\<you>\AppData\Roaming\npm\claude.cmd (v1.x)
  ✓ codex      → C:\Users\<you>\AppData\Roaming\npm\codex.cmd (codex-cli 0.130.0)
  ✗ gemini     → not found
  ✗ aider      → not found

Which AI should be PRIMARY (proposes / commits changes)?
  [1] claude  (Claude Code CLI)
  [2] codex   (Codex CLI)
> 1

Which AI should be SECONDARY (cross-reviews)?
  [1] codex   (Codex CLI)            ← only candidate (must be different family from primary)
> 1

Verifying both CLIs respond...
  ✓ claude --version → 1.x
  ✓ codex --version  → codex-cli 0.130.0

Writing .consensus/config.yaml ...
Writing .consensus/.gitignore (excludes runtime state) ...

✓ Setup complete. Try: consensus run "fix a typo in the README"
```

### Behavior contract

1. **Project scan is fast + non-invasive.** Reads `README.md`, `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod` if present, plus the file tree (top-N dirs by size, depth 1). Used only to seed a `project.summary` hint in config. Never makes architectural decisions. Skip with `--skip-scan`.

2. **Family-coherent suggestions.** If the user picks `claude` primary, only non-`claude`-family adapters appear as secondary candidates. The `model_family` field on each adapter manifest is load-bearing for this filter and for the closure invariant downstream.

3. **CLI verification before save.** Each chosen adapter's `probe.command` (typically `<binary> --version`) must succeed within its `probe.timeout_seconds`. On failure, init prints the manifest's `binary.not_found_help` string and re-prompts.

4. **Idempotent.** If `.consensus/config.yaml` exists, init asks "reconfigure? [y/N]" before overwriting. The non-interactive form `consensus init --non-interactive --primary claude --secondary codex` skips all prompts.

## 6. `consensus run` flow

```
$ consensus run "fix the off-by-one in the pagination helper" --scope src/pagination/

[1/5] Authoring goal_packet ......................................... ✓ (32ms)
      iteration:  iter-0042-fix-pagination-helper
      scope:      src/pagination/ (3 files, 421 lines)
      primary:    claude  (claude-code-author-1)
      secondary:  codex   (codex-iter0042-1)

[2/5] Dispatching primary AI (claude) ............................... ✓ (47s)
      diff: 4 lines changed in src/pagination/helpers.py
      bundle_sha: 8a91...3d2c

[3/5] Authoring review-packet ....................................... ✓ (8ms)

[4/5] Dispatching secondary AI (codex) for cross-review ............. ✓ (38s)
      findings: 0 blocking, 1 medium, 0 low
      goal_satisfied: true

[5/5] Consensus verdict ............................................. ✓
      ✓ Consensus reached. Diff applied to working tree.
      ⚠ 1 medium finding (non-blocking) — see consensus-state/active/iter-0042-fix-pagination-helper/codex-review.yaml
      ✓ Auto-committed: fix(pagination): off-by-one [iter-0042]

To inspect: consensus status
```

### Algorithm

1. **Author goal_packet** auto-generated from:
   - `intent` → `goal.summary`
   - `--scope` glob → `allowed_files` (default: files git-diff-from-clean would touch). `forbidden_files` always includes `.consensus/`, `consensus-state/`, `.git/`, `.env*`, and any user-added entries from `safety.forbidden_files`.
   - `consensus init` project summary → `goal.desired_end_state` prefix
   - `pilot_id` = `iter-NNNN-<slug-of-intent>` (auto-incremented)
   - `authorization.authorized_by` = `cli:<$USER>`; `authorized_at_utc` = now; `scope_signature` from `_self_drive._scope_signature`

2. **Primary AI subprocess** via the primary adapter's `invocation.primary.command`. The prompt embeds the goal_packet contents + file contents matched by `--scope` (capped at `min(primary, secondary).prompt_budget_bytes` after deducting system-prompt overhead). Output is parsed per the adapter's `output_contract`. Dispatch reuses the watchdog + streaming + abort-signal machinery already in `_dispatch_codex.py` (refactored).

3. **Apply diff** via `git apply`. With `--commit` (default), auto-commits using the `defaults.commit_message_template`. With `--no-commit`, just applies to working tree.

4. **Author review-packet** via existing `_author_review_packet.author_review_packet()`. No new code.

5. **Secondary AI dispatch** via the secondary adapter's `invocation.secondary.command`. Same JSON-schema output, same audit log, same `review.write_and_seal`.

6. **Verdict + exit.** Read sealed codex review. Compute exit code per §4. Print summary; halt (no auto-iteration).

`consensus run --continue` re-dispatches the primary with the prior secondary's findings embedded in the prompt, then loops back through 3–6. Iteration count is bounded by goal_packet's `max_iterations` (default 3).

### Failure modes

- Primary returns no diff / garbage → save raw to `consensus-state/active/<iter>/primary-output-raw.txt`; exit 2.
- Primary's diff hits `forbidden_files` → existing `_self_drive` scope check; exit 2; nothing applied.
- `git apply` fails → diff was structurally invalid; exit 2; suggest `--continue`.
- Secondary stalls → existing watchdog auto-aborts at `stall_silence_seconds`; exit 2 with dispatch-log path.
- Cross-AI disagreement (blocking finding) → exit 1; print finding IDs + citations; suggest `--continue`.
- Scope exceeds prompt budget → exit 3 with explicit budget breakdown.

## 7. Config schema

Two files in `.consensus/`:

### `.consensus/config.yaml` (committed)

```yaml
schema_version: 1
project:
  name: my-app
  language: python
  summary: |
    Brief one-paragraph summary from README scan.

primary: claude
secondary: codex

defaults:
  workflow: 3
  auto_commit: true
  auto_iterate: 0                     # halt + report; >0 enables auto-iteration
  scope_glob_default: null
  commit_message_template: "{type}({scope}): {intent} [iter-{iter_id}]"

adapters:                             # inline overrides for built-in/plugin adapter manifests
  claude:
    prompt_budget_bytes: 800000
  codex:
    stall_silence_seconds: 900

safety:
  forbidden_files:
    - .consensus/
    - consensus-state/
    - .git/
    - .env*
    - secrets/
  max_patch_size_default: 2000
```

Adapter overrides live inline in `config.yaml` (user choice — see §13).

### `.consensus/.gitignore` (committed)

Excludes:
- `cache/` (probe results, debounce data)
- `*.log`

The config file itself is committed.

## 8. Adapter system

An **adapter** is a YAML manifest declaring how to invoke one AI CLI. Built-ins ship inside the Python package at `consensus_mcp/adapters/<name>.yaml`. User plugins live at `.consensus/adapters/<name>.yaml` and shadow built-ins by exact name match.

### Manifest schema

```yaml
schema_version: 1
name: claude
model_family: claude                   # REQUIRED — used by cross-family invariant
display_name: Claude Code CLI
description: Anthropic Claude Code CLI in print-mode for headless invocations.

binary:
  name: claude
  windows_suffixes: [".cmd", ".exe", ""]
  not_found_help: |
    Claude Code CLI not found on $PATH. Install with:
      npm install -g @anthropic-ai/claude-code
    then run `consensus init` again.

probe:
  command: "{{binary}} --version"
  timeout_seconds: 10
  expect_exit_code: 0

invocation:
  primary:
    command: "{{binary}} -p {{quoted_prompt}} --output-format text"
    output_contract: unified_diff_in_fenced_block
    stdin: null
  secondary:
    command: "{{binary}} -p {{quoted_prompt}} --output-format json"
    output_contract: codex_review_schema_v1
    stdin: null

prompt_budget_bytes: 600000
stall_silence_seconds: 90
heartbeat_interval_seconds: 30
timeout_seconds: 600

preflight: null
```

Codex's manifest is structurally identical with `model_family: codex`, different `binary.name`, larger `stall_silence_seconds: 600` (learned the hard way during iter-0002/0003), and uses `--sandbox read-only --output-schema {{schema_path}}` for the secondary invocation.

### Output contracts (v1)

Three contracts ship in v1 (codex-consulted in iter-0004; see §13):

- `unified_diff_in_fenced_block` — extracts the first ` ```diff ` block; multi-block fails; validated via `git apply --check`.
- `unified_diff_only` — stdout IS the diff (no fence required); banner/version-string leaks are caught by the init probe step + `git apply --check`.
- `codex_review_schema_v1` — the existing strict JSON schema from `dispatch_templates/codex_review_schema.json`.

Contract names are keys into a Python parser registry in `consensus_mcp/_adapter_runtime.py`. New contracts are added by registering a parser; no schema versioning needed at the manifest level.

### Template substitution

The `command:` strings use mustache-style substitution. Variables (whitelisted, no eval):

- `{{binary}}` — resolved binary path
- `{{quoted_prompt}}` — full prompt, shell-quoted for the platform
- `{{schema_path}}` — path to the output-contract JSON schema (when applicable)
- `{{iteration_dir}}` — current iteration directory absolute path

### Adapter resolution at `consensus run` time

1. Read `primary` and `secondary` from `config.yaml`.
2. For each: check `.consensus/adapters/<name>.yaml` first (user plugin / override); fall back to `consensus_mcp/adapters/<name>.yaml` (built-in). User plugin shadows built-in.
3. Apply `config.yaml`'s `adapters.<name>:` inline overrides on top.
4. Verify `model_family` differs between primary and secondary; abort with exit 3 if same.
5. Resolve binary path: `shutil.which(binary.name)`, then Windows-suffix loop if not found. If still missing, print `binary.not_found_help` and exit 3.

## 9. Integration with existing internals

### Module map

```
consensus_mcp/
├── _cli.py                       # NEW — argparse entry, dispatches to subcommands
├── _adapter_runtime.py           # NEW — manifest loading, binary resolution, invocation
├── _onboarding.py                # NEW — `consensus init` wizard logic
├── _run.py                       # NEW — `consensus run` orchestration
├── adapters/                     # NEW — built-in adapter manifests
│   ├── claude.yaml
│   └── codex.yaml
├── _resume.py                    # EXISTING — powers `consensus status`
├── _dispatch_codex.py            # EXISTING — refactored to accept adapter manifest
├── _author_review_packet.py      # EXISTING — unchanged
├── _self_drive.py                # EXISTING — unchanged
├── _closure_invariant.py         # EXISTING — unchanged
└── tools/                        # EXISTING MCP tools — unchanged
```

`pyproject.toml` adds:

```toml
[project.scripts]
consensus = "consensus_mcp._cli:main"
```

### `_dispatch_codex.py` refactor (only breaking-shape change in existing code)

- `_invoke_codex(...)` is renamed `_invoke_subprocess_ai(adapter_manifest, role, ...)` where `role ∈ {"primary", "secondary"}`. Adapter manifest carries `command`, `stall_silence_seconds`, `timeout_seconds`, `output_contract`.
- A thin shim preserves the existing `python -m consensus_mcp._dispatch_codex …` CLI invocation; new `--adapter <name>` flag defaults to `codex` so existing scripts work unchanged.
- The Popen + reader threads + heartbeat + abort-signal + cross-platform process-group termination behavior is unchanged.

### State layout

| Path | Purpose | Gitignored? |
|---|---|---|
| `.consensus/config.yaml` | User config | NO — committed |
| `.consensus/adapters/<name>.yaml` | User-defined plugin manifests | NO — committed |
| `.consensus/.gitignore` | Excludes `.consensus/cache/` | NO — committed |
| `.consensus/cache/probes.json` | Last successful probe results (skip slow re-probes) | YES |
| `consensus-state/active/iter-NNNN-<slug>/` | Per-iteration working state | YES (except `.gitkeep`) |
| `consensus-state/archive/review-passes/` | Sealed review-pass archive | YES |
| `consensus-state/state/dispatch-log.jsonl` | Append-only audit log | YES |

No new top-level directories. `.consensus/` is the only addition.

### Backward compatibility

- MCP tool surface unchanged.
- `python -m consensus_mcp._dispatch_codex …` unchanged (with new `--adapter` defaulting to codex).
- Hand-authored `goal_packet.yaml` workflow unchanged.

## 10. Test plan

### Existing tests
Unchanged for `_self_drive.py`, `_closure_invariant.py`, `_author_review_packet.py`, `_resume.py`, MCP tools, `_visibility_*`. The `_dispatch_codex.py` signature change ripples into ~3 test files (passing an adapter manifest fixture).

### New tests (target ~30)
- `test_adapter_runtime.py` — manifest loading, template substitution, binary resolution including Windows suffix loop, family-coherence enforcement, missing-binary handling.
- `test_onboarding.py` — project-scan output shape; family-coherent secondary filter; idempotency; non-interactive mode; probe failure recovery.
- `test_run.py` — the 6-step orchestration, mocked subprocess; failure-mode coverage (no-diff, garbled output, scope violation, stalled dispatch, blocking finding).
- `test_cli.py` — argparse routing; subcommand dispatch; exit code mapping.

### Integration smoke
`tests/integration/test_consensus_run_smoke.py` (skipped unless `CONSENSUS_INTEGRATION=1` env var set) — actually invokes claude + codex against a fake one-file project. CI optional.

## 11. Implementation order

Suggested sequence for the implementation plan (handed off to writing-plans skill next):

1. Adapter manifests + `_adapter_runtime.py` (foundation; no external dependencies).
2. `_dispatch_codex.py` refactor to accept adapter manifest (the only breaking-shape change in existing code).
3. `_onboarding.py` + `consensus_mcp/adapters/claude.yaml` + `consensus_mcp/adapters/codex.yaml`.
4. `_run.py` (depends on 1 + 2 + 3).
5. `_cli.py` + `pyproject.toml` console_scripts entry.
6. Test suite layered onto each step.

Each step ships green tests before the next begins.

## 12. Open questions / future work

- **Workflow #4** (claude proposes, codex pre-reviews, claude integrates). Multi-round headless orchestration; complex but high catch-rate. Future spec.
- **Auto-iterate** (`defaults.auto_iterate > 0`). Pipe findings back to primary, loop. Cap at N rounds. Requires budget-monitoring telemetry.
- **CI integration**: pre-commit hook, GitHub Actions wrapper. Reuses the same CLI.
- **Locked `safety.forbidden_files`** — enforce that `.consensus/`, `consensus-state/`, `.git/`, `.env*` cannot be removed from the user's config. Distant revision per user decision in section 4.
- **Cross-iteration learning index** — guild-inspired memory layer; remains deferred.
- **Web/GUI surface** — not in scope.

## 13. Decisions confirmed by cross-AI consult (iter-0004)

Two design choices were ratified by codex via the consensus-mcp dispatcher itself (codex-iter0004-1 sealed 2026-05-11):

| Question | Winner | Codex's rationale (severity: low) |
|---|---|---|
| Binary path resolution: explicit `windows_suffixes` array vs. `resolution_strategy: npm/brew/...` enum | **Explicit array** | "Option B hides platform behavior in registry code; first-time plugin authors pick the wrong strategy and get confusing discovery failures. Option A pays manifest-noise but keeps YAML behavior inspectable." |
| Output contracts in v1: just `{fenced_diff, codex_review}` vs. add `unified_diff_only` | **Add `unified_diff_only`** | "Option A forces diff-native CLIs into wrappers or custom parsers, raising plugin friction. Option B's loosened invariant is acceptable because the init-probe + `git apply` validation catch the banner-leak failure mode." |

Both confirmations are independent of claude's prior soft leans — codex emitted rationale for *why the loser loses*, not generic concurrence. The sealed packet is at `consensus-state/active/iteration-0004-onboarding-design-consult/codex-review.yaml` (packet sha `e1c1...` — see archive for full hash).

This is the first design-only consult in consensus-mcp. The pattern (small review-target doc + two findings carrying the verdict in `recommendation` + rationale in `risk`) is reusable for future architecture decisions.

## 14. Decisions log (in order)

| Decision | Choice | Rationale source |
|---|---|---|
| Scope of redesign | All three threads (startup perf, first-run, pluggable AI) as one design | user choice |
| Entry point | Standalone `consensus` shell CLI | user choice |
| Execution model | Headless (both AIs are subprocesses) | user choice |
| Config storage | Per-repo `.consensus/config.yaml`; no per-user defaults | user choice |
| Default workflow | Workflow #3 (primary commits, secondary post-reviews) | user choice |
| Iteration policy | Halt + report on blocking findings; no auto-iterate | user choice |
| Adapter scope | Built-in `{claude, codex}` + user plugins | user choice |
| Implementation approach | Wrap-and-extend (reuse `_dispatch_codex` internals) | user choice |
| Project scan depth | README + manifests + file tree (top-N by size, depth 1) | user choice |
| Wizard interactivity | Prompt-based with `--non-interactive` for CI | user choice |
| Auto-commit default | `true`, configurable via `defaults.auto_commit` | user choice |
| Prompt-size cap | Per-adapter `prompt_budget_bytes`, hard-fail by default with `--allow-truncate` opt-in | user pushback (256KB was arbitrary) |
| Adapter overrides | Inline in `config.yaml` (not separate files) | user choice |
| `safety.forbidden_files` lock | Unlocked in v1; revisit later | user choice |
| Binary resolution | Explicit `windows_suffixes` array | codex consult (iter-0004) |
| Output contracts v1 | Three: fenced-diff, bare-diff, codex-review-schema | codex consult (iter-0004) |
