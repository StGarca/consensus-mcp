# Design spec: symbiotic superpowers + consensus iteration pipeline

- **Date:** 2026-05-23
- **Status:** Approved (3-AI consensus consult - codex + gemini + host-peer; kimi quota-dropped)
- **Iteration:** `consensus-state/active/iteration-symbiotic-workflow-design-2026-05-23`
- **Consult provenance:** codex `codex-sym-1-pass1` (`167586d0...`), gemini
  `gemini-sym-1-pass1` (`540b1424...`), host-peer (blind Claude subagent). kimi
  (`kimi-sym-1`) failed with a 403 billing-cycle quota limit (non-retryable) - panel
  ran 3-voice, which matches the prior agents-init consult's composition.
- **Operator-approved scope:** A -> B -> C, dual-path.

## Goal

Make the iteration workflow **codified and session-independently repeatable**, so
the superpowers process skills (`brainstorming` -> `writing-plans` ->
`subagent-driven-development` -> `finishing-a-development-branch`) and the consensus
consult are **symbiotic**: one documented pipeline, using the already-shipped
(v1.21) host_peer machinery, with the 4-AI panel runnable through the engine.

## Verified ground truth (from the consult)

- `consensus.run_iteration` -> `WorkflowEngine.run_iteration` already runs
  blind-first-reveal propose-converge; codex/gemini adapters shell out to
  `_dispatch_codex`/`_dispatch_gemini`; claude + host_peer via callbacks.
- **host_peer shipped v1.21** (commit b269dd0) - real, tested (~190 refs).
- **Gap:** no `KimiAdapter` (built-ins are claude/codex/gemini); `default_config()`
  enables kimi but ships no profile -> `run_iteration` can't build a kimi adapter
  out of the box. kimi-via-`ProfileAdapter` bypasses the hardened `_dispatch_kimi`.
