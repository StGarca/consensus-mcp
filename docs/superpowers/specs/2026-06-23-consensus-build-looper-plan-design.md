# Consensus Build - Looper Plan (opt-in design front-door)

**Status:** design RATIFIED by the 3-AI anchored consult
`iteration-looper-plan-design-2026-06-23` (codex + grok + kimi; all
`goal_satisfied=true`, zero blocking objections). Improvements folded in below;
see `consensus-state/active/iteration-looper-plan-design-2026-06-23/converged-plan.yaml`.
**Date:** 2026-06-23
**Branch:** feat/looper-plan (off the v2.0.1 HEAD that carries Consensus Build / workflow D)
**Upstream dissected:** the Looper plan source (MIT; attribution in consensus_mcp/looper_plan/NOTICE)

---

## 1. Summary

Add an opt-in, vendored **Looper design coach** as a pre-flight front-door to
Consensus Build (architect-build / workflow D). When the operator launches
Build they choose **with or without a Looper plan**. The "with" path forks into
a trimmed, vendored Looper coaching wizard that produces a well-designed loop
spec (coached goal, typed verification, termination caps), then **seeds** the
Build goal from it. The "without" path is today's flow, byte-for-byte.

**Governing principle:** Consensus Build is *untouched*. Looper's only output is
**inputs Build already consumes** (the goal dir's `problem.md` plus optional
context files). No change to `_architect_lane.py`, `architect.loop_step`,
`architect.approve_spec`, `architect.cleanup`, the containment/integrity
invariants, the seals, the human gates, or any schema. (HOST-VERIFIED that the
existing architect-tree snapshot already covers the goal dir - see 6.)

This is exactly the boundary Looper itself names. Looper's own
`RUN_IN_SESSION.md` says: *"If the loop needs scheduled runs, child-agent
lifecycle management, concurrency control, or restart-safe step retries, ... hand
this Looper spec to a durable orchestrator."* Consensus Build is that durable
orchestrator. Conversely, Build's only design front-door today is bare - the
architect is handed a raw `{problem_statement}` and authors `spec.yaml` from
scratch, with no coached goal, no forced verification taxonomy, and no
pre-flight design critique. Looper fills precisely that gap.

---

## 2. Why these two layers compose (the thesis)

Looper and Build are non-overlapping layers that each name the other's gap.

| Concern | Looper (design) | Consensus Build (execution) |
| :-- | :-- | :-- |
| Coaches the goal | yes (goal rubric) | no |
| Forces typed verification | yes (programmatic/judge/human) | partial (acceptance_gates, no coaching) |
| Pre-flight design critique | yes (4 rubrics) | no |
| Durable orchestration | **no (explicitly defers)** | yes (supervisor, sealed provenance) |
| Containment of a write-enabled builder | no | yes (lane + integrity snapshots) |
| Cross-AI review | single judge member | full cross-family panel + ruling gate |
| Human gates | optional checkpoints | two mandatory gates (spec, delivery) |

The data shapes already line up:

- Looper `goal.statement` + `goal.definition_of_done` -> Build `problem.md` /
  the architect's `spec.yaml` goal + desired_end_state.
- Looper typed `verification` criteria -> Build `acceptance_gates`
  (id/description/check) and the frozen `architect_loop.verification` gate
  (mapping rules in 4.1, seed.py).
- Looper `loop_control` (max_iterations, no_progress, budget) -> Build
  `architect_loop.max_cycles`, `stop_conditions`, `max_wall_clock_minutes`.
- Looper `council` / `gates` -> Build `roles:` (architect/builder/reviewer) +
  cross-family floor + review/ruling gates. **Build's runtime is strictly
  stronger here**, so we do not import Looper's runtime council; we pre-seed
  Looper's council stage FROM Build's roles so the coach does not re-litigate
  model choice.

---

## 3. Scope decisions (operator-locked in brainstorming; ratified by consult)

1. **Relationship:** *Coach, then seed Build.* Looper coaches the design; the
   coached plan seeds the Build goal; Consensus Build then executes it untouched.
2. **Vendoring:** *Vendor the complementary slice* self-contained into
   `consensus_mcp/looper_plan/`, mirroring how we vendor Superpowers skills.
   Works offline, contributor/operator parity, reproducible, no external
   dependency or drift.
