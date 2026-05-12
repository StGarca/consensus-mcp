# Changelog

## 1.12.0 - 2026-05-11

Standalone release. Extracted from ebook2audiobook-26.4.16 source
project. See `docs/architecture/codex-fix-author-roadmap.md` for the work
history that led here.

Renames vs the prior internal-only releases (1.0.0 - 1.11.0):

- Python package `agent_loop_mcp` -> `consensus_mcp`
- Python package `agent_loop` (validators) -> `consensus_mcp.validators`
- State directory `agent-loop/` -> `consensus-state/`
- Env var `AGENT_LOOP_MCP_REPO_ROOT` -> `CONSENSUS_MCP_REPO_ROOT`
- MCP server name `agent-loop-mcp` -> `consensus-mcp`
- Repo-root markers updated to `("consensus-state", "consensus_mcp")`
- Flat layout (no `scripts/` prefix)
