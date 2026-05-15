# iteration-0009-mcp-wrapper-v1-1-x — RESULTS

**Date:** 2026-05-09 / 2026-05-10 UTC
**Closing state:** `quorum_close_passed`
**Closure form:** FULL quorum ceremony (sealed reviews + consensus + T6 archive + T7)
**Operator scope:** "we want to test our agent-loop mcp on a real world scenario"

---

## TL;DR

First iteration in this project to close by the FULL autonomy-contract ceremony rather than gate-evidence-only. Drove the v1.1.x MCP wrapper for `_dispatch_codex` end-to-end. Both reviewers (claude opus + codex CLI) independently emitted `goal_satisfied=true` with **zero blocking objections**. All 5 acceptance gates green. 69/69 pytest. Cross-vendor reviewer pass (Anthropic + OpenAI) achieved for the first time.

---

## Scope

Add `scripts/agent_loop_mcp/tools/reviewer_dispatch_codex.py` — the thin MCP wrapper that exposes the proven `_dispatch_codex` helper as MCP tool `reviewer.dispatch_codex`, register it in `server.py`, and ship behavior-level pytest coverage.

**Anti-scope (forbidden_files):**
- `scripts/agent_loop_mcp/_dispatch_codex.py` — the helper itself, untouched
- `scripts/agent_loop_mcp/_self_drive.py`
- `scripts/agent_loop_mcp/dispatch_templates/`
- `scripts/agent_loop/`, `run/`, `wiki/`

**Goal-packet sealed signature:** `bf0febb01c9f6a995342688658ce514b29a336268222959742e16ea4db0ab3eb`

---

## Verdict

| | |
|---|---|
| `can_close` | **true** |
| `validate` | true (scope_signature_match=true) |
| `stop_rules` | none fired |
| `acceptance_gates` | **5/5** pass |
| `scope` | in_scope=true; 0 out_of_scope |
| `consensus_state` | `implementation_ready` |

### Reviewer alignment

| Reviewer | Method | Vendor | `goal_satisfied` | Blocking | Findings |
|---|---|---|---|---|---|
| claude-iter0009-1 | subagent dispatch (opus) | Anthropic | **true** | 0 | 4 low non-blocking |
| codex-iter0009-1 | helper-dispatched CLI (sealed via T6) | OpenAI | **true** | 0 | 0 |

**Independence:** Each reviewer received only the sealed `review-packet.yaml` plus repo source paths. No orchestrator verdict was passed. Claude did not see the codex review and vice versa. Each review-pass attests to this in its `independence_attestation` block.

### Acceptance gates

| Gate | Description | Result |
|---|---|---|
| A1 | `tools/reviewer_dispatch_codex.py` exists | pass (rc=0) |
| A2 | `server.py` registers the tool | pass (rc=0) |
| A3 | pytest `test_reviewer_dispatch_codex.py` (17 cases) | pass — 17 in 0.05s |
| A4 | pytest `test_dispatch_codex.py` regression | pass — 52 in 0.35s |
| A5 | server `tools/list` includes `reviewer.dispatch_codex` | pass (rc=0) |

Operator-form invocation requires `AGENT_LOOP_MCP_REPO_ROOT=<repo>` to override the installed-wheel `_self_drive.py` at `python_env/Lib/site-packages` (whose `repo_root` walks land at `python_env/Lib`). With the env var set, source-tree code path runs and gates pass.

---

## Execution timeline

| UTC | Step | Outcome |
|---|---|---|
| ~01:50 | Iteration scaffolding (`goal_packet.yaml` + `input.yaml`) | scope_signature recomputed twice (CMD vs bash check syntax fix); `validate` accepted |
| ~01:53 | RED: `tests/test_reviewer_dispatch_codex.py` authored | `ImportError` confirmed (module did not exist) |
| ~01:54 | GREEN: `tools/reviewer_dispatch_codex.py` implemented (145 LOC) | 17/17 pass |
| ~01:55 | `server.py` registration applied (+2 lines) | server boot probe (A5) green |
| ~01:58 | Acceptance gates A1–A5 evaluated via `_self_drive evaluate_gates` | all_passed=true |
| ~02:00 | `review-packet.yaml` authored (deliverable summary) | sha `46553826…` |
| ~02:03 | Claude reviewer subagent dispatched (model=opus) | `goal_satisfied=true`; 4 low non-blocking |
| ~02:08 | Codex reviewer dispatched via `_dispatch_codex.py` helper | sealed via T6; 0 findings; ~89s wall |
| ~02:09 | `consensus.yaml` synthesized (unanimous) | `consensus_state=implementation_ready` |
| ~02:10 | T6 archive: 5 events appended via `audit.append_event` | `independence-audit.yaml` accumulated 6 total events (helper wrote 1) |
| ~02:11 | `verification.yaml` + `iteration-outcome.yaml` authored | apply_landed=true |
| ~02:12 | `iteration_closed` event appended | closing_state=`quorum_close_passed` |

