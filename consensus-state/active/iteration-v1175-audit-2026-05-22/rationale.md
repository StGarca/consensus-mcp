# v1.17.5 batch — author's rationale for the Workflow B audit

**Reviewer:** you are auditing claude's fixes for 7 findings from the 2026-05-22
consensus-mcp code review. The unified diff is in the embedded `v1.17.5.diff`.
Audit each change for **correctness and regressions**, and **refute** the
design-judgment calls below if you disagree. Empty findings is a valid review —
do not manufacture findings. Verify any file content you cite is actually in the
embedded diff (a sibling finding, H-1, was a hallucination; another, H-8, was
materially overstated — see below).

Process: each finding was independently verified against the live code before
fixing (H-1 proved a code-review finding can be false). All fixes are strict TDD
(failing test first, watched fail, minimal fix, watched pass). Full suite at
author time: **977 passed, 1 skipped, 0 failed** (was 953 pre-batch; +24 = the
new tests). The differential each reviewer should reason from: is the *mechanism*
of each fix correct, and do the 4 judgment calls hold?

---

## H-3 — `_self_drive.cmd_close` emitted multiple JSON blobs  (CONFIRMED)

cmd_close calls cmd_validate / cmd_check_stop_rules / cmd_evaluate_gates /
cmd_verify_scope, each of which `print(json.dumps(...))`, then printed its own
blob → 5 JSON objects on stdout → `json.loads(stdout)` raises "Extra data".
**Fix:** wrap the 4 sub-command calls in `contextlib.redirect_stdout(io.StringIO())`,
collect return codes, emit ONE `{can_close, components}` object. Sub-commands'
standalone CLI contract is unchanged. No in-tree caller invokes `close` (the
orchestrator calls the sub-commands individually), so this is a CLI-contract fix.
**Scrutinize:** is suppressing children's stdout the right approach vs. refactoring
them to return-without-print? Does the single-object shape lose any signal a
consumer needs?

## M-11 — `cmd_transition` is a no-op but claimed to "record"  (CONFIRMED)

It validated `new_state` ∈ VALID_STATES and printed `{ok,...}` but persisted
nothing; module + function docstrings claimed transitions are "recorded."
**Design call (3):** fix = CONTRACT CORRECTION (docstrings now say STATELESS),
NOT adding persistence. Rationale: the canonical state store
(disposition-ledger.yaml) has a single authorized writer by design; the
orchestrator owns state via file-presence + ledger; adding a 2nd writer here
would create a drift-prone parallel source of truth. A terminal-state guard
(reject transitions out of terminal states) was considered and deferred — it
needs a `current_state` arg this command doesn't have (API addition).
**Scrutinize:** is the contract-correction the right minimal fix, or should
cmd_transition be removed / actually persist / get the terminal-state guard?

## H-4 — CRLF dirty-detection always-dirty on Windows  (CONFIRMED, empirically)

`_detect_dirty_paths` hashed raw working-tree bytes:
`hashlib.sha1(b"blob %d\0"+content)`. Under `core.autocrlf=true` (this repo has
it; no `.gitattributes`), git stores LF-normalized blobs, so the Python hash over
CRLF bytes never matches git's OID → every text file reads as dirty → restore
guard mis-fires. **Fix:** use `git hash-object -- <path>` (applies git's clean
filter, matches the stored OID), via the existing `_run_git` helper, `check=False`;
on non-zero (git unavailable / unreadable) mark the file dirty (conservative —
never silently skip a real change). `import hashlib` removed (now unused).
**Scrutinize:** is per-file `git hash-object` acceptable (one subprocess per file
under consensus-state/, ~40 files)? Is the "mark dirty on git failure" fallback
the right default here (vs. raising)?

## H-5 — mutation-completeness gate FAIL-OPEN when git unavailable  (CONFIRMED)

