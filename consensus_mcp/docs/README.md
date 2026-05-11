# consensus-mcp

MCP server enforcing the consensus pipeline G1-G5 gates for sealed-review iteration
ceremonies. Provides the 10-tool surface (T2-T11) over JSON-RPC 2.0 stdio.

## What it does

Five gates are enforced before any iteration artifact lands on disk:

- G1 - sealed-review provenance (pass-id + sha256, T6 + T7)
- G2 - canonical-006 dry-run on staged patches (T4)
- G3 - intra-file scope check on spec md sections (T9 + T10)
- G4 - production-scope-match against consensus (T11)
- G5 - mediated ledger updates with full validator pre-check (T8)

T2 (state.read_decision_ledger), T3 (audit.append_event), T5 (patch.apply_consensus_patch)
round out the 10-tool surface.

## Install

```
pip install -e .
```

The wheel ships:

- `consensus_mcp` - server + 10 tools
- `consensus_mcp.tools` - tool implementations
- `consensus_mcp.validators` - validator dependencies (V0-V6)

## Usage

```
# Boot then exit (smoke check)
CONSENSUS_MCP_REPO_ROOT=/path/to/repo consensus-mcp --boot-and-exit

# Long-running stdio JSON-RPC (consumed by an MCP client)
CONSENSUS_MCP_REPO_ROOT=/path/to/repo consensus-mcp
```

`CONSENSUS_MCP_REPO_ROOT` must point at the source repo so the boot-time
disposition check can read the canonical spec md and the tools can find
iteration directories. When run from the source tree directly, the env var
is optional (defaults to in-tree discovery).

## Tool inventory

| Tool                                            | Gate | Purpose                                |
|-------------------------------------------------|------|----------------------------------------|
| state.read_decision_ledger                      | -    | Read current ledger + canonical SHA    |
| audit.append_event                              | -    | Append iteration audit event           |
| patch.stage_and_dry_run                         | G2   | Stage + dry-run validators on patch    |
| patch.apply_consensus_patch                     | G2   | Apply patch iff dry-run approved       |
| review.write_and_seal                           | G1   | Seal review packet, append to index    |
| review.read_post_seal                           | G1   | Verify packet provenance via SHA       |
| state.update_decision_ledger                    | G5   | Validate-then-write ledger update      |
| repo.get_section                                | G3   | Read named section of spec md          |
| repo.set_section                                | G3   | Write section (round-trip safety)      |
| gate.evaluate_production_with_scope_match       | G4   | Production-readiness gate              |

See `tool-reference.md` for input/output schemas and example calls.

## State files

Per-iteration artifacts live under `consensus-state/active/iteration-NNNN-*/`.
Sealed review packets land in `consensus-state/archive/review-passes/`. The
disposition ledger is `consensus-state/state/disposition-ledger.yaml`. See
`state-schema.md` for required fields.

## Smoke test

```
python -m consensus_mcp._smoke_test
```

51/51 tests pass on the source tree; the same count must hold from the
installed package (with CONSENSUS_MCP_REPO_ROOT pointing at source repo).

## Release gate check

```
python -m consensus_mcp._release_gate_check
```

Runs all 9 release gates (G_smoke + G_validators + G_frontmatter +
G_unstaged + G_untracked_pkg + G_install + G_install_smoke +
G_server_starts + G_real_iter). Exit 0 iff all pass. Required pre-ship.
