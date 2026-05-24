# Design spec: `consensus init --repair` (verify/repair a partially-broken install)

- **Date:** 2026-05-23
- **Status:** Approved (4-AI consensus consult — unanimous on all anchors)
- **Iteration:** `consensus-state/active/iteration-verify-repair-design-2026-05-23`
- **Follows up:** the v1.29.0 "already-installed" consult Q3c (deferred verify/repair)
- **Consult provenance (sealed passes):** codex `codex-vr-1-pass1` (`f0fdd94d…`),
  gemini `gemini-vr-1-pass1` (`6acbf15a…`), kimi `kimi-vr-1-pass1` (`9a4aeec8…`),
  host-peer (blind Claude subagent). Archived under
  `consensus-state/archive/review-passes/2026-05-24-iteration-verify-repair-design-2026-05-23-*`.

## Problem

A common reason to re-run `consensus init` is a *partially-broken* install:
`.mcp.json` deleted, the `.gitignore` managed block dropped by a merge,
`.claude/agents/` files missing, or the enforcement hook in `settings.json`
dead. v1.29.0 gave the re-run a clean menu (leave / reconfigure / force) but
"leave" is a pure no-op (leaves it broken) and reconfigure/force re-run the
whole wizard. There is no "just make this install healthy again" path.

## Approach (converged, unanimous)

Add **`consensus init --repair`**: a deterministic, comprehensive **verify** of
the install plus **non-destructive repair** of what's safely fixable.