3. **Seeding seam:** *Suggest + operator confirms* (consult Q1). Looper writes
   its plan and a synthesized `problem.md`, then an explicit
   `apply-looper-suggestions` step renders a **visible diff** before anything
   touches `.consensus/config.yaml`. Two-tier: (a) the EXECUTION CONTRACT
   (frozen `verification` + `acceptance_gates`) requires explicit
   per-field-or-batch confirmation; (b) TERMINATION CAPS (`max_cycles`,
   `max_wall_clock_minutes`) may be PRE-FILLED from `loop_control` but still
   require one confirm action - never auto-applied (silent caps fire
   `blocked_stop_rule`s = premature termination, not safe defaults). An "accept
   all suggestions" single action avoids friction theater.

---

## 4. Components

### 4.1 Vendored coaching slice - `consensus_mcp/looper_plan/`

MIT, attributed to the upstream author (`the Looper plan source`).

```
consensus_mcp/looper_plan/
  __init__.py
  rubrics/
    goal-rubric.md            # verbatim from upstream
    verification-rubric.md    # verbatim (the highest-leverage file)
    council-rubric.md         # verbatim
    control-rubric.md         # verbatim
  schemas/
    loop.v1.schema.json       # verbatim
    loop.resolved.v1.schema.json
  compile.py                  # TRIMMED port of upstream scripts/looper.py
  seed.py                     # NEW glue (ours): resolved.json -> Build inputs
  VENDORED.md                 # provenance, what was trimmed, reference-integrity
  NOTICE                      # MIT notice (Superpowers vendoring pattern)
```

**`compile.py`** is a trimmed port of upstream `scripts/looper.py`, exposing one
entry `compile_plan(loop_yaml_path) -> (resolved_dict, loop_md_text)`. It keeps
`load_yaml` + `normalize_spec` (full validation: criteria typing, the
reviewer-only `verdict_source` rule, gate/control guards, argv normalization),
`render_loop` (`LOOP.md`), and `render_ascii_diagram` (the flow preview - labeled
"design preview - NOT the Build supervisor loop").

**Dropped** (Build already provides stronger equivalents, or they are
execution-surfaces Build owns): model detection/registry (`detect-models`,
`register-model`, `MODEL_PROBES`, `~/.looper/models.json`), the external
`run-loop.py` runner, and `render_session_prompt` / `RUN_IN_SESSION.md`.

**Required-but-unused schema fields (consult Q4 GAP - load-bearing).**
`loop.v1.schema.json` REQUIRES `host`, `gates`, `council`, `workspace`, but the
trimmed wizard coaches only goal/verification/caps. To keep `normalize_spec`
verbatim (so the validation IP and ported tests stay intact), the wizard/compile
step DETERMINISTICALLY STUB-SYNTHESIZES the unused-by-us fields before
validation:
- `council` from Build `roles:` (builder=looper host, reviewer=judge,
  architect=spec author);
- `host` from Build `roles.builder`;
- `execution.mode: orchestrated`, `execution.isolation: worktree` (Build is the
  orchestrator and uses a worktree lane);
- minimal `plan_gate` / `delivery_gate` referencing the coached criteria;
- `workspace.dir` pointing at the goal dir's `looper-plan/`.
These stubs make a valid `loop.yaml` that compiles; they are NOT executed by
looper (Build executes).

**`seed.py`** is our glue (not a port). Given a validated `loop.resolved.json`
it produces:
- `problem.md` text - the coached goal statement + definition_of_done + the
  typed verification criteria rendered as the architect's context (judge/human
  criteria under a "design criteria" section with an explicit NON-AUTOMATION
  banner), plus a pointer to the looper-plan artifacts. This is what the
  architect spec dispatch consumes as `{problem_statement}`.
- `looper-suggestions.yaml` - a *suggestion* (never auto-applied): the frozen
  verification command + `acceptance_gates` + `architect_loop` caps for the
  `apply-looper-suggestions` confirm step (3).
- `looper-plan-manifest.yaml` - the looper file set + sha256s, sealed at
  goal-setup; powers the module-level re-coach refusal (6).