Total wall time: ~25 min from operator directive to closure.

---

## TDD discipline

Tests authored before implementation. RED confirmed via:
```
$ python_env/python.exe -m pytest scripts/agent_loop_mcp/tests/test_reviewer_dispatch_codex.py -q
ImportError: cannot import name 'reviewer_dispatch_codex' from 'agent_loop_mcp.tools'
```
Then GREEN on first implementation: 17/17 pass. No production code written before its corresponding failing test.

**17 test cases:**
- 4 SCHEMA-shape (name, required, optional, additionalProperties)
- 2 register() integration (registry+handler)
- 8 argv translation (required emit; optional omit-when-None; per-flag set-when-present)
- 2 return-value identity (success dict; failure dict on rc!=0)
- 1 stdout isolation (wrapper does not leak helper stdout to caller)

---

## Wrapper design (thin, by construction)

145 lines total, breakdown:
- ~80 LOC: `SCHEMA` dict (input/output JSON Schema)
- ~25 LOC: `_build_argv` (pure flag translation)
- ~15 LOC: `handle()` (`contextlib.redirect_stdout` + `json.loads`)
- ~5 LOC: `register()`

No logic from `_dispatch_codex.py` is re-implemented. The wrapper is a translation layer:
1. MCP kwargs → argv list (`_build_argv`)
2. Capture helper stdout in-process (`io.StringIO` + `contextlib.redirect_stdout`)
3. Parse single JSON line, return dict verbatim

Failure path: when `main()` returns non-zero, the helper still prints `{"ok": false, "error": …, "error_type": …}`. The wrapper returns this dict **without raising** — matches the canonical MCP-tool error pattern (cf. `audit_append_event.py`).

---

## Findings (claude — all non-blocking, all low severity)

| ID | Target | Concern |
|---|---|---|
| `claude-iter0009-001` | wrapper SCHEMA | Three helper flags (`--prompt-template`, `--schema`, `--codex-bin`) not exposed via MCP. First two are sensible internal defaults; `--codex-bin` is borderline operator-facing. |
| `claude-iter0009-002` | `handle()` | Raises `JSONDecodeError` on malformed helper stdout instead of wrapping in `{ok: false, error_type: ...}`. Programming-error path only; helper always prints JSON in normal flow. |
| `claude-iter0009-003` | docstring | Comment imprecision: `if smoke:` is "omit when falsy", not "omit when None". Behavior correct; doc-only. |
| `claude-iter0009-004` | tests | No malformed-stdout test case. Add `fake_main` printing non-JSON; assert handle wraps or raises predictably. |

All four deferred to a v1.1.x+1 polish iteration if/when operator escalates. None block production.

Codex emitted **zero findings**. Its `goal_satisfied_rationale`:
> review-packet.yaml describes an in-scope thin reviewer.dispatch_codex wrapper over _dispatch_codex.main, server registration, behavior-level argv/return/stdout tests, and all five acceptance gates passing including tools/list visibility and no dispatch_codex regression.

---

## Notable observations

- **Meta-recursive moment.** Codex reviewed its own wrapper via `_dispatch_codex.main()`. The helper held: sealed review, zero findings on the wrapper that calls back into it.
- **Cross-vendor pass.** Round 8 had cross-model only (sonnet+opus, both Anthropic). iter-0009 is first cross-VENDOR (Anthropic claude + OpenAI codex-cli 0.129.0).
- **Goal-packet seal mechanism worked end-to-end.** scope_signature recomputed twice (once after switching POSIX shell vs cmd-friendly check syntax for portability); validate confirmed match each time; verify_scope confirmed in_scope.
- **TDD discipline held.** ImportError on RED was the failure mode (not "test passes immediately"). Green on first implementation pass; no rewrites.
- **Subagent independence held.** Claude surfaced 4 substantively different observations than codex (codex emitted none); no co-authoring artifacts visible in either review.
- **iter-0008 vs iter-0009 closure form.** iter-0008 closed by gate-evidence-only and was acknowledged as PROTOTYPE-class evidence per Round 9 F2. iter-0009 closes by sealed-reviews + consensus + T6 + T7 — the contract's stated closure ceremony for shippable self-drive proof.

---

## Carry-forwards (next iteration, if escalated)

1. **`v1_1_x_polish_pass_optional`** — address claude-iter0009-001…004 in a small polish iteration. Priority 3.
2. **`install_smoke_for_wrapper_in_wheel`** — the wheel at `python_env/Lib/site-packages/agent_loop_mcp` does not yet contain `reviewer_dispatch_codex.py`. For MCP-clients-via-installed-wheel deployment, rebuild + reinstall (per gate `G_install`). Source-tree MCP server boot DOES register the new tool (verified by A5). Priority 2.
3. **`render_outcomes_redirect`** — operator-locked next required action: redirect to render outcomes (Pass 2 lessons-learned re-pass on Book 1, next chapter render). Priority 1.

