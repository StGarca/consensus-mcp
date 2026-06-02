# First-consult quickstart (untouched project, Path A)

If you're an AI agent running consensus-mcp for the first time on a project that has never been touched by it - follow this checklist in order. Each step has a copy-paste command. The intent is: zero source-reading required to reach a sealed iteration.

This page is verbatim derived from the operator's `iter-0001-houseinfo-design` debrief (`debrief-2026-05-26-iter-0001-first-consult.md` Section 7), packaged with the install so the next-session agent never needs to discover it.

If you're using **`consensus-mcp-seal-iteration`** (v1.32.0+), several of these steps collapse into one command - see Section 7 below. For the manual path, follow 1-6 in order.

---

## 1. Verify the install

```bash
command -v consensus-init
pipx list | grep consensus-mcp
command -v codex; command -v gemini; command -v kimi; command -v grok
```

If any peer CLI is missing AND your tier requires it: install the missing CLI(s), OR accept a smaller panel and inform the operator. v1.32.0 supports 2-to-N AIs.

## 2. Choose bootstrap vs. convention

**If the project already has `.consensus/config.yaml`** - bootstrap mode. Skip to step 3.

**If it doesn't** - convention mode. Create the containment markers at the project root:

```bash
mkdir -p consensus_mcp/validators consensus-state
printf '%s\n' '/consensus_mcp/' '/consensus-state/' '/.consensus/' '/.delivery-readiness/' >> .gitignore
export CONSENSUS_MCP_REPO_ROOT="$PWD"
```

(v1.32.0 error messages emit exactly this block on `RepoRootResolutionError`. If you get that error, copy from there.)

## 3. Author the goal_packet

Use the smoke fixture as a template:

```bash
PIPX_VENV=$(pipx environment --value PIPX_LOCAL_VENVS)/consensus-mcp
ls $PIPX_VENV/lib/python*/site-packages/consensus_mcp/tests/fixtures/dispatch_codex/goal_packet_smoke.yaml
```

Required fields: `goal.summary`, `goal.desired_end_state`, `goal.non_goals`, `allowed_files`, `acceptance_gates`, `stop_conditions`, `authorization.{authorized_by, authorized_at_utc, scope_signature}`.

For deep tier, add: `workflow.mode: propose-converge`, `convergence.{finding_disposition: weighted-synthesis, rule: strict-majority}`, `workflow.panel: [claude, codex, gemini, grok, kimi]`.

## 4. Author the review-packet

```bash
python -m consensus_mcp._author_review_packet \
  --iteration-dir consensus-state/active/<iter-id> \
  --files <comma-separated paths to in-scope files> \
  --repo-root "$PWD"
```

