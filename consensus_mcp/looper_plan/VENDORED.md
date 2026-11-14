# Vendored: ksimback/looper (design-coach slice)

- Upstream: https://github.com/ksimback/looper
- License: MIT (c) Kevin Simback
- Pinned commit: ab39948ced6512be56d43d03bba63025a96a7901
- Vendored: 2026-06-23 (consult iteration-looper-plan-design-2026-06-23)

## Kept verbatim
- references/goal-rubric.md         -> rubrics/goal-rubric.md
- references/verification-rubric.md -> rubrics/verification-rubric.md
- references/council-rubric.md      -> rubrics/council-rubric.md
- references/control-rubric.md      -> rubrics/control-rubric.md

## Vendored with minimal localization
- schemas/loop.v1.schema.json       -> schemas/loop.v1.schema.json
- schemas/loop.resolved.v1.schema.json -> schemas/loop.resolved.v1.schema.json
  (ONLY change: the `$id` URL localized to `consensus-mcp/looper-plan/*` so the
  package carries no external link outside this attestation; schema body
  unchanged.)

## Trimmed derivative
- scripts/looper.py -> compile.py: kept load_yaml, to_jsonable, normalize_argv,
  criteria_by_id, validate_member, validate_gate, normalize_spec, clip, ascii_box,
  render_ascii_diagram, render_loop, DEFAULT_REDACTIONS, LooperError. DROPPED:
  detect-models / register-model / MODEL_PROBES / registry I/O, run_probe,
  render_session_prompt, load_json / write_json, the argparse CLI (build_parser,
  cmd_*, main), and the external run-loop.py runner.

## Not vendored (Build supersedes)
- run-loop.py external runner; RUN_IN_SESSION.md handoff; model registry
  (~/.looper/models.json); privacy/egress machinery; single-judge council runtime.

## New code (ours, not upstream)
- compile.synthesize_stub_fields: fills the loop.v1 schema's required-but-unused
  fields (host/council/gates/workspace/execution) from Build roles so a
  goal/verification/caps-only coached spec validates. Never executed.
- seed.py: maps a validated loop.resolved.json into Consensus Build inputs
  (problem.md, looper-suggestions.yaml, looper-plan-manifest.yaml).
