# Changelog

## 1.15.7 - 2026-05-15

**CI fix: 4 codex-dispatch tests required a real `codex` binary.**
First green CI surfaced this: `test_dispatch_codex.py`'s
`test_main_smoke_with_mocked_codex`,
`test_main_smoke_flag_with_env_proceeds`,
`test_main_sealed_packet_embeds_dispatch_provenance`,
`test_dispatch_done_includes_archive_path_and_audit_id` predate the
iter-0037 refactor that moved the codex invocation from
`subprocess.run` to `subprocess.Popen`. They still mock only
`subprocess.run` (now just the `codex --version` probe), so the
actual dispatch executes the **real** codex via Popen. They passed
on dev machines purely because a real `codex` is on PATH; on CI
runners with none they failed `CodexInvocationError: codex binary
not found` (windows legs → exit 1) or reached a process-group kill
that SIGTERM'd the runner (ubuntu legs → exit 143). It stayed
hidden because CI was dormant v1.13.0→v1.15.3 (main-only trigger)
until v1.15.4 re-enabled it — so these were never actually
CI-covered.

Honest interim fix: a `skipif(shutil.which("codex") is None)`
guard so they skip cleanly when no real codex is present (they are
de-facto integration tests as written). Verified both ways: codex
present → all 4 run + pass (no regression, full suite 968/0);
codex absent (simulated) → 4 skip, 0 failed. **Named follow-up
(tracked):** rewrite the 4 to mock the Popen path via the existing
`make_fake_codex_popen_factory` / `popen_factory=` kwarg (the
pattern the genuinely-hermetic `main()` tests already use) and drop
the skip. Test-only change; no production code, no behavior.

Two more pre-existing CI-env debts re-enabled CI surfaced, fixed
here too:

- **Validator self-test `20/21` (windows legs).**
  `run_validator_tests.py::test_packet_build_sanitizes_injection`
  failed because `review_packet_known_good/input.yaml` listed
  `target_files: agent-loop/tests/fixtures/prompt_injection_doc.md`
  — a **parent-project path never rewritten during the standalone
  extraction**. The doc actually lives at
  `consensus-state/tests/fixtures/prompt_injection_doc.md`; the
  stale path meant the builder read nothing, sanitized nothing, and
  the empty `sanitization_log` failed the `>=7` check. Repointed the
  fixture path (the doc contains all 7 `SANITIZE_PATTERNS`); now
  **21/21**. Not Windows-specific — reproduced locally; latent since
  iter-0001, masked by dormant CI.
- **ubuntu job-kill (exit 143 / "operation canceled" ~25%) —
  ROOT-CAUSED + fixed.** Not a hang and not the 4 above:
  `test_dispatch_codex_streaming.py::_FakePopen.pid` returned
  **`0`**. On POSIX, `_dispatch_base._terminate_process_tree` does
  `os.killpg(os.getpgid(proc.pid), SIG*)`; `os.getpgid(0)` resolves
  to **the caller's own process group**, so the abort/watchdog
  tests SIGTERM'd the pytest / GitHub-Actions job *itself* (instant,
  hence pytest-timeout never fired). Windows uses the
  `send_signal`/`taskkill` branch, so it was masked locally and CI
  was dormant v1.13.0→v1.15.3 so it stayed hidden until v1.15.4
  re-enabled CI. Latent since iter-0039. Fixed two ways:
  (a) corrected the synthetic pid to a never-live value so
  `os.getpgid` raises `ProcessLookupError` → the documented
  `proc.terminate()` fallback runs; (b) a suite-wide
  `tests/conftest.py` autouse guard that neutralizes
  `os.killpg`/`os.getpgid` so NO test can ever signal a real
  process group (production `_terminate_process_tree` logic still
  runs; every `dispatch_aborted` assertion unchanged). `pytest-
  timeout` retained as permanent hardening. Verified: targeted
  abort/dispatch suites 144/0; full suite 968 passed / 1 skipped /
  0 regressions with the guard active.

## 1.15.6 - 2026-05-15

**Literal-zero pass** — completes the v1.15.5 identifier migration
per the operator "true zero" directive. The v1.15.5
changelog/commit/doctrine that *documented* the migration
necessarily still contained the legacy tokens (you cannot record
"X was removed" without writing "X"). Those are now phrased only
obliquely ("the former upstream project name", "the prior account
handle, now `stgarca`"). The entire working tree (tracked +
gitignored scratch + binary caches; `__pycache__` purged) was
swept, and a second `git filter-repo` pass (same substitutions)
cleaned all historical blobs, every commit/tag message, and the
snapshot branch. Verified: **zero literal occurrences in any blob
across every ref, in any commit message, in any tag message, and
in the working tree (including binary)** — including the record of
the migration itself. 18 release tags + all branches intact; full
suite green; a fresh pre-rewrite backup bundle was kept outside
the repo. Semantically identical to v1.15.5 (de-literalization
only; no doctrine/behavior change — the substance was
Workflow-B-audited in v1.15.5). Every published tag SHA changed
again; tag-pinned `pipx …@vX.Y.Z` URLs still resolve.
Canonical repo: `github.com/stgarca/consensus-mcp`.

## 1.15.5 - 2026-05-15

**Identifier migration + provenance scrub + doctrine reconciliation.**

Operator directed (1) removal of every reference to the former
upstream project's name, and (2) migration to the renamed GitHub
account (now `stgarca`). Both reached immutable commit/tag
messages, so — with explicit operator authorization — a full
`git filter-repo` history rewrite was performed:

- **Rewrite:** literal substitution of the former upstream
  project name → `upstream` and the prior account handle →
  `stgarca`, across ALL blob contents AND commit/tag messages,
  all 18 branches + 66 tags (127 commits). Verified before
  pushing: zero occurrences of either legacy token in any blob or
  message across every ref; all 17 release tags + branches
  present; full suite green on the rewritten tree (substitutions
  internally consistent). Pre-rewrite safety bundle kept outside
  the repo.
