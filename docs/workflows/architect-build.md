# Workflow D - architect-build

**Status:** shipped (design ratified by the 4-AI consult
`iteration-architect-build-design-2026-06-10`; engine `run_iteration`
permanently refuses the mode - it is supervisor-driven by design).

Architect-build is the asymmetric-cost workflow: an EXPENSIVE model plans
and rules, a CHEAP model builds, the repo remembers, and the human gates.
The architect (e.g. claude) writes one spec and one short ruling per cycle,
fed only a bounded HANDOFF.md digest - never the whole repo. The builder
(v1: codex, the only `builder_capable` profile) does all the volume work
write-enabled, but confined to a git worktree lane it cannot escape and
with git operations it is never allowed to run (supervisor-owned git). A
deterministic Python supervisor (`architect.loop_step`) drives the
spec -> build -> verify -> review -> ruling cycle at zero LLM cost, and the
human touches the loop at exactly two mandatory points: approving the spec
before any build, and approving + merging the delivery at the end. The
result: expensive-model spend stays flat per cycle while cheap-model cycles
do the iteration.

## Quickstart

### 1. Configure the mode

In `.consensus/config.yaml` (spec section 3):

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

Config-time rules (rejected at load, never at delivery):

- `roles:` is required, with all three keys (`reviewer` is REQUIRED in v1,
  consult Q4). Every role value must name a contributor in
  `contributors.enabled`.
- `roles.builder` must resolve to a profile with `builder_capable: true`
  (v1: only codex). Write-enabled dispatch is never granted implicitly.
- Cross-family floor (consult Q2): at least one of architect/reviewer must
  resolve to a DIFFERENT model family than the builder.
- `architect_loop` defaults when omitted: `max_cycles: 8`,
  `verification: ""`, `lane_branch_prefix: "arch-lane/"`,
  `max_wall_clock_minutes: 0`.

### 2. Set up a goal

```bash
mkdir -p .consensus/architect/<goal-id>
$EDITOR .consensus/architect/<goal-id>/problem.md   # the problem statement
```

Goal ids must match `[A-Za-z0-9][A-Za-z0-9._-]*` and may not be a Windows
reserved device name (CON, NUL, COM1...) or end with a dot - the layout has
to stay addressable on every platform.

#### Optional: design with a Looper plan (design coach)

At goal setup you may launch Build **with or without a Looper plan**. The host
should offer the choice: *"Design with a Looper plan first (design coach), or go
direct (Build execution loop)?"*

