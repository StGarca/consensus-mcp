# consensus-mcp

**A second opinion for AI-written code — from other AIs.** Instead of
trusting one AI to grade its own homework, consensus-mcp puts a small
panel of *different* AIs — the ones you choose — on every change, has them
review it independently, and only lets the change through when they
agree. Built-in support for Claude, Codex, Gemini, and Kimi, and you
can add any other AI just by writing a short config profile (no code).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)

---

## Why

AI coding assistants are fast, but they're confident even when they're
wrong — they invent functions, misremember files, and act on unstated
assumptions (a function's signature, that a file or dependency exists,
what "done" even means) instead of surfacing them. An AI reviewing its
*own* work shares the exact blind spots that caused the mistake.

Different AI models fail in different ways. When two of them disagree
about what a piece of code does, at least one is wrong — and that
disagreement is what catches bugs a single reviewer misses.
consensus-mcp turns that into an automatic step in your workflow.

## What it does

You decide who's on the panel. At setup you pick from the AIs you have
installed — Claude, Codex, Gemini, Kimi, or any other you've defined —
and they each review every change. When you ask the panel to write,
fix, or review code, consensus-mcp:

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

The payoff: a change that passes a panel of different AI families isn't
"one model liked it" — it's "several models that fail differently all
agree."

**Add an AI without touching code.** Each reviewer is described by a
small config *profile* — how to detect it, run it, and read its answer.
Built-in profiles ship for Claude, Codex, Gemini, and Kimi; to add a
new AI you just drop in another profile. No code change, no new
release.

## Quick start

Install once per machine (works in any project):

```bash
pipx install git+https://github.com/StGarca/consensus-mcp.git@v1.28.0

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

**Pick your panel at setup.** `consensus-init` runs an interactive
multi-select of the independent AIs it detects on your PATH and lets
you choose the panel (minimum two *independent* reviewers). The list is
derived dynamically from the installed profiles, so any AI you add —
Kimi or your own — shows up automatically. Claude is optional: you can
run, say, Codex + Gemini + Kimi with no Claude at all.

If the host AI (Claude) is on your panel, init then offers an
*optional same-model second opinion* — a blind reviewer that runs the
host's own model. It's a cheap extra pass if you have the tokens, but
it counts only as **+0.5**: supplementary, not independent consensus
(it shares the host's blind spots), with no vote at the gate, so it can
never be the deciding cross-model sign-off — though every good idea it
raises is still applied.

> **Guided, cross-platform setup.** If you pick an AI whose CLI isn't
> installed yet, init prints the exact install and login commands for
> your OS (Windows or Linux) — it never runs them for you. It also
> seeds shared reviewer "house rules" into each AI's own instructions
> file (`CLAUDE.md` / `AGENTS.md` / `GEMINI.md`) so every model on the
> panel plays by the same guidelines.

**See your track record.** `consensus results` prints a project
scorecard — findings by severity, how each was resolved (fixed, or
dismissed with evidence), fixes applied, and the convergence rate
across every run (`consensus-results --json` for the machine-readable
form).

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

## How big a panel?

Any panel works as long as it has **at least two** AIs — the floor that
makes "different models that fail differently" possible. Two, three,
four, or your own custom mix are all governed **identically**: same
rules, same enforcement, same guarantees. The size of the panel never
changes the doctrine; it's the *workflow mode* that does.

The only thing panel size changes is a sensible default agreement rule:
2 AIs default to "both must agree," 3-or-more default to "majority" —
and you can override either.

## Does it actually work?

Yes — and the record is auditable, not a number you have to trust. Every
review is sealed to disk with a content hash
([`consensus-state/archive/review-passes/`](consensus-state/archive/review-passes)).

In 76 review iterations on its own code, the panel found **230 issues — 60 of
them blocking or critical** — each addressed before the change merged (fixed, or
dismissed with the evidence that disproved it). The same setup is in active use
on other real, separate codebases; because those use different log formats, their
results aren't yet combined into one number, so every figure here comes from this
repo's sealed artifacts.

## Status

**Current: v1.28.0 — stable.** 1,300+ regression tests, green on
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
- At least two AI CLIs on your PATH for the panel. Built-in support for:
  - [`codex`](https://github.com/openai/codex)
  - [`gemini-cli`](https://github.com/google-gemini/gemini-cli)
  - [`kimi-cli`](https://github.com/MoonshotAI/kimi-cli)
  - Claude (when you run inside Claude Code) — optional, like the rest
- Don't see your AI? Add it with a short config profile — no code
  change needed. `consensus-init` detects which of these you have
  installed and, for any you pick that are missing, prints the right
  install + login commands for your OS.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

The project reviews itself: contributions go through the same
four-step cross-AI cycle, so expect review feedback from more than one
model on your change.
