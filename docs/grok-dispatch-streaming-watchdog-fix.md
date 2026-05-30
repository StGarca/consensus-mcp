# consensus-mcp patch spec — grok dispatcher: streaming vs silence-watchdog defect

Status: PROPOSED patch spec (non-trivial change to a dispatcher → must go through
Workflow A/B review before landing, per the consensus operating procedures).
Discovered: 2026-05-30 during iteration `iteration-moss-clip-codefix-2026-05-30`
(grok timed out on a real design-consult packet; root-caused to dispatcher config, not grok).

## Operator principle (load-bearing)
> "Every grok failure has been a Claude failure and not grok. It can handle large prompts perfectly fine."

grok stalls/timeouts in this project have been **consensus-mcp dispatcher / invocation bugs**, not grok
limitations. grok's context window is **512K** and it streams fine. The Claude-side fix is to invoke grok
*correctly* (below) — never to blame grok or skip it. (Note: "give grok a focused prompt" is part of the
correct invocation, not "grok can't handle large prompts" — see the agentic-loop finding below.)

---

## GETTING THE MOST OUT OF GROK — empirical findings (2026-05-30, for patch planning)

This section is the evidence base for a proper patch. All data points are from direct grok-CLI runs this
session (grok-build, `--output-format streaming-json`, clean empty cwd unless noted).

### Empirical observations (invocation → outcome)

| run | streaming | `--no-plan --no-subagents` | prompt | outcome |
|---|---|---|---|---|
| smoke (plain, no flags) | no (plain) | no | tiny | completed |
| round-1 full | yes | YES | long, 5 questions (~5KB) | **completed** (220 thought → 1885 text → EndTurn) |
| v2 | yes | YES | long, 5 questions (~4KB) | **Cancelled** (171 thought, 0 text) |
| v2b (retry of v2) | yes | YES | long, 5 questions | **Cancelled** (254 thought, 0 text) |
| v2c | yes | YES | SHORT, 3 questions (~1KB) | **completed** (112 thought → 801 text → EndTurn) |
| self-diagnosis | yes | NO | short (~1KB) | **Cancelled** (69 thought, 0 text) |

### What the failure actually is
- The cancel is NOT a rate limit / quota / 429 (the `~/.grok/logs/unified.jsonl` shows normal inference
  turns, no rate errors) and NOT the `timeout` wrapper (process exits 0).
- grok emits a long run of `{"type":"thought"}` events, then `{"type":"end","stopReason":"Cancelled"}`
  with ZERO `{"type":"text"}` — i.e. it **thinks, then self-cancels before producing the answer**.
- The logs show `loop_index` incrementing (e.g. `loop_index:4`, ~231s) — grok-build is an **agentic model
  that runs multiple internal inference loops**. On open-ended / multi-part prompts it loops until it
  self-cancels without committing a final answer.

### What helps (from the table)
1. **`--no-plan --no-subagents` materially reduce cancellation.** The short prompt WITH them completed
   (v2c); the short prompt WITHOUT them cancelled (self-diagnosis). They suppress agentic branching.
2. **Shorter / single-focus prompts complete far more reliably.** Long 5-part prompts cancelled 2/3 times
   (round-1 succeeded once — non-deterministic); the short 3-part prompt completed cleanly.
3. **`--output-format streaming-json`** is required for the dispatcher's watchdog (see DEFECT 1) AND lets
   you see `thought` vs `text` vs `stopReason` to detect a cancel programmatically.
4. **Clean empty cwd** avoids the recursive-watch `PermissionDenied` noise (DEFECT 2).

### Reliable recipe found this session
`grok -p '<ONE focused question, ~1-2KB>' --output-format streaming-json --no-memory --disable-web-search
--no-plan --no-subagents`, run from a **clean empty cwd**, parse the stream (concat `type==text`, ignore
`type==thought`, confirm `stopReason==EndTurn` NOT `Cancelled`). Decompose a big consult into several such
focused prompts rather than one large multi-part prompt.

---

## Policy tension & deferred decisions

This section captures the most important non-obvious finding from the session and the resulting policy questions.
It is intentionally **out of scope** for the minimal streaming + cwd patch below, but the data here should
drive a follow-up review of `dispatch-canon-validator` rules.

### The core tension

The dispatcher's `dispatch-canon-validator.GROK_FORBIDDEN_FLAGS` currently **forbids `--no-plan`,
`--no-subagents`, `--max-turns`** (and the validator also hard-codes only `/tmp` and `/tmp/` as allowed
`--cwd` values). Yet the evidence above shows that `--no-plan --no-subagents` **materially reduce** the
self-cancel behavior on non-trivial prompts.

