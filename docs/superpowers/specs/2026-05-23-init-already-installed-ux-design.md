# Design spec: graceful "already installed" handling for `consensus init`

- **Date:** 2026-05-23
- **Status:** Approved (operator + 4-AI consensus consult)
- **Iteration:** `consensus-state/active/iteration-init-already-installed-ux-2026-05-23`
- **Consult provenance (sealed passes):**
  - codex - `codex-init-ux-1-pass1` (packet `34864b60...`)
  - gemini - `gemini-init-ux-1-pass1` (packet `014ad469...`)
  - kimi - `kimi-init-ux-1-pass1` (packet `4fad0d26...`)
  - host-peer - blind Claude subagent (in-session)
  - Archived under `consensus-state/archive/review-passes/2026-05-24-iteration-init-already-installed-ux-2026-05-23-*`

## Problem

Re-running `consensus init` (or `consensus-init --from-claude-code`) in a
project that is already bootstrapped hard-errors:

```
error: <path>/.consensus/config.yaml already exists. Use --reconfigure to
update or --force to overwrite.
```

with exit code 4. Under Claude Code this is bad UX: the binary's stdin is not
a TTY (hardened in v1.28.1), so the raw "Error: Exit code 4" surfaces and
Claude is left to improvise an explanation and ask the user what to do.

Operator directive: *"autodetect if an install exists and PROMPT the user
options, not error out and have claude take over."*

## Constraint (load-bearing)

`consensus-init --from-claude-code` runs under Claude Code's Bash tool - **no
TTY**. v1.28.1 added `_stdin_is_interactive()` and falls back to
non-interactive defaults when stdin isn't a TTY. So the binary cannot pop an
interactive menu in the Claude Code path; a non-TTY "prompt" must be surfaced
by the **skill**.

## Approach (operator-approved)

**Approach A - skill-owned menu under Claude Code; binary-owned menu in a real
terminal. Applies to BOTH paths.**

- **Binary** (`_init_wizard.py`): replace the unconditional `return 4` hard
  error with an install-aware branch.
  - **TTY** -> interactive menu: `[1]` leave as-is (default) / `[2]`
    reconfigure / `[3]` force overwrite.
  - **Non-TTY** -> cannot prompt; emit a clean, machine-detectable
    "already configured" status the skill keys on.
- **Skill** (`skills/consensus/SKILL.md`): one carve-out from the
  "surface verbatim" rule - on the already-configured signal, present the three
  options via `AskUserQuestion`, then re-invoke with the chosen flag.

## Converged sub-decisions (4-AI consult, weighted-synthesis)

| Question | codex | gemini | kimi | host-peer | Converged |
| --- | --- | --- | --- | --- | --- |
| Q1 - non-TTY/CI exit code | a | a | a | a | **Q1a - keep exit 4** |
| Q2 - detection contract | a | a | a | a | **Q2a - exit 4 + `STATUS:` token** |
| Q3 - "leave as-is" | c | a | c | c | **Q3c - pure no-op + follow-up note** |

**Q1a - keep exit 4.** Back-compat is load-bearing: four existing tests and the
skill key on `rc == 4` == "already exists". Exit 0 (Q1b) would blur true
success vs. an unresolved operator choice and break the very contract Q2
hardens. We keep exit 4 and only make the message actionable.

**Q2a - exit 4 + stable token.** A coarse exit code plus a greppable
first-line token gives the skill a narrow, prose-independent contract, cheaper
than a `--json` mode (Q2c) and less brittle than exit-code-only (Q2b). Adopted
with the host-peer/kimi channel constraint (see contract below).

**Q3c - pure no-op now + written follow-up.** Runtime "leave" stays a pure
no-op (Gemini's concern - don't bundle verify/repair - is honored). A
CHANGELOG/ADR note records the known gap (a "leave" on a half-broken install
leaves the user in a broken-but-"already-configured" state) and names
verify/repair as a future *explicit* menu option. This preserves Gemini's
good idea instead of discarding it.

## The binary<->skill contract (precise)

When `config_path.exists()` and **neither** `--reconfigure` **nor** `--force`
is present:

- **Non-interactive (no TTY: `--from-claude-code`, CI, pipe - OR explicit
  `--non-interactive`/`--accept-defaults`):**
  - **stdout, first line, exactly:** `STATUS: already-configured`
    - The token is a fixed string, on its own line, with **no variable data**
      (no paths) so a skill matcher can anchor on it safely.
  - **stderr:** the human-readable guidance (what it means; the three options;
    how to pick via flags or a real terminal).
  - **exit code:** `4`.
  - The token is emitted **only** in this state. When a resolving flag
    (`--reconfigure`/`--force`) is present, the binary proceeds normally and
    **does not** emit the token (infinite-loop guard).
- **Interactive (TTY):** present the menu; do **not** emit the token; act on
  the choice (no-op / `--reconfigure` path / `--force` path).

