# Changelog

## 1.14.0 - 2026-05-13

Multi-AI contributor pool, blind-first-reveal workflow #4, configurable
governance, and snapshot/restore. Adds gemini-cli as a third peer alongside
codex-cli; introduces a workflow engine that orchestrates N contributors
per project-chosen rules; ships an interactive `consensus init` wizard for
operator-configurable workflow/independence/convergence/disposition/snapshot/
patch-authoring/timeout dimensions.

**New contributors + dispatch (iter-0009 through iter-0011):**

- `_dispatch_base.py` extracted shared dispatcher helpers (`_resolve_repo_root`,
  `_load_goal_packet`, `_build_prompt`, `_terminate_process_tree`,
  `_compute_per_patch_base_sha`, `_validate_patch_proposal`,
  `_build_sealed_packet`, `_seal_via_t6`, `_log_dispatch`).
- `_dispatch_gemini.py` wraps `gemini -p '<JSON directive>' -m <model>
  --approval-mode plan --skip-trust` with validator-retry semantics.
- `reviewer_dispatch_gemini` MCP tool surfaces the gemini adapter in the
  pool.
- Reader threads start before stdin write to avoid the codex-rev-001
  deadlock pattern.
- Codex stall-silence threshold now overridable via
  `CONSENSUS_MCP_STALL_SILENCE_SECONDS` env var (default 180s, was 45s).
- `reviewer_dispatch_codex` MCP wrapper catches `argparse.SystemExit` so
  bad CLI args surface as MCP errors instead of hangs.

**Snapshot/restore + parent history (iter-0012 through iter-0014):**

- Orphan git branch `consensus-state-snapshots` stores point-in-time
  snapshots of the gitignored `consensus-state/active/` tree, providing a
  recovery path against `git clean -fdX` accidents.
- `_snapshot_state.py` exposes `snapshot`, `list`, `restore`, `diff`
  subcommands; ISO-timestamped `snapshot-<TS>-<label>` tags;
  `^[A-Za-z0-9_-]{1,64}$` label regex; path-traversal validation; tag
  uniqueness with retry-suffix; temp-worktree extraction + filesystem
  copy on restore (no risky `git checkout`).
- `_import_parent_history.py` performs a one-time mirror of the parent
  agent-loop project's iter-0000..0042 (41 directories, 53 review passes)
  into `consensus-state/archive/imported-from-parent/` with byte-for-byte
  idempotency checks.
- Integration tests cover restore-after-dirty-state, restore-into-detached-
  HEAD, and concurrent-snapshot race handling.

**Configurable governance + workflow engine (iter-0015 through iter-0016d):**

Per iter-0015 converged design (the canonical workflow #4 reference run
across claude/codex/gemini), v1.14.0 introduces 9 operator-configurable
dimensions surfaced through `.consensus/config.yaml`:

