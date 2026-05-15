# Advisories

Standing channel for "shipped artifact has known doctrine drift"
notices. Each advisory names the affected versions, the issue, the
correct version to upgrade to, and any user action required.

Format: append-only, newest first. Older advisories may be marked
**resolved** when no users remain on the affected versions, but
the entry stays for the historical record.

---

## Advisory 2026-05-15: v1.15.7 named follow-ups (non-blocking)

**Affected versions:** `v1.15.7` (forward, until addressed).

**Severity:** Tracked engineering follow-ups — NOT defects in
shipped behavior. CI is fully green (all 6 legs) and the full
suite is 968/0; these are scoped, deliberately-deferred items
from the v1.15.7 CI-greening Workflow B audit.

1. **Cross-process dispatch-log serialization.** v1.15.7 made
   `_dispatch_base._log_dispatch` intra-process atomic (a
   module-level `threading.Lock`) — the verified root cause of the
   observed torn line (main + reader threads of one dispatcher).
   It does **not** serialize a shared `dispatch-log.jsonl` across
   *parallel dispatcher processes* (e.g. Workflow-A round-1
   codex+gemini). This is **unobserved** and was explicitly
   excluded (a blocking cross-process OS lock was tried and
   regressed windows-py3.12 — see CHANGELOG v1.15.7). Revisit only
   if parallel-dispatcher torn lines are ever observed.
2. **4 codex-dispatch "smoke" tests are integration tests.**
   `test_main_smoke_with_mocked_codex` + 3 siblings mock
   `subprocess.run` but not the iter-0037 `Popen` path, so they
   `skipif` when no real `codex` binary (CI runners). Proper fix:
   rewrite onto the existing `make_fake_codex_popen_factory` /
   `popen_factory=` pattern so they run hermetically everywhere,
   then drop the skip.

**User action:** none required. Recorded for the maintainer; no
upgrade implication.

---

## Advisory 2026-05-15: gemini dispatch fails on gemini CLI ≥ 0.43.0-preview.0

**RESOLVED in `v1.15.2`** — `_dispatch_gemini.py` now injects
`GEMINI_CLI_TRUST_WORKSPACE=true` into the gemini subprocess env
(`_gemini_subprocess_env()`). Users on `v1.15.1` or earlier with
gemini CLI ≥ 0.43.0-preview.0 must upgrade to `v1.15.2` (or apply
the env-var workaround below until they do). Entry retained for the
historical record.

**Affected versions:** all consensus-mcp versions through `v1.15.1`
(the gemini dispatcher behavior is unchanged across this range).

**Severity:** Functional — gemini peer reviews silently fail to
seal when the installed `gemini` CLI enforces workspace trust.
codex + claude consensus is unaffected; the cross-AI safety net
degrades from 3-AI to 2-AI until worked around.

