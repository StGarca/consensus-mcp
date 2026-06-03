---
description: Set up consensus-mcp - the per-project bootstrap, or (with --install-claude-code) the one-time global Claude Code helper + enforcement install.
---

The user invoked `consensus-init`. Their arguments are: `$ARGUMENTS`

FIRST decide which operation they asked for by inspecting those arguments, and run
EXACTLY that. NEVER silently substitute a different command than the flag the user
typed, and NEVER afterward offer to run a command the user just typed (that is the
loop we are fixing).

## A) `--install-claude-code` is present

This is the ONE-TIME, MACHINE-WIDE install the README documents as "add a small
Claude Code helper so you can type 'consensus init' in any project". It copies the
consensus skills + this slash command + the enforcement hooks into `~/.claude`. It
is NOT the per-project bootstrap, so do NOT run `--from-claude-code` for this.

Run via Bash (pass `--force` only if the user also typed it):

    consensus-init --install-claude-code

Surface stdout/stderr verbatim, then act on the exit code:

- **exit 0** - the helper + enforcement hooks are installed. Tell the user to
  restart Claude Code (or run `/mcp`) so the new hooks/skills load. THEN, because
  the helper install does not configure the current project, note that the next
  step is to set up THIS project, and OFFER via `AskUserQuestion` to run the
  per-project bootstrap (`consensus-init --from-claude-code`) now. Do **not** offer
  `--install-claude-code` again - you just ran it.
- **exit 5** - a managed file was STALE and SKIPPED (kept, not updated). Relay the
  `SKIP:` lines and that re-running with `--force` updates them.
- **exit 6** - the install is INCOMPLETE (enforcement is OFF: a stale/partial
  package, or settings.json could not be updated). Relay the exact remedy from
  stderr (e.g. `pipx install --force consensus-mcp`, or fix the reported
  settings.json problem) and that the user should re-run. Do not pretend it
  succeeded.

## B) `--uninstall-claude-code` is present

Run `consensus-init --uninstall-claude-code` via Bash and surface its output.

## C) Otherwise - per-project bootstrap (the default)

Run `consensus-init --from-claude-code` via Bash from the current working
directory, and **APPEND THE USER'S ARGUMENTS VERBATIM** - everything in
`$ARGUMENTS` (e.g. `--non-interactive`, `--accept-defaults`, `--reconfigure`,
`--force`, `--repair`, `--contributors`, `--workflow`). Concretely: if the user
typed `consensus-init --non-interactive --accept-defaults`, you run
`consensus-init --from-claude-code --non-interactive --accept-defaults`. The ONLY
thing you add is `--from-claude-code`; you NEVER drop a flag the user typed. Surface
the binary's stdout/stderr verbatim - it already prints the correct next-step
guidance, including the Claude Code restart instructions. If the binary is not on
PATH, tell the user to run `pipx install consensus-mcp` once globally and retry.

**Already configured:** if the binary exits with code 4 and the first stdout line
is exactly `STATUS: already-configured`, the project is already set up. Do not
surface the raw error - consume that token line. If the user passed
`--non-interactive` or `--accept-defaults` (they signalled NO prompts), do NOT show
an interactive menu: just relay the binary's one-line already-configured guidance
(re-run with `--reconfigure` or `--force`) and stop. Otherwise present four options
via `AskUserQuestion` (leave as-is / verify-repair / reconfigure / force overwrite),
then re-invoke `consensus-init --from-claude-code --repair`, `--reconfigure`, or
`--force` once as appropriate (one-shot; "leave" does nothing). The `--repair` flag
re-creates missing pieces and reports diverged ones non-destructively.

**Workspace umbrella:** if the binary exits with code 8 and the first stdout line
is exactly `STATUS: looks-like-workspace-umbrella`, the current directory is a
workspace folder containing git sub-projects - bootstrapping it directly would
blanket every sub-project. Do not surface the raw error. Consume the token line and
present options via `AskUserQuestion`: one entry per child project named in stderr
(capped at ~10) to initialize that project by re-running
`consensus-init --from-claude-code` from inside it, plus **Initialize here anyway**
(re-run with `--here`) and **Cancel**. Act on the choice one-shot; the resolved
flag or changed directory suppresses the token so no loop occurs.

After a SUCCESSFUL bootstrap, read the binary's "Enforcement status" line. If it
reports ADVISORY mode (the one-time global step has not run on this machine), OFFER
via `AskUserQuestion` to run `consensus-init --install-claude-code` now to enable
edit-gating + precedence injection across this machine. Only run it if the user
says yes. (If the user ALREADY asked for `--install-claude-code` in this
invocation, you are in branch A and must not reach this offer.)

Do not reimplement any of `consensus-init`'s logic. The binary handles `.mcp.json`
writing, `.consensus/config.yaml` creation, the `.gitignore` managed block, the
global `~/.claude` helper install, and Claude-Code-specific restart messaging.
