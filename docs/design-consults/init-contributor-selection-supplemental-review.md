# Design: first-init contributor selection - independent panel + supplemental same-model review

- **Date:** 2026-05-22
- **Status:** Draft (brainstorming output; Workflow A consult decision pending)
- **Scope:** `consensus_mcp/_init_wizard.py` (+ tests). NO engine / gate / convergence changes.

## Problem

The `consensus init` multi-select (v1.18.0) lists **all** merged profiles in one
flat list - `claude`, `claude-swe-reviewer` (a v1.20.0 `host_peer`), `codex`,
`gemini`, `kimi`. Consequences:

- The same-model review agent (`claude-swe-reviewer`) appears as a flat sibling
  of `claude`, which is confusing.
- `host_peer` reports as "always installed" (`_profile_installed` returns True
  for `kind: host_peer`), so it is pre-checked and **default-on** - contradicting
  its supplemental nature.
- The `>=2` minimum counts a `host_peer` like any contributor, so
  `claude + claude-swe-reviewer` (one model reviewing its own work) can satisfy
  "2 contributors."

Expected first-init experience: offer the in-house independent AIs (claude,
codex, gemini, kimi), ask which to use, and - only when the host AI (claude) is
chosen - ask whether to add a same-model **supplemental** reviewer.

## Decisions (from brainstorming)

1. **Host scope: claude-only (now).** The host is the orchestrator AI; today only
   claude can be `kind: host`. Reuse the existing `claude-swe-reviewer` profile
   and the already-wired host callback. Generic (codex/gemini/kimi) host +
   per-AI swe-reviewer profiles are future work.
2. **Supplemental reviewer is a conditional follow-up, opt-in (default No)** - not
   a flat sibling in the main list.
3. **0.5 weight is presentational only.** Independent AI = 1.0; host_peer = 0.5.
   Used for the init-minimum framing and the human-facing "N.5 reviewers"
   summary. The consensus GATE is untouched.
4. **The floor is ">=2 independent."** The minimum is
   `count(enabled where kind != host_peer) >= 2`. A host_peer never counts toward
   the floor, so `claude + claude-swe-reviewer` is rejected.
5. **Gate behavior unchanged.** host_peer stays `gate_eligible=false`,
   `weight=supplementary`, excluded from the cross-family closure invariant
   (v1.20.0). It gets **no gate vote** and **cannot close consensus** - claude
   already votes as host.
6. **Messaging.** The end user must understand the same-model reviewer is a
   SUPPLEMENTAL review with no gate vote - BUT every good idea it raises is still
   applied on merit (weighted-synthesis doctrine). "No vote" must not read as
   "ignored."
7. **Dynamic detection - NO hardcoded AI lists (operator, 2026-05-22).** The
   product promise is "add any AI with no code / any number of AIs." Therefore
   every path that enumerates or detects contributors MUST derive from the merged
   profile set (built-in profiles + `contributors.profiles` overlay), never a
   name literal. Concretely: `_detect_available_contributors` (today hardcoded to
   `["claude","codex","gemini"]`, omitting kimi and every operator-added AI) is
   replaced by "iterate merged profiles, keep the installed independents
   (`kind != host_peer` AND `_profile_installed`)". The static
   `config.py default_config()` enabled list must likewise be derived from the
   built-in independent profiles, not a literal - so dropping in a new built-in
   profile extends the default automatically. Adding an AI is a profile drop-in
   with zero code edits anywhere in the wizard.

## Behavior

### Main selection - independent AIs only

List only `kind: host` / `kind: cli_reviewer` profiles (claude, codex, gemini,
kimi + any operator overlay of those kinds). Exclude `kind: host_peer`.
Pre-check installed entries; require `>=2` independent.

```
Select the AI reviewers to use (>=2 required):
  [x] 1. claude  ([ok] installed)
  [x] 2. codex   ([ok] installed)
  [ ] 3. gemini  ([x] missing)
  [x] 4. kimi    ([ok] installed)
Enter comma-separated numbers [default: 1,2,4]:
```

### Conditional supplemental follow-up

Shown only if claude is in the selection AND a host-family `host_peer` profile
exists:

```
You're using claude as the host. Add a same-model claude review agent?

This is a SUPPLEMENTAL review (shown as +0.5 in the init summary only - NOT a
fully independent reviewer; it shares the host model's blind spots). It gets no
vote at the consensus gate and can't close consensus (claude already votes as
host) - but every good idea it raises is still applied on merit. A useful extra
pass if you have the tokens to spare.

Add it? [y/N]:
```

Default **No**. On yes -> append the host-family `host_peer` profile name
(`claude-swe-reviewer`) to `contributors.enabled`.

**Multiple host_peer (D8, future-proofing).** `family` is a *filter*, not a
unique selector. The current single-profile case appends `claude-swe-reviewer`.
If multiple same-family `host_peer` profiles ever exist, present a supplemental
mini-select (default none) or fail closed with an ambiguity message - NEVER
silently append the first sorted profile.

### Confirmation summary

Surface the weighted count so the supplemental status is explicit:

```
Panel: 2.5 reviewers - 2 independent (claude, codex)
       + 0.5 supplemental same-model (claude-swe-reviewer).
```

