# consensus-state/

Runtime state for the consensus pipeline. **Most of this tree is
gitignored** - it is per-iteration working scratch, not source.

| Path | What it is | Tracked? |
|---|---|---|
| `active/` | Live per-iteration working dirs (goal packets, proposals, sealed reviews, converged plans) | gitignored (only `.gitkeep`) |
| `archive/` | Sealed review-pass archive + one-time upstream-history mirror | gitignored (only `.gitkeep`) |
| `state/` | Dispatch log + audit log + decision ledger | gitignored (only `.gitkeep`) |
| `tests/fixtures/` | Static fixtures consumed by the test suite | **tracked** |

## Recovery

In-flight iteration state is gitignored by design, so a
`git clean -fdX` (or a CI agent that wipes untracked files) can
silently delete in-progress consensus work. It is snapshotted to
the `consensus-state-snapshots` orphan branch after each iteration
close:

```
python -m consensus_mcp._snapshot_state snapshot --label <short-label>
python -m consensus_mcp._snapshot_state restore  --tag <tag-name>
```

## Provenance

consensus-mcp was extracted from an upstream agent-loop project and
restarted as a standalone tool at iteration 0001. The full
per-release history is in [`../CHANGELOG.md`](../CHANGELOG.md);
release/branch policy and the cross-AI workflow doctrine live in
the bundled `consensus-workflow` skill.
