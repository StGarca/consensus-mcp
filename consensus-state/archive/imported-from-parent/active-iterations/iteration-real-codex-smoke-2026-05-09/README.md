---
title: Real-codex smoke iteration
date: 2026-05-09
type: smoke-test
status: passed_end_to_end_real_codex_smoke
purpose: First end-to-end real-codex invocation through `_dispatch_codex` helper. Validates v1.10.2+ hardening works against actual codex CLI 0.129.0, not just mocked subprocess. Precondition for v1.1.x MCP wrapper followup.
---

# Real-codex smoke iteration (2026-05-09)

## What this is

A SMOKE test: prove the pipeline works end-to-end with actual codex, not a production review. Output is intentionally minimal. The smoke fixture goal_packet has a placeholder scope, but `run-smoke.ps1` now passes an explicit `--review-target` so the test exercises the intended canonical review-target path rather than the F6 fallback path.

## NOT what this is

- Not a production review (use real goal_packet + --review-target for that)
- Not part of the agent-loop quorum loop (no claude review counterpart; no consensus; no T6 dual-seal ceremony beyond `_dispatch_codex`'s own T6 call)
- Not gating any spec/ledger change (unlike iter-0006/0007/0008-pilot)

## Operator command

```powershell
.\agent-loop\active\iteration-real-codex-smoke-2026-05-09\run-smoke.ps1
```

Expected exit code: 0
Expected outputs:
- `agent-loop/active/iteration-real-codex-smoke-2026-05-09/codex-review.yaml` (sealed YAML mirrored from T6 archive)
- `agent-loop/state/dispatch-log.jsonl` (appended): one `dispatch_start` event + one `dispatch_done` event with full provenance (codex_version, prompt_sha256, output_sha256, schema_sha256, goal_packet_sha256, scope_signature, packet_sha256, etc.)
- `agent-loop/archive/review-passes/2026-05-09-iteration-real-codex-smoke-2026-05-09-codex-real-smoke-1-pass.yaml` (T6 archive copy)

## Possible failure modes

1. **Codex auth not configured** → `codex exec` fails with auth error → `CodexInvocationError` → rc=1.
2. **Codex network failure** → timeout after 300s → `CodexInvocationError` → rc=1.
3. **Codex emits non-conforming JSON** despite `--output-schema` constraint → `CodexOutputParseError` → rc=1. (This would itself be an interesting v1.10.x finding.)
4. **T6 archive index collision** → if a prior test or smoke used the same pass_id → `index_collision` error from T6 → rc=2.
5. **Helper hangs** → not expected at 300s timeout but flag if observed.

## 2026-05-09 codex check notes

- Initial real-codex invocation reached Codex but failed before model output because `codex_review_schema.json` did not include every top-level property in `required`; current source and installed `python_env` schema now include `findings`, `goal_satisfied`, `goal_satisfied_rationale`, and `blocking_objections`.
- After schema correction, the smoke reached T6 and created the deterministic archive path for `codex-real-smoke-1`; repeated runs with the same reviewer id failed with `packet_path_collision`, which is expected T6 overwrite protection.
- `run-smoke.ps1` now derives repo root from the script location and generates timestamped reviewer/pass ids so repeated smoke runs on the same day do not collide with the existing archive path.
- Post-fix verification run passed end-to-end with reviewer `codex-real-smoke-20260509-234829-930`, pass `codex-real-smoke-20260509-234829-930-pass1`, archive `agent-loop/archive/review-passes/2026-05-09-iteration-real-codex-smoke-2026-05-09-codex-real-smoke-20260509-234829-930-pass.yaml`, and packet sha `6734cc0f9dc5879954027302e23c18b089ff13a9138989a49c2925829848312c`.

## After operator runs

Report results here. The orchestrator (claude) will:
- Read the sealed `codex-review.yaml`
- Read the dispatch_done event from `dispatch-log.jsonl`
- Verify the 11 audit fields are populated
- Verify the dispatch_provenance block (F5) is in the sealed packet
- Cross-check: codex's actual output format vs the schema we expected
- Surface any surprises as v1.10.3+ candidates (or accept as clean)