The helper embeds the file contents inline in `touched_files_contents` so peer reviewers (who run in sandboxes that can't reliably read repo files) reason from a snapshot.

## 5. Dispatch peers in parallel - PER-REVIEWER pass-ids

```bash
ITER=<iter-id>
DIR=consensus-state/active/$ITER

consensus-mcp-dispatch-codex   --goal-packet $DIR/goal_packet.yaml --iteration-dir $DIR --mode proposal \
                               --review-target $DIR/review-packet.yaml --reviewer-id codex-$ITER  \
                               --timeout-seconds 600 &

GEMINI_CLI_TRUST_WORKSPACE=true \
consensus-mcp-dispatch-gemini  --goal-packet $DIR/goal_packet.yaml --iteration-dir $DIR --mode proposal \
                               --review-target $DIR/review-packet.yaml --reviewer-id gemini-$ITER \
                               --timeout-seconds 600 &

consensus-mcp-dispatch-grok    --goal-packet $DIR/goal_packet.yaml --iteration-dir $DIR --mode proposal \
                               --review-target $DIR/review-packet.yaml --reviewer-id grok-$ITER   \
                               --timeout-seconds 600 &

consensus-mcp-dispatch-kimi    --goal-packet $DIR/goal_packet.yaml --iteration-dir $DIR --mode proposal \
                               --review-target $DIR/review-packet.yaml --reviewer-id kimi-$ITER   \
                               --timeout-seconds 600 &

wait
```

Each `--reviewer-id` is unique per family - this prevents the T6 seal-index collision when multiple peers dispatch with a shared `pass-id` (debrief Section 3.3).

**Codex cold-start tip:** `export CONSENSUS_MCP_STALL_SILENCE_SECONDS=300` if codex hangs at startup.

**Grok behavior (v1.32.0+):** the dispatcher passes `--max-turns 100` because grok counts MCP-tool-discovery messages as turn-budget. If grok's own MCP config has tools with `.` in their names, you'll see noisy ERROR logs about "invalid tool name" - non-fatal.

## 6. Author your own (claude's) proposal in PARALLEL

While the peer dispatches run in the background, author `$DIR/claude-proposal.yaml` from your own reading of the goal_packet + review-packet. **Do NOT read the peer outputs first** - that violates blind-first-reveal. Claude's proposal goes into the panel as the orchestrator's contribution.

## 7. Close + seal (v1.32.0 consolidated CLI)

This is the step where pre-v1.32.0 agents hit 4-5 friction cases in a row (Sections 3.5, 3.6, 3.7, 3.8 of the debrief). The new `consensus-mcp-seal-iteration` CLI eliminates them:

```bash
# 7a. canonicalize per-family review YAML names + scaffold iteration-outcome.yaml
consensus-mcp-seal-iteration prepare --iteration-dir $DIR

# 7b. EDIT iteration-outcome.yaml - set closing_state to one of the sealed states:
#     quorum_close_passed | implementation_ready_apply_landed
$EDITOR $DIR/iteration-outcome.yaml

# 7c. lint every YAML (catches embedded ':' in unquoted scalars BEFORE the verifier)
consensus-mcp-seal-iteration lint --iteration-dir $DIR

# 7d. mint the design-approved marker (computes canonical hash + writes pointer)
#     For brainstorming -> writing-plans pipeline, use --writing-plans-followup
#     to get scope_glob='docs/consensus/**' (eliminates Section 3.9 re-mint).
consensus-mcp-seal-iteration mint \
  --iteration-dir $DIR \
  --closing-state quorum_close_passed \
  --writing-plans-followup    # or pass --scope-glob explicitly

# 7e. verify against a known in-scope path (smoke check; CI gate)
consensus-mcp-seal-iteration verify --target-path docs/consensus/plans/<your-plan>.md
```

Pre-v1.32.0 (manual): see the debrief's Section 7 steps 6-11 for the hand-rolled equivalent.

## 8. Yaml hygiene rule

Every value containing `:` MUST be quoted. The `lint` subcommand (7c) catches this - but you can also pre-flight:

```bash
python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" $DIR/*.yaml
```

## 9. Windows shell quirks

If you're running through the Bash gate on Windows:
- Use `command -v X` (POSIX) instead of `where X`.
- Avoid `2>&1` in commands that go through the gate (drop the redirection or use the Grep tool).
- For env-var-prefixed commands (`VAR=x cmd`), use `env VAR=x cmd` or dispatch via `ctx_execute`.

## 10. Where to look when something goes wrong

`consensus-state/state/dispatch-log.jsonl` is the single most important debugging artifact. It records:
- Every dispatch start / heartbeat / streamed line / completion / abort with provenance hashes.
- `last_streamed_line_seq: null` heartbeats mean the CLI is cold-starting (NOT stuck).
- `dispatch_aborted` events name `abort_source` and `abort_reason` verbatim.

Read it before any retry:
```bash
tail -50 consensus-state/state/dispatch-log.jsonl | python -m json.tool
```

## 11. Don't claim "shipped" before the delivery token

The design-approved marker authorizes **implementation**. The **delivery-readiness token** (`consensus_mcp._delivery_readiness.mint_delivery_token`) authorizes **done**. Run BOTH:

```bash
# After all your modifications are in:
python -c "
from pathlib import Path
from consensus_mcp._delivery_readiness import mint_delivery_token
for f in ['<file1>', '<file2>']:
    mint_delivery_token(
        Path(f),
        design_consensus_ref='<iter-id>',
        vetted_by=['codex-<iter-id>', 'gemini-<iter-id>'],
        known_flaws=[], operator_ack=False, action_classes=[],
        repo_root=Path.cwd(),
    )
"
```

---

## Falsification

If you reach the end of this quickstart and STILL hit friction the debrief catalogued, that's the **R3 refuting observation** for v1.32.0's converged plan - file an updated debrief and the next iteration will name a more aggressive fix (likely the deferred resolver rewrite).

The target: completing a fresh Workflow A propose-converge consult on a new project in **20-30 minutes**, not 90.
