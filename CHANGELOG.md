# Changelog

## 1.14.1 - unreleased

Hot-patch and small-bump release branch from v1.14.0 tip. Open scope:

- iter-0044: implement adapter `--mode` forwarding fix per iter-0043
  converged plan (CodexAdapter + GeminiAdapter forward `packet.phase`
  → dispatcher `--mode`; centralized phase-to-mode helper; MCP
  wrappers expose `phase` parameter; skill workaround removed).

## 1.14.0 - 2026-05-14

Multi-AI contributor pool, blind-first-reveal workflow #4, configurable
governance, snapshot/restore, Claude Code bootstrap pack, bundled
operating-procedure skill, and codified operator-directive defaults
(parallelism, weighted-synthesis convergence, Friday release cadence).
Adds gemini-cli as a third peer alongside codex-cli; introduces a
workflow engine that orchestrates N contributors per project-chosen
rules; ships an interactive `consensus init` wizard for operator-
configurable workflow/independence/convergence/disposition/snapshot/
patch-authoring/timeout dimensions.

**Claude Code integration + skill bundling (iter-0040, iter-0041):**

- `consensus init --install-claude-code` standalone global op installs
  the Claude Code bootstrap pack (skill + slash command) for any
  Claude Code project that uses consensus-mcp.
- `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`
  ships in the wheel: load-bearing operating procedures (workflow
  selection, dispatch, gemini 429 handling, codex auth, iteration-
  state persistence, peer-cited content verification, peer-review
  thresholds, "consensus" trigger word) automatically present in
  every project that runs the bootstrap pack.

**Operator-directive defaults codified (iter-0043):**

- Maximize parallelism — always. Default to parallel; serial is the
  choice that needs justification. Applies to round-1 peer dispatch,
  round-2+ batches, multi-file investigation, background long-running
  ops, and cross-iteration parallelism.
- Weighted-synthesis convergence as default. All ideas of all
  proposals weighed for benefit to the project as a whole. No good
  ideas lost; no babies tossed with bathwater. `all-or-nothing`
  finding-disposition is now edge-case opt-in only (binary scope
  decisions, safety gates, compliance verdicts). Engine-level
  follow-up flagged: `config.py:295-308` still enforces all-or-
  nothing for workflow #4 — separate iteration will lift that
  constraint so the data layer matches doctrine.
- Friday release-cadence rule. Cut a release tag every Friday if at
  least one iteration closed that week. Release-cut is a procedure
  with a trigger, not an ad-hoc decision. 10-step cut sequence
  documented in skill (CHANGELOG date stamp, version verify, test
  suite, tag, build, smoke, publish, push, branch next). This
  release is the first cut under the new cadence rule and clears
  3 days of accumulated work (iter-0009..iter-0043).

**Known issue carried forward:**

- 5 tests in `consensus_mcp/tests/test_dispatch_codex.py` flake when
  the full pytest suite runs (pass in isolation). Documented in
  `docs/known-issues/pytest-ordering-flake.md`. Predates v1.13.0;
  not a v1.14.0 regression. Tracked for a future fix iteration.

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
  (5 new tests including the lazy-resolution regression demo).
- iter-0034 (Phase B step 2): migrated `repo_get_section` to
  `_paths.project_root()`. Removed local `_resolve_repo_root`.
- iter-0035 (Phase B steps 3-8): batched 6 LOW-impact tools onto
  the lazy resolvers in one commit — `repo_set_section`,
  `state_update_decision_ledger`, `patch_stage_and_dry_run`,
  `patch_apply_consensus_patch`,
  `gate_evaluate_production_with_scope_match`, `review_read_post_seal`.
  PEP 562 `__getattr__` hooks added per-tool for external
  `module.REPO_ROOT` back-compat.
- iter-0036 (Phase B step 9, HIGH-impact audit-trail tool): migrated
  `audit_append_event` from cached `REPO_ROOT/ACTIVE_DIR` to lazy
  `project_root()`/`active_dir()`. Cleaned up 3 closure_invariant
  tests that used unsafe `monkeypatch.setattr(audit_append_event,
  "ACTIVE_DIR", tmp_path)` — pytest's monkeypatch captures the
  `__getattr__`-synthesized value at setattr time and restores it
  into `__dict__` at teardown, permanently poisoning subsequent
  tests. Switched to `monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT",
  ...)` matching the iter_0018 pattern.
