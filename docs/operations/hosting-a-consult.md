# Hosting a consult as any AI

Consensus-mcp is host-agnostic. In weighted-synthesis / proposal mode the host
AI cherry-picks ideas on their merits after collecting blind reviews. The panel
then reviews the host's plan in standard and deep tiers. The host cannot dismiss
a reviewer objection to the plan on its own: unresolved holdouts go to the user.

## Scaffold, collect, disposition, approve

```bash
consensus-mcp-start-consult --question "What should change?" --scope-glob 'src/**'
consensus-mcp-consult --iteration <iteration-from-scaffold> --tier standard
consensus-mcp-approve --iteration <iteration> --init-dispositions
```

The scaffold creates `goal_packet.yaml` and `review-packet.yaml`. Finalize the
packet before dispatch. The one-command panel runner launches independent seats
concurrently and writes `consult-summary.yaml`, including requested/used models,
fallback reasons, sealed/failed status, failure class, wall time, and token counts
when available. No reviewer-specific dispatch order is required.

`quick` requires exactly two seats (configured `consult.quick_seats`, otherwise
the first two); `standard` uses the configured/default panel; `deep` adds
`opencode-deepseek` when its CLI is available. The default panel is Claude,
Codex, Kimi, Grok, and Gemini, plus opt-in HTTP DeepSeek when
`CONSENSUS_MCP_DEEPSEEK_BASE_URL` is set and opt-in HTTP Qwen when
`CONSENSUS_MCP_QWEN_BASE_URL` (with `CONSENSUS_MCP_QWEN_API_KEY`) is set.
`--seats` overrides the panel explicitly. The engine's separate tier router
uses all enabled independent reviewers, with a minimum of two, and applies a
deep governance/security floor.

The tiers also set the number of passes:

- `quick`: one ideas pass, followed by the existing dispositions-only approval.
- `standard` (default): one ideas pass and one plan-review pass. Every objection
  must be accepted or decided by the user; a host rebuttal remains a holdout.
  If the host edits the plan or dispositions after that plan pass to apply an
  accepted objection, a narrow confirmation check runs with the same reviewers.
  It checks that the edits apply accepted objections and change nothing unrelated;
  reviewers must not raise new ideas.
- `deep`: repeat plan-review passes until no sealed reviewer has an open
  objection. A rebuttal goes into the next packet so the reviewer can reconsider
  it. If it is not raised again, it is resolved. After five plan passes with
  holdouts, the run returns `needs_user_decision` and refuses another pass until
  the user decides. `consult.max_plan_passes` in `.consensus/config.yaml`
  overrides the default limit of five.

## Sensitive data

Kimi and Qwen are marked `sensitive_data_allowed: false` and never review a
project that holds sensitive data. When either is on the panel and neither the
run nor the project says whether the project is sensitive, the consult launches
nothing and returns `status: needs_sensitivity_answer` with a `question` (the CLI
exits 2 and prints it). The host must ask the user that question, not guess, then
re-run with `--sensitive-data yes` (those seats sit the run out) or
`--sensitive-data no` (they take part). The MCP tool takes the same answer as
`sensitive_data`. A project can answer once with `sensitive_data: yes` or
`sensitive_data: no` in `.consensus/config.yaml`; an explicit run answer wins.
The answer, its source and any excluded seats are recorded in
`consult-summary.yaml`.

The same policy covers every other way a restricted seat can be launched. The
engine path (`consensus.run_iteration`) returns `needs_sensitivity_answer` with
the question when Kimi or Qwen is enabled and the project has not declared
`sensitive_data`, and leaves them out when it is `yes`. A restricted seat's own
dispatcher (`consensus-mcp-dispatch-kimi`, the HTTP dispatcher for Qwen) refuses
unless the run or the project explicitly says `no`. The restriction on the
built-in Kimi and Qwen seats cannot be switched off in project config; a
self-hosted endpoint for the same model needs its own seat name.