The original forbidding decision was based on an older, more complex flag combination
(`--prompt-file` + `--max-turns N` + `--permission-mode` + `--no-plan` + `--no-subagents` + project-subdir
`--cwd`) that produced indefinite stalls. The data now indicates the problem was the **combination** (most
likely the prompt-file + max-turns + permission-mode + project cwd), not `--no-plan`/`--no-subagents` in
isolation.

**Patch stance**: This change keeps `GROK_FORBIDDEN_FLAGS` and the allowed-cwd list untouched so the patch
remains small, reviewable, and non-controversial. A deliberate follow-up policy review (with new
empirical runs under the streaming + clean-cwd shape) is recommended.

### Open questions (recommended for the follow-up policy review)

1. Is there a flag or model that forces a **true single-turn** completion (no agentic loop) regardless of
   prompt complexity? (`--max-turns 1`? a non-agentic chat model instead of `grok-build`?) Only
   `grok-build` (agentic coding, `api_backend: responses`) was observed in this session.
2. What thinking/turn budget actually triggers the self-cancel on `grok-build`, and is it configurable
   (environment variable, `~/.grok/config.toml`, or CLI flag)?
3. **Highest-leverage architectural question for consensus**: Should `_dispatch_grok` **decompose** a long
   goal packet into N focused sub-prompts (each ~1-2 KB, single-question) and stitch the answers, rather
   than sending one large multi-part packet? This aligns with the "reliable recipe" above and with the
   operator principle that grok itself is not the limiter.
4. Re-validate (and potentially relax) the `GROK_FORBIDDEN_FLAGS` list and `ALLOWED_GROK_CWDS` policy
   against fresh data using the streaming + clean per-pass cwd + inline `-p` shape.

> **Operator principle (re-stated for this section)**: Every observed grok stall in this project has been a
> dispatcher/invocation bug, not a limitation of grok. grok's 512K context window and streaming behavior
> are not the problem when invoked correctly.

---

## Symptom
`consensus-mcp-dispatch-grok --mode proposal` on a non-trivial packet returns
`{"ok": false, "error": "grok stuck: no output for <N>s", "error_type": "GrokInvocationError"}`
after the full stall window, with **no proposal produced**. Trivial smoke prompts succeed, masking the bug.

## Root cause (file: `consensus_mcp/_dispatch_grok.py`)

### DEFECT 1 (primary) — buffered output + silence watchdog is self-contradictory
- `_build_grok_cmd` sets **`--output-format plain`** (around line 219). In `plain` mode the grok CLI emits
  **nothing to stdout until the full answer is ready** (buffered).
- `_invoke_grok` runs a **silence watchdog**: `stdout_reader` (around lines 295-316) updates `last_streamed_ts`
  only on each stdout *line*; the main loop (around line 338+) kills grok when `now - last_streamed_ts`
  exceeds `stall_silence_seconds`.
- Therefore: on any prompt grok thinks about for longer than the stall window, grok produces zero
  stdout lines (by design in `plain`), `last_streamed_ts` never advances, and the watchdog **kills
  grok for being silent while grok is silent by design.** The smoke prompt only survives because it
  answers before the timer fires.

### DEFECT 2 — hardcoded `--cwd /tmp` trips grok's recursive watcher
- `_build_grok_cmd` appends **`--cwd /tmp`** (~line 227). grok sets up a recursive filesystem watcher
  on its cwd; from `/tmp` it hits `PermissionDenied` on `systemd-private-*` dirs and logs
  `ERROR failed to watch root recursively`. Non-fatal, but it pollutes stderr and is the basis of a
  wrong rationale (below). A **clean empty dir** avoids it entirely.

### DEFECT 3 — misleading code comment (a Claude misdiagnosis baked into source)
- The comment at lines 100-109 claims grok "counts every prompt chunk + MCP rejection + tool-call attempt
  against the message budget and never reaches model output" and implies grok cannot handle large
  "real packets." This is **wrong** and misleads maintainers: grok has **no MCP servers configured**
  (`grok mcp list` → "No MCP servers configured"), handles 512K context, and the real failure is
  DEFECT 1. Remove/correct it.

## Fix

