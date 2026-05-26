# Grok CLI integration — v1.31.0 design spec

**Status:** approved by operator 2026-05-26 — pending Workflow A consult ratification before implementation.
**Release target:** v1.31.0 (minor bump — feature-additive).
**Authoring channel:** brainstorming session 2026-05-26 (operator + claude).

## Goal

Add `grok` (xAI's `grok` CLI, version `0.1.219` as of writing) as the **5th first-class contributor** in consensus-mcp, alongside `claude` (host), `codex`, `gemini`, and `kimi`. UX MUST be identical to the existing four contributors from the operator's perspective; no CLI internals leak through.

Grok joins the **default panel** (every deep-tier consult dispatches all five). Quick tier stays at 2-AI (claude + codex); standard tier stays at 4-AI; deep tier grows from 4-AI to 5-AI.

## Why now

- Cross-family signal: grok is a fully independent family (xAI). Adding it raises the `>=2 non-claude reviewer families` floor's safety margin and the number of distinct differentials a deep consult can surface.
- The CLI shape matches our existing dispatcher contract: `--single` / `--prompt-file` headless mode, `--output-format plain`, stable independence flags (`--no-memory --no-plan --no-subagents --disable-web-search`, `--max-turns 1`). Integration cost is mostly mechanical.
- Operator request, 2026-05-26.

## Non-goals

- Replacing any existing contributor.
- Changing the convergence model, the gate logic, or the `_design_approval` / `_delivery_readiness` rules.
- Building sandbox / integrity hardening for grok before there's evidence we need it. (Kimi's hardening earned its complexity from a real field bug — grok doesn't carry that history yet. Wire `--sandbox` reactively if needed.)
- Quick-tier panel composition changes — operator chose to keep quick at 2-AI.

## Approach (selected)

**Gemini-twin.** Copy the gemini dispatcher + adapter + templates, rename, adjust the CLI args to grok's flags. Plain-text output, YAML parsed from the model's response — same parser as gemini/kimi.

### Rejected alternatives

- **Kimi-twin with `--sandbox` from day one.** Imports kimi's integrity-check + concurrent-write false-positive surface (per memory `kimi-integrity-concurrency`) without evidence grok needs it. Grok's first-class `--sandbox <profile>` is a strictly better hook if hardening becomes necessary later — wire then.
- **JSON-output codex-style.** Grok's `--output-format json` wraps the model's text in a JSON envelope but does NOT enforce a schema (unlike codex's `--output-schema`). We'd still parse YAML out of the JSON `content` field — strictly more parse layers than gemini-style for no benefit, and it breaks UX parity with the gemini/kimi dispatchers.

## CLI invocation contract

For every dispatch (propose or review mode):

```bash
grok --prompt-file <iter_dir>/grok-prompt.txt \
     --output-format plain \
     --no-memory \
     --no-plan \
     --no-subagents \
     --disable-web-search \
     --max-turns 1 \
     --permission-mode dontAsk \
     --cwd <iter_dir> \
     [--model <model_id>]
```

Flag rationale:

| Flag | Reason |
|------|--------|
| `--prompt-file <path>` | Multi-KB prompts (goal_packet + review_packet + touched_files_contents) without shell-quoting drama. The fallback `--single` is used only for smoke tests. |
| `--output-format plain` | Matches the gemini/kimi parser. Verdict YAML embedded in stdout. |
| `--no-memory` | Independence — no carryover from prior grok sessions on this machine. Essential for blind-first-reveal. |
| `--no-plan` | Skip planning mode. A review is a single-turn structured task, not a multi-step plan. |
| `--no-subagents` | No fan-out. Single-pass review only. |
| `--disable-web-search` | Determinism + repo-confined. Reviewer must reason from the embedded review-packet content, not the web. |
| `--max-turns 1` | Bounded — one turn is all a verdict needs. |
| `--permission-mode dontAsk` | Headless mode requires no interactive prompts. |
| `--cwd <iter_dir>` | Confine grok's working dir to the iteration directory (cosmetic — `--disable-web-search` + `--no-subagents` already constrain side effects). |
| `--model <id>` | Optional. Default is grok's CLI default (lets grok roll forward without dispatcher releases). Operator can pin via `.consensus/config.yaml`. |

Auth: CLI-managed via `~/.grok/auth.json` (xAI OAuth — same shape as codex's token store). The dispatcher checks `grok login`-state by probing for the auth file. If absent, emits:

```
GrokAuthRequiredError: no grok authentication found at ~/.grok/auth.json.
Run `grok login` to authenticate, then re-run.
```

Clean parity with codex's auth-missing error.

## Files

### New (6)

```
consensus_mcp/_dispatch_grok.py
consensus_mcp/contributors/grok.py
consensus_mcp/dispatch_templates/grok_review_template.md
consensus_mcp/dispatch_templates/grok_proposal_template.md
consensus_mcp/tests/test_dispatch_grok.py
consensus_mcp/tests/test_grok_adapter.py
```

Templates are copies of `gemini_*` templates with no functional change — they call the contributor by name in the rendered prompt header for clarity.

### Modified (6)

```
pyproject.toml                            # add console script entry
consensus_mcp/_engine_factory.py          # import GrokAdapter; add to BUILT_IN_ADAPTERS
consensus_mcp/config.py                   # add 'grok' to default_config + known kinds
consensus_mcp/_init_wizard.py             # add grok detect/profile path
consensus_mcp/contributors/__init__.py    # re-export GrokAdapter
README.md                                 # update headline + the "Claude, Codex, Gemini, Kimi" lists
CHANGELOG.md                              # v1.31.0 entry
```

`_dispatch_base.py` and shared dispatch helpers are reused as-is — no base-layer changes. The verdict YAML schema is shared via the existing template machinery — no new YAML contract.

## Engine wiring

`_engine_factory.BUILT_IN_ADAPTERS` gains one entry:

```python
"grok": GrokAdapter,
```

`config.default_config()` enables grok in the default panel so a config-less project auto-gets all 5 in deep tier. The tier router's existing logic (per `docs/consensus/routing-decision-table.md`) drops grok from quick / standard tiers via the existing per-tier panel filter — no new branching.

## Cross-family count + gates

Grok is an independent family (xAI). `_design_approval._count_non_claude_reviewers` and `_delivery_readiness.mint_delivery_token` already iterate `<family>-review.yaml` files under the iteration directory — adding `grok-review.yaml` increments the cross-family count automatically. No gate-logic change required.

## Test surface

### Unit tests (no real grok CLI)

| File | Coverage |
|------|----------|
| `tests/test_dispatch_grok.py` | auth-missing error path; CLI arg shape (assert all 9 flags present); prompt-file resolution; seal flow round-trip; timeout handling; `--smoke` env gate |
| `tests/test_grok_adapter.py` | DispatchPacket round-trip; error → DispatchError mapping; phase=propose vs phase=review; provenance fields |

### Integration test (env-gated)

`tests/test_dispatch_grok_smoke.py` — env-gated via `CONSENSUS_MCP_RUN_REAL_GROK_SMOKE=1` (mirrors `CONSENSUS_MCP_RUN_REAL_GEMINI_SMOKE`). CI does not set the env by default. Operator opt-in.

### Modified tests

- `test_engine_factory_profiles.py` — assert `grok` in `BUILT_IN_ADAPTERS`
- `test_config.py` — assert `grok` in `default_config()` panel
- `test_package_data_completeness.py` — assert `grok_review_template.md` + `grok_proposal_template.md` ship in the wheel

## Failure modes the design admits

| Mode | Probability | Mitigation |
|------|-------------|------------|
| grok CLI flag rename between versions | Medium (CLI is young at 0.1.x) | Dispatcher logs `grok --version` in `dispatch_provenance.grok_version`. Test asserts the version string is present. Operator pin via wrapper script if upstream breaks. |
| grok auth missing on a clean machine | High (first install) | Clean error message + exit code; `consensus-init` wizard probes and prints a one-line hint. |
| Default-panel cost: every deep consult adds ~25% wall-clock + tokens | Certain | Documented in CHANGELOG. Operator disable via `.consensus/config.yaml: contributors.grok.enabled: false`. |
| Larger v1.31.0 surface immediately after a v1.30.7 hot patch | Certain | Full suite must stay green; smoke is env-gated so CI doesn't burn xAI credits on every run; the converged plan reviewed by the consult is the rigor check. |

## Release plan

`v1.30.7` → `v1.31.0` (minor bump — new contributor is feature-additive, no breaking changes). Follow the standard release-cut runbook:

1. CHANGELOG entry with the converged-plan ref.
2. pyproject version bump.
3. README install URL + Status bump **before tagging**.
4. Full pytest green.
5. Tag `v1.31.0`.
6. Push `refs/heads/main` + `refs/tags/v1.31.0`.
7. `gh release create` (or the API equivalent) — Latest + the tag note.
8. `pipx install --force …@v1.31.0` + `consensus-init --install-claude-code --force`.
9. Mint delivery tokens for every modified source file before the cut commit.

## Open question for the consensus consult

The anchor for the Workflow A consult: **gemini-twin pattern + grok joins the default panel + no day-one hardening**.

Adversarial questions for the panel:

1. **Auth model.** xAI OAuth tokens have a different refresh shape than gemini's API key or codex's session token. Is `~/.grok/auth.json`-existence a sufficient pre-flight, or does the dispatcher need to probe `grok inspect` to confirm the token is non-expired?
2. **Default-panel cost.** Every deep consult now burns 5 CLI dispatches instead of 4. Is the cross-family signal worth the ~25% wall-clock + token bump for every governance-surface change? Or should grok join only on operator-declared `lean=cross-family-deep`?
3. **Prompt-file vs --single.** `--single` accepts the prompt inline, `--prompt-file` reads from disk. Disk-read introduces a (small) FS race window if the iteration dir is written concurrently. Worth it for shell-quoting safety, or use `--single` with shlex-quoting?
4. **Hardening day-one.** Is the gemini-twin pattern (no sandbox) safe enough, or does grok's broader default tool surface (web-search, subagents, plan-mode — all of which we disable) warrant a defensive `--sandbox <profile>` from the first commit anyway?
5. **Schema enforcement.** Codex enforces verdict YAML via `--output-schema`. Grok's `--output-format json` does not enforce a schema — but maybe a `--system-prompt-override` + structured rules could approximate it. Worth the prompt complexity, or accept the same plain-text-parse failure mode gemini/kimi already have?