**Skill behavior (`skills/consensus/SKILL.md` + `commands/consensus-init.md`):**
the existing "surface stdout/stderr verbatim" rule gets ONE carve-out - if the
binary exits `4` **and** stdout's first line is `STATUS: already-configured`:
1. The skill **consumes** the token line and surfaces only the remaining prose
   (no machine cruft shown to the user).
2. It presents the three options via `AskUserQuestion`:
   - **Leave as-is** -> do nothing (no re-invoke).
   - **Reconfigure** -> re-invoke `consensus-init --from-claude-code --reconfigure`.
   - **Force overwrite** -> re-invoke `consensus-init --from-claude-code --force`.
3. The re-invoke is **one-shot** - the resolving flag prevents the token from
   firing again, so there is no menu loop.

Everything else still surfaces verbatim.

## Components & files in scope

- `consensus_mcp/_init_wizard.py` - install-aware branch (see Ordering, below).
- `consensus_mcp/claude_extensions/skills/consensus/SKILL.md` - the carve-out.
- `consensus_mcp/claude_extensions/commands/consensus-init.md` - mirror the
  carve-out (same verbatim rule lives here).
- `consensus_mcp/tests/test_init_wizard.py` - update the 4+ tests that assert
  the old `rc == 4` + `"already exists"` message.
- `consensus_mcp/tests/test_init_wizard_contract.py` (new) - the mandatory
  contract regression test.
- `CHANGELOG.md` - the Q3c follow-up note (verify/repair as future work).

**Generated, do NOT hand-edit:** `build/lib/consensus_mcp/...` (build artifact;
regenerated by the build).

## Critical implementation note - ordering (host-peer finding)

The existing-config guard at `_init_wizard.py:1733` currently runs **before**
the TTY downgrade at `:1742`. A naive edit that adds a menu after the guard is
**dead code** - exit 4 fires first. The fix must **merge TTY-awareness into
the guard**: when `config_path.exists() and not (reconfigure or force)`, branch
on `_stdin_is_interactive()`:
- TTY -> menu;
- non-TTY -> emit token + guidance, `return 4`.

## Edge cases (must be defined + tested)

- **TTY menu input handling:** empty/Enter -> default `[1]` leave (exit 0);
  invalid input -> re-prompt (or default leave); **EOF/Ctrl-D -> leave (exit 0)**;
  **Ctrl-C -> exit 1** (consistent with v1.28.1 `KeyboardInterrupt` handling).
- **`--dry-run` on an existing install:** define explicitly - dry-run reports
  what *would* happen for the chosen/again-default path without writing; lock
  it down in a test so behavior can't drift.
- **Conflicting flags `--reconfigure --force`:** define precedence - **`--force`
  wins** (full overwrite supersedes re-prompt); document + test.
- **`consensus` (with a space) alias:** same entry point (`_init_wizard:main`),
  so it inherits the behavior; add a smoke assertion.

## Testing plan (TDD - write tests first)

1. **Binary, non-TTY (mock `isatty`->False):** existing config, no flags ->
   stdout first line == `STATUS: already-configured`, exit 4, guidance on
   stderr; token absent when `--reconfigure`/`--force` present.
2. **Binary, TTY (mock `isatty`->True + mock `input`):** menu shown; choice 1 ->
   no-op exit 0; choice 2 -> reconfigure path; choice 3 -> force path; Enter ->
   leave; EOF -> leave exit 0; Ctrl-C -> exit 1.
3. **Contract regression test (mandatory):** asserts the exact token string,
   exit code 4, and the stdout/stderr channel split - fails if any drifts so
   the skill matcher can't silently break.
4. **Update legacy tests:** the 4+ tests asserting the old `"already exists"`
   message + `rc == 4` move to assert the new token/message.
5. **`--dry-run` + conflicting-flag** behaviors locked down.
6. Full suite green before tagging any release that carries this.

## Out of scope (explicit follow-ups)

- **Verify/repair of a partially-broken install** (Q3c follow-up): a future
  *explicit* menu option / flag that checks `.mcp.json` presence + enforcement
  hook health and repairs. Recorded in CHANGELOG so it isn't lost. NOT bundled
  into "leave as-is".
- Packaging / publishing / registry changes - none required (verified: no PyPI
  channel; distribution is git-tag + pipx per the release procedure).
- Any unrelated init-wizard refactor.

## Risks (from the panel)

1. **Contract drift** between binary and skill - mitigated by the mandatory
   contract regression test (highest-priority mitigation).
2. **Menu loop** if the token ever fires under a resolving flag - mitigated by
   the one-shot re-invoke + "token only when no resolving flag" rule.
3. **Token/stdout vs. verbatim collision** - mitigated by the skill consuming
   the token line and surfacing only the remainder.
4. **Ordering** - mitigated by merging TTY-awareness into the guard (above).
