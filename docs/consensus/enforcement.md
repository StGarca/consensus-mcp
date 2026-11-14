# Consensus enforcement layer

How consensus-mcp makes a Claude Code session **defer to consensus** instead of
barreling ahead - and, just as importantly, what this is **not**.

## Threat model (read this first)

When enabled, the enforcement layer governs a **cooperating agent's workflow discipline**. It stops
the host agent from implementing or claiming completion before the required
consult evidence is sealed and checked.

It is **NOT a sandbox** and **not a defense against a malicious local process**. Any
process with shell access can write files, edit `settings.json`, or disable hooks; no
in-process Claude Code hook can prevent that. Pretending otherwise was the false
security an audit (2026-05-22, `iteration-v1210-audit`) exposed in the first cut. The
honest bar: **the host AI is the approver by design** in weighted-synthesis /
proposal mode. It collects blind reviews and records every idea as accepted
with reasons, rejected with refuting evidence, or deferred in `dispositions.yaml`.
`consensus-mcp-approve` validates that table and seals its hash with the outcome;
it does not require majority or unanimous agreement. Failed reviewers do not
count against the change. Here, family means the recorded model, not vendor.

## Trust root: the T6 seal, not a file

The marker `.consensus/design-approved` is a **pointer/cache, not the trust root**. It
holds the design reference, plan and disposition hashes, scope, and repo identity.
On every check, `verify_design_approval` **re-validates** it against the
live seal:

1. `resolve_consensus_ref(design_consensus_ref)` - the referenced iteration must be a
   real **CLOSED/SEALED** consensus iteration (reused from `_delivery_readiness`).
2. The sealed iteration must carry **>=2 non-claude (different-family) reviewer
   artifacts** (mirrors `mint_delivery_token`).
3. `converged_plan_sha256` and `dispositions_sha256` must match the sealed plan
   and disposition table (tamper guard).
4. The edited target is **repo-confined** (rejected if outside the repo / absolute /
   `..`) and `fnmatch`-matched against `scope_glob` (which may not be `*`/`**`).

So forging an accepted approval requires forging the **sealed cross-family
artifacts** - exactly what T6 sealing + the cross-family closure invariant already
protect. The old `cross_family_sealed: true` boolean is gone.

## The gates

- **PreToolUse** - `Edit/Write/MultiEdit/NotebookEdit` are denied (exit 2) unless a
  re-validated marker covers the target. **Bash is default-deny**: allowed only if the
  command is on a small **read-only allowlist** (`ls`, `cat`, `grep`, `git
  status/diff/log`, `pytest`, ...) or a tight-scope sealed marker is in force. A
  blocklist was removed - it could never enumerate every file-writer (`python -c`,
  `ln`, `install`, `patch`, `dd`, ...); default-deny is fail-safe (unknown => denied).
- **Stop** - a *soft* directive (Claude Code does not support a hard Stop deny):
  blocks "done/shipped/fixed" claims when modified or recently-committed source files
  lack a delivery-readiness token. Exception-safe.
- **SessionStart / UserPromptSubmit** - inject the precedence mapping (brainstorming ->
  Workflow A; requesting/receiving-code-review -> Workflow B; verification -> sealed
  gate). Repo root resolved via `git rev-parse --show-toplevel`.

## Activation + graceful degradation

Hooks fire only when **registered in `~/.claude/settings.json`** - copying `hooks.json`
into `~/.claude` does nothing. `consensus-init --install-claude-code` **merges**
consensus hook groups into `settings.json` (idempotent, preserves unrelated hooks +
top-level keys, tagged `_consensus_mcp_managed` for clean
`--uninstall-claude-code`). When the consensus runtime/CLIs are **absent**, every hook
**fails open** (exit 0) - the session behaves exactly like a plain workflow, never
worse.

The default governance mode is on-demand: a consult seals its result without
arming edit enforcement. A configured enforcing mode enables the scoped gates.
Server boot validation warns and audits findings by default;
`CONSENSUS_MCP_STRICT_BOOT=1` restores refusal (exit 2).

## The kimi reviewer is read-only

The kimi peer reviewer runs over **stdin** (`--quiet --thinking`, no `--print`/`--afk`
tool auto-approval), inside a **disposable temp copy** of the repo. Isolation and
integrity checks protect the real repo; the panel runner can launch Kimi alongside
other reviewers.