- `workflow.mode`: `post-review` (#3), `propose-converge` (#4), `advisory`
- `workflow.independence`: `blind-first-reveal`, `visible`, `sequential`
- `convergence.rule`: `unanimous`, `strict-majority`, `inclusive-majority`,
  `advisory`
- `convergence.finding_disposition`: `all-or-nothing`, `per-finding`
- `snapshots.trigger`: `manual-only`, `on-iteration-close`, `periodic`
- `snapshots.periodic.every_iterations`: integer cadence
- `patches.authoring`: `claude-only`, `any-contributor`, `none`
- `workflow.timeout_policy`: `treat-as-no-vote`, `treat-as-blocking`,
  `shrink-quorum`
- `contributors.enabled`: ordered list (claude orchestrator always present;
  codex/gemini optional)

Components shipped:

- `config.py` — schema constants, defaults, normalize, validate, load,
  effective-config sha256, legacy-mode synthesis. Cross-validation
  enforces (e.g.) workflow #4 with N≥2 contributors, strict-majority
  with N≥2, manual-only snapshots requiring null cadence.
- `contributors/` package — `ContributorAdapter` ABC + `DispatchPacket`,
  `SealedArtifact`, phase constants. Concrete `ClaudeAdapter` (in-process
  orchestrator self), `CodexAdapter` (subprocess wrapper),
  `GeminiAdapter` (subprocess wrapper). All seal via T6 with confinement
  checks. Fake adapters (`FakeAlwaysApprove`/`FakeAlwaysBlock`/
  `FakeRaisesDispatchError`) for hermetic tests.
- `workflow_engine.py` — `WorkflowEngine.run_iteration()` routes per
  `workflow.mode` to one of three runners. Workflow #3 dispatches non-
  claude contributors as post-review reviewers. Workflow #4 runs blind-
  proposal phase then reveal-and-converge rounds with all contributors
  seeing the full set of prior round artifacts. Convergence evaluation
  applies the rule across responsive contributors, mapped to config
  contributor keys (not adapter names). Timeout policies adjust the
  effective denominator and block-vote set.
- `_init_wizard.py` — `consensus init` CLI. Interactive (default) +
  `--non-interactive` + `--accept-defaults` + `--reconfigure` + `--check`
  + `--print-defaults` + `--dry-run` + `--force` + `--config <path>` +
  `--no-update-gitignore`. Exit codes 0/1/2/3/4 per converged-plan
  Section A. Atomic temp+rename writes. `.gitignore` managed-block with
  bracketed markers (`# >>> consensus-mcp managed <<<` /
  `# <<< consensus-mcp managed >>>`), idempotent across reruns even with
  malformed (orphan, reversed, nested) user-supplied markers — orphan
  markers are preserved untouched. Repo-root detection walks upward for
  `.git`.

**Packaging (iter-0017):**

- `[tool.setuptools].packages` includes `consensus_mcp.contributors`.
- `[project.scripts]` adds `consensus-init` and
  `consensus-mcp-dispatch-gemini` alongside existing entries.

**Workflow + provenance discipline:**

Every non-trivial change shipped on v1.14.0 went through workflow #3 with
codex + gemini reviewing in parallel. iter-0015 was the canonical
workflow #4 design consult (blind proposals from claude + codex + gemini,
then convergence rounds). iter-0016d converged after 6 review rounds
(both reviewers goal_satisfied=true, zero findings). iter-0017 converged
on first pass.

**Test isolation (iter-0019):**

- The 5 long-standing test_dispatch_codex.py full-suite failures are
  fixed. Root cause: `review_write_and_seal.py` and `audit_append_event.py`
  cache `REPO_ROOT` / `ARCHIVE_DIR` / `INDEX_PATH` / `ACTIVE_DIR` at module
  import time, so `monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))`
  in tests had no effect — sealed packets landed in the real archive index,
  polluting subsequent test runs. iter-0019 adds an `_isolate_archive_root`
  test helper that monkeypatches the cached module attributes directly, so
  every dispatch test now seals into a tmp_path-isolated archive. Full
  suite: 658 pass + 1 skipped + 0 fail under any ordering.

**Lazy path resolution (iter-0024 design consult → iter-0025 Phase A
→ iter-0026 first per-tool migration):**

- iter-0024 ran the workflow #4 consult on whether to refactor the
  module-level `REPO_ROOT` caches into lazy resolvers. Converged on
  SHIP-PHASED: introduce `_paths.py` first (Phase A), then migrate
  tools one at a time (Phase B).
- iter-0025 (Phase A): introduced `consensus_mcp/_paths.py` with 9
  lazy-resolver functions (`repo_root`, `state_root`, `project_root`,
  `spec_path`, `archive_dir`, `index_path`, `active_dir`,
  `audit_log_path`, `dispatch_log_path`). Each reads env state on
  every call. Backward compatible — no existing tool touched.