---

## Files reference

### Iteration dir (`agent-loop/active/iteration-0009-mcp-wrapper-v1-1-x/`)

| File | Role | Canonical sha256 |
|---|---|---|
| `goal_packet.yaml` | Sealed scope (validate=true) | `39517199be6196611f8fc6c7cf38cdbc912844fcedc81cd4fb94dd153e8dc6e0` |
| `input.yaml` | Iteration framing | (see file) |
| `review-packet.yaml` | Deliverable summary | `46553826460ffd9f5adb60cb16262d99aaa39d6f716ae025b181d4e5e153fcef` |
| `claude-review.yaml` | Sealed claude review | `78c68ac5e57d25c1fd177d75d16bf857c10a87bb67970d5d251e5b51a81aeb4e` |
| `codex-review.yaml` | Sealed codex review (helper-produced) | `4c321c7679fddf6ec9cbf1d2b2d2dece8c3c391677b80a294610e64a4aab3167` |
| `consensus.yaml` | Synthesized; consensus_state=implementation_ready | `61b744c3250277204cfa1b086e8315f5b2d4490ca30a881908068da5b35e002a` |
| `independence-audit.yaml` | T6 audit log (7 events) | (mutates per append) |
| `verification.yaml` | Post-apply checks | (see file) |
| `iteration-outcome.yaml` | Closure ceremony | (see file) |
| `RESULTS.md` | This file | — |

### Source artifacts

| File | LOC | Notes |
|---|---|---|
| `scripts/agent_loop_mcp/tools/reviewer_dispatch_codex.py` | 145 | New thin wrapper |
| `scripts/agent_loop_mcp/tests/test_reviewer_dispatch_codex.py` | 246 | New, 17 cases |
| `scripts/agent_loop_mcp/server.py` | 299 | +2 lines registering the tool |
| `scripts/agent_loop_mcp/_dispatch_codex.py` | 820 | **untouched** (forbidden_files) |

### Immutable T6 codex archive

`agent-loop/archive/review-passes/2026-05-10-iteration-0009-mcp-wrapper-v1-1-x-codex-iter0009-1-pass.yaml`
- packet_sha256: `565ce44bb9277021fe3f7be206a6e182326e358e49311c6c46291a7b524a5ae0`
- t6_audit_event_id: `2026-05-10T02:08:39Z_review_returned_and_sealed_codex-iter0009-1`

### Dispatch log additions (`agent-loop/state/dispatch-log.jsonl`)

- `dispatch_start` (codex-iter0009-1, schema_path, codex_bin, timeout=600)
- `dispatch_done` (rc=0, packet_sha256, archive_sealed_path, sealed_path, t6_audit_event_id)

---

## Reproduction

```bash
# 1. Validate the goal_packet
python_env/python.exe -m agent_loop_mcp._self_drive validate \
  agent-loop/active/iteration-0009-mcp-wrapper-v1-1-x/goal_packet.yaml

# 2. Run all 5 acceptance gates (env-var-overridden installed-wheel _self_drive)
AGENT_LOOP_MCP_REPO_ROOT="$(pwd)" python_env/python.exe -m agent_loop_mcp._self_drive evaluate_gates \
  agent-loop/active/iteration-0009-mcp-wrapper-v1-1-x/goal_packet.yaml

# 3. Run final close (combined: validate + stop_rules + gates + scope)
AGENT_LOOP_MCP_REPO_ROOT="$(pwd)" python_env/python.exe -m agent_loop_mcp._self_drive close \
  agent-loop/active/iteration-0009-mcp-wrapper-v1-1-x/goal_packet.yaml \
  agent-loop/active/iteration-0009-mcp-wrapper-v1-1-x

# Expected: {"can_close": true, "components": {"validate": true, "stop_rules": true, "gates": true, "scope": true}}

# 4. Re-run the codex dispatch (~90s wall) to verify the helper still works:
AGENT_LOOP_MCP_REPO_ROOT="$(pwd)" PYTHONPATH=scripts python_env/python.exe -m agent_loop_mcp._dispatch_codex \
  --goal-packet agent-loop/active/iteration-0009-mcp-wrapper-v1-1-x/goal_packet.yaml \
  --iteration-dir agent-loop/active/iteration-0009-mcp-wrapper-v1-1-x \
  --reviewer-id codex-iter0009-2 \
  --pass-id codex-iter0009-2-pass1 \
  --review-target agent-loop/active/iteration-0009-mcp-wrapper-v1-1-x/review-packet.yaml
```
