# consensus-mcp - Thorough Code Review (2026-05-22)

**Method:** 5 parallel read-only reviewers over risk-grouped areas (dispatchers/subprocess, orchestration core, validators/gates, MCP tools/patch-apply, state/config/init/release). ~25K LOC of Python.
**Reviewed at:** main `d948334` (post-PII-scrub).
**Confidence** is the reviewer's self-rated 0-100; I've dropped findings the reviewers retracted mid-analysis and down-weighted spec-ambiguities.

> **Status (updated 2026-05-22):**
> - **Security cluster (CR-1...CR-5, H-2, M-6): FIXED, shipped in tag `v1.17.4`** (consensus-audited - see the "Consensus audit" section at the end).
> - **`test_visibility_watchdog` (4 failures): FIXED** on `v1.17.5` - a time-bomb test (hardcoded absolute timestamp aged past the watchdog's 7-day `--window-days` filter), not a product bug.
> - **H-1: DISMISSED.** The `state_root`/`project_root` cwd-fallback is the **intentional multi-project mechanism** (consensus-mcp writes state into the current project's cwd; a project-scoped MCP server runs with cwd = project dir). A `__file__`-walk "fix" was implemented and **reverted** because it ties state to consensus-mcp's source location and breaks multi-project (caught by `test_paths.py:198 test_unsetting_env_falls_back_correctly`). The env-free `.mcp.json` from v1.17.4 is correct.
> - Remaining **gates/state** (H-4, H-5, H-6, H-7) and **orchestration** (H-3, H-8, M-11) are in progress on `v1.17.5`. Non-trivial fixes go through the consensus peer-review workflow.

---

## Executive summary - headline themes

1. **Operator/AI-supplied file paths are used without repo-containment checks** in three tools, enabling arbitrary file read/write outside the repo. `apply_codex_patch.py` (T6) is missing BOTH the path-traversal guard AND the scope-vs-`allowed_files` check that its sibling `patch_apply_consensus_patch.py` (T5) already has. This is the most important cluster.
2. **Several safety gates fail OPEN** - they return pass/empty on an error or empty input where they must reject: empty `allowed_files` = allow-all; git-failure = "no unaudited mutations"; majority-rule convergence when only timeouts block.
3. **Windows-specific correctness bugs** - SHA-1 dirty-detection ignores CRLF normalization (always-dirty), non-atomic `write_text` on audit/verdict/ledger/gitignore files.
4. **`cmd_close` emits multiple JSON blobs** to stdout -> unparseable by any machine consumer.
5. **My `.mcp.json` change has a real downside** the review caught - see the dedicated section below.

---

## CRITICAL

| ID | File:line | Issue | Conf |
|----|-----------|-------|------|
| **CR-1** | `tools/apply_codex_patch.py` ~272-352 | `files_touched` (from `codex-review.yaml`) used to build write targets with **no `resolve()`+containment check** -> a `../../..` or absolute entry writes **outside the repo**. T5 has this guard (lines 221-229); T6 does not. | 100 |
| **CR-2** | `tools/apply_codex_patch.py` ~300-320 | `files_touched` **never checked against `goal_packet.allowed_files`** -> a codex patch can modify source files **outside the authorized scope**; only `validate_disposition_index` runs, which checks provenance, not target files. | 95 |
| **CR-3** | `validators/build_review_packet.py` ~298-311 | `target_files` from operator `input.yaml` read after `.resolve()` with **no containment check** -> arbitrary file **read/exfil** into the review packet (e.g. `../../etc/passwd`). | 92 |
| **CR-4** | `validators/scope_check.py:379` | `if allowed_files and not _matches_any(...)` - an **empty `allowed_files: []` short-circuits to allow-ALL** touched files. `scope_check_block.passed=True`, zero findings. Fail-open. Sibling `check_autonomy_scope` (line ~720) does it correctly. | 95 |

**Note on CR-1/2/3:** exploitation requires the dual operator interlock to be satisfied, so this is "semi-trusted input." But `files_touched` originates from an *AI's* output - a hallucinated or manipulated codex/consensus patch is exactly the threat model this product exists to contain. Treat as real. The clean fix is to lift the guard already present in `patch_apply_consensus_patch.py` into T6 and `build_review_packet.py` (`is_relative_to(REPO_ROOT)` + scope membership).

---

## HIGH

| ID | File:line | Issue | Conf |
|----|-----------|-------|------|
| **H-1** | `server.py:86-94` + `.mcp.json` | `_resolve_state_root()` / `_resolve_project_root()` fall back to **`Path.cwd()`** (no `__file__`-walk). After dropping `CONSENSUS_MCP_REPO_ROOT` from `.mcp.json`, state/project root depend on the server's cwd. **See dedicated section.** | - |
| **H-2** | `tools/patch_stage_and_dry_run.py:231,242,259` | No traversal guard on `file_rel`: reads arbitrary file into memory, then `relative_to()` raises an **uncaught `ValueError`** that escapes `handle()` and breaks the `{ok,state}` MCP contract. | 95 |
| **H-3** | `_self_drive.py:1150-1163` (`cmd_close`) | Calls sub-commands that each `print(json.dumps(...))`, so `close` emits **5 JSON blobs** -> `json.loads(stdout)` fails for any consumer. | 100 |
| **H-4** | `_snapshot_state.py:538-539` | Python-side git blob SHA-1 ignores **CRLF normalization**; with `core.autocrlf=true` every text file reads as **always-dirty** -> restore guard mis-fires on Windows. Use `git hash-object`. | high |
| **H-5** | `tools/audit_append_event.py:425-485` | `_detect_working_tree_changes` returns `[]` on **any git failure** -> `iteration_closed` mutation-completeness gate **fail-open** when git is unavailable. | 87 |
| **H-6** | `_release_gate_check.py:453` (+ smoke/validator gates) | Gate passes only if output contains literal **`"95 passed"`** - brittle: passes a build where a test was deleted but count still nets 95; fails a good build when tests are added. Use `returncode == 0` / `>= N`. | high |
| **H-7** | `workflow_engine.py:456-458` | `_evaluate_convergence` under `TIMEOUT_BLOCKING`: the `blocking_ids` veto for majority rules is **bypassed when only timeouts block**, and the rationale string misreports `n_block`. Correct-for-wrong-reason today; fragile. | 85 |
| **H-8** | `_resume.py:48,136,168,234,333,362,457` | Seven bare `except Exception:` swallow errors -> **silent wrong-state snapshots** (e.g. empty dispatch-log read makes orchestrator think nothing is in-flight). `_self_drive.py` was already narrowed (iter-0036); `_resume.py` wasn't. | 82 |

---

## MEDIUM

| ID | File:line | Issue | Conf |
|----|-----------|-------|------|
| M-1 | `_dispatch_codex.py:1124` / `_dispatch_gemini.py:1011` | `_build_prompt(review_packet_path=str(goal_packet_path))` - the **goal-packet path is passed where the review-packet path is expected**, mislabeling `{review_packet_path}` in every prompt. (Relates to the known "dispatch must target the .yaml packet" gotcha.) | 88 |
| M-2 | `tools/audit_append_event.py:384` | Audit log written with non-atomic `write_text` (no tmp+`os.replace`) -> corruption window on Windows. | 82 |
| M-3 | `tools/loop_verify_codex_patch.py:347` | Verdict file non-atomic write; corrupt file later reads as `{}` -> silent perpetual `verification_not_approved`. | 82 |
| M-4 | `tools/state_update_decision_ledger.py:256,258` | Sibling temp files keyed only on `os.getpid()` -> collision between concurrent same-process (threaded) calls. Add uuid. | 80 |
| M-5 | `_init_wizard.py:679` | `.gitignore` written non-atomically while config/.mcp.json use tmp+rename - inconsistent. | 80 |
| M-6 | `tools/patch_stage_and_dry_run.py:329` | `gate_decision` blocks on `("high","blocking")` but **not `"critical"`** -> can return `APPROVED` with a critical finding (T6 re-checks critical, but the field is misleading). | 88 |
| M-7 | `_dispatch_gemini.py:449` | Poll loop uses bare `time.sleep`, not the injectable `_sleep` seam the codex adapter has -> can't deterministically test gemini abort paths without the forbidden global monkeypatch. | 90 |
| M-8 | `_dispatch_gemini.py` | `CONSENSUS_MCP_STALL_SILENCE_SECONDS` override honored in codex adapter but **not gemini** -> operators can't tune gemini's stall threshold for slow/large prompts. | 83 |
| M-9 | `_dispatch_gemini.py:476` | Gemini stdout decoded `errors="replace"` before `json.loads` -> invalid UTF-8 silently becomes U+FFFD and corrupts parsed values. Use strict decode for the JSON path. | 82 |
| M-10 | `_dispatch_gemini.py:358-359` | stdin writer daemon thread never joined -> resource/delay window on forced kill; non-`BrokenPipeError` exceptions silently lost. | 85 |
| M-11 | `_self_drive.py:327-338` | `cmd_transition` validates and prints `{ok:true}` but **persists nothing** - no state store exists; transitions out of terminal states "succeed." Either persist or fix the docstring/contract. | 95 |
| M-12 | `validators/validate_consensus.py:188` | `observational_mode` is **self-declared in the artifact being validated** and relaxes enum checks - a crafted/over-broad artifact can widen its own validation. | 83 |
| M-13 | `config.py:398-404` | `autonomous-execute` error message hardcodes `(claude + codex + gemini)` and the count is locked to exactly 3 - contradicts the open-contributor model (min 2 / no cap). | 80 |
| M-14 | `_snapshot_state.py` `_compute_previous_iteration_summary` | `archive.rglob(...)` walks the whole archive on **every** `snapshot()` (polling) -> O(archive) per call. Index at seal time instead. | 80 |
| M-15 | `_release_gate_check.py:311-337` | `gate_server_starts` PASS budget is 2s wall-clock incl. spawn+import of 18 tool modules -> spurious FAIL on loaded CI. Raise to match the 10s subprocess timeout. | mid |

---

## LOW / informational

- **`_self_drive.py:314-324`** `cmd_validate` returns `valid:true` in JSON but exit `2` on scope-signature mismatch - dual-signal API; `cmd_close` handles it fail-closed, so low risk, but confusing. (conf 85)
- **`_resume.py` blocker detail** - `satisfiable_now=False` always reports `needs_cross_family_reviewer` even when the real cause is "mutation actor family unknown." (conf 88)
- **`_self_drive.py:147`** `_scope_signature` double YAML round-trip without `sort_keys` on the inner dump -> latent cross-PyYAML-version signature instability. Consider `json.dumps(sort_keys=True)`. (conf 87)
- **`_resume.py:335`** abort-signal path hardcodes `iter_dir.parent.parent` (assumes fixed depth). (conf 83)
- **`workflow_engine.py:369-393`** convergence packet inlines full proposal file contents and grows each round -> can exceed downstream adapter parse/timeout limits. Store refs+hashes. (conf 80)
- **`validators/consensus_gate.py:228`** `APPROVAL_MISSING` emitted `severity:"medium"` while every sibling blocking condition uses `"high"` -> severity-filtered consumers under-weight it. (conf 88)
- **`validators/scope_check.py:125-128`** leading `**/` rewritten to `*/` -> `**/foo.py` fails to match a repo-root `foo.py` (under-match; fail-open for root-level `forbidden_files`). (conf 85, speculative)
- **`_import_parent_history.py:157`** `extraction_commit="ff0164f"` default is a now-nonexistent SHA (history was rewritten) - should be `None` + required. (conf low)

### Dropped (reviewers retracted these mid-analysis)
- Dispatcher "temp file leak" - `finally` covers it.
- Orchestration `validate_consensus.py:395` or/and - logic is correct.
- `_import_parent_history.py:233` conditional-for - valid Python.
- Validators `HIGH-3` - non-issue on re-read.

---

## On the `.mcp.json` change made during this session (disconfirming evidence)

I dropped the env block from `.mcp.json` (it hardcoded `C:\Users\steve\...` - PII) on the basis that `_resolve_repo_root` and `_resolve_spec_path` walk up from `__file__`. **Reviewer H-1 correctly caught what I missed:** `_resolve_state_root()` and `_resolve_project_root()` do **not** have a `__file__`-walk fallback - their final fallback is `Path.cwd()`. The old `.mcp.json` set `CONSENSUS_MCP_REPO_ROOT`, which fed the *legacy REPO_ROOT* branch of those resolvers; dropping it shifts state/project resolution to **cwd-dependent**.

**Impact:** if Claude Code launches the server with cwd != repo root, the dev folder's consensus state/audit writes land in the wrong tree - silently. Whether that happens depends on Claude Code's MCP cwd behavior (it typically inherits the session cwd = project root, but this is not guaranteed and should be confirmed, not assumed).

**Recommended resolution (keeps PII out AND removes the doubt):** add a `__file__`-parent-walk fallback (with repo-root marker detection) to `_resolve_state_root()` and `_resolve_project_root()`, mirroring `_resolve_repo_root`/`_resolve_spec_path`. Then the env-free `.mcp.json` is correct for source-tree checkouts regardless of cwd, and pip-installed users keep using the `consensus init`-written env vars. This is a code change -> run it through the consensus workflow. Until it lands, the safe interim is to confirm Claude Code's cwd is the repo root for this project (or temporarily re-add only `CONSENSUS_MCP_STATE_ROOT`/`PROJECT_ROOT` pointing at a non-PII relative location - not viable on an absolute Windows path, hence the code fix is preferred).

---

## Suggested remediation order

1. **CR-1 / CR-2 / CR-3 / H-2** - path containment + scope membership on every operator/AI-supplied file path (one shared helper; lift from T5). Highest security value, low blast radius.
2. **CR-4 / H-5 / H-7** - close the fail-open gates (empty `allowed_files`, git-unavailable, timeout-only-blocking).
3. **H-1** - state/project root `__file__`-walk fallback (unblocks the env-free `.mcp.json`).
4. **H-3 / H-4 / H-6** - `cmd_close` JSON, Windows CRLF dirty-detection, release-gate count strings.
5. **Atomic-write cluster** (M-2/3/4/5) - one tmp+`os.replace` helper.
6. Remaining MEDIUM/LOW as capacity allows.

Each batch is a coherent consensus iteration. Items 1-2 share the "fail-closed boundary" doctrine and could bundle.

---

## Consensus audit of the security cluster (codex + gemini) - 2026-05-22

The 6 security-cluster findings (CR-1...CR-4, H-2, M-6) were dispatched to **codex** (`codex-cli 0.130.0`) and **gemini** (`gemini-2.5-pro`) as a Workflow-B audit (sealed packets in `consensus-state/archive/review-passes/2026-05-22-iteration-codereview-audit-2026-05-22-*`). Reviewers were explicitly asked to *refute* where wrong. Every claim below was then re-verified by direct code inspection (grep + read), so this is grounded convergence, not rubber-stamping.

**Outcome: all 6 confirmed; codex refined 3 and added 1 new finding.**

| My finding | gemini | codex | Verified verdict |
|---|---|---|---|
| CR-1 (arbitrary write, T6) | [ok] validate (`gemini-rev-001`) | [ok] implicit (basis for rev-003) | **CONFIRMED** - no `relative_to` anywhere in `apply_codex_patch.py`; `:340-345` writes `repo_root/rel` unguarded. |
| CR-2 (scope bypass, T6) | [ok] validate (`gemini-rev-002`) | [ok] | **CONFIRMED** - comment at `:300-305` admits only `validate_disposition_index` runs; no `allowed_files` membership check. |
| CR-3 (arbitrary read, build_review_packet) | [ok] validate (`gemini-rev-003`) | - | **CONFIRMED** - `:298-311` resolve+read of `target_files` unguarded (the file's `is_relative_to` uses at `:414/:606` are display-only). |
| CR-4 (empty `allowed_files` allow-all) | [ok] validate (`gemini-rev-004`) | [edit] **broaden** (`codex-rev-001`) | **CONFIRMED + BROADENED** - `scope_check.py:300` `... or []` means missing/null/empty all fail open, not just `[]`. Fix must **fail closed unless a non-empty list of valid string patterns** is present. |
| H-2 (traversal in patch_stage_and_dry_run) | [ok] validate (`gemini-rev-005`) | [edit] **correct mechanism** (`codex-rev-002`) | **CONFIRMED + CORRECTED** - the dangerous case is a relative `../` path: `project_root()/'../../x'`.`relative_to(project_root())` returns `../../x` **without raising**, so `staging_path/rel`->`write_text` (`:259-262`) **silently writes outside staging**. The ValueError crash is only the absolute-path sub-case. Fix: require resolved project-root containment **before** any read (`:242`), patch-map insert, `relative_to`, or staging write. |
| M-6 (gate omits `critical`) | [ok] validate (`gemini-rev-006`) | [edit] **under-ranked** (`codex-rev-004`) | **CONFIRMED** - `:329`; codex argues promote to HIGH since `gate_decision` is part of the public tool contract. |

### NEW finding from the consult (verified)

| ID | File:line | Issue | Source |
|----|-----------|-------|--------|
| **CR-5** | `tools/apply_codex_patch.py:161` | `iteration_dir` is accepted **raw** - authorization is then read from `iter_dir/goal_packet.yaml` (`:179`) and the patch from `iter_dir/codex-review.yaml` (`:194`). With the env interlock set, a caller can point T6 at a non-canonical dir holding its **own** `goal_packet` (`codex_patch_apply_authorized: true`) + `codex-review.yaml`, so the goal_packet half of the dual interlock is self-satisfied -> degrades to env-flag-only. Combined with CR-1, that's "auth your own write, then write anywhere." Fix: resolve `iteration_dir` and require it under the canonical active-iterations root before reading any auth/patch artifact. | `codex-rev-003` (verified) |

### Net effect on remediation order

Remediation step 1 (the path-containment helper) now **also** covers H-2's write-escape and CR-5's iteration_dir canonicalization, and step 2's CR-4 fix must be the broadened "non-empty valid list or fail closed" version. The security cluster (CR-1, CR-2, CR-3, CR-4-broadened, CR-5, H-2-corrected, M-6) is the right first consensus iteration - it's one coherent "fail-closed boundary for untrusted paths + directories" doctrine.

*Not run through consensus yet:* the gates/state cluster (H-1, H-4, H-5, H-6, H-7) and the orchestration cluster (H-3, H-8, M-11). Available on request.
