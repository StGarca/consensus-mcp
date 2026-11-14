# Design: `architect-build` workflow mode (alias "D") - the architect loop

Date: 2026-06-10
Status: RATIFIED WITH MODIFICATIONS - 4-AI anchored consult 2026-06-10
  (iteration-architect-build-design-2026-06-10, panel codex+gemini+grok+kimi,
  4/4 goal_satisfied, 0 blocking objections; converged-plan sha256
  06c47a14b7d69931ea6ce931cbff5d444adf9155747bb37c390cf9628c186c84).
  All panel amendments are folded into this document; section 12 records the
  resolutions.
Source idea: @jumperz11 "the architect loop" (x.com/jumperz/status/2064749412803289168)
  - "fable thinks - codex builds - the repo remembers - you judge"
  - "the architect is the edge - the builder is the hands - the repo is the brain"

## 1. Problem and concept

All existing consensus modes (post-review "B", propose-converge "A", advisory,
autonomous-execute "C" contract) treat contributors as symmetric reviewers or
proposers voting on artifacts. There is no mode for the asymmetric
cost-optimized pattern now common in the field: an expensive model PLANS and
RULES while a cheap model BUILDS, with the repository as shared memory and the
human making a small number of gate calls.

`architect-build` adds that mode. The user poses a problem, maps three roles
onto models of their choice, and a supervisor state machine orchestrates
spec -> build -> review -> ruling cycles to completion. There is NO voting and
NO convergence evaluation across peers: the architect rules, subject to two
human gates and the stop-rule machinery.

Operator decisions locked during brainstorming (2026-06-10):

1. Builder executes with REAL write access inside an isolated git worktree
   lane (not patch emission).
2. Role set v1: architect + builder + reviewer, freely mappable, single build
   lane (lane a/b parallelism deferred to v2).
3. Human gates: spec approval + delivery/merge approval; everything between is
   autonomous.
4. Architecture: supervisor tool + config contract, mirroring `loop_run_goal`;
   the engine does NOT drive multi-cycle loops.

## 2. Roles

| Role      | Cost tier | Responsibility | Dispatch shape |
|-----------|-----------|----------------|----------------|
| architect | expensive | author spec; rule on cycle results (accept / revise / kill); answer pushback | host callback when `claude`, read-only CLI dispatch otherwise; digest-fed |
| builder   | cheap     | EDIT FILES inside the lane worktree per the spec slice; may raise pushback instead of building. Never runs git - the supervisor commits (consult Q1) | write-enabled CLI dispatch confined to the lane worktree |
| reviewer  | cheap     | pre-check the builder's diff before it reaches the architect. REQUIRED in v1 (consult Q4) | existing read-only review dispatch against the lane diff |

Any enabled contributor can fill any role, with the constraints enforced by
validators (section 5). `reviewer` may equal `builder` (fresh dispatch, fresh
context) - this is how the cheap two-subscription config works; `reviewer:
none` is NOT allowed in v1 (consult Q4: removing the reviewer would force the
expensive architect to first-pass-review raw builder output, inverting the
mode's cost optimization; a `none` sentinel with a strict cross-family
architect validator is preserved as a v2 follow-up).

When `architect: claude`, the host session IS the architect (host-adapter
precedent, `kind: host`): the host authors spec and rulings via the existing
artifact-callback path. This is the original tweet's "$20 sub" case. When the
architect is a CLI contributor, it is dispatched read-only with a new
architect template.

## 3. Config contract

```yaml
workflow:
  mode: architect-build        # alias: D / d
roles:
  architect: claude
  builder: codex
  reviewer: codex
architect_loop:
  max_cycles: 8                # stop rule: max_cycle_count_reached
  verification: "pytest -q"    # frozen gate command, run by the supervisor
                               # in the lane worktree each cycle; empty string
                               # disables (state is skipped - no phantom gate)
  lane_branch_prefix: "arch-lane/"
  max_wall_clock_minutes: 0    # optional; 0 = disabled (consult Q3)
```

- `WORKFLOW_ARCHITECT_BUILD = "architect-build"` joins the mode constants;
  aliases `"D"`/`"d"` join `WORKFLOW_ALIASES`.
- `roles:` is a NEW top-level config block, only legal (and required) when
  `workflow.mode: architect-build`. All three role names are required keys.
