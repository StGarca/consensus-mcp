# claude proposal — init contributor-selection supplemental-review spec

- **contributor:** claude (host / orchestrator)
- **reviewer_id:** claude-init-supp-1
- **mode:** proposal (Workflow A round 1, blind — authored before reading codex/gemini)

## Prior / differential I reason from

I authored the seed spec, so I carry an **anchoring/confirmation risk** toward
"sound as-is." To counter it I traced every code path that sets or validates
`contributors.enabled` rather than re-reading my own prose. That trace changed my
verdict: the spec's "verify config.py" line understates a load-bearing change.

## selected_target: **spec-needs-changes**

The spec's CORE is sound and should ship as designed: exclude `host_peer` from
the main multi-select; make the same-model reviewer a conditional opt-in
follow-up (default No); the 0.5 is presentational; the gate is untouched
(`host_peer` stays `gate_eligible=false`). But the floor-enforcement section is
under-specified in a way that will ship a real hole. Required changes below.

## Answers to the 6 central questions

### 1. Floor-enforcement completeness — INCOMPLETE as written
The authoritative minimum is **`config.py`**, not the wizard:
- `config.py:405` `n_contributors = len(enabled)` — counts **all** enabled,
  including a `host_peer`.
- `config.py:408` propose-converge requires `n_contributors >= 2`.
- `config.py:418` autonomous-execute requires `n_contributors == 3`.
- `config.py:454/461` strict-majority and sequential also key off
  `n_contributors`.

So today `enabled: [claude, claude-swe-reviewer]` PASSES the ≥2 gate — one model
reviewing itself masquerades as 2. The fix is NOT only in the wizard:
- Add a single **independent-count** helper that resolves each enabled name to
  its `kind` (via merged built-in + overlay profiles) and counts
  `kind != host_peer`. Reuse it in `config.py` (the `n_contributors` checks),
  `_validate_contributor_selection` (the `--contributors` flag path), and the
  reconfigure path.
- **Decide explicitly** (the spec is silent): does `host_peer` also drop out of
  the autonomous-execute `== 3` and strict-majority/sequential counts?
  Recommended: **yes** — every one of those gates means *independent* reviewers,
  so `host_peer` must be excluded consistently, or the "0.5 ≠ a vote" doctrine
  leaks. This means `config.py` needs both a raw count and an independent count.

### 2. Backward-compat — needs an explicit rule
A v1.18.0 config may already list `claude-swe-reviewer` in `enabled`. The
reconfigure path (`interactive_overrides:700-705`) seeds the default from the
existing list and takes free-text. The redesign must:
- **Preserve** an already-enabled `host_peer` (do NOT silently strip it on
  reconfigure), AND
- enforce the independent floor (≥2 non-host_peer) so a legacy
  `[claude, claude-swe-reviewer]` is flagged on the next `init`/validate.
Runtime is already safe: `_engine_factory` builds `host_peer` only when the
callback is wired, else skips gracefully — so no migration is needed, only the
validation tightening + preservation rule.

### 3. Conditional follow-up trigger — sound, with refinements
Trigger = claude selected AND a host-family `host_peer` profile exists. Map
host→host_peer by the profile `family` field (v1.20.0), which generalizes to
future host_peers. Refinements: (a) if claude is selected but NO matching
`host_peer` profile exists, **skip silently** (no error); (b) offer it even when
`host_peer_review_callback` is not yet wired at runtime (it's a config choice;
runtime is gracefully absent) — optionally add one line that it only runs when
the host wires the callback.

### 4. Default-state correctness — achieved, one path to confirm
`_profile_installed` returns True for `host_peer` (pre-checks it today), but the
redesign removes `host_peer` from the main list entirely, so it can't be
pre-checked there, and the follow-up defaults to No. The non-interactive default
`_detect_available_contributors` (line 332) already excludes `host_peer`
(returns only claude/codex/gemini) — so default-off holds on every path. **Note
(tangential):** that same function omits **kimi**, so the non-interactive
`consensus init` default can never pick kimi even when installed. Out of scope
for this spec but worth a follow-up.

### 5. Messaging accuracy — accurate; keep the "ideas still used" clause
"Supplemental, 0.5, not fully independent, no gate vote, can't close consensus
(claude already votes as host), but every good idea is still applied on merit"
matches v1.20.0 + the weighted-synthesis doctrine. The last clause is the
important one — without it "no vote" reads as "ignored." Keep it verbatim.

### 6. Test coverage — add these
- main list EXCLUDES `host_peer` (assert displayed options).
- follow-up appears iff claude selected; absent when claude not selected.
- follow-up default-No (empty input) → `host_peer` NOT appended; yes → appended.
- `--contributors claude,claude-swe-reviewer` → REJECTED (1 independent);
  `claude,codex,claude-swe-reviewer` → ACCEPTED (2 independent + 0.5).
- `config.py` independent-count: `[claude, claude-swe-reviewer]` fails ≥2;
  `[claude, codex, claude-swe-reviewer]` passes.
- autonomous-execute count semantics with a `host_peer` present (per the
  decision in Q1).
- reconfigure preserves an existing `host_peer`.
- summary line shows the weighted "N.5" count.

## Net
Ship the core design unchanged; expand the spec's enforcement section to
(a) centralize an independent-count helper, (b) apply it in `config.py` (not just
the wizard) including a decision on autonomous/strict-majority/sequential, and
(c) add the reconfigure preservation rule + the test matrix above.