- iter-0026 (Phase B step 1): migrated `state_read_decision_ledger`
  to use `_paths.state_root()`. First test coverage for that tool
  (5 new tests including the lazy-resolution regression demo). Phase
  B migrations continue in iter-0034+ for remaining 9 tools.

**Codex proposal mode (iter-0027 → iter-0028):**

- iter-0021 and iter-0024 design consults discovered that codex
  structurally-abstained on every workflow #4 dispatch because the
  codex review template is hard-coded for code-review tasks: codex
  kept returning "missing review target" errors instead of engaging
  with design questions. Documented as "structural-abstention" in
  prior converged plans.
- iter-0027 ran the workflow #4 consult on what to do about it.
  Claude and gemini independently picked "fix codex template friction"
  as the highest-leverage move. Codex itself structurally-abstained
  on this very consult (proving the problem).
- iter-0028 shipped `--mode {review,proposal}` on both codex and
  gemini dispatchers. New `codex_proposal_template.md` and
  `codex_proposal_schema.json` (plus gemini equivalents) frame the
  task as proposal generation, not code review. Proposal schema
  enforces `selected_target`, `rationale_vs_alternatives`,
  `deliverable_scope`, `risks`, `estimated_complexity`,
  `structural_abstention`. The seal pipeline detects proposal-shape
  payloads and embeds them under a top-level `proposal` block in the
  sealed YAML while keeping outer review-shape fields valid (empty
  findings, computed goal_satisfied) for audit-event compatibility.
- iter-0029: smoke test of proposal mode on a synthetic design
  question — codex returned a proper proposal-shape sealed YAML.
- iter-0030: **first real workflow #4 consult where codex engaged
  substantively as a peer.** The meta-arc iter-0021 → iter-0024 →
  iter-0027 → iter-0028 → iter-0030 closed: the project that proves
  cross-AI consensus works now genuinely produces three-AI consensus
  on its own design questions (gemini env-abstaining due to upstream
  capacity issues notwithstanding).

**`consensus-init` auto-bootstraps `.mcp.json` (iter-0030 design →
iter-0031 implementation):**

- iter-0030 converged on extending `consensus-init` to write
  `.mcp.json` automatically. Two substantive votes (claude + codex)
  agreed on merge-mode for existing files + opt-out flag +
  marker-based project-root detection + PATH-portable command
  discovery + byte-for-byte idempotency.
- iter-0031 implemented the converged design. New flags: `--no-mcp-json`
  (opt out), `--mcp-command STR` (override), `--mcp-force` (replace
  existing consensus-mcp entry on divergence). Merge mode preserves
  other MCP servers; conflict detection skip+warns instead of
  clobbering; malformed JSON skip+warns instead of mutating.
- `_detect_repo_root` extended from `.git`-only to marker-based:
  `git rev-parse --show-toplevel` → strong markers (.git,
  pyproject.toml, package.json, CLAUDE.md, .mcp.json, consensus-state)
  → cwd fallback.
- Operator UX is now one command: `pipx install consensus-mcp`, then
  `cd <any-project>; consensus-init`. The wizard writes
  `.consensus/config.yaml` + `.gitignore` managed block + `.mcp.json`
  (with correct PATH-portable command + per-project env vars).

**Defaulting to workflow #4 for design questions** (operator policy
correction mid-v1.14.0):

The original heuristic "workflow #3 for execution; workflow #4 for
explicit design questions" was systematically biased toward #3
because of cost asymmetry. Corrected mid-cycle: default to workflow
#4 for any decision with real design surface; require an explicit
reason to fall back to #3. iter-0027 onward applies this rule. Saved
to operator memory as
[[feedback-default-workflow-4-for-design]].

**Operator memories saved during v1.14.0 cycle:**

- `feedback_no_phantom_proceed.md` — never end a turn with "proceeding
  with X" unless the tool call is in the same turn
- `feedback_gemini_429_skip.md` — priority-tiered handling of upstream
  Gemini 429 errors: low-priority iterations skip gemini on first
  failure; high-priority iterations allow one retry