- **Without** (default): write `problem.md` yourself, as above. Nothing changes.
- **With**: invoke the `consensus-looper-plan` skill against the goal id. It is a
  design coach (built on a vendored MIT-licensed loop-design slice; attribution
  in `consensus_mcp/looper_plan/NOTICE`) that **coaches** a sharp goal, typed
  verification (programmatic / judge / human), and termination caps against
  built-in rubrics, then **seeds** this goal: it writes a synthesized
  `problem.md` (the architect's context), a `looper-plan/` directory
  (`loop.yaml` + `loop.resolved.json` + `LOOP.md` design preview), a
  `looper-suggestions.yaml`, and a `looper-plan-manifest.yaml`. It then presents
  the suggested frozen `verification` command + `acceptance_gates` + cycle/budget
  caps for explicit confirmation (suggest+confirm - nothing lands in
  `.consensus/config.yaml` silently). After that you run `step` exactly as below.

Looper is the **design layer**; Consensus Build is the **executor** (Looper's own
docs defer durable orchestration to a tool like Build). The integration is
surgical by construction: the supervisor, invariants, gates, seals, and schemas
get a **zero diff** - Looper only produces inputs Build already consumes
(`problem.md` + goal-dir context). The looper artifacts are written once at
goal-setup, before the first builder dispatch, and are write-once-immutable: the
existing architect-tree integrity recheck covers them for free, and the wizard
refuses to re-coach a goal Build has already begun. See
`docs/superpowers/specs/2026-06-23-consensus-build-looper-plan-design.md`.

### 3. Run the loop

```bash
consensus-mcp-architect step --goal-dir .consensus/architect/<goal-id>
```

Call it repeatedly. Each step inspects the goal directory, advances ONE
mechanical step where it can (builder dispatch, frozen-gate verification),
seals the resulting artifact, regenerates `HANDOFF.md`, and prints a JSON
result with `state`, `next_action`, `cycle`, `actions_taken`, and
`stop_rules_fired`. The same supervisor is exposed as the MCP tool
`architect.loop_step` (`--no-dispatch` / `auto_dispatch: false` reports
`needs_build` instead of dispatching). The supervisor never calls an LLM
API itself and never merges a branch.

### State table

Every state the supervisor can report, and what it asks of you:

| State | What it means | What it asks of you (next_action) |
|---|---|---|
| `goal_invalid` | goal dir, problem.md, or config invalid | fix the goal dir / config and re-run `step` |
| `closed` | `outcome.yaml` has a closing_state | nothing to do |
| `killed` | architect ruled `kill`; outcome sealed | nothing; lane retained for forensics |
| `blocked_stop_rule` | a stop rule fired (see list below) | operator decision required |
| `dispatch_in_flight` | the in-flight lock is held for a running dispatch (claimed O_EXCL test-and-set before any lane work, so concurrent steps can never double-dispatch into the same lane) | call `step` again later |
| `needs_spec` | no sealed spec.yaml | ARCHITECT action: author the spec and seal it to `<goal>/spec.yaml` via `_architect_paths.seal_artifact` (host callback when architect=claude; otherwise dispatch the architect CLI with `architect_spec_template.md`) |
| `awaiting_spec_approval` | spec sealed, no spec-approval.yaml | HUMAN gate: `consensus-mcp-architect approve-spec` (below) |
| `blocked_base_drift` | main HEAD no longer matches the approved base_sha | the supervisor has no drift override (the approval binds the exact base): restart the goal from the new HEAD, or take over manually - inspect the lane branch and rebase/merge it yourself outside the loop |
| `pushback_raised` | newest build-result.yaml carries builder pushback | ARCHITECT action: seal a ruling (disposition `revise` or `overrule`; `kill` also legal; `accept` is FORBIDDEN on a pushback cycle); a spec revision seals as `spec-rev-N.yaml`; the human spec gate does NOT re-fire |
| `needs_build` | cycle has no build-result (reported with `--no-dispatch`) | re-run `step` with dispatch enabled, or dispatch the builder manually and seal `build-result.yaml` |
| `built` | builder ran; supervisor committed the lane | call `step` again |
| `needs_verification` | build sealed, frozen gate not yet run | call `step` again - the supervisor runs the gate itself; skipped entirely when `verification` is empty (no phantom green gate) |
| `verification_red` | frozen gate RED; a MECHANICAL revise ruling was sealed (regular artifact shape, consult Q3; re-sealed idempotently on resume if the ruling write was lost to an interrupt - a RED build can never fall through to review) | call `step` again to start the next cycle; red builds never reach the reviewer or the architect (cost guard) |
| `needs_review` | verification green (or disabled), no review.yaml | REVIEWER action: review the lane diff (`git -C <lane> diff <base>..HEAD`) and seal `review.yaml` `{verdict, lane_head_sha}` into the cycle dir |
| `needs_ruling` | review sealed, no ruling.yaml | ARCHITECT action: read HANDOFF.md + the cycle review, seal `ruling.yaml` `{disposition: accept|revise|kill, lane_head_sha, reason?}` |
| `cycle_advance` | a revise/overrule ruling closed the cycle | call `step` again; the next step starts cycle N+1's build with the ruling's feedback |
| `awaiting_delivery_approval` | ruling=accept, gate green (or disabled), signer invariant holds, and the delivery integrity re-check is clean (main snapshot re-verified, lane re-scanned, lane HEAD still the sha the signer judged); `actions_taken` carries the bound `lane_head_sha` + `base_sha` for the delivery mint | HUMAN gate: delivery approval, then merge the lane branch yourself - the supervisor never merges |

Stop rules (the `blocked_stop_rule` set is MODE-SPECIFIC, consult Q3):
`max_cycle_count_reached`, `repeated_verification_failure_same_signature`
(3 consecutive RED cycles with the same output signature; volatile tokens -
durations, hex addresses, clock times - are normalized out before hashing
so e.g. pytest's wall-clock line cannot make identical failures look
distinct),
`stale_dispatch_in_flight` (in-flight lock older than the TTL, default
3600s via `CONSENSUS_MCP_ARCHITECT_IN_FLIGHT_TTL`),
`wall_clock_budget_exceeded`, `cross_document_drift` (a HANDOFF.md newer
than the latest spec seal that claims a different spec sha),
`lane_integrity_violation` / `builder_containment_breach` /
`verification_containment_breach` (containment section below),
`spec_seal_invalid` (the spec/spec-rev driving a build no longer reproduces
its payload_sha256 - edited after sealing), `pushback_accept_forbidden`,
`signer_invariant_violated` (includes a missing/unbound/stale review.yaml:
the v1-required reviewer is enforced at runtime, not just config time),
`delivery_integrity_recheck_failed` (the accept-time re-check found a
main-tree delta, a lane scan violation, or a lane HEAD that moved after the
build seal), `builder_dispatch_failed`, and `verification_machinery_failed`.

## The two human gates

### Gate 1: spec approval

```bash
consensus-mcp-architect approve-spec \
  --goal-dir .consensus/architect/<goal-id> --approver <you>
```

(Also the MCP tool `architect.approve_spec`.) Read the sealed spec first;
the gate seals `spec-approval.yaml` binding `spec_sha256` (the exact spec
you read - a tampered seal is refused) plus `base_sha`, the main HEAD the
lane will branch from. It refuses a second approval: the architect owns
spec evolution between gates (`spec-rev-N.yaml` after pushback), and the
human gate fires once per goal. If main HEAD later moves past `base_sha`,
the loop blocks on `blocked_base_drift` rather than building on a stale
base. There is no drift override: the approval binds the exact base, so
either restart the goal from the new HEAD or finish the lane manually
(inspect the lane branch, rebase/merge it yourself outside the loop).

### Gate 2: delivery approval + manual merge

When the loop reaches `awaiting_delivery_approval`, the supervisor has
verified the GateEligibleCrossFamilySigner invariant (consult Q2): the
delivery-authorizing artifact (cross-family reviewer's review, else the
architect's accept ruling) is cross-family vs the builder, binds the exact
`lane_head_sha` it judged, and was sealed after the final build - plus a
fresh, hash-bound `review.yaml` exists (the v1-required reviewer, enforced
at runtime). It has also independently re-checked the integrity snapshot
and re-scanned the lane at the accept transition, and its `actions_taken`
carries the bound `lane_head_sha` + `base_sha`. From here everything is
yours:

1. Inspect the lane branch (`<lane_branch_prefix><goal-id>`) and its diff.
2. Approve delivery (the existing `delivery_request` / `delivery_mint`
   tooling can bind the exact lane HEAD + base sha if you want a sealed
   approval artifact).
3. Merge the lane branch yourself. The supervisor NEVER merges; merge
   conflicts are reported to you, never auto-resolved.
4. Seal the outcome (`outcome.yaml` with `closing_state: delivered` via
   `_architect_paths.seal_artifact`), then optionally
   `consensus-mcp-architect cleanup --goal-dir <g> --prune-lane` to remove
   the worktree + branch. Cleanup is fail-closed: only `delivered` goals
   are prune-eligible; `killed` goals retain their lane for forensics
   (consult Q7), and any unknown closing state is refused.

## Containment contract

The builder edits FILES ONLY. The 2026-06-10 consult withdrew the
assumption that a builder may run git in a worktree (a worktree's `.git`
is a pointer file into the main repo's gitdir), so containment is layered:

1. **Lane boundary** - the supervisor creates a git worktree at
   `.consensus/architect/<goal-id>/lane/` on
   `<lane_branch_prefix><goal-id>`, branched from the approved `base_sha`.
   Goal-id/branch collisions are refused at lane creation.
2. **Write-enabled canon** - the builder argv must match an EXACT 12-token
   positional allowlist (`validators/validate_builder_dispatch.py`,
   separate from the read-only dispatch canon, consult Q6): codex
   `exec --skip-git-repo-check --cd <lane> --sandbox workspace-write
   --output-schema <schema> -o <out> -`. Anything longer, shorter, or
   positionally different - including the token `git` anywhere - aborts the
   dispatch fail-closed.
3. **Supervisor-owned git** - the supervisor alone runs `worktree add` /
   `add -A` / `commit` / `diff`, every invocation with hooks neutralized
   and a scrubbed env, and it refuses to run git against a lane whose
   `.git` pointer no longer targets the main gitdir's worktrees.
4. **Post-build lane scan** - after every builder dispatch, any symlink or
   Windows junction in the lane, any hardlink whose inode also lives
   outside the lane, or a tampered `.git` pointer fires
   `lane_integrity_violation`.
5. **Integrity snapshots (the load-bearing guard).** The decisive
   experiment (below) PROVED that codex `workspace-write` does **not**
   confine writes to `--cd` - a builder can write anywhere, including the
   repo root. The design never assumed otherwise; this guard is
   root-cause-independent and is what actually contains the builder. Before
   and after every builder dispatch the supervisor records main HEAD,
   working-tree status, branch refs, and hooks/config hashes (ANY non-lane
   delta fires `builder_containment_breach`), AND brackets the dispatch with
   an **architect-tree snapshot** - sha256 of every file under
   `.consensus/architect/` except the active lane. The cycle seals (spec,
   approval, review, ruling) are content hashes, not authenticity
   signatures, and the whole architect subtree is excluded from the
   git-status view, so a builder escaping the lane to forge this goal's
   seals - or a *sibling* goal's - would otherwise be invisible. The frozen
   verification gate (which executes lane content unsandboxed, with all
   AI-provider API keys scrubbed from its environment) gets the same
   architect-tree treatment - a delta fires
   `verification_containment_breach` - and the accept transition re-runs the
   snapshot check + lane scan before `awaiting_delivery_approval`
   (`delivery_integrity_recheck_failed` on any delta).

When any of these fire, the supervisor seals `containment-breach.yaml` in
the goal dir and the loop stops; the record's presence is a PERSISTENT stop
rule. What to do: read the `violations` list in the breach record, inspect
the lane and `git -C <repo> status` / `git reflog` for what actually
changed, and treat the lane content as untrusted. The supervisor never
clears the record - resuming requires you to delete
`containment-breach.yaml` yourself after investigating (an explicit
operator decision), or to kill/restart the goal. The main working tree is
never touched by any dispatched process, and the lane branch is never
auto-merged.

## Hardening pass (post-v2.0.0, consult 2026-06-11)

Ratified by the 3-AI anchored consult
`iteration-architect-hardening-2026-06-11` (codex+gemini+grok, zero
blocking objections); design doc:
`docs/superpowers/specs/2026-06-11-architect-hardening-design.md`.

1. **Point-of-use seal verification.** Every sealed artifact is
   re-verified where its content grants state progression - spec
   approval, build result, verification, review, ruling, outcome, the
   integrity baseline - each with its own stop rule
   (`*_seal_invalid`). A body edited after sealing blocks the loop
   instead of steering it.
2. **Spec-approval binding is enforced.** `loop_step` refuses to build
   when the latest sealed spec is not the approved one
   (`spec_approval_binding_mismatch`). Re-approval is legal exactly
   when the spec evolved: `approve-spec` archives the prior approval to
   `spec-approval-superseded-<n>.yaml` and re-binds; a true duplicate
   is still refused; re-approval is refused while a dispatch is in
   flight; and when the lane already exists the original `base_sha` is
   carried forward (re-approval never un-sticks the head-moved stop).
3. **Symlinks are recorded snapshot entries.** Tree snapshots record
   every symlink (file or directory) by its literal target and never
   follow it - a planted or retargeted symlink-dir now surfaces in the
   diff, and a symlink resolving to the lane is recorded, not silently
   pruned.
4. **Delivery-gate architect-tree recheck.** The human delivery gate
   re-checks the whole architect tree against a sealed post-bracket
   baseline (`architect-tree-baseline.yaml`, format-versioned), so
   sibling-goal or architect-root tamper during review/ruling/approval
   time blocks delivery (`delivery_architect_tree_recheck_failed`).
   Only the KNOWN supervisor artifact set in the active goal is exempt.
5. **Default-deny subprocess environments.** Builder AND verification
   subprocesses get an allowlist-composed env (base operational vars +
   a small toolchain preset for verification + the operator extension
   `CONSENSUS_MCP_BUILDER_ENV_ALLOW` /
   `CONSENSUS_MCP_VERIFICATION_ENV_ALLOW`), then a hard-floor
   credential scrub that explicit allows can NEVER override
   (GITHUB_TOKEN, AWS keys, SSH_AUTH_SOCK, KUBECONFIG, etc.). An
   env-dependent test suite declares its variables explicitly - visible
   configuration instead of silent credential exposure.
6. **Discoverability.** `consensus results` lists architect goals with
   advisory derived states, and `consensus-init --workflow D` /
   `architect-build` is accepted (preview-labeled), with the
   interactive wizard prompting the role mapping.

## v1 boundaries (documented simplifications)

- **The supervisor auto-runs only two things:** the builder dispatch and
  the verification command. Architect actions (spec, rulings) and reviewer
  actions are returned as `next_action` hints for the orchestrating host -
  host callback when the role is claude, a host-driven CLI dispatch
  otherwise. Automatic reviewer dispatch is the named v1.1 follow-up.
- **Builder dispatch has no streaming watchdog.** It runs under
  Popen + communicate with a hard timeout; timeout/failure paths terminate
  the whole process tree (codex's Node descendants would otherwise keep
  writing in the lane - a containment hazard, not just litter). The
  stall-detection watchdog of the read-only dispatchers is a named
  follow-up.
- **Pushback cycles forbid `accept` rulings.** A pushback build is a
  refusal, not work - it has no verification and no review, so accepting it
  would route an unverified cycle straight to delivery. Legal dispositions
  on pushback: `revise`, `overrule` (and `kill`).
- **Builder network policy** is the CLI's own default in v1, documented per
  profile.
- **v2 items, gated per the converged plan:** builder-owned lane commits
  (gated on the decisive experiment passing AND a renegotiated contract
  with an explicit git-command whitelist), the `reviewer: none` sentinel
  with a strict cross-family architect validator, the
  `strict_signer: cli_only` flag (CLI-only delivery signers), parallel
  build lanes with a merge-ordering policy, builder_capable for
  kimi/gemini/grok, and per-role budget telemetry.

## The decisive experiment

The consult's falsification block: the containment claim is EMPIRICAL, and
ships with the named test that decides it -
`consensus_mcp/tests/test_builder_containment_smoke.py`. It dispatches REAL
codex (workspace-write, lane cwd) into a throwaway repo and instructs it to
(1) edit a lane file, (2) attempt `git status`, (3-5) attempt writes one,
two, and four levels above the lane. Run it explicitly (env-gated; it is
skipped otherwise and needs a `codex` binary on PATH):

```bash
CONSENSUS_MCP_RUN_REAL_BUILDER_SMOKE=1 \
  .venv/bin/python -m pytest \
  consensus_mcp/tests/test_builder_containment_smoke.py -q -s
```

**Result (2026-06-10, codex-cli 0.137.0): the sandbox-confinement
hypothesis is REFUTED, and the design guarantee is VERIFIED.** The builder
reported every write "succeeded with exit code 0" - the lane file, and the
escapes one, two, and four levels up (goal dir, architect root, repo root).
`--cd` is a working directory, not a jail. This is exactly why the consult
made the snapshot guard root-cause-independent: the experiment confirms the
supervisor caught all three escapes (`check_architect_tree` flagged the
goal-dir and architect-root writes; `check_main_integrity` flagged the
repo-root write) and would block delivery. The test now asserts that
*design guarantee* - sandbox does not confine, supervisor does - and a
cooperative build that touches only the lane trips nothing.

Consequence: **builder-owned lane commits stay closed (v2 deferred).** A
refuted confinement hypothesis means the supervisor must keep owning git
and keep treating lane content as untrusted; the integrity snapshots are
not a backstop you can lean off of - they are the boundary.
