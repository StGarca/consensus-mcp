# Architect-build hardening pass - design (2026-06-11)

Post-v2.0.0 hardening of Consensus Build (workflow D). Six gaps were
verified against the shipped code (main == feat/architect-build-mode tip,
v2.0.0 tag). Three are mechanical fixes against the ratified
architect-build design (declared scope M1/M3/M4 below - not consult
questions). Three carry genuine design forks (Q1/Q2/Q3) and are the
subject of this anchored consult.

## Verified gap inventory (evidence)

| # | Gap | Evidence |
|---|-----|----------|
| 1 | Sealed artifacts not verified at point of use | Only the spec gets a point-of-use `seal_is_intact` check (`architect_loop_step.py:371`). spec-approval (`:379,:401,:649`), review (`:702`), ruling (`:707`), build seal (`:725,:780`), integrity-before (`:752`) are consumed unchecked. |
| 2 | Spec approval binding not enforced | `approve_spec` seals `spec_sha256 + base_sha`; `base_sha` is enforced (`:662`) but NOTHING compares `approval["spec_sha256"]` to the spec fed to the builder. A post-approval spec rev builds unapproved. |
| 3 | Symlink-to-directory blind spot in tree snapshots | `snapshot_architect_tree` / `snapshot_goal_artifacts` use `os.walk(followlinks=False)`; symlinks to DIRECTORIES land in `dirnames`, are never descended AND never hashed - a planted symlink-dir is invisible to the diff. The `snapshot_goal_artifacts` docstring claim ("a link planted during the run surfaces as a created path") is only true for file symlinks. Any symlink resolving to the lane is silently pruned (`_architect_lane.py:496`). |
| 4 | No architect-tree recheck at the delivery gate | The delivery recheck (`architect_loop_step.py:746-789`) covers main integrity + lane scan + lane HEAD, but NOT the architect tree; tree brackets only wrap build (`:421`) and verification (`:537`). Sibling-goal tamper between verification and human delivery approval is unseen. |
| 5 | Builder/verification env isolation is a 6-name denylist | `scrub_env_keys(os.environ.copy(), ALL_PROVIDER_SCRUBBED_ENV_KEYS)` (`_dispatch_builder.py:122`; `_VERIFICATION_SCRUBBED_ENV_KEYS = ALL_PROVIDER_SCRUBBED_ENV_KEYS`). `GH_TOKEN`, `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, AWS/SSH/npm credentials all pass into builder + verification subprocesses. |
| 6 | results + init wizard have no workflow-D surface | `tools/results.py` has zero architect awareness; `--workflow` choices are A/B/C only (`_init_wizard.py:2780`) while `config.py:45` defines and validates `architect-build`. |

## Declared mechanical scope (NOT consult questions)

These execute as TDD fixes against the already-ratified design; the panel
may flag objections under Q4 (misses), but they carry no open fork:

- **M1 (gap 1):** add `seal_is_intact` checks at every point of use listed
  above. Each failure becomes `blocked_stop_rule` with a specific rule name
  (`spec_approval_seal_invalid`, `review_seal_invalid`,
  `ruling_seal_invalid`, `build_seal_invalid`,
  `integrity_snapshot_seal_invalid`). Fail-closed, same refusal style as
  the existing spec check.
- **M3 (gap 3):** tree snapshots record EVERY symlink (file or directory)
  as an entry `symlink:<readlink-target>` keyed by its relative path,
  never followed. A symlink planted/retargeted/deleted during a guarded
  window then surfaces as created/modified/deleted. The lane-exclusion
  prune applies ONLY to the real lane path itself (its literal relative
  path), not to arbitrary paths that resolve to the lane - a non-lane
  symlink resolving INTO the lane is recorded like any other symlink, not
  silently pruned.
- **M4 (gap 4):** the delivery gate recheck additionally compares a fresh
  `snapshot_architect_tree` against a NEW sealed baseline
  (`architect-tree-after-verification.yaml`) taken when the verification
  bracket closes green, EXCLUDING the active goal dir (whose
  review/ruling/handoff writes between verification and delivery are
  legitimate supervisor activity) as well as the lane. Any sibling-goal or
  architect-root delta blocks with
  `delivery_architect_tree_recheck_failed`.

## Q1 - Spec-approval binding enforcement + re-approval semantics

**Problem.** The approval artifact binds `spec_sha256`, but nothing
enforces it, and `approve_spec` hard-refuses a second approval ("the
architect owns spec evolution between gates"). Naive enforcement would
deadlock: spec evolves post-approval -> binding check blocks -> operator
cannot re-approve.

**Anchor proposal.**

1. Point-of-use enforcement in `_run_build`: after the existing spec seal
   check, require `approval` seal intact AND
   `approval["spec_sha256"] == spec["payload_sha256"]`. Mismatch ->
   `blocked_stop_rule` rule `spec_approval_binding_mismatch`, detail
   naming both hashes + the remediation command
   (`consensus-mcp-architect approve-spec ...`).
2. Re-approval becomes legal EXACTLY when the binding would fail:
   `handle_approve_spec` refuses only a TRUE duplicate (existing approval
   intact AND its `spec_sha256` == latest sealed spec hash). When the spec
   evolved, it archives the prior approval to
   `spec-approval-superseded-<n>.yaml` (audit chain preserved) and seals a
   fresh approval binding the new hash.
3. `base_sha` on re-approval: if the lane ALREADY exists, the new approval
   CARRIES FORWARD the original `base_sha` (re-binds the spec hash only).
   Rationale: the lane branch is in-flight work anchored at the original
   base; letting re-approval re-stamp `base_sha` to current HEAD would
   silently un-stick the `head moved past approved base_sha` stop rule
   (`architect_loop_step.py:662`) through a side door. If NO lane exists
   yet, the fresh approval stamps `base_sha` = current HEAD as today.

**Alternatives the panel should weigh.** (a) Forbid post-approval spec
revs entirely (architect must converge spec pre-gate; simpler, but kills
the documented spec-evolution affordance and forces kill+recreate for any
mid-goal spec amendment). (b) Auto-expire approval on spec change without
re-approval support (deadlock unless operator hand-deletes the artifact -
rejected: gate must never require manual artifact surgery).

## Q2 - Builder/verification environment isolation shape

**Problem.** Both the builder subprocess and the frozen verification
command inherit nearly the full operator environment. The decisive
experiment proved filesystem containment is supervisor-enforced, but env
credentials enable EXFILTRATION and remote mutation (git push tokens,
cloud creds) that no filesystem snapshot can catch.

**Anchor proposal - asymmetric by role:**

1. **Builder dispatch: strict allowlist (default-deny).** Pass only:
   `HOME`, `USER`, `LOGNAME`, `PATH`, `SHELL`, `TMPDIR`, `TEMP`, `TMP`,
   `TERM`, `LANG`, `TZ`, all `LC_*`, `PYTHONUTF8`, `PYTHONIOENCODING`,
   `NO_COLOR`, `CODEX_HOME`; on Windows additionally `SYSTEMROOT`,
   `SYSTEMDRIVE`, `COMSPEC`, `PATHEXT`, `WINDIR`, `USERPROFILE`,
   `APPDATA`, `LOCALAPPDATA`, `PROGRAMDATA`, `OS`,
   `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE`. Operator extension
   via `CONSENSUS_MCP_BUILDER_ENV_ALLOW` (comma list). Codex CLI auth is
   token-file based under `~/.codex` (reachable via HOME), NOT env-key
   based - verified via the project's codex auth doctrine - so the
   allowlist does not break builder auth.
2. **Verification subprocess: expanded credential denylist
   (default-allow).** Verification runs the OPERATOR's own frozen command
   over arbitrary project toolchains (npm/node/pytest/gradle config vars);
   a strict allowlist breaks real suites unpredictably. Scrub: the
   existing provider keys, plus exact names (`GITHUB_TOKEN`, `GH_TOKEN`,
   `ANTHROPIC_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
   `AWS_SESSION_TOKEN`, `SSH_AUTH_SOCK`, `NPM_TOKEN`, `NODE_AUTH_TOKEN`,
   `PYPI_TOKEN`, `TWINE_USERNAME`, `TWINE_PASSWORD`, `HF_TOKEN`,
   `OPENROUTER_API_KEY`, `CLOUDFLARE_API_TOKEN`, `VERCEL_TOKEN`), plus
   case-insensitive suffix patterns (`*_API_KEY`, `*_TOKEN`, `*_SECRET`,
   `*_PASSWORD`, `*_CREDENTIALS`, `*_ACCESS_KEY`, `*_PRIVATE_KEY`) and
   prefix `AWS_`. Operator keep-list via
   `CONSENSUS_MCP_VERIFICATION_ENV_KEEP` for false positives (e.g. a test
   suite that needs a dummy `FOO_TOKEN`).

