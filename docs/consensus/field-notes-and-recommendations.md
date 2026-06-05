# Consensus field notes & recommendations

A running record of design findings and recommendations surfaced by real consensus
runs — especially **foreign-host** runs (a non-Claude AI driving the consult),
which exercise paths Claude-hosted runs in this repo never hit. Newest first.

---

## 2026-06-04 — Codex-hosted consult in a consuming project (`external-project`)

A non-Claude AI (Codex) hosted a real 5-contributor propose-converge consult in a
consuming project. The run itself was genuine: 5/5 responsive, converged
strict-majority (4 approve, 1 substantive codex block), sealed provenance, ~9m51s.
Reviewing the run artifacts surfaced the items below. The two design items were
taken to a Workflow-A consult (`iteration-approve-two-consensus-mcp-changes-...
-f641f060`, 4/4 unanimous approve) and implemented.

### F1 — Dispatch-log payload bloat (SHIPPED, no consult)

**Severity: high (every governed project).** A kimi workdir copytree failure raised
a `shutil.Error` whose `str()` embedded the *entire copied-file manifest* (~188 MB).
`_log_dispatch` wrote it verbatim → a single 188 MB JSON line and a **702 MB**
append-only `dispatch-log.jsonl` accreted across iterations (6 records = 685 MB of
702 MB).

**Root cause:** the dispatch-log writer had no per-field size bound, and any adapter
can hand it an arbitrarily large exception string.

**Fix (root, one primitive):** `_dispatch_base._log_dispatch` now caps every string
field via the shared `cap_text_field()` helper (`_MAX_DISPATCH_FIELD_CHARS = 16384`)
with a `...[truncated N chars]` marker that preserves the original length. Because
the cap is at the single writer every adapter shares, the 188 MB-into-the-log path
is structurally impossible for codex/gemini/grok/kimi alike. Tests:
`tests/test_dispatch_log_cap.py`.

**Why not in Claude-hosted runs:** this repo has `.git`, so kimi stages via
`git clone --local` and never reaches the copytree fallback. The fallback only
triggers in a no-`.git`/clone-failed repo — i.e. some consuming projects.

**Dropped as speculative:** changing kimi's *copy behavior* on a non-ENOSPC
copytree error (degrade-to-no-copy vs fail-loud). The log cap already neutralizes
the real harm; relaxing physical isolation is a safety decision with no demonstrated
need. Revisit only if a real consuming-project case demands it.

### F2 — Convergence packet omitted the target document (SHIPPED, consult-approved)

**Severity: high (workflow correctness).** In propose-converge,
`workflow_engine._build_convergence_packet` bundled the per-contributor proposal
YAMLs but **never embedded the actual document under review**. Converge-round
reviewers could critique but **could not author line-accurate patches** — in the
field run all three codex findings returned `patch_proposal: null` citing "the
actual Markdown is not embedded in this review target."

**Fix:** `_build_convergence_packet` now accepts an optional `target_path` and embeds
its content **first** in `touched_files_contents` (the call site threads
`problem_statement_path`, which is the target document for workflow 4). Size-guarded
with the shared `cap_text_field()` helper. `target_path` defaults to `None` →
prior behavior is byte-identical for any other caller. Tests:
`tests/test_workflow_engine_convergence_packet.py`.

**Open-question dispositions (weighted synthesis, 4/4 approve):**
- *Storage (Q1):* fold the target into the existing `touched_files_contents` map
  (codex/gemini/grok) rather than a dedicated key. Reviewers and patch tooling
  already read that map; a separate namespace risks being overlooked.
- *Independence (Q2):* neutral — embeds the review *subject* (already visible in the
  propose round), not unsealed contributor reasoning. Restores propose↔converge
  parity.
- *Truncation (Q3):* reuse the 16 KB cap + marker via a shared helper factored out of
  the dispatch-log fix (grok DRY refinement), so the two cap sites cannot drift.

**Residual design note (kimi's dissent, deferred — not wrong):** folding the target
into `touched_files_contents` makes that map hold *two* semantic kinds — the
contributor proposals and the document under review. Kimi argued for a dedicated
`target_document` key to keep the map "proposals only," warning that consumers
iterating the map could mis-handle the new entry. We took the majority's structure
but kept kimi's concern as a guard: acceptance gate **A4** is a test proving existing
consumers (e.g. the `contributor_weights` ordering invariant) still pass with the
target entry present, and the target path is listed first for human scan order.
**If a future consumer ever needs to distinguish "the thing under review" from "the
proposals" (e.g. typed metadata, a patch helper that treats every entry as a
proposal to merge), revisit kimi's dedicated-key proposal** — it was deferred as
YAGNI, not rejected on merit.

