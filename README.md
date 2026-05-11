# consensus-mcp

MCP server enforcing the G1-G5 consensus gates for sealed-review,
multi-agent iteration loops. Designed for workflows where two or more
AI agents (e.g. Claude + Codex) co-review proposed changes and a state
machine mediates which writes are allowed when.

## What it does

`consensus-mcp` exposes a small surface of MCP tools that orchestrate a
five-gate pipeline:

| Gate | Tool                                       | What it enforces |
|------|--------------------------------------------|------------------|
| G1   | `review.write_and_seal`                    | Sealed-review provenance: a review yaml can only be sealed once, and after sealing no further edits land |
| G2   | `patch.stage_and_dry_run`                  | Dry-run validator suite passes before any apply |
| G3   | `repo.get_section` / `repo.set_section`    | Mediated single-writer access to docs sections |
| G4   | `gate.evaluate_production_with_scope_match`| Production readiness requires intra-file scope match against the implementation_scope declaration |
| G5   | `state.update_decision_ledger`             | Only the MCP server may mutate the decision ledger |

Plus a Phase 4 supervisor surface:

- `reviewer.dispatch_codex` -- dispatch a codex reviewer subprocess with a sealed goal-packet
- `loop.run_goal` -- run one iteration of the codex-fix-author loop
- `loop.verify_codex_patch` -- verify a codex-produced patch against the proposal it was supposed to apply
- `apply.codex_patch` -- the only authorized writer for codex-produced patches

## Installation

```bash
pip install consensus-mcp
```

Or from source:

```bash
git clone https://github.com/<user>/consensus-mcp.git
cd consensus-mcp
pip install -e .
```

## Quickstart

`consensus-mcp` needs a repo root with two directory markers:

- `consensus-state/` -- runtime state (decision ledger, audit log, iteration dirs)
- `consensus_mcp/` -- this package (when running from source)

The server resolves repo root by walking up from `__file__`. To override
(e.g. when running as an installed wheel), set:

```bash
export CONSENSUS_MCP_REPO_ROOT=/path/to/your/project
```

Then boot the server (one-shot, for smoke testing):

```bash
consensus-mcp --boot-and-exit
```

The boot gate runs `validate_disposition_index` against the spec at
`docs/architecture/orchestration-spec.md`. If the spec has findings,
the server refuses to start (exit 2).

For long-running stdio mode (the actual MCP protocol):

```bash
consensus-mcp
```

## Documentation

Architecture and design docs live under `docs/`:

- `docs/architecture/orchestration-spec.md` -- the hub specification (the file the server's boot gate validates)
- `docs/architecture/autonomy-contract.md` -- what the supervisor is and is not allowed to do
- `docs/architecture/phase-1-completion.md` -- Phase 1 G1+G2 build summary
- `docs/architecture/visibility-tui-design.md` -- the visibility TUI
- `docs/architecture/codex-fix-author-roadmap.md` -- the codex-fix-author work history
- `docs/postmortems/iter-0019-0036-failures.md` -- recent-iteration failures (cautionary)
- `docs/workflows/workflow-4-preferred.md` -- preferred operator workflow
- `docs/workflows/codex-fix-author-directive.md` -- the codex fix-author directive

## Running tests

```bash
pip install -e .
python -m pytest consensus_mcp/tests/ -q
```

Validator self-tests:

```bash
python consensus_mcp/validators/run_validator_tests.py
```

In-tree smoke test:

```bash
python -m consensus_mcp._smoke_test
```

## Layout

```
consensus_mcp/                  # this package
  server.py                     # MCP stdio server entry point
  _dispatch_codex.py            # codex subprocess dispatcher (CLI-only)
  _smoke_test.py                # in-tree smoke suite
  _release_gate_check.py        # release-gate runner
  tools/                        # MCP tool implementations (T2-T11)
  validators/                   # Phase 0 validator scripts (V0-V6)
  dispatch_templates/           # codex review template + JSON schema
  tests/                        # pytest suite
  docs/                         # internal docs shipped with the package

consensus-state/                # runtime state (created at runtime)
  active/                       # active iteration dirs
  archive/review-passes/        # sealed review yamls
  state/                        # decision ledger, audit log, dispatch log
  tests/fixtures/               # validator test fixtures

docs/                           # repo-level documentation
  architecture/
  postmortems/
  workflows/
```

## License

MIT. See `LICENSE`.