`_detect_working_tree_changes` returned `[]` on any git failure; the
`iteration_closed` gate treats empty as "no unaudited mutations → allow close,"
so a git-unavailable environment silently passes the gate that exists to catch
out-of-band edits. **Design call (2):** fix = FAIL CLOSED, non-configurable. Added
`GitUnavailableError`; the function now tracks `any_ok` and raises only when ALL
git commands fail (a single sub-command non-zero still continues, so one odd
sub-command doesn't nuke the check); `handle` converts the raise to
`{"error": "mutation_completeness_unverifiable: ..."}`. Claim: no "git optional"
design exists in consensus-mcp (closure invariant, snapshots all assume git), so
hard fail-closed is correct and configurability is unwarranted.
**Ripple / design call (4):** 3 capstone tests built fake repos in non-git
tmp_path and exercised iteration_closed WITHOUT stubbing the helper, so they
newly hit the gate and got `mutation_completeness_unverifiable`, pre-empting the
closure-invariant assertions they actually test. Fix: a new `stub_clean_working_tree`
fixture stubs `_detect_working_tree_changes -> []` (the legitimate "git ran, no
changes" signal, matching the Finding-3/5 idiom). **Scrutinize:** does the stub
mask a real problem, or is it the faithful simulation? Is hard fail-closed right?

## H-6 — release gates pass on literal test-count strings  (CONFIRMED + already broken)

Gates required substrings like `"95 passed"` / `"60/60 tests passed"`. Brittle:
passes a build where a test was deleted but the count nets the same; fails a good
build when tests are added — and it WAS already failing (the dispatch-codex gate
required "95 passed" but the suite now collects 96). The dispatch-codex gate also
pointed pytest at a non-existent `scripts/` path (dead gate). **Fix:** two robust
parse helpers (`_pytest_gate_pass` for `N passed`, `_smoke_gate_pass` for
`X/Y tests passed` requiring X==Y) — PASS iff `returncode==0` AND no failure word
AND `passed >= floor`. Floors: smoke 60, validators 21, install_smoke 60,
dispatch_codex 90. Fixed the dead `scripts/` path.
**Scrutinize:** the floor (`>= N`) deliberately relaxes the exact-count
anti-deletion tripwire; acceptable, or should a `pytest --co` baseline guard
deletions? Two regexes correct for the two output formats? Note the dispatch-codex
gate becomes LIVE for the first time (was a no-op) → it will now run the real
~82s suite during release gating.

## H-7 — TIMEOUT_BLOCKING veto bypassed under majority rules  (core CONFIRMED; one sub-claim imprecise)

`blocking_ids` is populated only from responsive artifacts' `blocking_objections`.
Under `timeout_policy == TIMEOUT_BLOCKING`, timed-out contributors land in
`block_votes`/`n_block` but NOT `blocking_ids`, and the majority paths compute
`converged = n_approve >= threshold and not blocking_ids` — so a timeout that is
"treated as blocking" does NOT veto a majority (correct-for-wrong-reason today,
fragile). (The review's "misreports n_block" wording is imprecise — the rationale
printed `len(blocking_ids)`, not `n_block`.) **Design call (1):** fix = Option B
(timeout-only): `timeout_block = timed_out if policy==TIMEOUT_BLOCKING else []`,
ANDed into the veto for both majority branches; rationale now reports n_block.
Option A (`n_block == 0`) was REJECTED because it would also let a responsive
"soft no" (goal_satisfied=False, no formal blocking_objection) veto a majority —
defeating majority semantics. **Scrutinize:** is Option B the right line? Under
TIMEOUT_BLOCKING the operator chose to treat non-response as a block, so a timeout
should veto even a majority; a responsive soft-no should not. Agree?

## H-8 — bare `except Exception` in `_resume.py`  (PARTIAL — HIGH framing NOT supported)

The review's HIGH headline ("silent: empty/failed dispatch-log read makes the
orchestrator think nothing is in-flight") is **false**: the dispatch-log read
path was never bare — it already catches the failure and appends a
`"dispatch-log.jsonl read failed"` warning (a regression-lock test confirms this
passes on UNMODIFIED code). The cited line list was also partly miscited (168 is
not bare; 802 was missed). **Fix = doctrine-parity refactor only:** narrow 6
helpers to the iter-0036 set (`OSError, UnicodeDecodeError, yaml.YAMLError`;
`ValueError` for the fromisoformat parse), matching `_self_drive._read_yaml_or_empty`,
so genuinely unexpected (programmer) errors propagate instead of being mislabeled
as parse/IO failures. Behavior for missing/malformed files is UNCHANGED.
`_resolve_stall_threshold` (L48) and `_scope_signature` recompute (L802) left
as-is (defensive bootstrap / already-warns). **Scrutinize:** confirm the HIGH
framing is unsupported (or refute with the actual silent path if I missed one);
confirm the narrowed exception sets preserve missing/malformed behavior.

---

## What to return
Per-finding: is the fix correct? Any regression, missed case, or scope drift?
Then a verdict on each of the 4 design calls. `goal_satisfied: true` only if the
diff fully meets the desired end state with no blocking findings.