**Verification taxonomy mapping (seed.py owns the table; consult Q4).**
- `programmatic` + `expect: exit_zero` -> Build frozen `verification` /
  `acceptance_gates.check` directly.
- `programmatic` + other `expect` (`exit_nonzero`, `stdout_contains`) -> wrapped
  in a cross-platform shim OR surfaced for operator edit; NEVER silently remapped
  to different semantics.
- multiple `programmatic` criteria -> Build exposes ONE
  `architect_loop.verification` string, so: primary frozen gate =
  operator-selected or first programmatic; the rest -> `acceptance_gates`.
- `judge` -> reviewer/ruling-gate rubric guidance (architect context), not a
  deterministic gate.
- `human` -> human-checkpoint annotations + the `problem.md` design-criteria
  section.

**Cross-platform (consult Q4; HOST-VERIFIED Build runs verification
`shell=True`, `_architect_lane.py:451`).** `seed.py` renders verification
command strings with `shlex.join` on POSIX and `subprocess.list2cmdline` on
Windows; `compile.py` keeps argv arrays + `posix=os.name != "nt"`; reuse Build's
goal-id Windows-reserved-name guard.

### 4.2 Coaching wizard - vendored skill `consensus-looper-plan`

A trimmed port of upstream `SKILL.md` driven by the host. It runs a focused
interview (goal -> verification -> gates/control -> preview), loading the
matching rubric per stage (progressive disclosure). **The host/council stage is
pre-seeded from Build's `roles:`** and the operator may NOT re-litigate model
choice in looper. To avoid council pre-seed staleness (consult Q4), the wizard
**re-reads `.consensus/config.yaml` immediately before** calling `compile.py`.
It writes `loop.yaml`, calls `compile.py`, shows the ASCII preview, runs
`seed.py`, and presents the suggested config.

### 4.3 Launch toggle

Surfaced in the `consensus-workflow` skill and documented in
`docs/workflows/architect-build.md`. At Build launch, an AskUserQuestion:
*"Design with a Looper plan first (design coach), or go direct (Build execution
loop)?"*

- **With** -> run `consensus-looper-plan` against
  `.consensus/architect/<goal-id>/` -> writes
  `looper-plan/{loop.yaml, loop.resolved.json, LOOP.md}` + a synthesized
  `problem.md` + `looper-suggestions.yaml` + `looper-plan-manifest.yaml`;
  operator confirms suggested config via `apply-looper-suggestions`; then the
  standard `consensus-mcp-architect step` flow runs, untouched.
- **Without** -> exactly today's flow (operator writes `problem.md`, runs
  `step`).

**The toggle lives ONLY in goal-setup paths** (`consensus-init` wizard /
`consensus-workflow` skill), NEVER in `architect.loop_step` (consult Q4). The
without-looper path must NOT import `consensus_mcp.looper_plan` (lazy-import
boundary).

---

## 5. Data flow

```
operator launches Build
        |
        +--[without looper plan]--> problem.md (operator) --> step ... (UNCHANGED)
        |
        +--[with looper plan]
                |
                v
        consensus-looper-plan wizard
          - interview (goal/verification/gates/control), rubric per stage
          - council/host stage pre-seeded from Build roles: (re-read config)
          - writes loop.yaml (+ stub-synthesized host/gates/council/workspace)
                |
                v
        compile.py: loop.yaml -> loop.resolved.json (+ LOOP.md, ASCII preview)
                |
                v
        seed.py: loop.resolved.json
          -> problem.md             (the seam: a Build input)
          -> looper-suggestions.yaml (frozen gate + acceptance_gates + caps)
          -> looper-plan-manifest.yaml (sha256s; powers re-coach refusal)
                |
                v
        apply-looper-suggestions: visible diff -> operator confirms -> .consensus/config.yaml
                |
                v
        consensus-mcp-architect step  ----- UNTOUCHED Build supervisor -----
          needs_spec -> architect reads coached problem.md + looper-plan/ as
                        context, authors spec.yaml
          ... spec approval (human gate 1) ... build/verify/review/ruling ...
          ... delivery approval + merge (human gate 2)
```

---

## 6. The surgical guarantee (zero diff to Build) + write-once enforcement

