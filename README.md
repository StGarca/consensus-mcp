# consensus-mcp

**Automated peer review for AI-written code.** Instead of trusting one
AI to check its own work, consensus-mcp puts a small panel of AIs
(Claude, Codex, and Gemini by default) on every change — and the
change only ships when they agree.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
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
pipx install git+https://github.com/stgarca/consensus-mcp.git@v1.15.6

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

consensus-mcp is built using itself. During its own development the
AI panel caught **38 real defects before commit, with zero false
positives** — including a race condition, a safety check that silently
failed open, and a path-matching bug, each missed by single-AI review.

## Status

**Current: v1.15.6.** Recent work hardened the review machinery
(machine-enforced plan conventions, dispatcher fixes), made the
documentation match shipped reality, and fixed the release/branch
model so `main` (and this landing page) and GitHub Actions CI stay
on the latest released state. Full per-release detail is in
[`CHANGELOG.md`](CHANGELOG.md); upgrade guidance for known-issue
releases is in [`docs/advisories.md`](docs/advisories.md).

Extracted from the project that produced and stress-tested it, then
restarted as a standalone tool. ~970 regression tests, green.

## Learn more

- [`CHANGELOG.md`](CHANGELOG.md) — what changed in each release
- [`docs/architecture/orchestration-spec.md`](docs/architecture/orchestration-spec.md)
  — the full multi-AI orchestration design
- [`docs/workflows/`](docs/workflows/) — the review modes
  (propose-converge, post-review, advisory, autonomous-execute) and
  when to use each
- [`docs/advisories.md`](docs/advisories.md) — known-issue releases
  and the right version to upgrade to

(Power-user options — bootstrap flags, single-reviewer escape hatches,
the full configuration table, the architecture map — moved to
[`CHANGELOG.md`](CHANGELOG.md) and the `docs/` tree to keep this page
readable.)

## Requirements

- Python 3.10+
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
