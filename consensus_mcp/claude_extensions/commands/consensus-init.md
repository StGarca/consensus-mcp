---
description: Bootstrap consensus-mcp in this project (writes .mcp.json + .consensus/config.yaml via the consensus-init shell binary)
---

Run `consensus-init --from-claude-code` via Bash from the current working
directory. Surface the binary's stdout/stderr verbatim — it already prints
the correct next-step guidance, including the restart instructions for
Claude Code.

If the binary is not on PATH, tell the user to run
`pipx install consensus-mcp` once globally and retry. If the bootstrap
succeeds, confirm the binary's restart message to the user and stop —
the consensus-mcp MCP tools will not be available until Claude Code
reloads.

**Already configured:** if the binary exits with code 4 and the first stdout
line is exactly `STATUS: already-configured`, the project is already set up. Do
not surface the raw error — consume that token line and present four options
via `AskUserQuestion` (leave as-is / verify/repair / reconfigure / force
overwrite), then re-invoke `consensus-init --from-claude-code --repair`,
`--reconfigure`, or `--force` once as appropriate (one-shot; "leave" does
nothing). The `--repair` flag re-creates missing pieces and reports diverged
ones non-destructively.

**Workspace umbrella:** if the binary exits with code 8 and the first stdout
line is exactly `STATUS: looks-like-workspace-umbrella`, the current directory
is a workspace folder containing git sub-projects — bootstrapping it directly
would blanket every sub-project. Do not surface the raw error. Instead, consume
the token line and present options via `AskUserQuestion`: one entry per child
project named in stderr (capped at ~10) to initialize that project by re-running
`consensus-init --from-claude-code` from inside it, plus **Initialize here
anyway** (re-run with `--here`) and **Cancel**. Act on the choice one-shot;
the resolved flag or changed directory suppresses the token so no loop occurs.

Do not reimplement any of `consensus-init`'s logic. The binary handles
`.mcp.json` writing, `.consensus/config.yaml` creation, `.gitignore`
managed block, and Claude-Code-specific restart messaging.