- Role values must name enabled contributors (`contributors.enabled`).
- `architect_loop:` defaults: `max_cycles: 8`, `verification: ""`,
  `lane_branch_prefix: "arch-lane/"`, `max_wall_clock_minutes: 0`.

## 4. Supervisor tool: `architect.loop_step`

A new MCP tool + CLI mirroring `loop_run_goal` exactly: a state-machine
coordinator that detects loop state from filesystem inspection of the goal
directory, advances ONE step where it can act mechanically, dispatches at most
one role action, seals the resulting artifact, and returns a `next_action`
hint to the external orchestrator (the host). It never drives the host and
never calls any LLM API itself.

Goal directory: `.consensus/architect/<goal-id>/`.

States (precedence order, top wins; amendments from consult Q3 marked):

1.  `goal_invalid`            - problem statement / config invalid
2.  `closed`                  - outcome.yaml has closing_state
3.  `blocked_stop_rule`       - any stop rule fires. The stop-rule SET is
                                MODE-SPECIFIC (consult Q3, kimi dissent
                                adopted): the supervisor mirrors the
                                `_self_drive` stop-rule PATTERN
                                (filesystem-inspect, fail-closed) but the
                                quorum-shaped rules do not transfer. v1 set:
                                `max_cycle_count_reached`,
                                `repeated_verification_failure_same_signature`
                                (3 consecutive RED cycles with the same
                                stderr-hash), `lane_integrity_violation`
                                (symlink/hardlink escape found in lane),
                                `builder_containment_breach` (main-repo
                                integrity snapshot changed),
                                `stale_dispatch_in_flight` (in-flight lock
                                older than TTL), `cross_document_drift`
                                (HANDOFF.md vs sealed artifacts),
                                `wall_clock_budget_exceeded` (optional
                                `architect_loop.max_wall_clock_minutes`)
4.  `dispatch_in_flight`      - (NEW, consult Q3) an atomic in-flight lock
                                seal exists for a running dispatch: loop_step
                                returns wait - never double-dispatches. Lock
                                written before any subprocess, cleared on
                                artifact seal; stale lock past TTL fires
                                `stale_dispatch_in_flight`
5.  `needs_spec`              - no spec.yaml: dispatch architect with the
                                problem statement -> seal spec.yaml
6.  `awaiting_spec_approval`  - spec.yaml exists, no spec-approval.yaml:
                                next_action = human gate via the NEW
                                `architect.approve_spec` thin gate tool
                                (consult Q5 - `consensus_approve` is NOT
                                reused: verified precondition mismatch, it
                                requires a converged plan + >=2 non-claude
                                reviews). spec-approval.yaml binds spec_sha256
                                + base_sha (the main HEAD the lane branches
                                from) + approver + approved_at_utc
7.  `blocked_base_drift`      - (NEW, consult Q3) main repo HEAD no longer
                                matches the sealed base_sha: block before
                                stale-base work/merge; next_action = operator
                                decision (rebase lane / restart / accept risk)
8.  `pushback_raised`         - newest build-result.yaml carries pushback:
                                dispatch architect -> ruling (revise spec or
                                overrule with rationale). A pushback ruling
                                that revises the spec seals it as
                                spec-rev-N.yaml; HANDOFF records the current
                                spec_sha so the architect never reads a stale
                                digest; the HUMAN spec gate does NOT re-fire
                                (the human approved the goal; the architect
                                owns spec evolution between gates)
9.  `needs_build`             - spec approved, cycle N has no build-result:
                                ensure lane worktree exists, dispatch builder
                                write-enabled in the lane -> build-result.yaml;
                                the SUPERVISOR then runs the lane git ops
                                (section 6) and records lane_head_sha
10. `needs_verification`      - build-result exists, no verification record:
                                supervisor runs `architect_loop.verification`
                                in the lane worktree, records the frozen gate
                                result. If the gate is RED, the supervisor
                                seals a MECHANICAL cycle-N/ruling.yaml
                                (disposition=revise,
                                reason=verification_failed, stderr digest -
                                consult Q3: regular artifact shape, no
                                "implicit" transitions) and advances to
                                cycle_advance; red builds never reach the
                                reviewer or the architect (cost guard).
                                When `architect_loop.verification` is empty,
                                this state is SKIPPED (no phantom green gate)
11. `needs_review`            - verification recorded (or skipped), no
                                review.yaml: dispatch reviewer read-only
                                against the lane diff
