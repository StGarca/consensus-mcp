# v1.18.0 implementation — author's rationale for the Workflow B audit

**Reviewer:** audit claude's implementation of the v1.18.0 contributor-selection
feature (the embedded `v1.18.0-impl.diff`) for **correctness and regressions**.
This implements the converged plan from the 4-way Workflow A design consult
(B-routing + universal profiles). Empty findings is a valid review — don't
manufacture. Verify any file content you cite is actually in the diff.

Process: built via strict TDD across 4 components + a config reconciliation.
Full suite: **1053 passed, 1 skipped, 0 failed** (+~80 new tests). E2E smoke:
a no-claude, profile-backed config (`enabled=[codex,gemini,kimi]`, zero adapters
entries) validates and builds → `{codex:CodexAdapter, gemini:GeminiAdapter,
kimi:ProfileAdapter}`.

## What the diff does, and what to scrutinize

### 1. Config-driven profiles (`_contributor_profiles.py` + `contributor_profiles/*.yaml`)
`Profile` schema + `load_builtin_profiles` (resolves dir via `__file__` → pip-safe)
+ `merge_profiles` (config overrides built-in by name; whole-profile replacement)
+ `validate_profile` (required fields, kind/transport enums, flag↔prompt_flag
consistency, OS-key check). 4 built-in YAMLs.
**Scrutinize:** schema completeness; the verified install/auth strings (codex
`npm i -g @openai/codex`, gemini `npm i -g @google/gemini-cli`, kimi
`pipx install kimi-cli` — all verified on this machine, both OSes); claude=host
(no detect/install). codex/gemini profiles are **metadata-only** (dispatched by
their existing classes via B-routing); kimi is the only profile actually consumed
by ProfileAdapter.

### 2. `ProfileAdapter` (`contributors/profile_adapter.py`)
Generic `ContributorAdapter` that consumes a profile and reuses `_dispatch_base`
(prompt-build, JSON parse, T6 seal), parameterizing only the invoke (transport
stdin|flag, base_args, env, workdir_flag, output strip) — the same shape the
parent kimi wrapper proved, but first-class.
**Scrutinize (highest value):** (a) PROVENANCE — it builds dispatch_provenance
explicitly from the profile (model=profile.model, adapter='profile',
contributor=name); confirm a kimi seal can NEVER carry model='gemini-2.5-pro'
(the parent wrapper's bug). (b) Both transports invoke correctly (stdin → no
prompt flag; flag → prompt after prompt_flag). (c) It reuses `_seal_via_t6` (not
reimplemented). (d) Any divergence from how the existing gemini adapter parses/
seals that could break the sealed-artifact contract.

### 3. Factory routing (`_engine_factory.py`)
`build_adapters` resolution: (a) registered, (b) built-in class
(claude/codex/gemini → existing classes — **R8**), (c) merged profile
kind:cli_reviewer → ProfileAdapter; kind:host reserved (fail-closed); unknown →
fail-closed error. `adapters` entries are optional (`per_contributor.get(key) or {}`).
**Scrutinize:** R8 (existing [claude,codex,gemini] configs build identical
classes); the lazy profile-load; the host-profile-misuse guard; the unknown-key
error path.

### 4. Wizard (`_init_wizard.py`)
Numbered multi-select over merged profiles; `shutil.which` install status; min-2
re-prompt; claude optional; `--contributors a,b,c` non-interactive (flags-over-
prompts); OS-aware detect+guide (install/auth printed for missing, **nothing
executed**); non-destructive instruction-file provisioning (managed block between
sentinels, idempotent, dedupe by filename so codex+kimi share one AGENTS.md);
`--no-instructions` opt-out.
**Scrutinize:** min-2 enforcement; flag precedence; that detect+guide truly
executes nothing; the managed-block idempotency (running twice = one block) and
non-destructiveness (existing user content preserved); the AGENTS.md dedupe;
non-TTY degrade.

### 5. Config reconciliation (`config.py`) — DESIGN-JUDGMENT to scrutinize
Removed two rules: claude-mandatory and adapters-required-per-contributor.
Rationale: the operator chose **claude fully optional** (open-contributor model),
and the file's own comment says validation is "STRUCTURAL only — constructibility
is the engine_factory's fail-closed job." So both rules contradicted the stated
philosophy + the design. The `adapters` mapping stays optional config (type-check
kept). `contributors.profiles` overlay is validated.
**Scrutinize (key judgment call):** is dropping claude-mandatory SAFE — does any
code path assume claude is an enabled contributor (vs the host orchestrator)?
build_adapters + WorkflowEngine operate on the enabled set generically (claude
callback only used if claude enabled), and the E2E smoke ran a no-claude panel —
but please refute if you find a hidden claude assumption.

### 6. Cross-platform (`.mcp.json`, `pyproject.toml`, `README.md`)
Committed `.mcp.json` → `consensus-mcp` entry point (was Windows-only `py -3.11`);
`requires-python >=3.10`→`>=3.11`; `contributor_profiles/*.yaml` +
`contributor_instructions/*.md` added to package-data; README python range → 3.11+.
**Scrutinize:** the entry point is on PATH on both OSes (matches
`_resolve_mcp_command`); package-data globs correct (else pip users get no
built-ins).

### 7. Instruction files (`contributor_instructions/base.md`)
Vendored from `andrej-karpathy-skills/CLAUDE.md` (HTTP 200 verified) with a
citation header. Provisioned per-AI (CLAUDE.md/AGENTS.md/GEMINI.md).
**Scrutinize:** the citation header is present; provisioning writes the right
file per profile.instructions.filename.

## What to return
Per-component: correct? regression? Then a verdict on the config-reconciliation
judgment call (claude-optional safety) and the ProfileAdapter provenance. Mark
`goal_satisfied: true` only if the diff fully implements the converged plan with
no blocking findings.