**Deferred to a follow-up iteration:**

- Remaining 9 Phase B tool migrations (per iter-0024 plan):
  `repo_get_section`, `repo_set_section`, `state_update_decision_ledger`,
  `patch_stage_and_dry_run`, `patch_apply_consensus_patch`,
  `gate_evaluate_production_with_scope_match`, `review_read_post_seal`,
  `audit_append_event`, `review_write_and_seal`. Each migrates from
  cached `REPO_ROOT` to `_paths.state_root()` composition.
- 20 stale `iter-9999-*` fixture entries remain in
  `consensus-state/archive/review-passes/index.yaml`. Cosmetic only;
  separate cleanup iteration if desired.

## 1.13.0 - 2026-05-12

Multi-project resolution: consensus-mcp now boots in any project without a
local checkout. The frozen wheel ships a spec template, and state auto-
initializes in CWD.

**Changes (per iter-0007 codex consult, sealed packet 39f8b7a8b...):**

- New resolvers in `server.py` split the legacy `REPO_ROOT` into three
  concerns: `_resolve_spec_path` (spec source), `_resolve_state_root`
  (`consensus-state/` location), `_resolve_project_root` (reviewable-file
  root for goal_packet `allowed_files`).
- Resolution order for each:
  - Spec: `CONSENSUS_MCP_SPEC_PATH` > legacy `CONSENSUS_MCP_REPO_ROOT` >
    walked-up checkout > shipped `consensus_mcp/spec_template.md`
  - State: `CONSENSUS_MCP_STATE_ROOT` > legacy `CONSENSUS_MCP_REPO_ROOT` >
    `Path.cwd() / "consensus-state"`
  - Project: `CONSENSUS_MCP_PROJECT_ROOT` > legacy `CONSENSUS_MCP_REPO_ROOT`
    > `Path.cwd()`
- `consensus_mcp/spec_template.md` shipped in the wheel (added to
  `pyproject.toml [tool.setuptools.package-data]`). Frozen-wheel users get a
  bootable spec without cloning.
- `_resolve_repo_root` kept for back-compat; no longer load-bearing for
  spec/state/project-root in v1.13.0.

**Deferred to v1.14.0** (per codex Q1 scoping):

- Dispatcher auto-discovery of `iteration_dir/review-packet.yaml` when
  `--review-target` is non-yaml (currently silent-failure, prompt ships
  without embedded touched-file contents).
- Cold-start grace period for the watchdog (pre-first-byte vs.
  post-first-byte silence thresholds; 45s is too aggressive for bigger
  prompts).
- Visibility upgrade: dispatch_heartbeat events should expose a `status`
  field (loading / streaming / stalled) for operator clarity.

**Known pre-existing pytest flake** (predates v1.13.0; **fixed in v1.14.0
iter-0019**): 5 tests in `test_dispatch_codex.py` that pass in isolation
but fail in the full suite due to test-ordering pollution. Not introduced
by v1.13.0; tracked separately. See the v1.14.0 "Test isolation" section
above for the resolution.

## 1.12.0 - 2026-05-11

Standalone release. Extracted from upstream-26.4.16 source
project. See `docs/architecture/codex-fix-author-roadmap.md` for the work
history that led here.

Renames vs the prior internal-only releases (1.0.0 - 1.11.0):

- Python package `agent_loop_mcp` -> `consensus_mcp`
- Python package `agent_loop` (validators) -> `consensus_mcp.validators`
- State directory `agent-loop/` -> `consensus-state/`
- Env var `AGENT_LOOP_MCP_REPO_ROOT` -> `CONSENSUS_MCP_REPO_ROOT`
- MCP server name `agent-loop-mcp` -> `consensus-mcp`
- Repo-root markers updated to `("consensus-state", "consensus_mcp")`
- Flat layout (no `scripts/` prefix)
