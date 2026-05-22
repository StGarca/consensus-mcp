# Design: first-init contributor selection — independent panel + supplemental same-model review

- **Date:** 2026-05-22
- **Status:** Draft (brainstorming output; Workflow A consult decision pending)
- **Scope:** `consensus_mcp/_init_wizard.py` (+ tests). NO engine / gate / convergence changes.

## Problem

The `consensus init` multi-select (v1.18.0) lists **all** merged profiles in one
flat list — `claude`, `claude-swe-reviewer` (a v1.20.0 `host_peer`), `codex`,
`gemini`, `kimi`. Consequences:

- The same-model review agent (`claude-swe-reviewer`) appears as a flat sibling
  of `claude`, which is confusing.
- `host_peer` reports as "always installed" (`_profile_installed` returns True
  for `kind: host_peer`), so it is pre-checked and **default-on** — contradicting
  its supplemental nature.
- The `>=2` minimum counts a `host_peer` like any contributor, so
  `claude + claude-swe-reviewer` (one model reviewing its own work) can satisfy
  "2 contributors."

Expected first-init experience: offer the in-house independent AIs (claude,
codex, gemini, kimi), ask which to use, and — only when the host AI (claude) is
chosen — ask whether to add a same-model **supplemental** reviewer.

## Decisions (from brainstorming)

1. **Host scope: claude-only (now).** The host is the orchestrator AI; today only
   claude can be `kind: host`. Reuse the existing `claude-swe-reviewer` profile
   and the already-wired host callback. Generic (codex/gemini/kimi) host +
   per-AI swe-reviewer profiles are future work.
2. **Supplemental reviewer is a conditional follow-up, opt-in (default No)** — not
   a flat sibling in the main list.
3. **0.5 weight is presentational only.** Independent AI = 1.0; host_peer = 0.5.
   Used for the init-minimum framing and the human-facing "N.5 reviewers"
   summary. The consensus GATE is untouched.
4. **The floor is ">=2 independent."** The minimum is
   `count(enabled where kind != host_peer) >= 2`. A host_peer never counts toward
   the floor, so `claude + claude-swe-reviewer` is rejected.
5. **Gate behavior unchanged.** host_peer stays `gate_eligible=false`,
   `weight=supplementary`, excluded from the cross-family closure invariant
   (v1.20.0). It gets **no gate vote** and **cannot close consensus** — claude
   already votes as host.
6. **Messaging.** The end user must understand the same-model reviewer is a
   SUPPLEMENTAL review with no gate vote — BUT every good idea it raises is still
   applied on merit (weighted-synthesis doctrine). "No vote" must not read as
   "ignored."

## Behavior

### Main selection — independent AIs only

List only `kind: host` / `kind: cli_reviewer` profiles (claude, codex, gemini,
kimi + any operator overlay of those kinds). Exclude `kind: host_peer`.
Pre-check installed entries; require `>=2` independent.

```
Select the AI reviewers to use (>=2 required):
  [x] 1. claude  (✓ installed)
  [x] 2. codex   (✓ installed)
  [ ] 3. gemini  (✗ missing)
  [x] 4. kimi    (✓ installed)
Enter comma-separated numbers [default: 1,2,4]:
```

### Conditional supplemental follow-up

Shown only if claude is in the selection AND a host-family `host_peer` profile
exists:

```
You're using claude as the host. Add a same-model claude review agent?

This is a SUPPLEMENTAL review (counts as 0.5 — NOT a fully independent
reviewer; it shares the host model's blind spots). It gets no vote at the
consensus gate and can't close consensus (claude already votes as host) —
but every good idea it raises is still applied on merit. A useful extra
pass if you have the tokens to spare.

Add it? [y/N]:
```

Default **No**. On yes → append the host-family `host_peer` profile name
(`claude-swe-reviewer`) to `contributors.enabled`.

### Confirmation summary

Surface the weighted count so the supplemental status is explicit:

```
Panel: 2.5 reviewers — 2 independent (claude, codex)
       + 0.5 supplemental same-model (claude-swe-reviewer).
```

If no host_peer chosen: `Panel: 2 independent reviewers (claude, codex).`

## Enforcement points (all entry paths)

The ">=2 independent" floor must hold in every path that sets
`contributors.enabled`:

- interactive multi-select (`_select_contributors_interactive` /
  `_validate_contributor_selection`)
- `--contributors` flag (explicit path)
- reconfigure path (existing-config edit)
- any minimum independently enforced in `config.py` (verify during planning)

Rule: `len([c for c in enabled if profile_kind(c) != host_peer]) >= 2`.

## Out of scope

- Generic (non-claude) host + per-AI swe-reviewer profiles.
- Any change to convergence / quorum / closure / gate math.
- Authoring new `host_peer` profiles.

## Test impact

- `test_init_wizard.py`, `test_init_wizard_contributors.py`: main list excludes
  host_peer; conditional follow-up; default-No; weighted summary line.
- `test_n_contributor_acceptance.py`: floor counts independents only;
  `claude + claude-swe-reviewer` rejected.
- New cases: host_peer appended on yes; NOT appended on No/empty input; the
  `--contributors` flag rejects a selection that uses a host_peer to pad below
  2 independents.
