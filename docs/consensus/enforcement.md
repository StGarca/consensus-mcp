# Consensus enforcement layer

How consensus-mcp makes a Claude Code session **defer to consensus** instead of
barreling ahead — and, just as importantly, what this is **not**.

## Threat model (read this first)

The enforcement layer governs a **cooperating agent's workflow discipline**. It stops
the host agent from implementing or claiming completion *before consensus has
approved* — even if the agent's own reasoning would skip the gate.

It is **NOT a sandbox** and **not a defense against a malicious local process**. Any
process with shell access can write files, edit `settings.json`, or disable hooks; no
in-process Claude Code hook can prevent that. Pretending otherwise was the false
security an audit (2026-05-22, `iteration-v1210-audit`) exposed in the first cut. The
honest bar: **a cooperating agent cannot lazily or accidentally self-approve** —
approval requires a real cross-family consult to have actually happened.

## Trust root: the T6 seal, not a file

The marker `.consensus/design-approved` is a **pointer/cache, not the trust root**. It
holds `{schema_version, design_consensus_ref, converged_plan_sha256, scope_glob,
repo_root_id}`. On every check, `verify_design_approval` **re-validates** it against the
live seal:

1. `resolve_consensus_ref(design_consensus_ref)` — the referenced iteration must be a
   real **CLOSED/SEALED** consensus iteration (reused from `_delivery_readiness`).
2. The sealed iteration must carry **≥2 non-claude (different-family) reviewer
   artifacts** (mirrors `mint_delivery_token`).
3. `converged_plan_sha256` must match the iteration's `converged-plan.yaml` hash
   (tamper guard).
4. The edited target is **repo-confined** (rejected if outside the repo / absolute /
   `..`) and `fnmatch`-matched against `scope_glob` (which may not be `*`/`**`).

So forging an accepted approval requires forging the **sealed cross-family
artifacts** — exactly what T6 sealing + the cross-family closure invariant already
protect. The old `cross_family_sealed: true` boolean is gone.

## The gates

- **PreToolUse** — `Edit/Write/MultiEdit/NotebookEdit` are denied (exit 2) unless a
  re-validated marker covers the target. **Bash is default-deny**: allowed only if the
  command is on a small **read-only allowlist** (`ls`, `cat`, `grep`, `git
  status/diff/log`, `pytest`, …) or a tight-scope sealed marker is in force. A
  blocklist was removed — it could never enumerate every file-writer (`python -c`,
  `ln`, `install`, `patch`, `dd`, …); default-deny is fail-safe (unknown ⇒ denied).
- **Stop** — a *soft* directive (Claude Code does not support a hard Stop deny):
  blocks "done/shipped/fixed" claims when modified or recently-committed source files
  lack a delivery-readiness token. Exception-safe.
- **SessionStart / UserPromptSubmit** — inject the precedence mapping (brainstorming →
  Workflow A; requesting/receiving-code-review → Workflow B; verification → sealed
  gate). Repo root resolved via `git rev-parse --show-toplevel`.

## Activation + graceful degradation

Hooks fire only when **registered in `~/.claude/settings.json`** — copying `hooks.json`
into `~/.claude` does nothing. `consensus-init --install-claude-code` **merges**
consensus hook groups into `settings.json` (idempotent, preserves unrelated hooks +
top-level keys, tagged `_consensus_mcp_managed` for clean
`--uninstall-claude-code`). When the consensus runtime/CLIs are **absent**, every hook
**fails open** (exit 0) — the session behaves exactly like a plain workflow, never
worse.

## The kimi reviewer is read-only

The kimi peer reviewer runs over **stdin** (`--quiet --thinking`, no `--print`/`--afk`
tool auto-approval), inside a **disposable temp copy** of the repo (`--work-dir`), and
a **post-dispatch `git status` integrity check** rejects the review if the *real* repo
was mutated. A reviewer never changes the code it reviews.
