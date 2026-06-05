# Gate UX friction G2 + G3 — consult problem statement

Status: OPEN — bound to a 4-contributor, open-contest, propose-converge
(Workflow A) consult. Tier: **deep, locked** (touches governance machinery — the
PreToolUse design gate and the design-approval marker — so the monotone
governance safety floor applies and forbids downgrade).

This document is a **problem statement**, not a design. It states the two
forks, the hard constraints every proposal must respect, and the acceptance
gates a converged design must satisfy. Each contributor proposes independently;
convergence is by weighted-synthesis.

Source: `docs/consensus/field-notes-and-recommendations.md` (G2, G3), surfaced
by a 2026-06-04 Codex-hosted consult in a consuming project. G1 and F1–F3 are
already shipped (commit 93c1344); this consult covers ONLY the two remaining,
deliberately-deferred gate recommendations.

---

## Background — the two primitives in play

**The PreToolUse design gate** (`consensus_mcp/claude_extensions/hooks/
consensus_pretooluse_gate.py`) is a DEFAULT-DENY allowlist over `Bash` and
`Edit`. For Bash it splits the command quote-aware on `| || && & ; newline`
(`_split_segments`) and allows the line iff EVERY segment is read-only
(`_segment_is_read_only`: leading token in `_READ_ONLY_COMMANDS` /
`_CONSENSUS_TOOLING`, with redirects `> <`, command substitution `$( ` `` ` ``
`${`, and subshell parens pre-rejected) OR a tight-scope sealed marker is in
force.

**The design-approval marker** (`consensus_mcp/_design_approval.py`) is a sealed
pointer carrying a single `scope_glob` string. `verify_design_approval` does one
`fnmatch(rel, scope_glob)` to authorize an `Edit`; `mint_design_approval` and
`marker_is_sealed` reject an overbroad glob (`{"*","**","**/*","*/*"}`).

---

## G2 — multi-segment / prefix denials on read-only commands

**Observed friction.** A leading `cd <dir>` or a bare `VAR=value ` assignment
prefix makes an otherwise read-only line denied, because that leading token is
not on the allowlist. Operators must run sanctioned commands standalone relying
on session cwd. The field note recommends *"consider allowlisting `cd` and bare
`VAR=value` assignments (they are not writers)."*

**The non-obvious risk this consult must resolve.** `cd` is genuinely
state-only (no FS mutation; a writer chained after it rides a separate,
still-denied segment). But a bare `VAR=value` prefix is **not** uniformly safe:
assignments such as `LD_PRELOAD=`, `LD_LIBRARY_PATH=`, `PATH=`, `BASH_ENV=`,
`ENV=`, `IFS=`, `SHELLOPTS=`/`BASHOPTS=`, `GIT_SSH_COMMAND=`,
`GIT_EXTERNAL_DIFF=`, `GIT_PAGER=`, `PERL5OPT=`, `PYTHONSTARTUP=` turn an
otherwise-allowlisted command into arbitrary code execution. "Allowlist bare
assignments" is therefore a real security fork, not a pure convenience.

### Open questions (G2)

- **Q1 (cd):** Allowlist `cd <dir>` (and `pushd`/`popd`?) as a read-only leading
  token? State exactly which forms stay denied (the existing redirect / `$()` /
  subshell pre-rejection already covers `cd $(...)`).
- **Q2 (assignments):** Allow a bare `VAR=value` prefix? If yes, choose the
  posture: (a) reject all assignments — status quo, safest; (b) strip leading
  assignments then require the trailing command to be allowlisted AND denylist
  the exec-affecting variable names above; (c) some other confinement. A bare
  assignment only helps when the trailing command is itself allowlisted, so any
  "allow" answer must still evaluate that trailing command.
- **Q3 (surface):** Does any relaxation apply to the always-on read-only path
  (every governed session, pre-consult — higher stakes) or only when a
  tight-scope marker is already in force? Justify the chosen surface.

---

## G3 — single-glob approval scope cannot cover multi-root changes

**Observed friction.** A change spanning `consensus_mcp/`, `docs/`, and
`pyproject.toml` cannot be covered by one non-overbroad `scope_glob`. Today that
means minting the marker once per scope phase. The field note recommends
*"consider supporting a list of scope globs on a single approval marker, or a
documented phased-mint pattern."*

### Open questions (G3)

- **Q4 (model):** Extend the marker to a LIST of globs (target matches if it
  fnmatches ANY) vs. keep the single glob and document a phased-mint pattern as
  the supported answer. Weigh added schema/verifier surface against operator
  ergonomics.
- **Q5 (if list — safety):** Backward compatibility (schema_version bump;
  continue accepting a legacy single `scope_glob`); overbroad-rejection applies
  to EACH glob; `marker_is_sealed` (Bash authorization) requires EVERY glob
  tight; and a bound on list length / breadth so "a list of narrow globs" cannot
  reconstitute an effective `**`. Specify the anti-bypass rule precisely.
- **Q6 (mint surface):** How `consensus-mcp-approve` / `consensus.approve`
  accept multiple globs (repeated flag vs comma-list), keeping the single-glob
  call byte-compatible.

---

## Hard constraints (every proposal MUST respect)

1. **DEFAULT-DENY is preserved.** No relaxation may allow an unknown or writing
   command. The prime directive (fail-open only on unreadable/foreign events,
   fail-safe on unknown commands) is unchanged.
2. **The protected-install tamper guard is untouched.** Writes to
   `~/.claude/settings.json` / `~/.claude/hooks/consensus_*.py` stay refused.
3. **Writer-rejection survives.** `cd /x && rm -rf y`, `FOO=bar rm x`,
   `LD_PRELOAD=evil.so cat f`, redirects, `$()`, subshells — all stay DENIED.
4. **No new overbroad-scope bypass.** A multi-glob marker (if adopted) cannot
   authorize a Bash command or an Edit it could not authorize as separate
   tight single-glob mints.
5. **Backward compatibility.** Existing single-`scope_glob` markers and the
   existing `consensus-mcp-approve` call must keep working byte-identically.
6. **Independent safeguard (governance-tier requirement).** Ship adversarial
   tests that still pass even if the relaxation's rationale were wrong — i.e.
   tests asserting each bypass attempt in constraint 3/4 stays denied. These
   must be valuable regardless of the chosen posture.

## Acceptance gates (a converged design is DONE when)

- **A1:** Q1–Q3 resolved with an explicit, testable rule for `cd` and for
  assignments, naming the surface (always-on vs marker-gated).
- **A2:** Q4 resolved; if list adopted, Q5/Q6 fully specified (schema bump,
  per-glob overbroad check, all-tight rule for Bash auth, length/breadth bound,
  mint CLI shape).
- **A3:** A test matrix enumerated covering constraint 3 (each bypass string)
  and constraint 4 (no multi-glob over-authorization), plus the convenience
  cases that should now ALLOW (`cd dir && consensus-mcp-dispatch-codex ...` if
  Q1/Q2 allow it; a 3-root change under one multi-glob marker if Q4 allows it).
- **A4:** Backward-compat test: a legacy single-`scope_glob` marker and the
  existing approve call behave identically.
- **A5:** Implementation cost fits one session (per the completion test); any
  genuinely-deferred sub-item names its specific blocker, not "complexity."

## Out of scope

- G1 (stale installed-hook drift) — operator/process, already addressed in 93c1344.
- F1–F3 — already shipped and tested.
- Any change to the convergence engine, dispatch adapters, or delivery-token
  model beyond what G3's marker schema strictly requires.
