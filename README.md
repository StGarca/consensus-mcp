# consensus-mcp

**Don't let one AI be the only judge of its own work.**

consensus-mcp asks several different AIs to review the same change, each on
its own, without seeing the others' answers. Your main AI (the "host") then
reads every review, keeps the good ideas, rejects the wrong ones with a
reason, and writes down every decision. The same reviewers then check the
host's plan, so the host never has the final word on its own choices.
Nothing is decided by a vote, and every review is sealed with a fingerprint
so the record can't be quietly changed later.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)

---

## What's new in 2.5

| Before (2.4) | Now (2.5) |
| --- | --- |
| Behind the scenes, the host AI juggled a separate background job per reviewer and had to hold Kimi back until the others finished | **One call runs every reviewer in parallel**, Kimi included (`consensus-mcp-consult`), with a time limit per reviewer and for the whole panel, plus one summary of who ran which model |
| If a model ran out of quota, that reviewer just failed | **Automatic fallback** to the next model in the same seat (e.g. Fable 5.2 -> Opus 5.5) |
| Reviewers were sent whole files and every earlier round again | **Only the changes are sent**, within a size budget; one real review went from about 51k to 17k tokens |
| Approval only checked that enough reviews existed | **Approval requires a decision on every idea** (accepted, rejected or deferred, with a reason) |
| The host's choice of ideas was never checked | **The reviewers review the host's plan.** The host can accept their objections but can't overrule one; you decide anything still disputed |
| No record of which model was actually useful | **A per-model scorecard**: ideas raised, accepted, proven wrong, and failures by type |
| Every failure looked like "timed out" | **Failures are named**: quota, rate limit, login, timeout, bad output, crash |
| Long reviews could block the MCP server | **Long reviews run as background jobs** that survive a restart and can be cancelled |
| Four separate copies of the reviewer-launching code (about 5,900 lines) | **One shared runner** (about 1,700 lines) |
| Fixed, older models | **Current models**, a free self-hosted option, and any OpenAI-compatible server |
| Nothing stopped private code reaching any reviewer | **Asks before sensitive projects reach Kimi or Qwen**, and leaves them out when the answer is yes |

2.5 also fixes the bugs found during an independent five-AI review and live
testing, including one where a consult with zero reviews could report
success. See [CHANGELOG.md](CHANGELOG.md) for the full list.

---

## How a review works

1. **Ask.** "Get a consensus review on this change", or run
   `consensus-mcp-start-consult --question "..." --scope-glob "<files>"`.
2. **Review.** `consensus-mcp-consult --iteration <id>` sends the change to
   every reviewer at once. Each works alone. A reviewer that crashes or runs
   out of quota never blocks the others, and never counts against the change.
3. **Cherry-pick.** The host reads the sealed reviews, records one decision
   per idea (*accepted*, *rejected* with proof, or *deferred*) and writes one
   plan from the best ideas.
4. **Check the plan.** `consensus-mcp-consult --iteration <id> --plan-pass`
   sends that plan and the host's decisions back to the same reviewers. They
   object if the host dropped a good idea, kept a bad one, or the pieces
   don't fit together. The host can accept an objection, but it can't
   overrule one: anything it disagrees with comes to you. If the host edits
   the plan to fix accepted objections, the reviewers quickly confirm the
   edit does only that (`--confirm-pass`); any "no" comes to you.
5. **Approve.** `consensus-mcp-approve` checks that every idea and every
   objection is settled, seals the result, and only then allows edits to the
   files in scope.
6. **Learn.** `consensus results` shows how each model has done over time.

### Pick how thorough

- **Quick**: two reviewers, one pass. Good for small changes.
- **Standard** (default): the full panel, two passes - ideas, then one review
  of the host's plan (plus a quick check of the host's edits, if it made any).
- **Deep**: the full panel (plus a tool-using reviewer that explores the whole
  project), and the plan is reviewed again and again until no reviewer
  objects. After 5 rounds it stops and shows you who is still holding out
  and why; you accept the objection, dismiss it, or send it back for up to 5
  more rounds.

### The default panel

| Seat | Model | If it runs out of quota |
| --- | --- | --- |
| Claude | Fable 5.2 | Opus 5.5 |
| Codex (OpenAI) | GPT-6 Astra | GPT-6.1 Sol |
| Kimi (see below) | K3.1 | |
| Grok | Grok 4.7 | |
| Gemini (Antigravity `agy`) | Gemini 4 Argon High | |
| DeepSeek (optional) | DeepSeek V4.1 Flash on your own server | |
| Qwen (optional, see below) | Qwen4 Max Preview (Alibaba Cloud) | |

DeepSeek joins automatically when you point
`CONSENSUS_MCP_DEEPSEEK_BASE_URL` at an OpenAI-compatible server, and Qwen
when you set `CONSENSUS_MCP_QWEN_BASE_URL` and `CONSENSUS_MCP_QWEN_API_KEY`.

**Sensitive projects.** Kimi and Qwen never review sensitive code or data.
Before a review that includes them, consensus-mcp asks whether the project
contains sensitive data (private, personal, customer or company-confidential).
Answer **yes** and both sit that review out; answer **no** and they take part.
DeepSeek is trusted on your own server or a provider outside China and
Singapore; any reviewer pointed at DeepSeek's own API or another Chinese or
Singapore provider is treated like Kimi and Qwen.
To stop being asked, put `sensitive_data: yes` or `sensitive_data: no` in the
project's `.consensus/config.yaml`.

You can
change any seat, or add another AI, with a short profile file; no code change
is needed. Claude is optional.

---

## Quick start

```bash
pipx install git+https://github.com/StGarca/consensus-mcp.git@v2.5.2
consensus-init --install-claude-code
```

Then just ask your AI in plain language. If the project isn't set up yet,
it asks which reviewers you want and sets things up. Prefer the terminal?
Run `consensus-init`; it finds the AI tools you already have and prints
install and login steps for any you pick that are missing.

**It stays out of your way.** Installing it does not switch it on everywhere.
By default (`on-demand`) it only runs when you ask for a review. Set
`governance: mode: continuous` in `.consensus/config.yaml` if you want it to
guard every change in a project.

**Build mode** (optional): a top-tier "architect" model plans and signs off,
a cheaper "builder" model writes the code in an isolated copy, and a reviewer
checks each step. You approve the plan and the final merge. See the
[build guide](docs/workflows/architect-build.md).

---

## Requirements

- Python 3.11+ (installing with [`pipx`](https://pipx.pypa.io/) is easiest)
- At least two AI command-line tools, signed in. Built in:
  [Codex](https://github.com/openai/codex),
  [Kimi](https://github.com/MoonshotAI/kimi-cli),
  [Grok](https://docs.x.ai/grok-cli),
  Antigravity (`agy`) for Gemini, Claude Code, and any OpenAI-compatible
  server. All are optional.

**Windows tips:** run the tests without `PYTHONPATH` set, and start OpenCode
from PowerShell.

## Status

2,671 automated tests pass. In 2.5 every reviewer seat was also checked
against the real models: all six sealed a live review of consensus-mcp's own
code, and the automatic quota fallback switched Fable 5.2 to Opus 5.5 by
itself.

- [Releases](https://github.com/StGarca/consensus-mcp/releases) and
  [CHANGELOG.md](CHANGELOG.md)
- [Hosting a consult](docs/operations/hosting-a-consult.md), step by step
- [Known issues](docs/advisories.md)
- [Full design](docs/architecture/orchestration-spec.md)

## License

MIT; see [LICENSE](LICENSE).

## Contributing

Changes go through the same multi-AI review, so expect feedback from more
than one model.
