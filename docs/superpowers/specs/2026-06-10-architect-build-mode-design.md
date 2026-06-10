# Design: `architect-build` workflow mode (alias "D") - the architect loop

Date: 2026-06-10
Status: operator-approved design; AWAITING consensus consult ratification
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
| builder   | cheap     | implement the spec slice; commit to the lane branch; may raise pushback instead of building | write-enabled CLI dispatch confined to the lane worktree |
| reviewer  | cheap     | pre-check the builder's diff before it reaches the architect | existing read-only review dispatch against the lane diff |

Any enabled contributor can fill any role, with two constraints enforced by
validators (section 5). `reviewer` may equal `builder` (fresh dispatch, fresh
context); the architect's ruling still provides the independent check.

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
                               # disables (ruling-only acceptance)
  lane_branch_prefix: "arch-lane/"
```

- `WORKFLOW_ARCHITECT_BUILD = "architect-build"` joins the mode constants;
  aliases `"D"`/`"d"` join `WORKFLOW_ALIASES`.
- `roles:` is a NEW top-level config block, only legal (and required) when
  `workflow.mode: architect-build`. All three role names are required keys.
- Role values must name enabled contributors (`contributors.enabled`).
- `architect_loop:` defaults: `max_cycles: 8`, `verification: ""`,
  `lane_branch_prefix: "arch-lane/"`.

## 4. Supervisor tool: `architect.loop_step`

A new MCP tool + CLI mirroring `loop_run_goal` exactly: a state-machine
coordinator that detects loop state from filesystem inspection of the goal
directory, advances ONE step where it can act mechanically, dispatches at most
one role action, seals the resulting artifact, and returns a `next_action`
hint to the external orchestrator (the host). It never drives the host and
never calls any LLM API itself.

Goal directory: `.consensus/architect/<goal-id>/`.

States (precedence order, top wins):

1.  `goal_invalid`            - problem statement / config invalid
2.  `closed`                  - outcome.yaml has closing_state
3.  `blocked_stop_rule`       - any stop rule fires (reuses `_self_drive`
                                stop-rule machinery + new `max_cycle_count_reached`)
4.  `needs_spec`              - no spec.yaml: dispatch architect with the
                                problem statement -> seal spec.yaml
5.  `awaiting_spec_approval`  - spec.yaml exists, no approval seal:
                                next_action = human gate (`consensus_approve`)
6.  `pushback_raised`         - newest build-result.yaml carries pushback:
                                dispatch architect -> ruling (revise spec or
                                overrule with rationale). A pushback ruling
                                that revises the spec seals it as
                                spec-rev-N.yaml; the HUMAN spec gate does NOT
                                re-fire (the human approved the goal; the
                                architect owns spec evolution between gates)
7.  `needs_build`             - spec approved, cycle N has no build-result:
                                ensure lane worktree exists, dispatch builder
                                write-enabled in the lane -> build-result.yaml
                                (+ lane commits)
8.  `needs_verification`      - build-result exists, no verification record:
                                supervisor runs `architect_loop.verification`
                                in the lane worktree, records frozen gate
                                result. If the gate is RED, the supervisor
                                advances directly to cycle_advance with an
                                implicit revise carrying the failure output
                                back to the builder - red builds never reach
                                the reviewer or the architect (cost guard);
                                max_cycles bounds repeated red cycles
9.  `needs_review`            - verification recorded, no review.yaml:
                                dispatch reviewer read-only against lane diff
10. `needs_ruling`            - review.yaml exists, no ruling.yaml: dispatch
                                architect with HANDOFF digest -> ruling.yaml
                                with disposition in {accept, revise, kill}
11. `cycle_advance`           - ruling=revise: increment cycle counter,
                                next state will be needs_build for cycle N+1
12. `ready_to_deliver`        - ruling=accept AND frozen gate green:
                                next_action = human merge gate
                                (delivery_request / delivery_mint; lane branch
                                merge is performed by the operator/host AFTER
                                mint, never by the supervisor)
13. `killed`                  - ruling=kill: outcome sealed with
                                closing_state=killed, lane branch left intact
                                for forensics

Anti-scope (mirrors loop_run_goal): does not synthesize rulings mechanically,
does not auto-approve gates, does not merge branches, does not call LLM APIs.

## 5. Validators

1. `roles.builder` must reference a profile with NEW field
   `builder_capable: true`. v1: only `codex` (has `--sandbox workspace-write`).
   Profiles without the field default to false.
2. Cross-family closure preserved: at least one of {architect, reviewer} must
   be a DIFFERENT family than builder; that contributor's artifact is the
   gate-eligible signer for the delivery gate. A host_peer cannot satisfy this
   (gate_eligible=false precedent).
3. `roles:` block present iff mode is architect-build; all three keys present;
   values in contributors.enabled.
4. `architect_loop.max_cycles` integer >= 1; `lane_branch_prefix` non-empty,
   no path separators beyond the trailing segment slash.

## 6. Build lane: write-enabled dispatch, contained

- The supervisor creates (or reuses) a git worktree at
  `.consensus/architect/<goal-id>/lane/` on branch
  `<lane_branch_prefix><goal-id>`, branched from the repo's current HEAD at
  spec-approval time.
- Builder dispatch is a NEW invocation shape: for codex,
  `codex exec --skip-git-repo-check --cd <lane-worktree> --sandbox
  workspace-write ...` (prompt via stdin, sealed YAML out, same plumbing as
  the review dispatch). The dispatch-canon validator gains exactly ONE new
  allowlisted builder shape; review dispatches remain read-only canon.
- The main working tree is NEVER touched by any dispatched process. The lane
  branch is NEVER auto-merged; `ready_to_deliver` hands the merge decision to
  the human delivery gate.
- Builder commits inside the lane are the builder's own (`git -C <lane>`
  usable by the builder CLI in workspace-write); the supervisor records the
  lane HEAD sha in build-result.yaml for provenance.
- Reviewer and architect dispatches against the lane read `git -C <lane> diff
  <base>..HEAD` digests, not the live tree, avoiding the kimi
  concurrent-edit integrity false-positive class.

## 7. Repo memory: HANDOFF.md

`.consensus/architect/<goal-id>/HANDOFF.md`, regenerated by the supervisor
after every sealed artifact ("the repo is the brain"):

- spec digest + approval state
- frozen gate definition + latest verification result
- cycle history table: cycle, builder sha, reviewer verdict, ruling
- pushback log
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

## 10. Testing

- State-machine unit tests: every state + transition with fake adapters and
  a tmp git repo (worktree creation, cycle advance, stop rules, kill path).
- Validator tests: roles block legality, builder_capable, cross-family rule,
  alias D resolution.
- Dispatch-shape tests: builder invocation allowlisted, review canon
  unchanged.
- HANDOFF.md renderer tests: golden-file digest from synthetic artifacts.
- Integration smoke with stub CLIs (existing pattern): one full goal from
  needs_spec to ready_to_deliver.
- Suite remains green cross-platform (HOME/USERPROFILE, UTF-8 lessons applied;
  worktree paths exercised on Windows path semantics in CI).

## 11. Deferred (v2+)

- Parallel build lanes (lane a / lane b) with merge-ordering policy.
- builder_capable for kimi/gemini/grok once safe write-mode invocations are
  verified per CLI.
- Architect-side budget telemetry (tokens per role per goal).
- Optional per-ruling human gate mode (config flag) for high-stakes repos.

## 12. Open questions for the ratification consult

1. Should the reviewer role be optional (architect-rules-raw) via
   `reviewer: none`, or always required in v1?
2. Spec-approval gate: reuse `consensus_approve` as-is or mint a dedicated
   spec-seal artifact type?
3. Is one allowlisted builder dispatch shape per CLI acceptable to the
   dispatch-canon validator philosophy, or should builder shapes live under a
   separate validator namespace?
4. Stop-rule set: which of the existing 9 apply verbatim to architect-build,
   and is max_cycles + scope-drift sufficient as the v1 additions?
