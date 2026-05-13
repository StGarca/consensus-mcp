# Imported parent project history

This directory contains a one-time import of iteration history from the project consensus-mcp was extracted from (`C:\Users\<you>\Downloads\upstream-26.4.16\agent-loop`).

**Provenance**: see `source-manifest.yaml` for per-entry sha256_tree + content hashes.

## Layout

- `active-iterations/iteration-NNNN/` — mirrors parent's `agent-loop/active/iteration-NNNN/`
- `archive-review-passes/*.yaml` — mirrors parent's `agent-loop/archive/review-passes/`

**This subtree is gitignored** (per the project-wide `.gitignore` for `consensus-state/archive/review-passes/*.yaml`). Durability is provided by the orphan branch `consensus-state-snapshots` (see `consensus_mcp/_snapshot_state.py`).

Imported: 2026-05-13T01:42:45Z
Source: C:\Users\<you>\Downloads\upstream-26.4.16\agent-loop
Standalone extraction commit: ff0164f