No change to any of: `_architect_lane.py`, `_architect_handoff.py`,
`_architect_paths.py`, `_dispatch_builder.py`; the `architect.loop_step` /
`approve_spec` / `cleanup` / `loop.run_goal` handlers; the goal-packet / spec
schema, the seal format, the containment/integrity snapshots, the
GateEligibleCrossFamilySigner invariant, or the stop-rule set.

**Why the inside-the-goal-dir looper artifacts are safe with zero supervisor
change (consult Q2; HOST-VERIFIED).** `snapshot_architect_tree`
(`_architect_lane.py:499-550`) hashes every file under `.consensus/architect/`
except the active lane; `snapshot_goal_artifacts` (`:446-471`) does the same for
the goal dir; `_diff_hashes` (`:483-496`) flags any created/deleted/**modified**
non-lane artifact. The looper files are written at goal-setup, BEFORE the first
builder dispatch bracket, so they are included in the first before-snapshot and
in the sealed delivery baseline; being immutable, the delivery recheck finds
them unchanged -> pass. They need NOT join the known-supervisor exempt set.

Enforcement of write-once is split to PRESERVE zero diff:
- (a) the EXISTING delivery recheck catches any post-baseline mutation for free;
- (b) the LOOPER MODULE (our code) refuses to re-coach / rewrite once a baseline
  or any sealed supervisor artifact exists for the goal (checked against
  `looper-plan-manifest.yaml`), turning the operator-re-run path into a clean
  early refusal instead of a delivery-time failure (root-cause-independent
  safeguard).
- Kimi's manifest is our module's self-check, NOT a supervisor tamper rule
  (a supervisor rule would break zero diff).

Regression guards for the without-looper path (post-review rev-002 - narrowed to
what is implemented and sufficient): (a) no supervisor module appears in the
feature diff; (b) source guards assert zero `looper` references in the supervisor
modules; (c) a fresh-import guard asserts `architect.loop_step` never pulls in
`consensus_mcp.looper_plan`; (d) the full architect suite passes UNCHANGED. A
separate "golden step-JSON" fixture is intentionally NOT added: the without-looper
path shares NO Python launch helper with the looper path (the toggle lives in the
wizard skill; the direct path is just operator-authored `problem.md` + `step`), so
there is no shared code surface to drift - (a)-(d) fully cover it. Plus the
baseline-inclusion guard (8).

---

## 7. Out of scope (YAGNI / already-have)

- Looper's external `run-loop.py` runner and `RUN_IN_SESSION.md` handoff - Build
  is the runner.
- Looper's model detection/registry - Build's contributor profiles resolve
  models.
- Looper's privacy/egress *machinery* - Build's dispatch redaction + consent
  already governs cross-vendor sends. We retain the egress *coaching* (the
  council rubric's privacy notes) as prose, not duplicate machinery.
- Looper's single-judge council/gates as a *runtime* - Build's cross-family
  panel + review/ruling gates supersede it.
- Auto-writing `.consensus/config.yaml` (rejected; suggest+confirm only).
- Any supervisor-side tamper rule for looper artifacts (would break zero diff;
  the existing recheck suffices).

---

## 8. Testing

- `compile.py`: port upstream `tests/test_looper.py` + `tests/fixtures/`
  (criteria typing, reviewer-only `verdict_source` rule, control guards, argv
  normalization, LOOP.md / ASCII render); add a stub-synthesis test (a
  goal/verification/caps-only `loop.yaml` compiles after host/gates/council/
  workspace are synthesized).
- `seed.py`: `loop.resolved.json` -> `problem.md` content +
  `looper-suggestions.yaml` mapping; `exit_zero` -> frozen gate/acceptance_gate;
  other `expect` -> shim/operator-edit (never silent); judge/human -> context,
  NOT deterministic gates; multi-programmatic composition rule.
- **Baseline-inclusion integration test (the decisive experiment):** create a
  goal, run the looper pre-flight, run `step` through the first builder dispatch,
  and assert the sealed architect-tree baseline payload CONTAINS the looper-plan
  file hashes; then a post-baseline edit makes the delivery recheck report
  `architect-tree artifact modified`.
- **Without-looper zero-diff guards** (post-review rev-002): source-level
  assertion that no supervisor module references `looper`, plus a fresh-import
  guard that importing `architect.loop_step` never pulls in
  `consensus_mcp.looper_plan`. (No golden step-JSON fixture: there is no shared
  launch helper between the two paths, so these guards + the unchanged full
  architect suite are sufficient and non-redundant.)
- **Goal-id validation** (post-review rev-001): `seed.resolve_goal_dir` rejects
  malformed/traversing/Windows-reserved ids by delegating to Build's
  `_architect_paths.goal_dir`; the wizard calls it before any write.
- Cross-platform: verification command strings round-trip through the host shell
  on POSIX and `cmd.exe` on Windows; Windows-path fixture.
- Reference-integrity (per our vendoring rule): every rubric/schema referenced
  by `compile.py` and the skill resolves on disk, is listed in `VENDORED.md`,
  and is shipped by `pyproject.toml` package-data.
- License/attribution: `VENDORED.md` + `NOTICE` carry the upstream URL, pinned
  commit, MIT notice, and kept/trimmed list; `compile.py` has a derivative
  header.

---

## 9. Licensing / attribution / packaging

Upstream is MIT (see NOTICE). We vendor a slice verbatim (rubrics, schemas)
and a trimmed derivative (`compile.py`). `VENDORED.md` records the upstream URL,
the **pinned source commit**, the MIT notice, and the exact kept-vs-trimmed
list; a `NOTICE` file carries the MIT notice (Superpowers vendoring pattern);
`compile.py`'s header attributes the derivation. `pyproject.toml` package-data
is updated to ship `consensus_mcp/looper_plan/**/*` (rubrics + schemas + py).

---

## 10. Risks and mitigations

- **Zero-diff claim falsified** if the toggle/suggestion-applier leaks into
  `architect.loop_step`, the without-looper path imports `looper_plan`, or the
  sealed baseline excludes pre-baseline goal-dir files. Mitigated by
  toggle-only-at-setup, lazy-import boundary, the byte-identity golden, and the
  baseline-inclusion integration test.
- **Snapshot-baseline coupling / post-baseline mutation.** Mitigated by the
  module-level re-coach refusal + manifest self-check + the existing recheck.
- **Semantic divergence** (looper taxonomy vs Build acceptance_gates). Mitigated
  by the explicit seed.py mapping table + operator confirmation; non-programmatic
  criteria never become fake deterministic gates.
- **Vendoring drift / licensing.** Mitigated by the pinned commit in
  `VENDORED.md`, the reference-integrity test, and the packaging update.
- **Council pre-seed staleness.** Mitigated by re-reading `.consensus/config.yaml`
  immediately before compile.
- **Cross-platform shell-quoting.** Mitigated by `shlex.join`/`list2cmdline` +
  round-trip tests.
- **Two-loop confusion.** Mitigated by the `consensus-looper-plan` naming, the
  toggle copy ("design coach" vs "Build execution loop"), and the ASCII preview
  label.

---

## 11. Resolved decisions (was: open questions; closed by the consult)

1. **Seam aggressiveness** -> RESOLVED: suggest+confirm for all config; caps
   pre-filled but still confirmed; no auto-write (consult Q1, unanimous).
2. **Artifact location vs integrity-snapshot scope** -> RESOLVED: inside the
   goal dir, write-once-immutable, enforced by the existing recheck + a
   module-level re-coach refusal; NOT a supervisor rule; HOST-VERIFIED safe
   (consult Q2, unanimous + code-verified).
3. **Trim vs full-copy** -> RESOLVED: trimmed `compile.py` derivative + `seed.py`
   adapter; rubrics/schemas verbatim; pin the upstream commit (consult Q3,
   unanimous).

---

## 12. Consult ratification

Anchored 3-AI consult `iteration-looper-plan-design-2026-06-23` (codex + grok +
kimi); all `goal_satisfied=true`, zero blocking objections; converged-plan at
`consensus-state/active/iteration-looper-plan-design-2026-06-23/converged-plan.yaml`.
Cited pass ids: `codex-lpd-1-be84e4455b8e692c`, `grok-lpd-c052e0a2892e`,
`kimi-lpd-001dd5bd6ee1`. Shared-prior caveat recorded (all reasoned from "the
snapshot is the boundary"); mitigated by host verification of the snapshot code.
Decisive regression experiment: the baseline-inclusion integration test (8).