- **Consequence (artifact-scoped truth):** every published tag SHA
  changed. Tag-pinned `pipx install …@vX.Y.Z` URLs keep working
  (tags moved with the rewrite); any raw-commit-SHA pin or clone
  against the previous account remote is dead. The canonical repo
  is now `https://github.com/stgarca/consensus-mcp`.
- **Doctrine reconciliation:** the v1.15.4 doctrine says `main` is
  never force-pushed. This rewrite force-pushed everything, so the
  bundled `consensus-workflow` skill now carries a
  **sanctioned-exception carve-out**: a full-history rewrite is the
  ONE non-routine reason to force-push `main`, gated on explicit
  authorization + verified backup + pre-push verification + suite
  green. Leaving the doctrine contradicting the action would be the
  exact currency-drift v1.15.3/v1.15.4 fixed.
- **`consensus-state/README.md`** added (tracked) — documents the
  runtime-state tree + recovery, and (doctrine-correctly, via a
  fresh commit rather than more history surgery) relabels the
  GitHub `consensus-state/` folder off the old root commit the
  operator flagged.
- **Literal-zero pass:** a follow-up scrub removed the legacy
  tokens from this changelog/commit/doctrine wording itself (they
  are described only obliquely now) and from snapshot scratch, so
  the repo contains zero literal occurrences anywhere — including
  the record of the migration itself.

No engine/config/behavior code touched. Full suite green. Workflow
B audit: codex + gemini (bundled-doctrine change).

## 1.15.4 - 2026-05-15

**Repo-presentation + CI + branch-doctrine fix.** Operator observed
that the GitHub landing page looked stuck at v1.13.0 and showed a
hallucinated "v2.0.0" commit label on `.github/workflows/`.

Root cause: the pre-v1.15.4 release-branching convention froze
`main` forever (releases lived only on `v<X.Y.Z>` branches/tags,
never merged back). `main` is GitHub's default branch, so the
landing page was frozen at v1.13.0 — and, more seriously,
`.github/workflows/test.yml` triggered **only** on `main`, so
GitHub Actions CI was **dormant from v1.13.0 → v1.15.3** (every
release ran on a `v*` branch CI never saw; those releases were
verified by local pytest only).

- **CI** (`.github/workflows/test.yml`): now triggers on push/PR to
  `main` **and** `v*` branches, so release-branch work is actually
  exercised by GitHub Actions.
- **Branch doctrine evolved** (`consensus-workflow` SKILL.md): the
  "`main` frozen forever / never merge back" convention is replaced
  by "`main` = latest released state; every release cut
  fast-forwards `main` to the just-cut tag (clean ff, never a merge
  or force-push); development continues on `v<next>` branches." A
  new cut-sequence step 8 documents the fast-forward with an
  ancestor-check safety guard (and step 3 moves README install/
  status currency pre-tag, since the tag's README is now the
  landing page). Updating the bundled doctrine here
  prevents the same currency-drift v1.15.3 just fixed.
- **`main` fast-forwarded** from v1.13.0 (`64f70ec`) to the v1.15.4
  tag — a verified clean fast-forward (no history rewrite, no
  dropped commits, no force-push). The GitHub landing page now
  reflects the current state, including the accessible README.
- **README** rewritten as an accessible ~150-line summary (was 364
  dense lines); deep internals moved behind CHANGELOG/`docs/`
  links. Load-bearing facts preserved.

No engine/config/behavior change. Full suite green. Workflow B
audit: codex + gemini.

## 1.15.3 - 2026-05-15

