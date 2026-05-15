---
name: consensus
description: Bootstrap or check consensus-mcp in this project. Trigger when the user says "consensus init", "bootstrap consensus", "set up consensus", "initialize consensus-mcp", or asks to install/enable the cross-AI consensus workflow in their current project.
---

# Consensus-mcp project bootstrap

This skill is the entry point for setting up the **consensus-mcp** cross-AI
consensus workflow in a project. It is invoked when the user expresses intent
to initialize consensus-mcp — typically by typing phrases like
"consensus init", "set up consensus", or "bootstrap consensus" without a
leading slash.

## What it does

1. Run the shell binary `consensus-init --from-claude-code` (installed by
   `pipx install consensus-mcp`).
2. Surface the binary's stdout/stderr verbatim — `consensus-init` already
   prints the right next-step guidance, so don't paraphrase or summarize.
3. If `consensus-init` fails because the binary is not on PATH, tell the
   user to run `pipx install consensus-mcp` once globally.
4. After a successful bootstrap, point the user at the restart step
   (Claude Code must reload to pick up the new MCP server). The CLI output
   already says this; just confirm.

## How to invoke

Use Bash to run `consensus-init --from-claude-code` from the current
working directory. The binary detects the project root automatically
(git rev-parse → strong markers → cwd, per iter-0031).

## Workflow modes the operator can pick (v1.14.4+)

The wizard will prompt for `--workflow`. Three modes plus advisory:

- **Workflow A** = `propose-converge` (DEFAULT) — all contributors
  propose blindly, then converge across reviewed rounds. Use for
  design questions where reasonable people could disagree.
- **Workflow B** = `post-review` — one AI implements, others audit.
  Lightweight; use for execution per a converged design or hot-patches.
- **Workflow C** = `autonomous-execute` (NEW) — runs to completion
  overnight without operator-in-the-loop, auto-approving emergent
  scope items within an operator-pre-declared `autonomy_contract`.
  v1.14.4 ships the CONTRACT (validators, scope_check, halt-set,
  audit-ledger); the multi-iteration engine ships in v1.15.0
  (named blocker). Operators can stage and validate Workflow C
  goal_packets in v1.14.4+; running them surfaces a clear
  `NotImplementedError` pointing at v1.15.0.
- **Advisory** — dispatches happen but no vote is load-bearing. Rare.

Numeric aliases (3, 4) still resolve at the CLI but emit a
`DeprecationWarning`; will be removed in a future minor release.

For the operating procedures (when to use which workflow, how to
dispatch peers, halt conditions, etc.) the bootstrap pack also
installs `~/.claude/skills/consensus-workflow/SKILL.md` — that
skill is the load-bearing reference and triggers automatically
on workflow-execution intent.

## What NOT to do

- Don't reimplement any of `consensus-init`'s logic. It writes
  `.consensus/config.yaml`, `.mcp.json`, and a `.gitignore` managed
  block — let the binary handle all of that.
- Don't paraphrase the binary's output. Operators rely on the exact
  next-step text from the CLI.
- Don't run if the user is asking a conceptual question
  ("what is consensus-mcp?", "how does consensus work?") — that is
  not a bootstrap intent.

## Expected output flow

```text
$ consensus-init --from-claude-code
.mcp.json written: <project-root>/.mcp.json
.consensus/config.yaml written: <project-root>/.consensus/config.yaml
.gitignore managed block added.

Detected --from-claude-code: Claude Code must reload to activate the
consensus-mcp server. Either restart Claude Code in this project
(Ctrl-C, then `claude code`), or run `/mcp` to reload MCP servers
if your build supports it.
```

After running, confirm the message to the user and stop. Do not
attempt to invoke any consensus-mcp MCP tool in the same session —
they won't be available until Claude Code reloads.