**Issue:**
- `gemini` CLI `0.43.0-preview.0`+ refuses headless/automated runs
  in an "untrusted" directory, emitting the trust error to stderr
  and **empty stdout**. `consensus_mcp/_dispatch_gemini.py` did
  not set `GEMINI_CLI_TRUST_WORKSPACE`; it already passed
  `--skip-trust`, but that flag is **not load-bearing** on this CLI
  version (empirically: with `--skip-trust` only, gemini bypassed
  trust but went autonomous and 429'd). So the dispatcher saw empty
  output and failed `GeminiOutputParseError` ("Expecting value:
  line 1 column 1").
- Observed first-hand 2026-05-15 during the v1.15.1 Workflow B
  audit: gemini pass-1 failed twice (initial + dispatcher
  auto-retry) for exactly this reason.

**Workaround (no upgrade needed):** export
`GEMINI_CLI_TRUST_WORKSPACE=true` in the environment that runs the
dispatcher (or the MCP server). The v1.15.1 audit was completed
this way; gemini returned clean approvals once the env var was set.

**Correct upgrade target:** `v1.15.2` — ships the dispatcher-level
fix (`_gemini_subprocess_env()` injects
`GEMINI_CLI_TRUST_WORKSPACE=true` into the gemini subprocess env).
`_dispatch_gemini.py` was in the v1.15.1 iteration's
`forbidden_files`, so the fix was correctly deferred to v1.15.2
rather than patched out-of-scope. The env-var workaround above
remains valid for anyone still on ≤ v1.15.1.

**Provenance:**
- Diagnosed during `iteration-converged-plan-machine-enforcement`
  (v1.15.1 Workflow B audit); recorded there as a v1.15.2 named
  blocker in `fix-response-pass2.md`.
- Fixed in `iteration-gemini-trust-env-fix` (v1.15.2; Workflow B
  audit clean — gemini approved end-to-end via the source fix with
  the env-var workaround explicitly unset; codex doc-accuracy
  findings integrated).

---

## Advisory 2026-05-15: v1.15.0 converged-plan convention is doctrine-only

**Affected versions:** `v1.15.0`

**Severity:** Scope clarification (no regression). The v1.15.0 tag
`4e81f9e` shipped the converged-plan convention
(`falsification` / `independent_safeguard` /
`decisive_experiment_before_next_iteration`) as an **authoring
convention enforced by doctrine only** — bundled skill + Workflow
B audit, **zero engine code**.

**Issue:** users on `v1.15.0` may assume the convention is
machine-validated. It is not. There is no schema, no validator, no
seal-time gate on `v1.15.0`.

**Correct upgrade target:** `v1.15.1` — adds the JSON Schema, the
structure/consequence validator, the fail-closed seal-time gate,
the `convergence.converged_plan_enforcement` knob (default
`graduated`), and read-time surfacing. Machine enforcement exists
only from the `v1.15.1` tag forward.

**Provenance:**
- `iteration-converged-plan-machine-enforcement` (Workflow A
  weighted-synthesis: claude + codex + gemini; shared-prior
  self-check PASSED; Workflow B audit clean: gemini ×2, codex
  pass-4b goal_satisfied=true, 0 blocking, 0 findings).

---

## Advisory 2026-05-14: v1.14.0 + v1.14.1 bundled-skill drift

**Affected versions:** `v1.14.0`, `v1.14.1`

**Severity:** Doctrine drift (no functional regression in
consensus-mcp itself). Affects the bundled `consensus-workflow`
SKILL.md content shipped in the wheel and installed by the
Claude Code bootstrap pack.

**Issue:**
- `v1.14.0` ships a bundled skill that incorrectly documents a
  PyPI publish step in the release cut sequence. consensus-mcp
  is NOT registered on PyPI; releases are git-tag-only via
  `pipx install git+https://github.com/.../@vX.Y.Z`. The PyPI
  step in the skill was added in error during the v1.14.0 cut
  and propagates misleading release-procedure documentation to
  every project that runs `consensus init --install-claude-code`
  against this version.
- `v1.14.1` ships a partially-corrected skill (PyPI step
  removed) but is missing the "Verify before invent" and
  "Artifact-scoped claims" doctrine sections that landed in
  v1.14.2.

**Correct version:** `v1.14.3` (or any later release).

**User action required:** Upgrade installs that pulled
`@v1.14.0` or `@v1.14.1`:

```
pipx install --force git+https://github.com/stgarca/consensus-mcp.git@v1.14.3
```

If you have run `consensus init --install-claude-code` against
v1.14.0 or v1.14.1, re-run it against v1.14.3 to refresh the
project-local skill copy (the bootstrap pack copies the bundled
skill into the project's `.claude/skills/` directory at install
time; old installs retain the stale copy).

**Provenance:**
- Originating audit: `iter-audit-2026-05-14-pypi-invention`
  (workflow #4 weighted-synthesis convergence; codex + gemini +
  claude all approved with no blocking objections).
- Doctrine fix landed: `v1.14.2` tag, commit `12eca6c`.
- Follow-up audit: `iter-audit-2026-05-14-three-followup-gaps`
  shipped this advisory mechanism + README install-URL bump in
  `v1.14.3`.

---