Any HTTP seat whose endpoint is a China- or Singapore-operated provider
(DeepSeek's own API, Alibaba Cloud, Moonshot, or a `.cn` host) is restricted the
same way, whatever its profile says. DeepSeek on a local server or a provider in
another region is not restricted.

The software cannot tell whether an answer came from the user or from an AI
answering for itself. It records where each answer came from; hosts must ask.

## Cherry-pick, then review the plan

Author `converged-plan.yaml` and fill every row of `dispositions.yaml`: accepted
with reasons, rejected with evidence proving it wrong, or deferred with rationale.
For standard and deep, submit the host's plan to the same panel:

```bash
consensus-mcp-consult --iteration <iteration> --plan-pass
consensus-mcp-approve --iteration <iteration> --init-objections
```

The MCP tool `consensus.consult_panel` uses `pass_kind: plan`; its default
`pass_kind: ideas` collects ideas. Plan passes reuse the original tier and seats,
and apply the same sensitive-data gate. Each packet includes the current
`converged-plan.yaml`, dispositions (highlighting rejected and deferred ideas
with the host's evidence), and earlier objections and host responses. Its plan
hash binds the reviewed version. In standard, applying an accepted objection
after the plan pass requires a confirmation pass on the changed plan and
dispositions:

```bash
consensus-mcp-consult --iteration <iteration> --confirm-pass
```

The MCP tool accepts `pass_kind: confirm` for this check. It shows reviewers the
before and current plan, the diffs for the plan and `dispositions.yaml`, and the
accepted objections with the host's responses. They decide only whether the
edits apply those objections faithfully and change nothing unrelated; they
cannot add ideas. Confirmation is available only for standard-tier consults and
is refused if neither file changed since the latest plan pass or no objection
was accepted. No findings with `goal_satisfied: true` confirms the edits; findings
tagged `[unfaithful]` or `[unrelated_change]` (and any other tag) require a user
decision. A confirmation finding must be shown to the user. A user `yay`
means the host must fix the change and run confirmation again; `nay` dismisses
the finding. A later clean confirmation on the current version resolves earlier
confirmation findings. This adds a check only when the host edits after the plan
pass and keeps standard at two review passes. In deep, every change needs a pass
on the new version, which is how the loop continues until no reviewer objects.
At least two distinct non-host model families must
seal reviews; failed reviewers never count against the plan.

For plan-review passes, reviewers prefix each finding's existing `summary` with one of
`[wrong_rejection]`, `[wrong_deferral]`, `[wrong_acceptance]`, `[conflict]`,
`[gap]`, or `[other]`. Missing or unknown tags mean `other`; reviewer output
schemas are unchanged. No findings with `goal_satisfied: true` means no objection.

Fill `plan-objections.yaml`: `pending` awaits a response; `accepted` means the
host changed the plan; `rebutted` requires evidence and a host response. In
standard, every rebuttal goes to the user. In deep, the next plan pass carries
the rebuttal to the reviewers. When a decision is needed, show the user each
holdout's reviewer, model, objection, and host response. Ask them and record the
answer; the host must never answer for them:

```bash
consensus-mcp-approve --iteration <iteration> --user-decision '<id>=yay'
consensus-mcp-approve --iteration <iteration> --user-decision '<id>=nay'
consensus-mcp-approve --iteration <iteration> --user-decision '<id>=send_back'
```

`yay` supports the objection: the host must accept it and update the plan.
`nay` dismisses it. `send_back` grants up to five more deep passes (or the
configured limit) for another review cycle. Each answer records
`decided_by: user`. The software records that attribution; it cannot establish
whether an AI actually asked the user.

Once the tier's checks pass, run:

```bash
consensus-mcp-approve --iteration <iteration> --scope-glob 'src/**'
```

Approval validates the complete disposition and objection tables, binds their
hashes to the outcome and any approval marker, and checks the current plan hash.
Two distinct non-Claude model results are required; family
means model, not vendor. Failed reviewers do not count against the change.
Default on-demand governance seals the result without arming an edit gate;
configured enforcing modes additionally mint scoped approval markers.

`consensus results` and `consensus.results` expose the results scorecard,
including per-model host dispositions. Host acceptance is a weak signal, not
proof of correctness. Gold outcomes require an external test, smoke, or
falsification result tied to an exact sealed idea and model; AI self-grading is
rejected. To record an external result, use:

```bash
consensus results --record-gold <iteration> <row-id> --useful true --evidence <test-ref> --adjudicator external-test
```

## Long-running jobs and boot behavior

The MCP surface includes `consensus.consult_panel`. For allowlisted long-running
reviewer tools or `consensus.run_iteration`, use `consensus.job_start`, then
`consensus.job_status` / `consensus.job_result`; `consensus.job_cancel` requests
process-tree cancellation. Server validation findings warn and audit by default;
set `CONSENSUS_MCP_STRICT_BOOT=1` to refuse startup with exit 2.

## Windows notes

- Run tests with a clean environment, without `PYTHONPATH`; use the intended
  virtual environment's Python with `-I` to avoid inherited Python path pollution.
- Launch `opencode` from PowerShell.
- Codex's sandbox keeps `.git` read-only. The host owns Git operations.
- Reviewer CLIs need writable session/config/log paths in their own user profile.