12. `needs_ruling`            - review.yaml exists, no ruling.yaml: dispatch
                                architect with HANDOFF digest -> ruling.yaml
                                with disposition in {accept, revise, kill}
13. `cycle_advance`           - ruling=revise: increment cycle counter,
                                next state will be needs_build for cycle N+1
14. `awaiting_delivery_approval` - (split from ready_to_deliver, consult Q3)
                                ruling=accept AND frozen gate green (or
                                verification disabled) AND the
                                GateEligibleCrossFamilySigner invariant holds
                                (section 5): next_action = human merge gate
                                (delivery_request / delivery_mint binding the
                                exact lane HEAD + base sha; merge conflicts
                                are REPORTED to the human, never
                                auto-resolved; lane branch merge is performed
                                by the operator/host AFTER mint, never by the
                                supervisor)
15. `killed`                  - ruling=kill: outcome sealed with
                                closing_state=killed, lane branch left intact
                                for forensics

All supervisor writes are atomic (temp + fsync + rename via the existing
`_atomic_io` helpers) under a goal-dir lock, so interrupted steps resume
deterministically (consult Q3).

Anti-scope (mirrors loop_run_goal): does not synthesize architect rulings
(the RED-gate mechanical revise ruling is the one defined exception and is
labeled as such in the artifact), does not auto-approve gates, does not merge
branches, does not call LLM APIs.

## 5. Validators

1. `roles.builder` must reference a profile with NEW field
   `builder_capable: true`. v1: only `codex` (has `--sandbox workspace-write`).
   Profiles without the field default to false.
