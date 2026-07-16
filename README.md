# consensus-mcp

**Don't let one AI be the only judge of its own code.**

consensus-mcp puts several different AIs on your code. They review
independently -- they can't see each other's answers first -- and a change
only ships when the panel agrees. Every review is sealed to disk with a
content hash, so the record is auditable, not a vibe.

Works with Claude, Codex, Gemini, Grok, and Kimi out of the box. Add any
other AI with a short config file -- no code change needed.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)

---

## What it does

Two modes, each composable with the other:

**Consult** -- a quality gate. You pick a panel of AIs (minimum two). Each
reviews the change independently, returns specific issues with file-and-line
citations, and the change ships only when the panel agrees (majority or
unanimous -- your choice). A different AI family must review the exact changed
state after the change was made, or the review is rejected automatically.

**Build** -- supervised code generation. An architect model (top-tier) writes
the spec and rules on every step; a builder model (cheaper) does the file
editing inside an isolated git worktree it can't escape; a reviewer checks
each change before the architect signs off. You touch the loop at two points:
approve the plan, then approve the final merge. A supervisor owns every git
operation -- the AI tool's "sandbox" is not trusted.

**Looper plan** (optional) -- a pre-build coach that pins down a sharp goal
and a concrete done-check before you spend anything. Skip it if your goal is
already crisp.

---

## Quick start

**One time, per machine:**

```bash
pipx install git+https://github.com/StGarca/consensus-mcp.git@v2.4.3
consensus-init --install-claude-code
```

**Then, in any project** -- just ask Claude Code in plain language:
*"get a consensus review on this change"* or *"set up a Consensus Build for
this goal."* If the project isn't initialized yet, it detects that, asks
which AIs you want, and sets up for you.

**Prefer the terminal?**

```bash
consensus-init                       # interactive
consensus-init --non-interactive --accept-defaults   # take the defaults
```

`consensus-init` detects which AI CLIs are on your PATH, and for any you pick
that are missing, prints the exact install + login commands for your OS. It
also seeds shared reviewer "house rules" into each AI's instructions file so
every model plays by the same guidelines.

Claude is optional -- you can run Codex + Gemini + Kimi with no Claude at all.

### Governance is opt-in

Installing consensus-mcp does **not** activate it in every project. New and
existing projects default to `governance.mode: on-demand`:

- AIs must not invoke consensus unless the user explicitly asks for it.
- A requested consult runs once, returns its sealed result, and stops.
- On-demand consults create no edit gate, design-approval block, or delivery
  token requirement.
- A stale marker or malformed/missing config always fails open and cannot lock
  ordinary project work.

Projects that intentionally want consensus guidance throughout development can
opt in explicitly:

```yaml
# .consensus/config.yaml
governance:
  mode: continuous
```

Or reconfigure from the terminal:

```bash
consensus-init --reconfigure --non-interactive --governance-mode continuous
```

Only `continuous` mode enables proactive AI guidance and the enforced
design-approval/delivery lifecycle. Reconfigure or repair an older initialized
project to refresh its managed AI instruction block with the new mode-specific
wording.

**See your track record:** `consensus results` prints a project scorecard --
findings by severity, how each was resolved, and convergence rate across runs.

---

## How it works

Every review runs four steps: **author** the contract -> **dispatch** each AI
in read-only mode -> **seal** responses with hashes into an append-only log ->
**verify** the result against the contract before any code is applied. A
background watchdog kills any AI call that stalls. Working state is mirrored
to a separate git branch so a stray `git clean` can't lose history.

Panel size: any count works as long as there are at least two AIs. The only
thing size changes is the default agreement rule (2 AIs: both must agree;
3+: majority) -- overridable per project.

### Rigor tiers

Choose how much work each review should do without dropping any configured
provider. Every tier dispatches all enabled independent AIs (minimum two):

- **Quick** -- newest suitable models at their fastest effort, one round.
- **Standard** -- the same current model generation at normal effort, one round.
- **Deep** -- strongest practical effort and two convergence rounds for hard
  architectural, security, or irreversible decisions. Deep has no automatic
  wall-clock or silence timeout; it runs until completion, provider failure, or
  explicit operator abort. Quick and Standard remain time-bounded.

The tier can be declared through the MCP `consensus.run_iteration` tool or the
CLI:

In an AI-hosted session, no switch is needed. Plain language is the primary
interface: *"let's get a quick consensus"*, *"get a standard consensus on
this"*, or *"we are going nowhere -- get a deep consensus"* are explicit tier
declarations. The host selects the named tier and starts the consensus workflow
automatically. CLI and MCP fields exist for scripts and direct integrations.

```bash
consensus-mcp-run-iteration --rigor-tier deep \
  --iteration-dir consensus-state/active/my-review \
  --goal-packet consensus-state/active/my-review/goal_packet.yaml \
  --target path/to/review-packet.yaml
```

Built-in current-generation defaults include GPT 5.6 Sol, Claude Fable 5,
Gemini 3.5 Flash, and Grok 4.5. Kimi deliberately uses each user's authenticated
CLI default; users with access to Kimi K3 (`kimi-k3`) or another paid model can
pin it locally without imposing that entitlement on other installations.
Provider-specific model and effort settings are recorded in sealed review
provenance.

---

## Maturity

Both modes are stable and in daily use. In **134 review iterations on its own
code**, the panel logged **548 findings -- 156 of them blocking or critical** --
each addressed before the change merged (fixed, or dismissed with the evidence
that disproved it). **2,353 tests green** on Linux + Windows / Python 3.11+,
ASCII-only tree, every reviewer pluggable by config, no Claude required. This
project reviews itself through its own cross-AI cycle.

- [Releases](https://github.com/StGarca/consensus-mcp/releases) /
  [CHANGELOG.md](CHANGELOG.md)
- [Known-issue releases](docs/advisories.md)
- [Build guide](docs/workflows/architect-build.md) -- roles, gates, containment
- [Orchestration spec](docs/architecture/orchestration-spec.md) -- full design
- [Workflow docs](docs/workflows/) -- review modes and when to use each

## Requirements

- Python 3.11+
- [`pipx`](https://pipx.pypa.io/) recommended
- At least two AI CLIs on your PATH. Built-in support for:
  [`codex`](https://github.com/openai/codex),
  [`gemini-cli`](https://github.com/google-gemini/gemini-cli),
  [`grok-cli`](https://docs.x.ai/grok-cli),
  [`kimi-cli`](https://github.com/MoonshotAI/kimi-cli),
  and Claude (when running inside Claude Code) -- all optional.

## License

MIT -- see [LICENSE](LICENSE).

## Contributing

Contributions go through the same cross-AI review cycle -- expect feedback
from more than one model on your change.
