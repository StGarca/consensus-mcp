# consensus-mcp

**Don't let one AI be the only judge of its own code.**

consensus-mcp puts *several different* AIs on your code instead of trusting a
single one. They review each other's work and only let a change ship when they
**independently agree** - and, when you want code *written* (not just checked),
a top-tier "architect" AI directs a cheaper "builder" AI, so you get
expensive-grade judgment at a fraction of the cost. Every step is sealed to
disk, so you can prove exactly what was decided, by which AI, and when.

Works with Claude, Codex, Gemini, Grok, and Kimi out of the box - and you can
add any other AI with a short config file (no code).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)

---

## Why one AI isn't enough

AI coding assistants are fast, but confidently wrong: they invent functions,
misremember files, and act on unstated assumptions about what "done" means. An
AI reviewing its *own* work has the exact blind spots that caused the mistake in
the first place.

Different models fail in different ways. When two of them disagree about what a
piece of code does, at least one is wrong - and that disagreement is what catches
the bug a single reviewer waves through. consensus-mcp turns "get a second
opinion from a *different* AI" into an automatic, enforced step in your workflow.

---

## Two ways to use it

### 1. Consult - a second opinion from other AIs

You pick a panel of AIs. When you write, fix, or review code, each one reviews
the change **independently** - they can't see each other's answers first, so
they can't just agree to be polite - returns specific issues with file-and-line
citations (not vague impressions), and the change only ships when the panel
**agrees** (you choose the rule: majority, or unanimous). Every review is sealed
with a content hash, so the record is auditable, not a vibe.

The payoff: a change that clears a panel of different AI families isn't "one
model liked it" - it's "several models that fail in different ways all agreed."

### 2. Build - expensive judgment, cheap labor

Consult *checks* code. Build is how you *produce* it well - without paying
premium rates for every line.

You assign three roles to the AIs you have:

- an **architect** (a top-tier model) writes the spec and rules on every step;
- a **builder** (a cheaper model) does the actual file editing, sealed inside an
  isolated git worktree it can't escape;
- a **reviewer** checks each change before the architect signs off.

The expensive architect is fed only a short, rolling summary of the project -
not your whole repo - so you pay for its *judgment*, not its typing. A cheap
model doing the typing under expensive direction turns out both better-directed
and far cheaper than putting the premium model on every keystroke. You touch the
loop at exactly two points: **approve the plan**, then **approve the final
merge**. Everything in between runs itself.

**How Build adds to Consult - and how they stack.** Consult is a *quality gate*
on changes; Build is a *cheap, supervised way to generate* them. They compose: a
Consult panel can ratify the spec a Build will follow, and a Build that gets
stuck can hand the question back to a Consult. That's literally how this project
is built - a panel ratifies the design, then a Build executes it as
review-gated steps.

> **A note on trust.** Build gives a model real write access. It does *not* trust
> the AI tool's "sandbox" to contain that - a real experiment showed the sandbox
> lets writes escape - so instead a supervisor owns every git operation and a
> repo integrity check blocks delivery on *any* change outside the builder's
> lane. Even so: run it on work you can review and repos you can roll back.

### Optional: a Looper plan - get the goal right before you spend

A build loop is only as good as the goal you point it at. Aim it at something
vague ("make onboarding better") and you get vague, expensive, wrong work. A
**Looper plan** is an optional coach that runs *before* the build and pins down
the two things loops usually get wrong:

- **A sharp goal** - exactly what you're producing, and what's explicitly *out*
  of scope.
- **A real way to check it's done** - not "looks good," but something concrete: a
  command that passes or fails, a second AI's verdict against a rubric, or your
  own sign-off. It pushes hard for the kind a machine can check on its own.

It interviews you, pushes back when your goal is fuzzy, shows you the plan as a
simple diagram, and then hands a clean spec to Build. If your goal is already
crisp, skip it. But if you've ever watched an AI confidently do the *wrong* thing
because "done" was never defined, that's the problem this prevents - before you
spend a cent building.

---

## Quick start

**One time, per machine:**

```bash
pipx install git+https://github.com/StGarca/consensus-mcp.git@v2.1.1

# Install the Claude Code helper once. This is what lets you set up and run
# consensus from chat in ANY project - including auto-initializing a new one.
consensus-init --install-claude-code
```

(If `consensus-init` isn't found right after install, run `pipx ensurepath` and
reopen your terminal.)

**Then, in any project, just ask.** Open Claude Code in the project and say it in
plain language - e.g. *"get a consensus review on this change,"* or *"set up a
Consensus Build for this goal."* If the project isn't set up for consensus yet,
it notices, asks which AIs you want on the panel, confirms, then initializes the
project for you (writes a small config + registers the MCP server) and runs. No
per-project setup command to memorize.