**Bundled-doctrine + status currency hot-patch** — doc/string only,
no behavior change. Triggered by an operator question ("on a new
install, will 2-AI follow all the upgraded workflow lessons, same
as 3-AI?"). Investigation answer: **yes — doctrine + v1.15.1
machine-enforcement are AI-count-AGNOSTIC, scoped by workflow mode,
never by contributor count.** But the investigation found shipped
v1.15.2 artifacts carrying stale forward-references that misled
every new installer (2-AI and 3-AI identically).

Scope set by a **Workflow A consult** (`iteration-v1153-bundled-
skill-currency`; claude + codex + gemini; weighted-synthesis;
shared-prior self-check PASSED-WITH-CORRECTION — fast 3/3 unanimity
on scope was partly a shared prior; claude's differing differential
surfaced F7, which was integrated, not laundered by the unanimity).

Fixed (one failure mode — a shipped artifact's forward-reference
whose target version already came due):
- **F1** `consensus-workflow/SKILL.md`: the converged-plan
  convention is no longer described as "machine validation is a
  sequenced follow-up" — it is **machine-enforced as of v1.15.1**
  (`validate_converged_plan` + fail-closed seal-time gate +
  `convergence.converged_plan_enforcement` knob, default
  `graduated`).
- **F2** same skill, Gemini section: empty gemini output is NOT a
  429 — `GEMINI_CLI_TRUST_WORKSPACE` guidance added (fixed v1.15.2;
  manual workaround only ≤ v1.15.1; don't burn the 429 budget).
- **F3** both bundled skills (`consensus-workflow` +
  bootstrap `consensus`): Workflow C engine status corrected — it
  did **not** ship in v1.15.0; it is UNIMPLEMENTED as of v1.15.2,
  no committed target version.
- **F4** `workflow_engine.py`: the Workflow C `NotImplementedError`
  message + comment no longer promise "lands in v1.15.0" (string/
  comment only — no control flow; no test pinned it).
- **F7** `docs/workflows/workflow-c-autonomous.md`: 5 stale
  "v1.15.0" forward-references corrected (the consult's
  shared-prior-correction finding — the doc F4's text points at).
- **Q4** `consensus-workflow/SKILL.md`: new normative
  **consistency invariant** stating doctrine + enforcement are
  workflow-mode-scoped, never contributor-count-scoped — 2-AI and
  3-AI installs governed identically; only the convergence-rule
  default differs (unanimous@2 vs strict-majority@3). Converts the
  operator's implicit invariant into a written guarantee.

No invented replacement version anywhere (artifact-scoped truth —
that would repeat the exact defect). Code+doc bundled in one tag
per the shared-failure-mode hot-patch doctrine (verified precedent:
v1.14.5, v1.14.6). Full suite green, 0 regressions. Workflow B
audit: codex + gemini.

## 1.15.2 - 2026-05-15

**gemini-dispatch workspace-trust fix** — closes the v1.15.1
named blocker (advisory 2026-05-15, now **resolved**).

`consensus_mcp/_dispatch_gemini.py` now injects
`GEMINI_CLI_TRUST_WORKSPACE=true` into the gemini subprocess env
via a new `_gemini_subprocess_env()` helper (returns a COPY of the
parent env; forces the var to `true` even if an inherited value is
`false`; never mutates `os.environ`). gemini CLI
`0.43.0-preview.0`+ refuses headless runs in an "untrusted"
directory — it writes the trust error to stderr and produces
**empty stdout**, which the dispatcher then fails as
`GeminiOutputParseError`.

**Empirically verified 2026-05-15** (the v1.15.1 audit diagnosed
this first-hand; gemini pass-1 failed twice for exactly this):
- `--skip-trust` alone (still passed, defense-in-depth) is NOT
  load-bearing on this version — gemini bypassed trust but went
  autonomous and 429'd.
- `GEMINI_CLI_TRUST_WORKSPACE=true` produced clean deterministic
  output, and the two clean v1.15.1 Workflow B audit approvals.

`_dispatch_gemini.py` was in the v1.15.1 iteration's
`forbidden_files`, so the dispatcher-level fix was correctly
deferred to this version rather than patched out-of-scope.

Workflow B audit clean: gemini `goal_satisfied=true`, 0 blocking,
0 findings — **dispatched from source with
`GEMINI_CLI_TRUST_WORKSPACE` explicitly unset**, so the audit pass
of this fix was made possible BY this fix (end-to-end in-vivo
proof; acceptance gate A4). codex: 0 blocking; doc-accuracy
findings on the advisory/changelog wording integrated. Full suite
green: 968 passed, 1 skipped, 0 regressions; 4 new unit tests for
`_gemini_subprocess_env` in `test_dispatch_gemini.py`.

## 1.15.1 - 2026-05-15

**Machine-enforcement of the converged-plan convention** — closes the
v1.15.0 NAMED BLOCKER. From `iteration-converged-plan-machine-
enforcement` (Workflow A weighted-synthesis: claude + codex + gemini;
shared-prior self-check PASSED; no blocking objections; the consult
dogfooded the very convention it enforces). The recorded v1.15.0
"starting design" (gemini's `severity` field + `consensus_gate.py`
mechanism) was **partially refuted by first-hand code-reading** during
the consult: `consensus_mcp/validators/consensus_gate.py` is the
Phase-0 production-readiness gate (P0-V6) and is the WRONG component —
the v1.15.0 doctrine working correctly on its own audit trail.

**Artifact truth (scoped claim):** the **v1.15.0 tag `4e81f9e` is
DOCTRINE-ONLY** — it shipped the convention as an authoring convention
enforced by the bundled skill + Workflow B audit, with **zero engine
code**. Machine enforcement exists **only from the v1.15.1 tag
forward**. Users on v1.15.0 get doctrine; they must upgrade to v1.15.1
to get the gate.

Shipped (real code paths — meaningful regression signal, unlike
v1.15.0's doc-only change):

- **`consensus_mcp/schemas/converged_plan_convention.schema.json`**
  (NEW; `schemas/` is net-new) — JSON Schema for the convention
  object; `empirical_status` enum `proven|pending|refuted|n/a` matches
  v1.15.0 verbatim.
- **`consensus_mcp/validators/validate_converged_plan.py`** (NEW;
  small; structure + consequence ONLY). Enforces the consequence of
  the orchestrator-attested `falsifiable_from_artifacts` bool — the
  engine does **not** classify the defect (keyword heuristics are the
  shared-prior trap the v1.15.0 report documents). **Recursive-trap
  defense (highest-order constraint):** the validator has zero code
  path deriving any approved/correct/ready/sound state from the
  blocks (pinned by `test_validator_source_sets_no_correctness_state`
  grepping the module), and every result carries an unconditional
  `gate_scope` disclaimer: *"presence-and-consistency only; NOT a
  soundness assertion … remains a human judgement."* A pass means the
  required thinking was **recorded**, never that it is **true**.
- **`workflow_engine._seal_converged_plan`** — ingests the optional
  orchestrator-authored `convention-input.yaml` via the ONE channel
  that already reaches seal time (a file in `iteration_dir`; no new
  parameter threaded through `run_iteration`/the MCP tool — codex's
  external-refuting-observation honored), validates it, and seals the
  blocks **INTO** `converged-plan.yaml` (same write, same hash) with
  required `cited_pass_ids` (provenance-by-citation: not a loose
  untracked sidecar, not a single-winner extraction — gemini's
  chain-of-evidence requirement). **Fail-closed:** a hard-reject does
  NOT write `converged-plan.yaml`.
- **Graduated strictness** (`convergence.converged_plan_enforcement`:
  `off|warn|graduated|strict`, default `graduated`). Hard-reject ONLY
  (i) operator-declared safety/data-loss/bricking/irreversible risk
  class missing a conforming root-cause-independent
  `independent_safeguard`, and (ii) `empirical_status:proven` with no
  recorded `experiment_result`; warn + annotate `convention_violations`
  otherwise.
- **`consensus_get_iteration_outcome`** surfaces `enforcement` +
  `convention_gate_scope` + `convention_violations`; a reader can
  never see a pass marker without the non-soundness disclaimer next
  to it. Legacy / absent-convention plans (this session's iter-0043
  .. v1.15.0) still load, explicitly marked `enforcement:
  doctrine-only` — **NOT silently valid, NOT rejected**.

**Named blocker (need-evidence, deferred — not a cop-out):** operator
goal_packet `defect_class`/`risk_class` declaration UX + an
anti-gaming cross-check on `falsifiable_from_artifacts`. Both are
ill-posed until the shipped slice produces real usage data.

**Workflow B audit chain** (codex + gemini, post-implementation):
gemini APPROVED (`goal_satisfied=true`, no blocking). codex pass-1
raised 2 blocking + 2 high — all verified against real code (no
hallucinations; all correctly caught the slice under-implementing
the converged plan) and integrated TDD-first: the third named block
(`decisive_experiment_before_next_iteration`) is now schema-required
+ validated; `doctrine-only` is a READ-time legacy classification
only (a new convergence missing blocks is validated under the
configured level, not bypassed); `convention_schema_version` is
pinned exactly (never defaulted/rewritten); a hard-reject removes any
stale `converged-plan.yaml` (fail-closed truly closed). **Design
note (intentional, peer-converged):** under the default `graduated`
level a new *non-safety* convergence missing blocks is sealed as
**warn + annotate** (loudly marked via `convention_gate.
enforcement_note`, never a silent pass), NOT hard-rejected — this is
the converged plan's deliberate q4/q5 decision (incl. codex's own
consult position) to avoid the rejected-goal_packet papercut. Strict
enforcement is opt-in via `converged_plan_enforcement: strict`.

Full suite green: 964 passed, 1 skipped, 0 regressions (38 new
code-path tests in `test_converged_plan_convention.py`, including
all audit-integration fixes — they close the v1.15.0 doc-only loose
end with a meaningful regression signal).

## 1.15.0 - 2026-05-15

**Convergence-correctness doctrine** — minor bump (cross-project
doctrine evolution, not a hot-patch). From
`iteration-convergence-correctness-doctrine` (Workflow A
weighted-synthesis: claude + codex + gemini, no blocking
objections; gemini dissented substantively on prominence +
machine-enforcement and that dissent was incorporated, not
outvoted — the shared-prior self-check passed), derived from a
real downstream failure: the ChilipadScreen ESP32-P4 i2c
boot-loop consensus-failure report (2026-05-15) where two clean
strict-majority convergences (one round-1 *unanimous*) were both
refuted on-device.

Doctrine added to the bundled `consensus-workflow` skill + both
dispatch-template preambles + a new
`docs/workflows/converged-plan-convention.md`:

- **Safety interlock first (headline rule).** Safety-critical /
  data-loss / bricking / irreversible-risk defects MUST ship a
  root-cause-INDEPENDENT safeguard with the hypothesized fix,
  valuable even if the hypothesis is 100% false. Auditable bar:
  "would it still work if the root cause were entirely
  different?" Field-proven: an independent boot-loop breaker
  un-bricked a medical-safety device across two failed
  root-cause iterations ("the single highest-value decision").
- **Convergence is agreement, not truth.** Fast independent
  unanimity is a verify-harder flag — a shared-prior artifact is
  the multi-agent analog of single-agent rationalize-away.
  Dispatch templates now require each contributor to state the
  differential/prior it reasoned from (shared prior exposed at
  reveal instead of laundered as "independent agreement").
- **Provisional-until-proven** for the defined not-falsifiable-
  from-artifacts class (hardware/firmware state, environment/
  toolchain, concurrency/timing): converged root cause is
  PROVISIONAL; "fixed/shipped/root-cause-correct" language
  forbidden until a pre-specified EXTERNAL discriminating
  experiment runs.
- **Anti-theater property:** a falsification is real only if its
  refuting observation is pre-specified, a specific observable,
  and (for that class) external to the reasoning that produced
  the hypothesis.
- **Converged-plan convention:** `falsification`,
  `independent_safeguard`, and
  `decisive_experiment_before_next_iteration` blocks documented
  as an authoring standard, doctrine-enforced now.

**Named blocker (sequenced follow-up):** engine/validator
MACHINE-enforcement of the converged-plan blocks (gemini's
`severity` field + `consensus_gate.py` mechanism is the recorded
starting design). Concrete blocker, file-verified: no standalone
converged-plan schema exists; `workflow_engine.py:505-525` reads
generic YAML keys. Needs its own schema-design consult + a
not-falsifiable-from-artifacts classifier first.

**Field validation of v1.14.9:** the same report independently
confirmed the v1.14.9 void-seal preservation working correctly —
void rounds from the v1.14.0 path-only review bug were preserved
as honest audit history and correctly not counted toward
convergence. No new tooling; cited as regression evidence.

Doc/skill/template/memory only — no engine, config, or code
paths touched. Full test suite green, 0 regressions.

## 1.14.9 - 2026-05-15

Seal-pipeline defect fix from iteration-seal-archive-collision-fix
(workflow A weighted-synthesis convergence; codex + gemini + claude;
no blocking objections). Workflow B audit of the implementation by
codex + gemini.

**Defect:** `review_write_and_seal.handle` built the archive filename
from `reviewer_id` only (`{date}-{iteration_id}-{reviewer_id}-pass.yaml`),
while the index keys uniqueness on `pass_id`. Re-using a reviewer_id
across passes (e.g. `gemini-iteration-0012-2` for pass1 AND pass2)
produced a hard `packet_path_collision` at the path-exists guard
*before* the pass_id-aware index logic ever ran — forcing operators
to mint throwaway reviewer-ids per attempt. `test_contributors.py:135`'s
docstring ("filename must contain iteration_id + reviewer_id + pass_id
tokens") documented the intended 4-token contract; the implementation
had silently regressed to 3 tokens at/before extraction.

**Fix (`consensus_mcp/tools/review_write_and_seal.py`):**

- Archive filename is now 4-token:
  `{date}-{iteration_id}-{reviewer_id}-{safe_pass_id}-pass.yaml`.
  Filename uniqueness key == index uniqueness key (pass_id).
- New `_sanitize_for_filename`: maps `[^A-Za-z0-9._-]`→`-`, collapses
  runs, strips, falls back to `pass`. Applied to the FILENAME only —
  the raw `pass_id` is preserved verbatim in the index entry and
  packet body.
- The index `pass_id` lookup is REORDERED ahead of the path-exists
  guard, and idempotency is judged on **content identity** — the
  canonical hash of the packet with the volatile seal-provenance
  fields (`sealed_at_utc`, `packet_sha256`) stripped — compared
  against the actual ON-DISK archived packet. An exact re-seal is
  an idempotent SUCCESS (`idempotent: true`, `index_updated:
  false`, existing recorded path; scheme-agnostic so old-scheme
  archives still resolve) **regardless of seal time**. This
  matters because a dispatch retry builds a fresh packet with no
  `sealed_at_utc`; a sha-based check would see a new timestamped
  hash and mis-classify the retry as a collision — defeating the
  whole feature for its primary use case (caught by codex Workflow
  B audit, HIGH).
- Integrity guard on the idempotent path: a tampered archive that
  no longer matches the incoming content identity yields
  `index_collision`; one that parses as valid **non-mapping** YAML
  (list/scalar) yields `idempotent_target_integrity_mismatch`
  without crashing; a deleted/unreadable archive yields
  `idempotent_target_missing` / `_unreadable`. The idempotent
  success return reports the **authoritative on-disk artifact
  hash** (the file's own `packet_sha256`, or a reconstruction if
  absent), never a missing/stale value copied from the index.
- The sealed body now always self-records the canonical `pass_id`
  (`packet.setdefault`), including for pass-label-only packets,
  and a parameter-vs-body `pass_id` consistency guard mirrors the
  existing `iteration_id` / `reviewer_id` guards.
- `index_collision` (same pass_id, substantively *different*
  content) preserved with a content-identity-based detail string.
- path-exists guard retained as a defense-in-depth backstop with a
  distinct detail string (unreachable in normal flow).
- `output_schema` + the `handle()` docstring updated to document
  the new `idempotent` success field and all new error codes; a
  schema-contract test pins every `handle()` error code to the
  schema enum so this drift cannot silently regress.

**Workflow B audit chain** (post-review; codex iterated, gemini
approved the core design): gemini APPROVED with no findings; codex
surfaced and drove resolution of 6 distinct real gaps across 4
audit passes — output-schema drift, missing pass_id consistency
guard, a HIGH timestamp-dependent-idempotency defect, a
non-mapping-archive crash, pass-label body identity, and a
stale/missing idempotent-return hash — before reaching
`goal_satisfied: true` with no blocking objections. The HIGH
finding (idempotency silently broken by the pre-existing
`sealed_at_utc`-in-hash stamp) would have shipped a dead feature
without this audit.

**Backward compat:** the 162 existing archives are NOT migrated.
`review_read_post_seal` resolves via the index's stored relative
path and never parses the filename, so old-scheme archives remain
fully readable. Mixed-scheme archive directory across the v1.14.x
boundary is expected; the index is the resolution source of truth.

**Tests:** new `consensus_mcp/tests/test_seal_collision_fix.py`
(19 cases: the exact `gemini-iteration-0012-2` same-reviewer-
distinct-pass scenario; timestamp-independent idempotent success;
non-mapping-archive integrity guard; target-missing; pass_id
parameter/body consistency; pass-label body identity stamping;
authoritative-hash return when the index sha is blank;
hostile-pass_id sanitization; schema-contract pinning of every
`handle()` error code); `test_contributors.py` fake_t6 helpers
updated to the 4-token scheme. Full suite **723 pass, 0
regressions** (excluding the pre-existing `test_dispatch_codex`
ordering flake + `jsonschema`-missing environmental issue, both
predating this work).

**Deferred with stated reasons** (recorded, not silently dropped):

- `superseded_by` index annotation for void seals — 2/3
  contributors scoped it out as beyond the minimal defect; file
  preservation alone already satisfies the audit-history
  requirement. Own small consult if causal supersession tracking
  is wanted.
- Forensic scan of the 162 existing archives for latent mis-seals
  — blocked on an undefined remediation model for immutable sealed
  history.
- `index.yaml` non-atomic read-modify-write race — pre-existing,
  orthogonal; separate hardening consult.

Carried forward from the overnight run (still open, unchanged):
Workflow C multi-iteration engine (v1.15.0 named blocker); iter-0045
PHASE_CONVERGE mapping empirical evaluation; project-level
`.consensus/autonomous-policy.yaml`.

## 1.14.8 - 2026-05-14

iter-5 of autonomous run-2026-05-15-overnight. Bundled
`consensus_mcp/claude_extensions/skills/consensus/SKILL.md` (the
bootstrap skill that triggers on "consensus init") gains a
"Workflow modes the operator can pick (v1.14.4+)" section:

- Documents Workflow A (propose-converge, default), Workflow B
  (post-review, lightweight), Workflow C (autonomous-execute,
  v1.14.4 contract / v1.15.0 engine), and advisory.
- Notes the numeric-alias deprecation cycle.
- Cross-references the operating-procedure skill
  (`~/.claude/skills/consensus-workflow/SKILL.md`) as the
  load-bearing reference for dispatch rules and halt conditions.

Operators running `consensus init` for the first time will now see
the workflow-mode vocabulary at bootstrap time instead of having to
discover it from the `--help` output.

No code changes; doc only. 704 tests pass.

## 1.14.7 - 2026-05-14

Doc hot-patch from autonomous run-2026-05-15-overnight iter-4. Two
stale references that user-facing surfaces still showed:

- `README.md` "Status" section bumped from "1.14.0 — multi-AI
  contributor pool, blind-first-reveal workflow #4 ..." to a
  "v1.14.6 (current)" header + per-release train summary covering
  v1.14.0 → v1.14.6. Includes pointer to `docs/advisories.md` for
  known-defect-release upgrade guidance.
- `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`
  one stale "Workflow #3" reference in the "Workflow A/B in one line"
  section body (the global rename in v1.14.4 missed this hyphenated
  form). Bumped to "Workflow B".

No code changes; docs only. 704 tests pass (no regression).

## 1.14.6 - 2026-05-14

Hot-patch from autonomous run-2026-05-15-overnight iter-3.

**Defect fixed:** `consensus-init --workflow A` (and B, C, lowercase
variants, and the new `autonomous-execute` semantic string) was
rejected at argparse parse-time before alias resolution could run.
v1.14.4 added letter aliases to `WORKFLOW_ALIASES` + the interactive
wizard prompt but missed the CLI `--workflow` argparse `choices` list,
which still hardcoded the pre-rename set
`["3","4","post-review","propose-converge","advisory"]`.

**Fix:** `consensus_mcp/_init_wizard.py` argparse `--workflow` choices
list expanded to include `["A","B","C","a","b","c","3","4",
"post-review","propose-converge","advisory","autonomous-execute"]`.
Help text updated to document A/B/C semantics inline so
`consensus-init --help` is self-documenting. Numeric aliases (3, 4)
still accepted at parse-time; deprecation warning fires at
`normalize()` time per the v1.14.4 contract (unchanged).

**Tests:** 8 new in `consensus_mcp/tests/test_init_wizard_workflow_choices.py`
asserting argparse acceptance for each new alias variant + sanity
test that letter parses through `WORKFLOW_ALIASES` to the canonical
semantic string. Total suite: 704 pass (was 696; +8 for iter-3).

## 1.14.5 - 2026-05-14

Bundled hot-patch from autonomous-mode `run-2026-05-15-overnight`
(operator-initiated; running per the v1.14.4 Workflow C contract,
manually-orchestrated since the v1.15.0 engine path is the named
blocker). Two completed iterations bundled per the no-deferral rule:

**iter-0044 — adapter `--mode` forwarding fix** (Workflow B audit;
codex + gemini both `goal_satisfied=true, blocking_objections=[]`).

The original defect: `CodexAdapter.dispatch` and
`GeminiAdapter.dispatch` built argv without `--mode`, causing every
workflow A round-1 dispatch through the engine to silently use the
review template + review schema even when `packet.phase ==
PHASE_PROPOSE`. The shell binaries (`consensus-mcp-dispatch-codex`,
`consensus-mcp-dispatch-gemini`) already accepted `--mode {review,
proposal}` per iter-0028; the adapter just wasn't forwarding it.
Test coverage didn't catch this because
`test_dispatch_codex_proposal_mode.py` only tested the dispatcher's
own argparse in isolation, never the adapter boundary.

Implementation per iter-0043 converged plan:

- `consensus_mcp/contributors/_phase_mode.py` NEW — single source
  of truth: `PHASE_PROPOSE → "proposal"`, `PHASE_REVIEW → "review"`,
  `PHASE_CONVERGE → "review"` (interim per iter-0043 q1
  weighted-synthesis; iter-0045 candidate revisits with empirical
  data). Strict-dict lookup; raises `ValueError` on unmapped phase
  (no silent default — that's exactly what allowed the original
  defect).
- `consensus_mcp/contributors/codex.py` — append `--mode` to argv
  via `phase_to_mode(packet.phase)`.
- `consensus_mcp/contributors/gemini.py` — same.
- `consensus_mcp/tools/reviewer_dispatch_codex.py` — schema gains
  `phase` (engine abstraction, primary) + `mode` (dispatcher escape
  hatch, optional). `_resolve_mode(phase, mode)` helper: explicit
  mode wins; otherwise translate phase via `_phase_mode`; otherwise
  None (caller omits `--mode` for backward compat).
- `consensus_mcp/tools/reviewer_dispatch_gemini.py` — mirrors codex
  wrapper exactly.
- `consensus_mcp/tests/test_phase_mode_forwarding.py` NEW — 21 tests
  covering: phase_to_mode mapping (all 3 phases + ValueError on
  unknown); CodexAdapter + GeminiAdapter argv forwarding for all 3
  phases (mock-based); MCP wrapper `_resolve_mode` + `_build_argv`
  (phase-to-mode mapping + mode-wins-over-phase + neither =
  backward-compat omission).

Test results: 696 pass (was 675; +21 for iter-0044). Adapter
boundary now covered by tests that would have caught the original
defect.

**README staleness sweep** (doc-only; no peer audit needed).

- `README.md`: "What it does" section updated to Workflow A/B/C
  vocabulary + Workflow C mention; install URLs already at
  `@v1.14.4`; "Pre-commit vs post-commit catch" section + table-of-
  contents reference Workflow A / Workflow B with "(was workflow
  #4 / #3 prior to v1.14.4)" historical note.
- `docs/workflows/workflow-4-preferred.md`: filename preserved for
  stable cross-references; frontmatter + body updated to Workflow A
  vocabulary; transition note at top.

Audit log: `consensus-state/autonomous-runs/run-2026-05-15-overnight/log.jsonl`
records both iterations + halt-set checks + scope-check decisions
per the v1.14.4 Workflow C contract schema.

## 1.14.4 - 2026-05-14

## 1.14.4 - 2026-05-14

Workflow A/B/C rename + Workflow C contract from
iter-workflow-abc-introduce (workflow A weighted-synthesis
convergence across claude + codex + gemini; no blocking objections).

**Operator-facing vocabulary: numeric → letter aliases.**

- Workflow A = propose-converge (was numbered #4) — DEFAULT
- Workflow B = post-review (was numbered #3) — LIGHTWEIGHT
- Workflow C = autonomous-execute (NEW) — LONG-FORM/OVERNIGHT
- Numeric aliases (3, 4) still resolve but emit `DeprecationWarning`;
  scheduled for removal in a future minor release.

**Workflow C — autonomous-execute (CONTRACT shipped, engine deferred).**

`consensus init` operators can configure a project for Workflow C;
goal_packet authors can declare an `autonomy_contract` block with
file boundaries, halt conditions, and iteration/wall-clock caps.
The validator and `check_autonomy_scope` helper are fully
implemented and tested. The actual multi-iteration auto-execution
loop is the named blocker for v1.15.0 (requires cross-platform
interrupt-file watching validation, integration tests with real
peer dispatches, autonomy-ledger replay design — multi-session
work that does not fit a hot-patch).

Workflow C requires exactly 3 contributors (claude + codex + gemini)
enforced at config-load — the wide cross-AI safety net is mandatory
for autonomous mode by default; v1.15.0+ may relax with explicit
operator opt-in.

When an operator runs a Workflow C goal_packet in v1.14.4, the
engine raises `NotImplementedError` with a clear message naming
v1.15.0 as the engine ship target and pointing at
`docs/workflows/workflow-c-autonomous.md`.

**Files in scope:**

- `consensus_mcp/config.py`: `WORKFLOW_AUTONOMOUS_EXECUTE` constant +
  alias map (A/B/C primary; numeric deprecated) + 3-AI requirement
  validator + DeprecationWarning emission for numeric aliases.
- `consensus_mcp/validators/scope_check.py`: new
  `validate_autonomy_contract` + `check_autonomy_scope` functions
  (approve/park/halt decisions); `DEFAULT_HALT_ON` constant lists
  the wide-by-default halt conditions; `AUTONOMY_CONTRACT_REQUIRED_FIELDS`
  documents required schema.
- `consensus_mcp/workflow_engine.py`: recognizes
  `WORKFLOW_AUTONOMOUS_EXECUTE`; raises `NotImplementedError` with
  v1.15.0 reference.
- `consensus_mcp/_init_wizard.py`: workflow prompt accepts letter
  aliases (A/B/C); resolves to canonical semantic string before
  storing.
- `consensus_mcp/tests/test_config.py`: 11 new tests covering alias
  rename, deprecation warning, 3-AI requirement, and Workflow C
  validation.
- `consensus_mcp/tests/test_scope_check_autonomy.py` (NEW): 17 tests
  covering autonomy_contract validation + check_autonomy_scope
  decision logic + glob matching edge cases.
- `consensus_mcp/dispatch_templates/codex_proposal_template.md`,
  `gemini_proposal_template.md`: A/B reference instead of #3/#4.
- `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`:
  new "Workflow A / B / C in one line each" section; #3/#4 references
  bumped to A/B throughout.
- `docs/workflows/workflow-c-autonomous.md` (NEW): operator-facing
  doc on Workflow C usage (autonomy_contract example, halt set table,
  scope-check decisions, interrupt mechanism, audit log location,
  v1.15.0 status note).

**Test summary:**

- 675 tests pass (full suite excluding pre-existing
  `test_dispatch_codex.py` ordering flake and `jsonschema`-missing
  environmental issue in the proposal-mode tests; both predate this
  release per `docs/known-issues/pytest-ordering-flake.md`).
- New: 11 in `test_config.py` for alias + Workflow C; 17 in
  `test_scope_check_autonomy.py` for the validator and decision
  logic.

**Named blockers for deferred work:**

- v1.15.0 — Workflow C multi-iteration auto-execution loop:
  requires cross-platform interrupt-file watching validation
  (Windows ReadDirectoryChangesW vs Unix select/poll), integration
  tests with real peer dispatches (cost: dispatcher latency × N
  iterations per test run), resume-after-halt semantics design,
  autonomy-ledger replay for failure recovery. Multi-session work.
- v1.16.0+ — project-level `.consensus/autonomous-policy.yaml` as
  default with goal_packet override: deferred until empirical
  evidence operators want it across multiple Workflow C runs (we
  have zero runs today; designing for hypothetical reuse is
  premature per the no-deferral rule's "real blocker" requirement).
- iter-0044 (adapter `--mode` forwarding fix) deferred to v1.14.5
  with named blocker (still requires adapter-boundary test
  infrastructure).

## 1.14.3 - 2026-05-14

Stabilization hot-patch from iter-audit-2026-05-14-three-followup-gaps
(workflow #4 weighted-synthesis convergence; codex + gemini + claude;
no blocking objections). Bundles all converged scope into one tag per
the no-deferral doctrine (operator directive: "consensus runs to
completion when goals + acceptance gates are clear").

**Doctrine: consensus runs to completion (no gratuitous deferral).**

New bundled-skill section "Consensus runs to COMPLETION" + dispatch-
template completion mandate codify the rule: deferring well-defined
work to "future iterations" without naming a specific blocker is an
anti-pattern. Includes the completion test (acceptance gates concrete?
open design surface? implementation cost small enough?) and explicit
anti-patterns ("iter-XXXX candidate" with no blocker, "Phase B" when
phases share a doctrine boundary, splitting hot-patches across tags
when fixes share a single failure mode).

**Doctrine in the data layer (Q1 — config.py):**

- `DISPOSITION_WEIGHTED_SYNTHESIS = "weighted-synthesis"` constant
  added to `consensus_mcp/config.py`.
- `VALID_DISPOSITION` now includes the new value.
- `VALID_DISPOSITION_FOR_PROPOSE_CONVERGE = {all-or-nothing,
  weighted-synthesis}` — workflow #4 accepts these two only;
  `per-finding` stays post-review-only (its semantics fit defect
  lists, not plan synthesis).
- `default_config()` workflow #4 default flips from `all-or-nothing`
  to `weighted-synthesis` (matches the iter-0043 doctrine).
- Validator at `config.py:303-309` updated to allow either valid
  workflow #4 disposition; rejects `per-finding` with actionable
  error message.
- `_init_wizard.py` defaults updated: workflow #4 setup now lands
  in `weighted-synthesis` by default; workflow #3 / advisory keep
  `all-or-nothing` as their default.
- 4 new tests in `test_config.py` cover both valid dispositions for
  workflow #4 + reject `per-finding` with regex-matched message.
  Pre-existing `test_default_disposition_all_or_nothing` renamed
  to `test_default_disposition_weighted_synthesis` and updated.
- All 107 tests in `test_config.py` + `test_init_wizard.py` pass.

**Procedural enforcement of disconfirming-evidence pattern (Q2):**

- Bundled skill: completion mandate in templates + skill (also
  lands the no-deferral rule above as the structural sibling).
- Dispatch templates (`codex_proposal_template.md`,
  `gemini_proposal_template.md`): completion mandate preamble so
  peer AIs default to in-scope completion, not deferral.

**Stale-tag mitigation (Q3):**

- `README.md` install URLs bumped from `@v1.14.0` to `@v1.14.3`
  (both pipx and pip-in-venv examples). Stops the bleeding wound
  where new installs were getting the v1.14.0 buggy bundled skill.
- `docs/advisories.md` NEW — standing channel for "shipped artifact
  has known doctrine drift" notices. First entry covers v1.14.0
  and v1.14.1 with explicit upgrade instructions (`pipx install
  --force` + re-run `consensus init --install-claude-code`).
- Bundled skill cut sequence (steps 8 + 10) gains explicit "bump
  README install URL on the new dev branch" + "add advisory entry
  if applicable" steps so this drift cannot recur structurally.

iter-0044 (adapter `--mode` forwarding fix per iter-0043 converged
plan) deferred to v1.14.4 with named blocker: still requires test
infrastructure setup for adapter-boundary fixtures that does not
fit in this hot-patch's scope.

## 1.14.2 - 2026-05-14

## 1.14.2 - 2026-05-14

Doctrine hot-patch from iter-audit-2026-05-14-pypi-invention
(workflow #4 postmortem consult; weighted-synthesis convergence
across claude + codex + gemini, no blocking objections).

The audit examined two compounding errors observed in the v1.14.0
release cycle: (1) inventing a "publish to PyPI" step in the cut
sequence based on the inference "Python project + pipx → PyPI"
without verifying the actual install URL form (the package is
not registered on PyPI; ships via git tags), and (2) gluing two
correct statements ("v1.14.0 tag is on origin" + "git tags are
the only channel") into a misleading composite ("v1.14.0 is
fully shipped via the only channel that exists") that masked
the artifact defect (the v1.14.0 tag at commit 8e0dab2 still
ships the buggy skill in its wheel).

Three orthogonal failure modes converged: verification gap +
defaulting bias + sloppy framing. Layered defenses applied:

- **Bundled skill: "Verify before invent"** section requires
  positive citation of the source for any step touching an
  external system / channel. Disconfirming evidence (missing
  creds, missing workflow, registry 404) is treated as the
  SIGNAL, not as a credential gap to fill.
- **Bundled skill: "Artifact-scoped claims"** section forbids
  global "fixed" / "shipped" claims when the surface is broader
  than what was changed. Required form names version + commit/
  tag + install path + bundled content + residual defects;
  immutable tag and dev branch are NOT the same artifact.
- **Dispatch templates** (codex_proposal_template,
  gemini_proposal_template) gain a verification-first mandate
  preamble so all peer AIs in future consults apply the rule,
  not just claude.
- **Project-local memories** (gitignored, claude-personal):
  feedback_verify_before_invent, feedback_partial_fix_surfacing,
  feedback_disconfirming_evidence (names the
  bias-rationalizes-evidence pattern explicitly), and
  reference_default_priors_to_distrust (enumerated list of
  high-risk inferences: PyPI/npm/Docker/MIT/pytest/etc.,
  append-only).

No code changes; doctrine-only. Scope unchanged for v1.14.3
(iter-0044 adapter `--mode` forwarding fix per iter-0043
converged plan).

## 1.14.1 - 2026-05-14

## 1.14.1 - 2026-05-14

Hot-patch: corrects the bundled `consensus-workflow` skill so it
no longer documents a PyPI publish step in the release cut
sequence. consensus-mcp ships via git tags + pipx (`pipx install
git+https://github.com/.../@vX.Y.Z`); the package is not
registered on PyPI. The v1.14.0 skill incorrectly added a PyPI
step that does not match the project's actual distribution model;
v1.14.1 removes it and adds an explicit "if you catch yourself
proposing PyPI, stop" warning so future sessions don't repeat
the mistake.

iter-0044 (adapter `--mode` forwarding fix per iter-0043 converged
plan) is open scope on v1.14.2 — not in this hot-patch.

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
