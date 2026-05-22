# consensus-mcp

**A second opinion for AI-written code — from other AIs.** Instead of
trusting one AI to grade its own homework, consensus-mcp puts a small
panel of *different* AIs (Claude, Codex, and Gemini by default) on
every change, has them review it independently, and only lets the
change through when they agree.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)

---

## Why

AI coding assistants are fast, but they're confident even when they're
wrong — they invent functions, misremember files, and skip
assumptions. An AI reviewing its *own* work shares the exact blind
spots that caused the mistake.

Different AI models fail in different ways. When two of them disagree
about what a piece of code does, at least one is wrong — and that
disagreement is what catches bugs a single reviewer misses.
consensus-mcp turns that into an automatic step in your workflow.

## What it does

When you ask the AI panel to write, fix, or review code, consensus-mcp:

1. **Writes down the request as a contract** — what's changing, which
   files, what "done" looks like, who approved it.
2. **Sends it to each AI independently** — they review without seeing
   each other's answers first, so they can't just agree out of
   politeness.
3. **Collects structured findings** — each AI returns specific issues
   with file-and-line citations, not vague impressions.
4. **Requires agreement before anything lands** — a configurable rule
   (e.g. "majority" or "unanimous") decides when the panel has
   converged.
5. **Seals every step** with content hashes, so you can later prove
   exactly what was reviewed, by which AI, and when.
6. **Backs up its working state** to a separate git branch so a stray
   `git clean` can't lose review history.

The payoff: a change that passes three different AI families isn't
"one model liked it" — it's "three models that fail differently all
agree."

## Quick start

Install once per machine (works in any project):

```bash
pipx install git+https://github.com/StGarca/consensus-mcp.git@v1.17.5

# Optional: add a small Claude Code helper so you can type
# "consensus init" inside Claude Code chat in any project.
consensus-init --install-claude-code
```

Then, in any project:

```bash
cd /path/to/your-project
consensus-init                       # interactive setup, or:
consensus-init --non-interactive --accept-defaults
```

That writes a small config and registers the tool with Claude Code.
Reopen Claude Code in the project and just ask in plain language —
e.g. *"get a consensus review on this change."*

> Codex and Gemini are optional and auto-detected if their CLIs are on
> your PATH. Claude is always there as the coordinator. With just
> Claude + Codex you still get full cross-AI review — see
> "2 AIs or 3?" below.

## How it works (the short version)

Every review runs the same four steps: **author** the contract →
**dispatch** each AI in a locked-down, read-only mode → **seal** the
responses with hashes into an append-only log → **verify** the result
against the contract before any code is applied.

A change can only "close" if a *different* AI family than the one that
wrote it reviewed the *exact* changed state *after* the change was
made. Miss any of those and the review is rejected automatically. A
background watchdog also kills any AI call that stalls, so reviews
can't hang indefinitely.

You pick how strict things are per project — the review style, who's
on the panel, the agreement rule, and more. `consensus-init` walks you
through it; `consensus-init --print-defaults` shows every option.

## 2 AIs or 3?

A 2-AI setup (Claude + Codex) and a 3-AI setup (adding Gemini) are
governed **identically** — same rules, same enforcement, same
guarantees. The number of AIs never changes the doctrine; it's the
*workflow mode* that does. The only difference is a sensible default:
2 AIs default to "both must agree," 3 default to "majority" — and you
can override either.

## Does it actually work?

consensus-mcp is built using itself — every change goes through
its own cross-AI review. The original bootstrap deployment
measured **38 real defects caught before commit, zero false
positives** across 6 subsystems (a race condition, a fail-open
safety gate, a path-matching bug — each missed by single-AI
review). It's been self-hosted continuously since, across **70+
consensus iterations** on the v1.13–v1.15 line, with cross-AI
audits routinely catching blocking defects pre-merge.

(38 is the original *measured* baseline, not a running tally — a
tool built to catch inflated metrics shouldn't inflate its own.)

## Status

**Current: v1.17.5 — stable.** 1,000+ regression tests, green on
CI across Linux + Windows and Python 3.11+. Self-hosted:
every release is built through consensus-mcp's own cross-AI
review.

- What changed in each release → [`CHANGELOG.md`](CHANGELOG.md)
- Known-issue releases + which version to upgrade to →
  [`docs/advisories.md`](docs/advisories.md)

Extracted from the project that produced and stress-tested it,
then restarted as a standalone tool.

## Learn more

- [`CHANGELOG.md`](CHANGELOG.md) — what changed in each release
- [`docs/architecture/orchestration-spec.md`](docs/architecture/orchestration-spec.md)
  — the full multi-AI orchestration design
- [`docs/workflows/`](docs/workflows/) — the review modes
  (propose-converge and post-review are the day-to-day modes; an
  autonomous-execute mode is staged but not yet runnable) and when to
  use each
- [`docs/advisories.md`](docs/advisories.md) — known-issue releases
  and the right version to upgrade to

(Power-user options — bootstrap flags, single-reviewer escape hatches,
the full configuration table, the architecture map — moved to
[`CHANGELOG.md`](CHANGELOG.md) and the `docs/` tree to keep this page
readable.)

## Requirements

- Python 3.11+
- [`pipx`](https://pipx.pypa.io/) recommended (isolated, reusable
  across projects)
- Optional: [`codex-cli`](https://github.com/openai/codex-cli) and/or
  [`gemini-cli`](https://github.com/google-gemini/gemini-cli) on PATH
  for the multi-AI panel (Claude is always present)

## License

MIT — see [LICENSE](LICENSE).

## Contributing

The project reviews itself: contributions go through the same
four-step cross-AI cycle, so expect review feedback from more than one
model on your change.