- **Q1a — repair MISSING, report DIVERGED.** Re-create absent pieces; for files
  that exist-but-diverged from shipped content, only REPORT them ("pass
  `--force` to overwrite") — never clobber user edits. Reuses the existing
  asset-write ethos ("missing => write; diverged => SKIP unless `--force`").
- **Q2a — comprehensive verify, project-scoped repair.** Check all 6 health
  components; REPAIR the 5 project-level ones; for global enforcement (#6)
  REPORT and point at `consensus-init --install-claude-code` (don't write
  `~/.claude` from a per-project command).
- **Q3a — both surfaces.** A `--repair` CLI flag AND a 4th option in the
  existing-config menu; the skill's AskUserQuestion menu gains the same option
  and re-invokes `--repair`.
- **Q4a — keep `--check` as-is** (config-only, back-compat; its 3 tests stay
  green). `--repair` is the new comprehensive verify+repair entry.
- **Q5a — deterministic.** `--repair` behaves the SAME in TTY and non-TTY (fix
  missing, report diverged, print summary, exit per taxonomy). It does NOT emit
  the `STATUS: already-configured` token (it is a resolving action).

## Health components & repair disposition

| # | Component | Detect via | Missing → | Diverged → | Repairable? |
|---|---|---|---|---|---|
| 1 | `.consensus/config.yaml` | `cfg.load` (exists + schema) | exit 2, report | exit 3, report | **No** (can't synthesize panel choices) |
| 2 | `.mcp.json` consensus-mcp entry | `_load_mcp_json` / `_mcp_entry_matches` | rewrite entry (`_write_mcp_json`) | report (`SKIP … --force`) | Yes (missing) |
| 3 | `.gitignore` managed block | bracketed managed markers | re-add block | report if malformed | Yes (missing) |
| 4 | `.claude/agents/` (`_PROJECT_AGENT_FILES`) | file present + matches shipped | re-copy file | report | Yes (missing) |
| 5 | per-AI instruction managed block | managed-block sentinels | re-add block | report | Yes (missing) |
| 6 | global enforcement (settings.json hooks + hook scripts present) | `claude_home` settings + script existence | **report only** → `--install-claude-code` | report only | **No** (global; out of project scope) |

**Critical:** detectors MUST reuse the *exact* sentinels/markers the writers use
(esp. the `.gitignore` bracketed-marker semantics — a known sharp edge). A
detector that doesn't match the writer's markers will spuriously recreate or
skip. `config.yaml` (#1) is the prerequisite: if it's missing/invalid, `#2–#5`
cannot be derived (repair needs a valid config to know contributors/workflow).

## Contract

### Exit-code taxonomy
- **`0`** — healthy OR fully repaired (do NOT distinguish "was already fine"
  from "I fixed it" via exit code — that distinction lives in the summary; this
  keeps the idempotency invariant clean).
- **`2`** — `config.yaml` missing (cannot repair → run `consensus init`). Mirrors
  `--check`.
- **`3`** — `config.yaml` invalid (cannot repair → run `consensus init
  --reconfigure`). Mirrors `--check`.
- **`7`** — repair INCOMPLETE: project repaired as far as safe, but unrepairable
  items remain — diverged managed files left for `--force`, AND/OR global
  enforcement (#6) detected dead. (Next free code after the install path's
  `5`=SKIP / `6`=incomplete; same two-tier philosophy.)

Dead global enforcement (#6) is the *most common* silent breakage that
project-scoped repair will NOT fix, so it must be **loud**: a prominent
`REPORT-GLOBAL:` line AND exit `7` (not a quiet `0`), so neither a human nor the
skill mistakes the install for fully healthy.

### Machine-readable summary (version-stable — the skill parses it)
One status line per component, stable prefixes the skill/CI can match without
scraping prose:
- `OK: <component>` — present and healthy.
- `REPAIRED: <component>` — was missing, recreated.
- `SKIP: <component> diverges from shipped content; pass --force` — exists but
  diverged; left intact (may be an intentional user edit — phrase so the user
  isn't alarmed into `--force` on a healthy custom block).
- `REPORT-GLOBAL: enforcement — run consensus-init --install-claude-code` —
  global #6 issue, not repaired here.

Treat this format as a contract (a regression test pins the prefixes), exactly
like the `STATUS: already-configured` token — changing it later is breaking.

### Gate carve-out (critical — host-peer)
`--repair` sets neither `--reconfigure` nor `--force`, so as written it would
trip the v1.29.0 existing-config gate and emit `STATUS: already-configured`
(exit 4) → the skill would mis-route it into the reconfigure menu. `--repair`
MUST be added to that gate's exemption (alongside `--reconfigure`/`--force`).
**Regression test required.**

### `--dry-run` composition
`--repair --dry-run` previews what WOULD be repaired/skipped/reported, writes
nothing, and returns the exit code it *would* return (so CI can gate on it).

### Idempotency
A second `--repair` immediately after a successful first run = all `OK:`, zero
writes, exit `0`. **Explicit regression test.**

## Surfaces

- **CLI:** `consensus init --repair` AND `consensus-init --repair` (both entry
  points — they share `_init_wizard:main`).
- **TTY menu:** the existing-config menu gains a 4th option →
  `[1]` leave / `[2]` verify/repair / `[3]` reconfigure / `[4]` force. Choosing
  `[2]` runs the repair path then returns its exit code.
- **Skill / command:** the `consensus` skill + `consensus-init` command
  AskUserQuestion menu gains a "Verify / repair" option that re-invokes
  `consensus-init --from-claude-code --repair` **one-shot** (no loop — `--repair`
  is a resolving flag, like reconfigure/force).

## Testing plan (TDD)

1. **Per-component detection** (all 6): healthy → `OK`; missing → `REPAIRED` +
   file actually written; diverged → `SKIP …--force` + file untouched. Use the
   real markers/sentinels (not approximations).
2. **config gates:** missing config → exit 2 + report, no writes; invalid config
   → exit 3 + report, no writes; valid config + missing `#2–#5` → repaired, exit
   0 (happy path).
3. **Exit `7`:** a diverged managed file (left for `--force`) → exit 7; dead
   global enforcement (mock `claude_home`) → exit 7 + `REPORT-GLOBAL` line.
4. **Gate carve-out:** `--repair` on an existing config does NOT emit
   `STATUS: already-configured` and does NOT return 4 (regression test).
5. **Idempotency:** two consecutive `--repair` runs — 2nd is all `OK`, no writes,
   exit 0.
6. **`--dry-run`:** `--repair --dry-run` writes nothing, reports the would-be
   actions + would-be exit code.
7. **Menu:** the existing-config menu now offers 4 options; assert the TTY menu
   text AND the skill AskUserQuestion labels so the two surfaces don't drift.
   Do NOT regress the non-TTY `STATUS: already-configured` 3-path contract.
8. **Summary contract:** a regression test pins the `OK:/REPAIRED:/SKIP:/
   REPORT-GLOBAL:` prefixes (skill depends on them).
9. **`--check` untouched:** its 3 existing tests still pass.
10. Full suite green before any release.

## Out of scope (explicit)

- **Repairing global enforcement** (`~/.claude` settings/hooks) — report + redirect
  to `--install-claude-code` only.
- **Synthesizing a missing/invalid `config.yaml`** — report + redirect to
  `consensus init` / `--reconfigure`; never auto-launch the wizard (would break
  `--repair`'s determinism).
- A separate `--verify` report-only alias — YAGNI; `--repair --dry-run` covers
  preview. (Revisit only if demand appears.)

## Risks (from the panel)

1. **Gate-interaction bug** (most likely defect) — mitigated by the gate
   carve-out + its regression test.
2. **Dead global enforcement reported quietly** — mitigated by the loud
   `REPORT-GLOBAL` line + exit `7`.
3. **Detector/marker mismatch** — mitigated by reusing the writers' exact
   sentinels and testing each component against real fixtures.
4. **Exit-code taxonomy creep** — mitigated by NOT splitting healthy/repaired and
   reusing `--check`'s `2/3` + one new `7`.
