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
not surface the raw error — consume that token line and present three options
via `AskUserQuestion` (leave as-is / reconfigure / force overwrite), then
re-invoke `consensus-init --from-claude-code --reconfigure` or `--force` once
(one-shot; "leave" does nothing).

Do not reimplement any of `consensus-init`'s logic. The binary handles
`.mcp.json` writing, `.consensus/config.yaml` creation, `.gitignore`
managed block, and Claude-Code-specific restart messaging.