2. GateEligibleCrossFamilySigner invariant (consult Q2, adapted from the
   Task #28 closure invariant in `_closure_invariant.py`): the
   delivery-authorizing artifact (the cross-family reviewer's review.yaml,
   else the architect's accept ruling.yaml) must satisfy ALL of:
   (a) cross_family - signer model_family != builder model_family;
   (b) hash_binding - the signer binds the exact lane_head_sha (+ base_sha)
       it judged;
   (c) freshness - signer sealed AFTER the final build-result;
   (d) host_peer artifacts can never be the signer (gate_eligible=false
       precedent). The host filling the ARCHITECT role IS gate-eligible
       (first-class role, human delivery gate remains the trust root) - but
       when the host architect is the ONLY cross-family signer, HANDOFF.md
       flags it prominently; a v2 `strict_signer: cli_only` flag is preserved
       for operators who want CLI-only signers.
   Config-time enforcement: the roles config is REJECTED at start (not at
   delivery) if no member of {architect, reviewer} is cross-family vs
   builder.
3. `roles:` block present iff mode is architect-build; all three keys present
   (reviewer REQUIRED in v1 - consult Q4); values in contributors.enabled.
4. `architect_loop.max_cycles` integer >= 1; `lane_branch_prefix` non-empty,
   no path separators beyond the trailing segment slash; optional
   `max_wall_clock_minutes` integer >= 0 (0 = disabled).
5. Builder-shape rule (consult Q1/Q6): builder dispatch argv must NOT contain
   git tokens - the builder-shape validator rejects any builder dispatch that
   attempts git.

## 6. Build lane: write-enabled dispatch, contained

The consult's central amendment (Q1, 4/4 IMPROVE): the draft's assumption
that the builder could run `git -C <lane>` under workspace-write is
WITHDRAWN. A worktree's `.git` is a FILE dereferencing into the main repo's
`.git/worktrees/<id>/`, so builder-run git either breaks under a strict
sandbox or escapes a permissive one. v1 containment contract (layered):

1. Lane boundary: the supervisor creates (or reuses) a git worktree at
   `.consensus/architect/<goal-id>/lane/` on branch
   `<lane_branch_prefix><goal-id>`, branched from the sealed base_sha at
   spec-approval time. The lane path must resolve (symlink-resolved, no
   `..`) under the goal dir. Goal-id uniqueness is checked at lane creation
   (branch-collision guard).
2. Builder edits FILES ONLY: for codex,
   `codex exec --skip-git-repo-check --cd <lane-worktree> --sandbox
   workspace-write ...` (prompt via stdin, sealed YAML out, same plumbing as
   the review dispatch). Builder argv must not contain git tokens
   (validator-enforced). Builder network policy in v1 is the CLI's own
   default, documented per profile.
3. SUPERVISOR-OWNED GIT: the supervisor alone runs `git worktree
   add/remove`, `git -C <lane> add -A`, `commit`, `diff`. Every lane git op
   runs with hooks neutralized (`-c core.hooksPath=<empty dir>`) and a
   scrubbed env. The supervisor records lane_head_sha in build-result.yaml
   for provenance.
4. Post-build lane scan BEFORE any git op: any symlink in the lane, or any
   hardlink whose inode resolves outside the lane (O_NOFOLLOW stat), fires
   `lane_integrity_violation` (root-fix doctrine: the symlink/TOCTOU class
   is fixed at one primitive).
5. Main-repo integrity snapshot (the consult's root-cause-INDEPENDENT
   safeguard - works even if every sandbox assumption is wrong): before and
   after every builder dispatch the supervisor records main working-tree
   status, HEAD + ref shas, and hooks/config hashes; ANY non-lane change
   fires `builder_containment_breach`, and the delivery gate independently
   re-checks the snapshot before `awaiting_delivery_approval`.

The main working tree is NEVER touched by any dispatched process. The lane
branch is NEVER auto-merged; the human delivery gate owns the merge.
Reviewer and architect dispatches against the lane read `git -C <lane> diff
<base>..HEAD` digests, not the live tree, avoiding the kimi concurrent-edit
integrity false-positive class.

Write-enabled builder shapes live in a SEPARATE validator namespace
(consult Q6, unanimous; e.g. `validate_builder_dispatch`) - never merged
into the read-only dispatch canon: exact argv shape per builder_capable CLI,
`--sandbox workspace-write` exactly, `--cd` resolving under
`.consensus/architect/*/lane`, no git tokens, env allowlist, no shell
wrapper, sealed YAML output required.

Builder-owned lane commits remain a v2 promotion candidate, gated on the
decisive experiment: an integration test dispatching REAL codex
(workspace-write, lane cwd) instructed to (1) edit a lane file, (2) attempt
a git commit, (3) attempt a write outside the lane - asserting the lane edit
succeeds, no git effect occurs, the outside write fails, and the main repo
is byte-identical per the snapshots. This experiment runs during v1
implementation as a named test (empirical_status: pending until then).

## 7. Repo memory: HANDOFF.md

`.consensus/architect/<goal-id>/HANDOFF.md`, regenerated by the supervisor
after every sealed artifact ("the repo is the brain"):

- spec digest + approval state + CURRENT spec_sha (tracks spec-rev-N so a
  post-pushback architect never reads a stale digest - consult Q7)
- frozen gate definition + latest verification result
- cycle history: ROLLING WINDOW of the last 5 cycles inline (cycle, builder
  sha, reviewer verdict, ruling) + a one-line summary of older cycles with
  pointers to their sealed artifacts - bounded size so architect cost does
  not inflate across cycles 5-8 (consult Q7)
- pushback log
- a prominent flag when the host architect is the only cross-family signer
  (consult Q2 transparency rule)
- pointers to raw sealed artifacts (spec.yaml, cycle-N/*.yaml)

Architect dispatches receive HANDOFF.md (+ the current cycle's review and
diff digest), NOT the whole repo - this is what keeps expensive-model calls
cheap and is the load-bearing cost optimization of the whole mode.

Sealed artifacts per cycle under `cycle-<N>/`: `build-result.yaml`,
`verification.yaml`, `review.yaml`, `ruling.yaml`; top level: `spec.yaml`,
`outcome.yaml`. All sealed with the existing provenance/seal machinery.

## 8. Cost shape

- Architect (expensive): 1 spec dispatch + 1 ruling per cycle + pushback
  rulings. Digest-fed, short.
- Builder + reviewer (cheap): all volume work.
- Supervisor: deterministic Python, zero LLM cost.
- Human: exactly 2 mandatory touchpoints (spec gate, merge gate).

## 9. Engine touchpoints

`run_iteration` is NOT extended with a fifth in-engine branch: architect-build
is supervisor-driven (decision 4). Config/constants/validators live in
`config.py`; the supervisor tool lives in `consensus_mcp/tools/`
(`architect_loop_step.py`) with dispatch templates in `dispatch_templates/`
(`architect_spec_template.md`, `architect_ruling_template.md`,
`builder_build_template.md` + per-CLI schema files as needed). If
`workflow.mode: architect-build` reaches `run_iteration`, it raises a clear
WorkflowError pointing to the supervisor tool (same pattern as C's
NotImplementedError, but permanent and intentional).

Additional tool surface from the consult:
- `architect.approve_spec` - thin human spec gate (mirrors
  `tools/delivery_gate.py`), seals spec-approval.yaml (consult Q5).
- `architect.cleanup` - lane lifecycle: removes worktree + branch for a
  closed goal (prune is opt-in); lanes of KILLED goals are retained for
  forensics (consult Q7).
- `consensus_mcp/validators/validate_builder_dispatch.py` - the separate
  write-enabled dispatch canon (consult Q6).

## 10. Testing

- State-machine unit tests: every state + transition with fake adapters and
  a tmp git repo (worktree creation, cycle advance, stop rules, kill path,
  dispatch_in_flight lock + stale TTL, blocked_base_drift, RED mechanical
  ruling, verification-disabled skip path).
- Containment tests: lane symlink/hardlink scan, main-repo integrity
  snapshot trip, hooks neutralization, lane path resolution (no `..`).
- THE DECISIVE EXPERIMENT (named test, real codex CLI, env-gated like the
  existing real-smoke tests): workspace-write lane dispatch attempting
  lane edit / git commit / outside write; asserts containment
  (consult falsification block - empirical_status pending until this runs).
  Also covers builder CLI auth under lane cwd (consult Q7, provisional).
- Validator tests: roles block legality, builder_capable, config-time
  cross-family rejection, signer invariant (hash binding + freshness),
  no-git-in-argv, alias D resolution.
- Dispatch-shape tests: builder shape in the SEPARATE namespace, read-only
  review canon byte-unchanged.
- HANDOFF.md renderer tests: golden-file digest from synthetic artifacts,
  rolling-window cap.
- Integration smoke with stub CLIs (existing pattern): one full goal from
  needs_spec to awaiting_delivery_approval.
- Suite remains green cross-platform (HOME/USERPROFILE, UTF-8 lessons applied;
  worktree path normalization + long-path/MAX_PATH exercised on Windows CI).

## 11. Deferred (v2+)

- Parallel build lanes (lane a / lane b) with merge-ordering policy.
- builder_capable for kimi/gemini/grok once safe write-mode invocations are
  verified per CLI.
- Builder-owned lane commits (grok's split-authority position), gated on the
  decisive containment experiment passing AND a renegotiated contract with an
  explicit git-command whitelist.
- `reviewer: none` sentinel + strict cross-family architect validator
  (grok/gemini position, preserved from consult Q4).
- `strict_signer: cli_only` config flag (from the consult Q2 kimi position).
- Architect-side budget telemetry (tokens per role per goal).
- Optional per-ruling human gate mode (config flag) for high-stakes repos.

## 12. Consult resolutions (2026-06-10)

Anchored 4-AI consult iteration-architect-build-design-2026-06-10
(codex-abd-1, gemini-abd-1, grok-abd-1, kimi-abd-1): 4/4 goal_satisfied,
0 blocking objections. Full weighted synthesis, votes, falsification +
independent-safeguard convention blocks, and rejected-position rationale:
`consensus-state/active/iteration-architect-build-design-2026-06-10/converged-plan.yaml`
(sha256 06c47a14b7d69931ea6ce931cbff5d444adf9155747bb37c390cf9628c186c84,
validated strict, approval minted via consensus-mcp-approve with 4 non-claude
reviewers).

1. Builder containment -> supervisor-owned git, 5-layer contract (section 6).
2. Closure -> GateEligibleCrossFamilySigner invariant (section 5).
3. State machine -> dispatch_in_flight, blocked_base_drift, mechanical RED
   ruling, mode-specific stop-rule set (section 4).
4. Reviewer -> REQUIRED in v1 (2-2 split, synthesis adopted always-require;
   none-sentinel preserved for v2).
5. Spec gate -> dedicated spec-approval.yaml + architect.approve_spec
   (unanimous after verification of consensus_approve preconditions).
6. Builder shapes -> separate write-enabled validator namespace (unanimous).
7. Misses adopted -> lane lifecycle, HANDOFF rolling window, Windows paths,
   auth-in-lane, delivery hash binding, conflict reporting, branch collision,
   network policy (sections 4/6/7/9/10).
