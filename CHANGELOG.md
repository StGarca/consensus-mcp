# Changelog

## 1.13.0 - 2026-05-12

Multi-project resolution: consensus-mcp now boots in any project without a
local checkout. The frozen wheel ships a spec template, and state auto-
initializes in CWD.

**Changes (per iter-0007 codex consult, sealed packet 39f8b7a8b...):**

- New resolvers in `server.py` split the legacy `REPO_ROOT` into three
  concerns: `_resolve_spec_path` (spec source), `_resolve_state_root`
  (`consensus-state/` location), `_resolve_project_root` (reviewable-file
  root for goal_packet `allowed_files`).
- Resolution order for each:
  - Spec: `CONSENSUS_MCP_SPEC_PATH` > legacy `CONSENSUS_MCP_REPO_ROOT` >
    walked-up checkout > shipped `consensus_mcp/spec_template.md`
  - State: `CONSENSUS_MCP_STATE_ROOT` > legacy `CONSENSUS_MCP_REPO_ROOT` >
    `Path.cwd() / "consensus-state"`
  - Project: `CONSENSUS_MCP_PROJECT_ROOT` > legacy `CONSENSUS_MCP_REPO_ROOT`
    > `Path.cwd()`
- `consensus_mcp/spec_template.md` shipped in the wheel (added to
  `pyproject.toml [tool.setuptools.package-data]`). Frozen-wheel users get a
  bootable spec without cloning.
- `_resolve_repo_root` kept for back-compat; no longer load-bearing for
  spec/state/project-root in v1.13.0.

**Deferred to v1.14.0** (per codex Q1 scoping):

- Dispatcher auto-discovery of `iteration_dir/review-packet.yaml` when
  `--review-target` is non-yaml (currently silent-failure, prompt ships
  without embedded touched-file contents).
- Cold-start grace period for the watchdog (pre-first-byte vs.
  post-first-byte silence thresholds; 45s is too aggressive for bigger
  prompts).
- Visibility upgrade: dispatch_heartbeat events should expose a `status`
  field (loading / streaming / stalled) for operator clarity.

**Known pre-existing pytest flake** (predates v1.13.0): 5 tests in
`test_dispatch_codex.py` that pass in isolation but fail in the full suite
due to test-ordering pollution. Not introduced by v1.13.0; tracked
separately.

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
