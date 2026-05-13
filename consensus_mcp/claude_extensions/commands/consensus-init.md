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

Do not reimplement any of `consensus-init`'s logic. The binary handles
`.mcp.json` writing, `.consensus/config.yaml` creation, `.gitignore`
managed block, and Claude-Code-specific restart messaging.
