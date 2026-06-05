# Hosting a consult as a non-Claude AI

Consensus-mcp is host-agnostic: any AI (Codex, Gemini, Grok, ...) can act as the
*host* that drives a consult, not just Claude. A Codex-hosted run in a consuming
project (2026-06-04) proved this end-to-end - but it also exposed two friction
points this page exists to remove. See the converged design at
`docs/consensus/field-notes-and-recommendations.md`.

## The supported entrypoint

Run a full iteration end-to-end with the console script:

```bash
consensus-mcp-run-iteration \
  --iteration-dir consensus-state/active/<iteration> \
  --goal-packet  consensus-state/active/<iteration>/goal_packet.yaml \
  --target       <document-under-review-or-produced-artifact> \
  --config       .consensus/config.yaml \
  --claude-proposal consensus-state/active/<iteration>/claude-proposal.yaml \
  --repo-root    .
```

It is a thin wrapper around `consensus_mcp.tools.consensus_run_iteration.handle()`
- the single execution implementation. **Do not hand-roll your own shim that calls
`handle()` directly** (the field host did, before this script existed); the
supported surface keeps you on the same dispatch / sealing / provenance path
everyone else uses.

Output contract:
- The structured outcome JSON is **always printed to stdout** (a no-extra-file
  path for batch hosts).
- It is **also written** to `--outcome`, defaulting to
  `<iteration-dir>/run-outcome.json`.

Exit code is `0` when the iteration result is `ok`, else `1`.

Optional flags: `--host-peer-review-yaml <path>` (supply a host_peer review),
`--outcome <path>` (override the outcome file location).

## The one environmental requirement: writable `$HOME`, no restrictive sandbox

The external reviewer CLIs (codex / gemini / grok / kimi) need **writable
`HOME` / config / session / log paths**. The field host's first attempt failed
because it launched the reviewers from inside a restrictive shell sandbox that
blocked those writes. Run `consensus-mcp-run-iteration` with **unsandboxed /
escalated execution** (or otherwise ensure the reviewer CLIs can write their own
HOME/config/session paths).

This is the *only* host-environment requirement. It is a local execution
constraint, not a packaging one: there is no PyPI/npm/registry/CI step involved -
consensus-mcp ships via git tags + pipx.

## What "host" means

The host authors the `goal_packet` + proposal, launches the panel, and (for
propose-converge with real design surface) synthesizes the converged plan. The
contributors - codex/gemini/grok/kimi - are dispatched as independent,
blind-first reviewers regardless of which AI is hosting. A non-Claude host is a
first-class host; nothing about the workflow assumes Claude specifically.