**The fork to rule on:** should verification ALSO be strict-allowlist
(strongest, since verification executes builder-authored code via the
project's test suite - it is exactly as attacker-reachable as the builder)
at the cost of breaking env-dependent suites, with the keep-list as the
escape hatch? The anchor says pattern-denylist for verification is the
pragmatic floor; dissent with evidence welcome.

## Q3 - results + init wizard workflow-D UX

**Anchor proposal.**

1. `consensus results` (tool + CLI): when `.consensus/architect/` exists,
   append an `architect_goals` section: per goal - goal id, derived state
   (`open` / `awaiting_spec_approval` / `awaiting_delivery_approval` /
   `closed:<closing_state>`), current cycle, last handoff timestamp.
   Read-only presentation; no new tool; derivation reuses the loop_step
   state-derivation helpers rather than duplicating logic.
2. `--workflow` gains `D`/`d`/`architect-build` choices mapped to
   `workflow.mode=architect-build`, help text labeled "(preview)".
   Non-interactive init with D and no roles config refuses with the
   existing actionable `config.py` error (roles block required).
   Interactive wizard offers D in the picker with the preview label and
   prompts for the architect/builder/reviewer role mapping.
3. Naming consistency: user-facing strings say "Consensus Build
   (workflow D, preview)" - matching README/docs vocabulary.

**Alternative:** keep D out of the wizard while Build is preview
(flag-only). Anchor rejects: v2.0.0 publicly features the two modes; an
operator following the README cannot reach Build through init today -
that IS the UX mismatch.

## Acceptance gates

- M1: unit tests prove each tampered artifact class blocks with its named
  rule; untampered flow unchanged (existing e2e lifecycle test stays
  green).
- M3: tests prove a symlink-to-directory planted under a sibling goal
  during a guarded window is reported; retarget + delete also reported;
  lane itself still excluded by literal path.
- M4: test proves a sibling-goal write AFTER verification-green but BEFORE
  delivery approval blocks delivery with
  `delivery_architect_tree_recheck_failed`; legitimate review/ruling
  writes in the active goal do not.
- Q1: tests prove binding mismatch blocks the build; true-duplicate
  re-approval still refused; evolved-spec re-approval archives + re-binds;
  lane-exists re-approval preserves base_sha.
- Q2: tests prove builder env contains ONLY allowlisted keys; verification
  env drops pattern-matched credentials; keep/allow lists honored;
  Windows-required vars present on win32.
- Q3: tests prove results payload lists goals with correct derived states;
  init --workflow D round-trips to a valid architect-build config.
- Full suite green; no behavior change to Consult (A/B) paths.

## Falsification

Pure-code changes refutable by unit tests - the tests above carry the
proof (no external decisive experiment required for this pass; the
containment-class experiment already ran 2026-06-10 and its
refuted-but-mitigated status is unchanged by this work).