### F3 — No supported "run a full iteration" entrypoint (SHIPPED, consult-approved)

**Severity: medium (host onboarding).** Entry points existed
(`consensus-mcp-start-consult`, the `consensus_run_iteration` MCP tool) but **no
console script ran a full iteration end-to-end**. The Codex host hand-rolled a
2.6 KB shim (`run/_consensus_launch.py`) calling
`consensus_run_iteration.handle()` directly, bypassing the supported surface.

**Fix:** added the `consensus-mcp-run-iteration` console script
(`consensus_mcp/_run_iteration_cli.py`), a thin wrapper around `handle()`. Output
contract (Q4, synthesis of all four reviewers): **always print** the structured
outcome to stdout (codex's no-extra-file batch path) **and write** `--outcome`,
defaulting to `{iteration-dir}/run-outcome.json` (gemini/grok/kimi) — the proven
field-shim contract. Threads `--host-peer-review-yaml` (grok). Doc:
`docs/operations/hosting-a-consult.md`. Tests: `tests/test_run_iteration_cli.py`.

---

## Gate UX friction (recommendations — NOT yet fixed)

These are real rough edges in the PreToolUse design gate, hit while running the
consult above from a Claude Code session (where the gate hook IS installed; the
field host's Codex environment had no such hook, so it never hit these).

### G1 — A STALE INSTALLED gate hook deadlocked the approval-minting CLI

Once `consensus-mcp-start-consult` **arms** the gate (writes the session marker),
the *installed* gate (`~/.claude/hooks/consensus_pretooluse_gate.py`) blocked
`consensus-mcp-approve` — the very command that mints `.consensus/design-approved`.

**Root cause (corrected):** NOT a current-source omission. The repo-source allowlist
`_CONSENSUS_TOOLING` already exempts `consensus-mcp-approve`, `-deliver`, and
`-start-consult` (and now `-run-iteration`). The **installed copy at `~/.claude` had
drifted to an older version** missing those entries, so it denied `approve` while
the source would have allowed it. The hook is a protected-install path the gate
refuses to let an in-session agent edit (correct, by design), and the env escape
hatch (`CONSENSUS_MCP_GATE_DISABLE=1`) is operator-launch-only — so an agent cannot
self-unblock against a stale installed hook.

**Workaround that works today:** the gate only intercepts `Bash` and `Edit` tools;
**MCP tool calls fall through to allow.** Use the `consensus.approve` MCP tool
(`mcp__consensus-mcp__consensus_approve`) instead of the `consensus-mcp-approve`
Bash CLI — it performs the identical validated mint and is immune to installed-hook
drift.

**Recommendations:**
- Refresh the installed hook to match source (`consensus-init --install-claude-code
  --force`, or reinstall) — installed-vs-source drift in a *protected* file silently
  changes enforcement and cannot be fixed in-session.
- Whenever a new console script is added to `pyproject.toml`, it must also be added
  to `_CONSENSUS_TOOLING` — the `test_governed_self_tooling_smoke` test enforces this
  (it caught `consensus-mcp-run-iteration` during this very change).

### G2 — Multi-segment / redirect commands are denied even when each part is read-only

The gate splits a Bash command on `; | && & newline` and requires **every** segment
to be allowlisted. Practical consequences while a consult is armed:
- A leading `cd <dir>` or a `VAR=... ` assignment makes the whole line denied (those
  tokens aren't allowlisted) — run sanctioned commands (dispatch, etc.) **standalone**,
  relying on the session cwd.
- Any redirection (`2>/dev/null`, `>`, `<`) or command substitution (`$(...)`,
  backticks, `${...}`) denies the segment — use the Read tool for files and drop
  `2>/dev/null`.

This is intended fail-safe behavior, not a bug, but it is a sharp edge worth knowing.
**Recommendation:** consider allowlisting `cd` and bare `VAR=value` assignments (they
are not writers), which would remove the most common false-deny.

### G3 — Approval scope is a single fnmatch glob; multi-root changes need phased mints

A change spanning `consensus_mcp/`, `docs/`, and `pyproject.toml` cannot be covered
by one non-overbroad `scope_glob`. Today that means minting the marker once per
scope phase (cheap via the MCP tool). **Recommendation:** consider supporting a list
of scope globs on a single approval marker, or a documented phased-mint pattern.
