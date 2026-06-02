# Operator-Controlled Gate-Stack Engagement

## What this is

The 5-layer gate stack (universal-action-gate, dispatch-canon-validator, pre-write-rule-consultation, tool-defect-gate, stop-claim-gate) was originally designed for a specific 24-hour episode where Claude was hallucinating defects and drifting from proven patterns. Always-on, the stack adds friction to every Claude action - intent records for every tool call, proof artifacts for protected paths, audit-log writes on every action, stop-hook scanning on every assistant message.

This switch makes the stack **default-OFF**. The operator engages it on demand - when noticing warning signs that Claude's reasoning is drifting - and disengages it with an all-clear when normal operation resumes.

Built per a converged design plan (Workflow A converge + Workflow B 4-of-4 cross-family ratification).

## Engagement marker

The canonical state is a marker file under `.claude/` (relative to the repo root - project-local; engaging it here does NOT affect any other project). The exact filename is defined in each hook script (`Path(__file__).resolve().parent.parent / <marker>`) and is checked on every hook invocation.

- File present -> 5 gates ENGAGED.
- File absent -> 5 gates IDLE (each hook exits 0 immediately, before any stdin parsing).

The file's contents are not consulted. Existence is the state. Empty file is fine.

## Slash commands

Two slash commands live in `.claude/commands/` and toggle the marker:

- **Engage** - creates the marker (idempotent `touch`).
- **Disengage** - removes the marker (idempotent `rm -f`).

Both echo the resulting state after the toggle.

## Manual verification

Hook exit behavior is deterministic from marker presence; run any hook script with `--self-test` to confirm both the engaged and idle paths.

## Behavior matrix

| Marker | universal-action-gate | dispatch-canon-validator | pre-write-rule-consultation | tool-defect-gate | stop-claim-gate |
|---|---|---|---|---|---|
| absent | exit 0 (idle) | exit 0 (idle) | exit 0 (idle) | exit 0 (idle) | exit 0 (idle) |
| present | enforces action-intent + golden-rule + audit | enforces canonical dispatch shape | enforces standing-rule consultation on memory/CLAUDE.md edits | enforces proof artifact on protected-path edits | enforces proof citation on forbidden-phrase claims |

## Mid-session behavior

The marker is re-read on every hook invocation (no caching). Flipping the switch:

- mid-conversation, the very next Claude action gets the new behavior
- across sessions, the on-disk marker survives (engaged state persists until disengaged)

No Claude Code restart is needed in either direction.

## Bootstrap exemption

When the gate stack is ENGAGED, the universal-action-gate's bootstrap exemption still permits writes to `./tmp/action-intent-*.yaml` without a pre-existing intent. This lets Claude author the intent record that authorizes its next action - necessary because the gate would otherwise require an intent to write the intent (infinite recursion).

The exemption is narrow (only `./tmp/action-intent-*.yaml`, only Write/Edit/MultiEdit/NotebookEdit tools) and logged to the audit trail.

## Cross-references

- Tool-defect bypass procedure (still applies when ENGAGED): `docs/consensus/tool-defect-bypass.md`
- Per-hook self-tests embedded as `--self-test` mode in each script under `.claude/hooks/`.

## Carry-forward notes

- **Hook startup cost**: marker-absent fast path is `python startup + Path(__file__) check + return` - empirically <50ms on a modern dev machine. Formal measurement should land before any decision to make any gate always-on.
- **Imports**: each gate imports stdlib only (json, re, sys, pathlib, datetime, hashlib, shlex, os) - no expensive initialization at module level.