### Primary: stream grok's output so the watchdog sees progress
- In `_build_grok_cmd`, use **`--output-format streaming-json`** instead of `plain`.
  Verified: in streaming mode grok emits `{"type":"thought","data":...}` and `{"type":"text","data":...}`
  lines continuously, ending with `{"type":"end"}`. The existing `stdout_reader` then advances
  `last_streamed_ts` on every token line → the silence watchdog never false-trips.
  (Additional benefit: `thought` events become visible in dispatch logs for the first time, turning
  previously opaque long-running invocations into observable ones.)
- Add a small parser to assemble the final answer from the stream: concatenate `data` of
  `type=="text"` events (the answer), ignore `type=="thought"` (reasoning), stop at `type=="end"`.
  Feed the assembled text into the existing JSON-proposal extraction/validation.
- (Alternative if `plain` must be retained: replace the stdout-silence watchdog with a **wall-clock-only**
  timeout for grok — `plain` is silent by design, so silence is not a liveness signal. Streaming is
  preferred because it preserves a real liveness signal AND surfaces progress in logs.)

### Secondary: clean cwd instead of `/tmp`
- Create a fresh empty per-pass dir (e.g. under the system temp root) and pass it as `--cwd`, so grok's
  recursive watcher has nothing unreadable to scan. Do not point grok at `/tmp` (shared, partially
  unreadable) or the project root (the `.mcp.json` auto-load hang the `/tmp` choice originally dodged).

### Tertiary: correct DEFECT 3 comment
- Replace the "budget exhaustion / can't handle large packets" rationale with the real cause
  (plain-output + silence-watchdog) and the streaming fix.

## Alternatives considered

**Wall-clock-only timeout instead of (or in addition to) streaming**

The silence watchdog could be replaced (or supplemented) by a pure wall-time timeout when using `plain` output,
since `plain` is silent by design until the answer is ready. This would have been a smaller code change.

**Why streaming was chosen instead**:
- Streaming preserves a *real* liveness signal (`thought` and `text` events advance the watchdog naturally).
- It gives operators and logs immediate visibility into whether grok is still thinking vs. stuck
  (major diagnostic improvement over the current opaque `plain` mode).
- It aligns with the long-term "reliable recipe" (streaming + focused prompts + `--no-plan --no-subagents`).
- A pure wall-time change would still leave the system blind during long thinking phases.

The streaming approach is therefore preferred on both correctness and observability grounds.

## Risks & unknowns

- **Streaming event format stability**: The parser added for `type=="text"` / `type=="thought"` / `type=="end"`
  events will need to be defensive. A future grok CLI change to the JSON event shape would break assembly
  even if the underlying model behavior is unchanged.
- **New parser error handling**: The existing `stdout_reader` is intentionally tolerant (it just appends
  raw bytes). The new text-assembly logic must handle partial lines, truncated streams, non-JSON events,
  and the case where `stopReason` is never `EndTurn`. No production data yet on how often these occur.
- **Per-pass clean cwd vs. provenance/audit**: The prompt file continues to be written to `iter_dir` for
  sha256 provenance and dispatch logs. grok itself will run from a fresh empty temp dir. This is
  intentional but should be verified in the audit trail after the change.
- **Test coverage**: `consensus_mcp/tests/test_dispatch_grok.py` will need new cases exercising the
  streaming parser path (happy case + malformed stream + cancel-before-text scenarios). Existing tests
  that assert on plain output shape may need updates.
- **Validator interaction remains unchanged by design**: Gate G3 explicitly requires that
  `dispatch-canon-validator` continues to pass with no modifications. The policy questions in the
  "Policy tension" section above are deferred.

**Positive side-effect (not a risk)**: Once streaming is in place, `thought` events become available in the
dispatch logs. This gives future operators a window into *why* a particular packet took time — something
that has been completely invisible under `plain` output.

## Validation performed (2026-05-30)
- Streaming smoke: `grok -p "..." --output-format streaming-json --no-memory --disable-web-search
  --no-plan --no-subagents` from a clean empty cwd → streamed `thought`/`text` tokens, exit 0.
- Full real packet (the moss-clip design consult, ~the same prompt that timed out under `plain`):
  completed in streaming mode, 2106 stream lines, ~9.3KB substantive proposal. grok's analysis
  materially improved the converged plan (it was the contributor that flagged the C6 garbage-frame risk).

## Scope + acceptance gates for the patch

**In scope**:
- `consensus_mcp/_dispatch_grok.py` (primarily `_build_grok_cmd`, the new streaming parser in `_invoke_grok`,
  and related text assembly logic).
- Dispatch log event shape for the new streaming lines (if we choose to log parsed `thought`/`text` events
  distinctly).
- Updates to `consensus_mcp/tests/test_dispatch_grok.py` required to cover the new parser paths.