- iter-0037 (Phase B step 10, final + HIGHEST-impact seal-pipeline
  tool): migrated `review_write_and_seal` from cached
  `REPO_ROOT/ARCHIVE_DIR/INDEX_PATH` plus the in-handle
  `from consensus_mcp.tools.audit_append_event import ACTIVE_DIR`
  capture to lazy `project_root()`/`archive_dir()`/`index_path()`/
  `active_dir()` resolvers. `_isolate_archive_root` test helper in
  `test_dispatch_codex.py` simplified — only `monkeypatch.setenv(
  "CONSENSUS_MCP_REPO_ROOT", ...)` remains; the previously-required
  5 `setattr` calls (now unsafe against `__getattr__` attributes)
  are gone.

**Claude Code bootstrap pack (iter-0039 design → iter-0040 implementation):**

- iter-0039 ran a workflow #4 consult (claude + codex + gemini, all
  proposal-mode, strict-majority convergence) on the discoverability
  gap reported by the operator after iter-0033: in a fresh project,
  typing "consensus init" into Claude Code chat returned "I don't
  recognize this command" because the pipx install registers a
  shell binary (`consensus-init`) not a Claude Code surface. All
  three contributors converged on shipping a Claude-Code-native entry
  point that wraps the existing shell binary.
- iter-0040 implemented the converged plan. New shipped assets:
  - `consensus_mcp/claude_extensions/skills/consensus/SKILL.md`
    triggers on "consensus init", "bootstrap consensus", "set up
    consensus" and runs the shell binary via Bash.
  - `consensus_mcp/claude_extensions/commands/consensus-init.md`
    gives explicit `/consensus-init` slash-command discoverability.
- New `consensus-init --install-claude-code` flag copies both into
  `$CLAUDE_HOME` (default `~/.claude`) using a managed idempotent
  pattern: byte-identical = no-op; divergent = skip-with-warning;
  `--force` overwrites. Honors `CLAUDE_HOME` env override for
  non-default installs (CI, devcontainers, multi-user systems).
- New `consensus-init --from-claude-code` flag prints contextual
  reload guidance after a successful init ("restart Claude Code or
  run `/mcp` to activate the consensus-mcp server"). Used by both
  the skill and slash command bodies; deterministic so we don't
  rely on env-var sniffing (codex's preferred convergence
  resolution per iter-0039 Q4).
- Bonus from iter-0039 Q6: added `consensus` console-script alias
  to pyproject.toml so `consensus init` (with a SPACE) at the
  shell works alongside the hyphenated `consensus-init`. The
  `init` subcommand is stripped from argv[0] inside `main()`;
  everything else flows through the same argparse setup.
- Side-effect hot-fix landed in the same release: the proposal-mode
  validation path in `_dispatch_codex.py` / `_dispatch_gemini.py`
  did `try: import jsonschema; ...; except jsonschema.ValidationError`
  — when the import failed (because `jsonschema` wasn't a declared
  dep), the except clause's reference to `jsonschema.ValidationError`
  surfaced as `UnboundLocalError`, masking the real problem and
  blocking iter-0039 from running in the pipx venv. Added
  `jsonschema>=4.0` to deps + split the try/except so `ImportError`
  surfaces with actionable wording. (workflow #3 hot-patch because
  it blocked iter-0039 progress.)

**Phase B closed.** All 10 tool migrations landed (iter-0026..0037).
No tool now holds a cached module-level `REPO_ROOT`/`ACTIVE_DIR`/
`ARCHIVE_DIR`/`INDEX_PATH`/etc. — every path read is lazy and honors
env-var redirection at call time. Suite green at 773 passed,
1 skipped. The cross-project pipx install pattern from iter-0030
now works in any project: `pipx install consensus-mcp` once, then
`consensus-init` in any project writes a working `.mcp.json` and
the lazy resolvers pick up the per-project state root automatically.

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

- 20 stale `iter-9999-*` fixture entries remain in
  `consensus-state/archive/review-passes/index.yaml`. Cosmetic only;
  separate cleanup iteration if desired.
- Phase C cleanup of `_isolate_archive_root`-style fixtures that are
  now no-ops or simplifiable; the test helpers still work but most of
  the `monkeypatch.setattr` calls inside them became redundant once
  Phase B finished. Pure refactor, no behavior change.

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