- **Static-echo (verified):** the CONVERGE `DispatchPacket` carries `round_number`
  + `convergence_packet_path` (`base.py:155-163`); the engine builds it per round
  (`workflow_engine.py:273,284`) and both callbacks receive it
  (`claude.py:54`, `host_peer_adapter.py:95`) - but `_build_claude_callback` /
  `_build_host_peer_callback` ignore it and echo one YAML. **However**, a pure
  closure change cannot make the host LLM *re-think* mid-run (the Python server
  can't spawn a Claude subagent). Genuine re-convergence needs **either**
  orchestrator-driven rounds (Path A) **or** a `run_iteration` pause/resume API.

## Converged decisions

### A - `KimiAdapter` (T1a + T3a)
- Add `consensus_mcp/contributors/kimi.py` `KimiAdapter`, a near-verbatim mirror of
  `contributors/codex.py`, wrapping `_dispatch_kimi.main(argv)` (identical argv:
  `--mode {review,proposal}`, `--reviewer-id`, `--pass-id`, `--goal-packet`,
  `--iteration-dir`, `--review-target`; identical JSON envelope:
  `ok`/`pass_id`/`sealed_path`/`archive_sealed_path`/`packet_sha256`). This keeps
  the hardened behavior (env-scrub of `KIMI_API_KEY`/`OPENAI_API_KEY`, exit-75
  retry, disposable workdir, integrity check) that `ProfileAdapter` bypasses.
- Register `"kimi": KimiAdapter` in `_engine_factory._BUILTIN_ADAPTERS` so
  `enabled: [..., kimi]` "just works" with no profile - like codex/gemini.
- **`ProfileAdapter` is retained for genuinely user-defined `cli_reviewer`s**, but
  is no longer the default-kimi path (avoid two kimi code paths drifting).
- **Phase->mode mapping (critical - the iter-0044 bug class):** kimi's `--mode`
  accepts only `review|proposal` (no `converge`). `KimiAdapter` must map the
  CONVERGE phase exactly as `CodexAdapter` does (via the same `phase`->mode helper)
  so converge rounds do NOT silently fall back to review-mode templates.

### B - Reconcile into a dual-path consult flow (T2 confirm)
Two paths, with an explicit selection rule (the orchestrator picks per consult):

- **Path B - `consensus.run_iteration`** (the built engine): for **post-review /
  execution / clear-design / hot-patches** (Workflow B, or Workflow A with low
  design surface). The engine dispatches codex/gemini/kimi + claude + host_peer,
  runs blind-first-reveal, evaluates the convergence rule, and seals. claude +
  host_peer are supplied as `claude_proposal_yaml` / `host_peer_review_yaml`
  (static across rounds - acceptable here).
- **Path A - orchestrator-driven** (dispatch binaries + host-supplied blind
  subagents + manual weighted-synthesis): for **propose-converge with real design
  surface**, where claude/host_peer must genuinely re-converge across rounds. This
  is **KEPT as the documented advanced path**, not retired - its sole remaining
  justification is genuine multi-round human/host convergence.
- **host_peer is first-class in BOTH** (Path B via `host_peer_review_yaml`; Path A
  via the codified dispatch procedure, C/T6).
- **Static-echo fix = named follow-up, NOT this iteration.** Recorded precisely:
  the convergence packet is already round-aware, but real host re-convergence needs
  a `run_iteration` pause/resume (round-by-round) API or Path A - a closure change
  alone cannot make the host LLM re-evaluate mid-run. When done, fix **both**
  claude + host_peer closures together (shared `(DispatchPacket)->dict` signature)
  and preserve round-1 blindness.

### C - Codify the symbiotic runbook (T4b + T5 + T6a)
- **Extend `consensus-workflow`** (the existing operating-procedures skill) with an
  "end-to-end iteration pipeline" section mapping each superpowers stage <-> consensus
  stage, plus a concise `docs/workflows/iteration-pipeline.md`. (No new skill -
  avoid trigger fragmentation.)
- **Release-currency (T5)** - add to the `consensus-workflow` release cut-sequence,
  after tag + push + FF main + GitHub Release:
  1. `pipx install --force git+https://github.com/StGarca/consensus-mcp.git@vX.Y.Z`
     (the global install is a non-editable COPY; without this the operator's CLI
     stays on the old tag).
  2. `consensus-init --install-claude-code --force` (refresh `~/.claude`
     skills/commands).
  3. **Smoke the INSTALLED binary AND assert its version == `vX.Y.Z`** (the
     stale-pipx failure is a binary that *runs* but reports the old version - a
     "binary runs" check is insufficient).
- **host_peer dispatch procedure (T6a)** - document, in the runbook, the repeatable
  steps + a `host_peer_review_yaml` schema template: orchestrator dispatches a
  **blind** Claude subagent (fresh context, no peer artifacts) using
  `host_peer_review_template.md`; captures output as YAML
  (`findings: list`, `goal_satisfied: bool`, `blocking_objections: list`); feeds
  `run_iteration` (Path B) or seals it (Path A). No new code (a helper is YAGNI
  until a second caller exists).
- **`.consensus` gate caveat** - this repo has no `.consensus/config.yaml`, so the
  enforcement gate (seal `design-approved` / mint delivery token) is **inactive
  here**. The runbook must label seal/gate steps "applies in gated projects," and
  may note the option to dogfood-activate the gate in this repo (separate decision,
  not in this scope).

## Components & files in scope

- **Create** `consensus_mcp/contributors/kimi.py` (`KimiAdapter`).
- **Modify** `consensus_mcp/_engine_factory.py` (register `kimi` built-in).
- **Create** `consensus_mcp/tests/test_kimi_adapter.py` (adapter-level) + extend a
  `build_adapters` test.
- **Modify** `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`
  (pipeline section + release-currency steps + host_peer dispatch procedure).
- **Create** `docs/workflows/iteration-pipeline.md` (the runbook).
- **Modify** `CHANGELOG.md`.

## Testing plan (TDD)

1. **KimiAdapter** (mirror codex adapter tests): argv translation per phase
   (propose->`proposal`, review->`review`, **converge->the correct mode**, asserting it
   is NOT silently `review` when the engine is in a converge round); non-JSON
   stdout -> `DispatchError`; `rc != 0` -> `DispatchError`; JSON envelope parsed into
   a `SealedArtifact`.
2. **`build_adapters`**: `enabled: [claude, codex, gemini, kimi]` (default config,
   no profiles) constructs a `KimiAdapter` for kimi (NOT a `ProfileAdapter`, NOT a
   build failure).
3. **Phase->mode regression**: a converge-phase dispatch maps to a kimi-valid mode +
   the converge template (guards the iter-0044 class).
4. **Docs/runbook**: a contract-style test that `consensus-workflow/SKILL.md`
   contains the release-currency steps (`pipx install --force`,
   `--install-claude-code --force`, version-assert smoke) and the host_peer
   procedure - so the codification can't silently regress.
5. Full suite green before any release.

## Out of scope (explicit follow-ups)

- **Static-echo / per-round host re-convergence in Path B** - needs a
  `run_iteration` pause/resume API or continued use of Path A. Named follow-up;
  fix both claude + host_peer closures together when done.
- **A `host_peer_review_yaml` formatter/validator helper** (T6b) - YAGNI until a
  second caller.
- **Dogfooding the `.consensus` enforcement gate in this repo** - separate decision.
- **Deprecating `ProfileAdapter`** - it stays for user-defined cli_reviewers.

## Risks (from the panel)

1. **Phase->mode mapping** (kimi has no `converge`) - the highest-impact correctness
   risk; mitigated by mirroring `CodexAdapter` + the explicit converge-mode test.
2. **kimi integrity check false-positives on concurrent edits** - a 4-AI engine run
   that mutates the repo (or a concurrent subagent write) can spuriously reject the
   kimi review; the runbook must warn against repo mutation during an engine run.
3. **Dual-path becomes permanent** (gemini) - the static-echo follow-up must be
   recorded sharply so Path B's design-surface weakness is fixed rather than
   normalized.
4. **Stale-pipx after release** - mitigated by the version-asserting smoke (T5).
5. **Artifact currency** - host_peer is in immutable v1.21; KimiAdapter + runbook
   land in the next tag; don't conflate them in claims.
