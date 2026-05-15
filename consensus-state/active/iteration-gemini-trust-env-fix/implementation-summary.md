# Workflow B audit target — v1.15.2 gemini workspace-trust fix

Closes the v1.15.1 named blocker (advisory 2026-05-15). Branch
`v1.15.2`. This is a clear-root-cause execution bugfix → Workflow B.

## Root cause (empirically verified 2026-05-15, not inferred)

gemini CLI `0.43.0-preview.0`+ refuses headless runs in an
"untrusted" directory: trust error → stderr, **empty stdout** →
dispatcher fails `GeminiOutputParseError` ("Expecting value: line
1 column 1"). The dispatcher already passed `--skip-trust` but no
`env=` to Popen.

Controlled comparison on this host (gemini 0.43.0-preview.0):
- **`--skip-trust` only, no env:** bypassed trust BUT gemini went
  autonomous (ignored the JSON directive, started planning a repo
  refactor) and hit 429. Non-deterministic, unusable.
- **`GEMINI_CLI_TRUST_WORKSPACE=true`, no flag:** clean `OK`,
  exit 0. Deterministic.
- Corroborated by the v1.15.1 audit: env var → 2 clean gemini
  approvals (pass-2, pass-3); without → 2 empty failures (pass-1 +
  auto-retry).

Conclusion: `--skip-trust` is NOT load-bearing on this version;
`GEMINI_CLI_TRUST_WORKSPACE` is. `--skip-trust` retained
defense-in-depth (harmless, accepted by the CLI).

## Change

`consensus_mcp/_dispatch_gemini.py`:
- NEW `_gemini_subprocess_env()`: returns `os.environ.copy()` with
  `GEMINI_CLI_TRUST_WORKSPACE` forced to `"true"` (an inherited
  `false` from operator/CI must not defeat the fix); never mutates
  `os.environ`.
- `_invoke_gemini` Popen call now passes
  `env=_gemini_subprocess_env()`.
- Docstring updated: `--skip-trust` is defense-in-depth, not
  load-bearing; the env var is the fix.

`consensus_mcp/tests/test_dispatch_gemini.py`: 4 new unit tests
(sets the var; preserves PATH + full inheritance; no os.environ
mutation; overrides an inherited `false`).

`CHANGELOG.md`: 1.15.2 entry. `docs/advisories.md`: advisory
2026-05-15 marked RESOLVED in v1.15.2.

## Verification

- `pytest -k subprocess_env`: 4 green.
- `test_dispatch_gemini.py`: 41 green.
- Full suite: **968 passed, 1 skipped, 0 regressions**.
- **End-to-end:** this iteration's own gemini audit pass is
  dispatched from source WITHOUT the manual env-var workaround. If
  gemini-review.yaml seals with a verdict, the fix is proven in
  vivo (acceptance gate A4).

## Audit questions

- Q1 goal_satisfied: does the fix correctly + minimally close the
  blocker without regressing gemini review semantics?
- Q2: is forcing the var over an inherited value correct (vs.
  `setdefault`)? Consider an operator who deliberately set
  `GEMINI_CLI_TRUST_WORKSPACE=false`.
- Q3: any blocking objection? State the differential/prior you
  reasoned from.
