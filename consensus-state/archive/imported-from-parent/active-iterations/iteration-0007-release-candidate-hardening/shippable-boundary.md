---
title: Phase 3 RC Shippable Boundary Manifest
date: 2026-05-09
type: release-boundary-spec
status: draft
related_iteration: iteration-0007-release-candidate-hardening
audience: ai-only-plus-operator-review
---

# Phase 3 RC Shippable Boundary Manifest

Per operator's Phase 3 outline step 4: decide what ships, what does not. This manifest is the authoritative source for `pyproject.toml` package-data + `.gitattributes` export-ignore rules + the operator's manual `git copy out` move when extracting the standalone tool.

## Ships (canonical core)

### A. MCP server + tools (the core product)

```
scripts/agent_loop_mcp/
├── __init__.py
├── server.py                          # stdio JSON-RPC + tool registry
├── tool_registry.py
├── _smoke_test.py                     # 51/51; ships as test harness for the installed package
└── tools/
    ├── __init__.py
    ├── _md_sections.py                # shared section parser (G3)
    ├── audit_append_event.py          # T3 / G1 partial
    ├── gate_evaluate_production_with_scope_match.py  # T11 / G4
    ├── patch_apply_consensus_patch.py # T5 / G2
    ├── patch_stage_and_dry_run.py     # T4 / G2
    ├── repo_get_section.py            # T9 / G3
    ├── repo_set_section.py            # T10 / G3
    ├── review_read_post_seal.py       # T7 / G1
    ├── review_write_and_seal.py       # T6 / G1
    ├── state_read_decision_ledger.py  # T2 / G5
    └── state_update_decision_ledger.py # T8 / G5
```

### B. Required validators (boot-time + dry-run gate dependencies)

```
scripts/agent_loop/
├── __init__.py
├── validate_disposition_index.py      # boot validator + state.update_decision_ledger gate
├── validate_iteration.py              # T4 default validator
├── validate_consensus.py              # T4 default validator
├── validate_review.py                 # T4 default validator
├── consensus_gate.py                  # production_state evaluation (T11 augments; doesn't replace)
├── scope_check.py                     # consumed by T4 implicitly via validate_iteration
└── build_review_packet.py             # builds review packets for reviewers
```

NOTE: `run_validator_tests.py` ships as the test harness counterpart to `_smoke_test.py`.

### C. Minimal state schema docs

Two MD files describing the contract surface the tool enforces:

```
docs/
├── README.md                          # NEW (P3 T5); install + usage + tool inventory
├── tool-reference.md                  # NEW (P3 T5); per-tool input/output schemas
└── state-schema.md                    # NEW (P3 T5); minimal consensus.yaml + iteration-dir layout
```

These are derived from the design spec sections 9, 13, 16, 17 — extracted into operator-readable form without the full agent-loop self-construction history.

## Does NOT ship

### A. Historical narrative + project memory

```
wiki/                                  # ENTIRE TREE — wiki narrative is audiobook-project-specific
├── audiobook-project/                 # all spec md, design spec, summary, findings docs
├── log.md                             # gitignored anyway (local operator log)
├── hot-cache.md                       # project-specific
├── characters/
├── concepts/
├── books/
└── ... (all other wiki content)
```

### B. Iteration archives (historical evidence of internal use; not for shippable product)

```
agent-loop/archive/                    # 22 sealed review-pass packets pre-dating the shippable tool
agent-loop/active/iteration-0000/      # internal validation history
agent-loop/active/iteration-0001/
agent-loop/active/iteration-0002/
agent-loop/active/iteration-0003/
agent-loop/active/iteration-0004/
agent-loop/active/iteration-0005-cf-007-readiness-flip/
agent-loop/active/iteration-0006/
agent-loop/active/iteration-0007-release-candidate-hardening/
```

NOTE: a future v1.10.x might ship a single example iteration (iteration-0006 OR iteration-0007) as a usage demo. v1.9.3 RC does not.

### C. State files that mutate at runtime

```
agent-loop/state/
├── disposition-ledger.yaml            # project-specific 1300+ line ledger
├── mcp-server-audit.jsonl             # generated; runtime artifact
└── *-report.yaml                      # 5 validator-run-output reports; regenerated on every run
```

The shipped tool reads/writes these paths (via T2 / T3 / T4 / T8) but doesn't carry the project's specific ledger content. A clean install creates an empty `agent-loop/state/disposition-ledger.yaml` with the minimal seed schema OR points the tool at an operator-supplied path via env var.

### D. Audiobook project artifacts

```
audiobooks/                            # rendered audio
ebooks/                                # source ebooks
.raw/                                  # ebook extractions
listen/                                # operator-facing renders
run/                                   # render orchestration scripts
lib/                                   # render-side primitives
fish-speech/                           # TTS engine
applio/                                # RVC voice conversion
python_env/                            # local venv
```

### E. Generated + cache files

```
**/__pycache__/
**/*.pyc
.pytest_cache/
run/pytest-of-<you>/                   # pytest temp output
agent-loop/state/*-report.yaml         # validator runtime reports
agent-loop/state/iteration-*-staging/  # tempfile staging dirs (cleaned per-run)
```

## Boundary integrity check

The operator's manual `git copy out` move would be:

```bash
mkdir agent-loop-mcp-extracted/
cp -r scripts/agent_loop_mcp/   agent-loop-mcp-extracted/scripts/agent_loop_mcp/
cp -r scripts/agent_loop/       agent-loop-mcp-extracted/scripts/agent_loop/
cp pyproject.toml               agent-loop-mcp-extracted/
cp -r docs/                     agent-loop-mcp-extracted/docs/
# Verify nothing else needed:
cd agent-loop-mcp-extracted
python -m build
python -m venv .venv && .venv/bin/pip install dist/*.whl
.venv/bin/python -m agent_loop_mcp._smoke_test  # MUST be 51/51 from clean env
```

If the smoke test fails post-install: the boundary has a hidden coupling that needs fixing. P3 T5 surfaces these via clean-env install test BEFORE extraction.

## Open questions for reviewers

- Should the shipped `_smoke_test.py` exercise paths the original test relied on (`agent-loop/state/disposition-ledger.yaml`, `agent-loop/archive/review-passes/index.yaml`)? If so, the smoke needs a clean-env `setUp` that creates these paths fresh; if not, the test count drops post-install.
- Should `docs/` be markdown-only or include a generated `tool-schemas.json` from each tool's `SCHEMA["input_schema"]` + `SCHEMA["output_schema"]`?
- Should the wheel include a `data_files=[(...)]` mapping that auto-creates a `~/.agent-loop-mcp/` runtime dir on install?

These open questions are for P3 T5 to resolve; not blocking for P3 T4.

## Status

DRAFT pending dual-reviewer ratification (P3 T3 in progress) + clean-env install test verification (P3 T5).