If no host_peer chosen: `Panel: 2 independent reviewers (claude, codex).`

## Enforcement points (all entry paths)

**The authoritative floor is `config.py`, not the wizard.** Verified
(`config.py:405-418`): `n_contributors = len(enabled)` counts EVERYTHING,
including a `host_peer` - so `[claude, claude-swe-reviewer]` passes the >=2
propose-converge gate today (one model reviewing itself). The same raw count
drives autonomous-execute `== 3` (line 418) and strict-majority / sequential
(454/461).

Implement a single **independent-count helper** that resolves each enabled name
to its `kind` (via merged built-in + overlay profiles) and counts
`kind != host_peer`. Keep it small; **do NOT import wizard code into `config.py`**
(coupling risk - codex).

- **`config.py` validators are the authoritative gate (`cfg.validate`)** - this
  is the one place flags, `--check`, `--accept-defaults`, reconfigure, and
  non-interactive default-derivation all funnel through. `host_peer` is excluded
  from the independent count for propose-converge (>=2) AND autonomous-execute
  (which means 3 *independent* reviewers) AND strict-majority / sequential.
  `config.py` tracks both a raw count (uniqueness/display) and an independent
  count (policy gates).
- **Open-contributor preservation (D3).** At the config layer, a name with NO
  resolvable profile is NOT rejected by the floor - it counts as *independent*
  unless it resolves to `host_peer`. Constructibility stays `engine_factory`'s
  fail-closed job. This protects config.py's open "any AI" model. The
  wizard/flag layer MAY still reject unknown names (it holds the explicit merged
  profile set).
- **Orphan-host_peer rejection (D4).** A `host_peer` may be enabled ONLY if its
  host (matched by the profile `family` field) is also enabled, on every path.
  So `--contributors codex,gemini,claude-swe-reviewer` is rejected (orphan);
  `claude,codex,claude-swe-reviewer` is valid; `claude,claude-swe-reviewer` is
  rejected (1 independent < 2).
- interactive multi-select (`_select_contributors_interactive`) - list excludes
  host_peer, so its min re-prompt is naturally over independents.
- reconfigure path (`interactive_overrides:700-705`) - **preserve** an
  already-enabled `host_peer` from a v1.18.0 config (do NOT silently drop): split
  existing `enabled` into independent defaults + a diff-visible keep/drop prompt
  for the supplemental **defaulting to the current state** (Yes if it was on),
  preserved only when independent_count >= 2 AND the matching host family is
  selected. If the legacy config is now invalid (e.g. `[claude,
  claude-swe-reviewer]`), guide the user to add an independent contributor with a
  clear ">=2 independent" message rather than erroring opaquely.

Rule: `independent_count(enabled) = len([c for c in enabled if resolved_kind(c) != host_peer]) >= 2`.

## Dynamic detection (decision 7) - implementation

- Replace `_detect_available_contributors` (line 332, hardcoded
  `["claude","codex","gemini"]`) with: load merged profiles -> return names where
  `kind != host_peer` AND `_profile_installed(profile)`. This auto-includes kimi
  and any operator-added profile.
- Derive `config.py default_config()` `enabled` from the built-in independent
  profile names (not the `["claude","codex","gemini"]` literal at line 174 / the
  `["claude","codex"]` literal at 577).
- Audit for any other name-literal AI list and route it through the profile set.

## Out of scope

- Generic (non-claude) host + per-AI swe-reviewer profiles.
- Any change to convergence / quorum / closure / gate math.
- Authoring new `host_peer` profiles.

## Test impact

- `test_init_wizard.py`, `test_init_wizard_contributors.py`: main list excludes
  host_peer; conditional follow-up appears iff claude selected; default-No;
  weighted summary line.
- `test_n_contributor_acceptance.py` + `config.py` tests: independent count
  excludes host_peer for propose-converge AND autonomous-execute;
  `[claude, claude-swe-reviewer]` rejected; `[claude, codex, claude-swe-reviewer]`
  accepted.
- New cases: host_peer appended on yes; NOT appended on No/empty input; the
  `--contributors` flag rejects a host_peer used to pad below 2 independents;
  reconfigure preserves an existing host_peer via the current-state path.
- Orphan rejection: `--contributors codex,gemini,claude-swe-reviewer` fails (host
  not enabled); `claude,codex,claude-swe-reviewer` passes; explicit
  `--contributors claude,codex` does NOT trigger the supplemental prompt.
- Open-contributor: a config-overlay `host_peer` is excluded from
  independent_count; an unknown contributor with no profile is NOT rejected at
  the config layer merely for lacking a profile.
- `--check` / `--accept-defaults` / non-interactive defaults all enforce the
  independent floor and exclude host_peer.
- Dynamic detection: with a fake profile set (e.g. kimi + a custom AI on PATH),
  `_detect_available_contributors` returns them and excludes host_peer;
  `default_config()` ordering is stable + pinned; no AI-name literal remains in
  the detection/default paths.
- Determinism: multiple same-family host_peers resolve via mini-select/fail-closed
  (never silently first).
- No test or code changes engine gate math; host_peer stays no-gate-vote.