**Prefer the terminal?** From the project root:

```bash
consensus-init                       # interactive: pick your panel, etc.
consensus-init --non-interactive --accept-defaults   # or take the defaults
```

**Pick your panel.** You choose from the independent AIs detected on your PATH
(minimum two *independent* reviewers). The list is built dynamically from the
installed profiles, so any AI you add shows up automatically. Claude is optional:
you can run, say, Codex + Gemini + Kimi with no Claude at all.

> **Guided, cross-platform setup.** If you pick an AI whose CLI isn't installed,
> init prints the exact install + login commands for your OS - it never runs them
> for you. It also seeds shared reviewer "house rules" into each AI's own
> instructions file (`CLAUDE.md` / `AGENTS.md` / `GEMINI.md`) so every model on
> the panel plays by the same guidelines.

**See your track record.** `consensus results` prints a project scorecard -
findings by severity, how each was resolved (fixed, or dismissed with evidence),
and the convergence rate across every run (`--json` for the machine-readable
form).

---

## How it works (the short version)

Every review runs the same four steps: **author** the contract -> **dispatch**
each AI in a locked-down, read-only mode -> **seal** the responses with hashes
into an append-only log -> **verify** the result against the contract before any
code is applied.

A change can only "close" if a *different* AI family than the one that wrote it
reviewed the *exact* changed state *after* the change was made. Miss any of those
and the review is rejected automatically. A background watchdog kills any AI call
that stalls, so reviews can't hang forever. Working state is also mirrored to a
separate git branch, so a stray `git clean` can't lose review history.

You set how strict things are per project - the review style, the panel, the
agreement rule, and more. `consensus-init` walks you through it;
`consensus-init --print-defaults` shows every option.

## How big a panel?

Any panel works as long as it has **at least two** AIs - the floor that makes
"different models that fail differently" possible. Two, three, four, or your own
custom mix are all governed **identically**: same rules, same enforcement, same
guarantees. Panel size never changes the doctrine. The only thing it changes is a
sensible default agreement rule (2 AIs: both must agree; 3+: majority) - which
you can override.

## Does it actually work?

Yes - and the record is auditable, not a number you have to take on faith. Every
review is sealed to disk with a content hash
([`consensus-state/archive/review-passes/`](consensus-state/archive/review-passes)).

In **134 review iterations on its own code**, the panel logged **548 findings -
156 of them blocking or critical** - each addressed before the change merged
(fixed, or dismissed with the evidence that disproved it). The same setup is in
active use on other, separate codebases; those use different log formats, so
their results aren't folded into one number here - every figure on this page
comes straight from this repo's sealed artifacts.

## Maturity

Both modes are stable and in daily use. Consult has been the core since the
project's first release; Build is the newer expensive-plans/cheap-builds mode,
hardened across many releases and used to build this project itself. **2,229
tests green** on Linux + Windows / Python 3.11+, an ASCII-only tree
(guard-tested), every reviewer pluggable by config, no Claude required.

- What changed, version by version ->
  [Releases](https://github.com/StGarca/consensus-mcp/releases) /
  [`CHANGELOG.md`](CHANGELOG.md)
- Known-issue releases + the right version to upgrade to ->
  [`docs/advisories.md`](docs/advisories.md)

Extracted from the project that produced and stress-tested it, then restarted as
a standalone tool.

## Learn more

- [`docs/workflows/architect-build.md`](docs/workflows/architect-build.md) - the
  full Consensus Build guide (roles, gates, containment, the Looper plan)
- [`docs/architecture/orchestration-spec.md`](docs/architecture/orchestration-spec.md)
  - the full multi-AI orchestration design
- [`docs/workflows/`](docs/workflows/) - the review modes and when to use each
- [`docs/advisories.md`](docs/advisories.md) - known-issue releases

(Power-user options - bootstrap flags, single-reviewer escape hatches, the full
configuration table - live in the `docs/` tree to keep this page readable.)

## Requirements

- Python 3.11+
- [`pipx`](https://pipx.pypa.io/) recommended (isolated, reusable across
  projects)
- At least two AI CLIs on your PATH for the panel. Built-in support for:
  - [`codex`](https://github.com/openai/codex)
  - [`gemini-cli`](https://github.com/google-gemini/gemini-cli)
  - [`kimi-cli`](https://github.com/MoonshotAI/kimi-cli)
  - Claude (when you run inside Claude Code) - optional, like the rest
- Don't see your AI? Add it with a short config profile - no code change needed.
  `consensus-init` detects which you have and, for any you pick that are missing,
  prints the right install + login commands for your OS.

## License

MIT - see [LICENSE](LICENSE).

## Contributing

The project reviews itself: contributions go through the same four-step cross-AI
cycle, so expect review feedback from more than one model on your change.