**Explicitly out of scope for this patch** (see Policy tension section above):
- Any change to `.claude/hooks/dispatch-canon-validator.py` (`GROK_FORBIDDEN_FLAGS` or `ALLOWED_GROK_CWDS`).
- Changes to contributor profiles or higher-level dispatch orchestration.

**Acceptance gates**:
- Gate G1: A real non-trivial packet that reproduces the `plain`-mode stall now completes under the patch
  (streaming + clean per-pass cwd).
- Gate G2: The assembled answer parses against `grok_proposal_schema.json` (review mode uses the grok
  review template path and corresponding extraction; no separate review schema exists).
- Gate G3: `dispatch-canon-validator` still passes with **zero changes** to the validator
  (`GROK_FORBIDDEN_FLAGS` and allowed-cwd rules are untouched; streaming-json + clean temp cwd are not
  forbidden today).
- Gate G4: Trivial smoke prompts still work with no regression in success rate or latency.
- Gate G5: New or updated unit tests in `test_dispatch_grok.py` exercise the streaming parser (including
  at least one case that would have triggered the old silence watchdog).
- Review: Full Workflow A/B per consensus operating procedures (any change to a dispatcher is non-trivial).

**Codification recommendation** (post-patch): The "reliable recipe" command line and parsing rules discovered
in this session should be captured as a constant, docstring, or small internal reference document so the
knowledge does not live only in this patch spec.

---

## RESOLUTION — landed 2026-05-30

Status: **LANDED** on branch `fix/grok-dispatch-streaming-watchdog-2026-05-30`. The full Workflow A/B consensus
review was **skipped by explicit operator authorization** (2026-05-30); instead the design was settled by a
**1-AI grok consult** (grok had final say) plus exhaustive self-review against the live code. Codified in
`_dispatch_grok.py` docstrings/comments per the recommendation above.

**Files changed**: `consensus_mcp/_dispatch_grok.py`; `consensus_mcp/tests/test_dispatch_grok.py` (parser +
flag tests); new `consensus_mcp/tests/test_dispatch_grok_streaming.py` (deterministic streaming/watchdog
integration). Validator and gate untouched.

**Key decisions (where the implementation analysis corrected an initial map):**

1. **cwd fix = Option A, not the "keep /tmp + change process cwd" reconciliation.** The dispatcher now passes a
   fresh empty per-pass temp dir as the **`--cwd` FLAG** (and the Popen cwd), cleaned up in a `finally`.
   Grounds: (a) `dispatch-canon-validator` only inspects grok invocations issued *directly via the Bash tool*
   and only when `--cwd` is present — it never sees the dispatcher's internal `Popen`, so a temp `--cwd` keeps
   **Gate G3** green with zero validator edits; (b) a direct **grok consult (2026-05-30)** ruled that grok's
   recursive watcher keys off the `--cwd` flag (not the OS process cwd), so Option A eliminates the
   `systemd-private` noise and the keep-`/tmp` variant would **not**.

2. **Streaming event shape verified LIVE** (`grok -p ... --output-format streaming-json`, 2026-05-30): events
   are `{"type":"thought"|"text"|"end",...}`; the answer field is **`data`**; `stopReason` is camelCase
   (`EndTurn` / `Cancelled`). The parser (`_assemble_grok_stream`) concatenates `text` events' `data`, ignores
   `thought`, stops at `end`, and raises `GrokStreamCancelledError` on cancel-before-text (a
   `GrokInvocationError` subclass → no wasted parse-retry). It is defensive (skips malformed/typeless lines,
   `data`→`text` fallback, plain-blob passthrough).

3. **Added a private `_sleep=` test seam to `_invoke_grok`** (defaults to `time.sleep`; zero production change)
   so the Gate-G5 watchdog-regression test runs on the *deterministic* clock harness — mirroring
   `_invoke_codex`'s consult-blessed v1.15.9 seam. The "tiny real `poll_interval` + `time_fn`" alternative was
   the exact pattern codex abandoned for flaking on CI.

4. **`output_sha256` keeps its plain-mode meaning** ("the answer grok produced") by assembling at the
   `_invoke_grok` return seam; `_invoke_grok_with_retry` / `main` provenance are unchanged.

**Gates**: G1 (real moss-clip packet already validated above; real-grok dispatcher smoke passes in ~17 s),
G2, G3 (validator self-test 13/13, untouched), G4 (full suite **1793 passed / 8 skipped**), G5 (new
streaming + silence-regression tests) — all green.
