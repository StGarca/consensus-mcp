---
name: consensus-workflow
description: Operating procedures for working with consensus-mcp in any project. Trigger when the user asks to run a consensus consult, dispatch codex/gemini for review, evaluate a workflow #3 vs #4 decision, debug a stalled or failed reviewer dispatch, or any question about HOW the cross-AI consensus workflow runs (as opposed to "consensus init" which only bootstraps). Phrases include "consensus review", "consensus iteration", "consensus consult", "run a consult", "dispatch codex", "dispatch gemini", "workflow 3", "workflow 4", "propose-converge", "post-review".
---

# Consensus-mcp operating procedures

Load-bearing rules for working with consensus-mcp. These are not
project-specific tips — they apply in every project that uses the
cross-AI consensus workflow. Follow them by default; deviate only when
the operator explicitly says otherwise.

## Workflow selection

**Default to workflow #4 (propose-converge with blind-first-reveal) for
any decision with real design surface** — API shape, trade-off, novel
mechanism, anything where reasonable people could disagree on the right
approach. Workflow #3 (post-review: claude implements, codex+gemini
audit) is for **execution per a converged design** or for hot-patches
that block in-flight work.

The mistake to avoid is defaulting to #3 because it's faster and
calling everything "execution." Test: did the converged plan specify
the API shape, error contract, mechanism? If not, those choices are
themselves design surface — go through #4.

Listing 2+ design choices in a single response = workflow #4
candidate. Stop and route to a consult.

## Workflow #3 vs #4 in one line

- **#3 (post-review)**: claude writes the patch first, codex + gemini
  audit afterward. Used for clear execution work.
- **#4 (propose-converge)**: all enabled contributors propose
  independently in round 1 (blind), then converge across reviewed
  rounds. Used for design questions.
- **Advisory**: dispatches happen but no vote is load-bearing. Rare.

## Maximize parallelism — always

**Default to parallel; serial is the choice that needs justification.**
Every operation in a consensus workflow that can run independently
SHOULD run in parallel. This applies to:

- Round-1 peer dispatches (codex + gemini in background, claude
  authors proposal in parallel)
- Round-2+ batches where multiple peers are re-dispatched
- Reading multiple sealed artifacts (one batched call, not N
  serial reads)
- Investigating multiple files / running multiple greps during
  iteration scoping
- Background long-running operations (snapshots, audits, blast-
  radius scans) kicked off to run alongside foreground work
- Cross-iteration parallelism: when two iterations have no data
  dependency, dispatch them as parallel branches (worktrees) and
  converge results

Practice has demonstrated drastic performance improvement when
parallelism is fully exploited. Burden of proof flips: if you
choose serial, the reason must be a real data dependency
(operation B needs operation A's output), not "felt simpler" or
"easier to reason about."

### Round-1 dispatch order (workflow #4) — specific case

Dispatch peer contributors FIRST in background, then author your
own proposal in parallel while they run. Independence (blind-
first-reveal) depends on **visibility**, not **temporal order**.
No contributor sees another's output → independence preserved
regardless of who started writing first. This optimization saves
wall-clock and surfaces dispatcher errors earlier.

Constraints: the goal_packet MUST be final before kicking off
contributors (mid-flight rewrites mean wasted dispatches), and the
"author in parallel" optimization applies to round 1 only — round
2+ explicitly require seeing round-1 outputs.

## Consensus runs to COMPLETION — no gratuitous deferral

**Default: consensus runs to completion when goals + acceptance
gates are clear. Defer only when open design surface genuinely
requires more analysis, the implementation cost is large
(multiple sessions worth), or the work has a real dependency
on something not-yet-built.**

The deferral instinct comes from "splitting reduces blast
radius" + "small PRs are better." Both are RIGHT for genuinely
independent units of work; both are WRONG when the items share
a doctrine boundary or when splitting just postpones the same
decisions without adding analysis.

### The completion test

When authoring a converged-plan.yaml or scoping a deliverable,
ask for each candidate item:

1. Are the acceptance gates concrete and verifiable?
2. Is there ANY open design question remaining (API shape,
   trade-off, mechanism) that actually needs more thought?
3. Is the implementation cost SMALL enough to fit in this
   session? (rough heuristic: <60 minutes of edits + tests)

If (1)=yes AND (2)=no AND (3)=yes → **execute in the same
session, not a future iteration.** "Future iteration" is the
cop-out — a way to pass the work to a hypothetical later-self
without admitting later-self has the same context window and
will face the same decision.

### Anti-patterns to abort

- "iter-XXXX candidate: <thing>" with no concrete reason it
  can't be done now → fold into current iteration.
- "Phase B / Phase C" sequencing when phases share the same
  doctrine boundary → bundle into one.
- "Defer to dedicated iteration" because something is "complex"
  when the complexity is BOUNDED (concrete decisions, finite
  alternatives) → execute now; consult only on the open part.
- Splitting a hot-patch across multiple version bumps when the
  fixes share a single failure-mode → bundle into one tag.
- Writing a goal_packet that ASSUMES future iterations will
  exist → write goal_packets that assume completion is the
  default.

### Application to live consults

Before writing `next_iteration_id: ...` or `deferred to ...`
in a converged plan, run the completion test on each item.
If the test passes, delete the deferral and add the work to
the current scope. Only ratify the deferral if the test
genuinely fails, and name the SPECIFIC blocker
(not "complexity," not "out of scope" — name the missing
data, the dependency, or the design question).

### Sequencing-as-deferral is the same anti-pattern

Don't sequence three independent fixes across three
hot-patches when bundling them into one passes the
completion test. Sequencing produces the same end-state with
more bookkeeping and more chances for drift. The cost of
"big PR" is real but bounded; the cost of perpetual deferral
backlog is unbounded.

## Convergence model — weighted-synthesis by default

**Default convergence model: weighted-synthesis.** All ideas of
all proposals are weighed for benefit to the project as a whole.
No good ideas are lost; no babies tossed with the bathwater.

When writing a `converged-plan.yaml`:

- Structure synthesis per-question (or per-decision-point), naming
  each contributor's position and explaining which element was
  adopted and why.
- When a contributor's position was NOT adopted, explicitly note
  WHY (sequencing, scope, evidence quality) so the good idea is
  preserved as a follow-up rather than lost.
- Scan each rejected proposal element: if there's no follow-up
  iteration, ADR, or CHANGELOG note capturing it, write one.
- Report votes per question (transparency) but treat the synthesis
  as an integration, not a vote winner.

`all-or-nothing` finding-disposition (the legacy default in
`config.py:295-308` for workflow #4) is **edge-case opt-in only.**
Reserve it for binary scope decisions ("ship X or not"), security/
safety gates ("approve patch or reject"), or legal/compliance
verdicts where partial acceptance is incoherent. Operator must
explicitly opt in via `goal_packet.convergence.finding_disposition:
all-or-nothing` with rationale.

**Engine-level follow-up:** `config.py:295-308` currently ENFORCES
`all-or-nothing` for `workflow.mode=propose-converge`. Until that
constraint is lifted (tracked as a separate iteration), the goal
packet must still declare `all-or-nothing` to pass validation, but
**author the convergence document in weighted-synthesis style
regardless.** Once the engine constraint is removed, the default
flips to weighted-synthesis at the data layer too.

## Dispatching codex / gemini

**`--review-target` must be a `.yaml` review-packet, not a raw `.md`
or `.diff` file.** The peer reviewer sandboxes refuse to read paths
under `consensus-state/`, so the file contents have to be embedded
inline. Use `python -m consensus_mcp._author_review_packet
--iteration-dir <dir> --files <comma-list>` to author a packet with
`defect_target.touched_files_contents` populated, then pass
`<dir>/review-packet.yaml` as the review target.

**The MCP wrapper `reviewer_dispatch_codex` has a 45-second silence
threshold** that is often too aggressive on cold-start. The shell
binary `consensus-mcp-dispatch-codex` respects
`CONSENSUS_MCP_STALL_SILENCE_SECONDS` (default 180s) and is the right
fallback when the MCP wrapper times out.

**Proposal-mode dispatch requires `--mode proposal`** on the shell
binaries. The MCP wrappers as of v1.14.0 don't expose this flag —
fall back to the shell CLI for round-1 #4 dispatch.

## Gemini 429 handling (priority-tiered)

- **Low-priority iterations** (chores, doc syncs, simple migrations):
  if gemini returns a single 429, skip it and proceed with a 2-AI
  consensus (claude + codex). Don't retry.
- **High-priority iterations** (design questions, security review,
  blocking decisions): allow one retry on 429; skip only after 2
  consecutive 429s.

The signal "skip vs retry" is the cost of getting it wrong — when a
chore goes through with 2-AI consensus, the worst case is "we get a
weaker review"; when a design decision goes through with 2-AI
consensus, the worst case is "we ratify a flawed design."

## Codex auth model

Codex uses **token-based auth, not API keys.** If a dispatch hangs
with "AuthRequired" messages, the most common cause is a **secondary
MCP** that codex itself is configured to use — not codex's own auth.
Check codex's MCP config (`~/.codex/config.toml`) for any nested MCP
references; remove or disable the offending entry. Codex's own auth
refresh is rarely the culprit.

## Iteration-state persistence (data-loss risk)

`consensus-state/active/iteration-*/` is **gitignored by design** —
in-flight iteration scratch (proposals, audits, partial reviews)
should not pollute the main branch's commit history. But this means
**a `git clean -fdX` (or a CI build agent that wipes untracked
files) will silently delete your in-progress consensus work.**

Mitigation: `python -m consensus_mcp._snapshot_state snapshot --label
<short-label>` after each iteration close. Snapshots live on the
orphan branch `consensus-state-snapshots`. To recover:
`python -m consensus_mcp._snapshot_state restore --tag <tag-name>`.

For long iterations: snapshot mid-flight too. Cheap insurance.

## Verifying peer-cited file content

When a codex or gemini reviewer cites file content in a finding,
**grep the cited file to confirm the content exists** before
acting on the finding. Reviewers occasionally hallucinate
("README.md line 67 contains @docs/roadmap/v1.14.0-deferred.md"
when line 67 actually contains `@v1.14.0`).

If the cited content doesn't exist (verified via grep), dismiss the
finding with empirical evidence in the commit message:
"codex-rev-NNN dismissed: cited content at <path>:<line> does not
exist (grep shows `<actual>`). Reviewer hallucination."

Don't ignore reviewer findings silently — write the dismissal as a
matter of record so the audit trail survives.

## Verify before invent — cite before propose

**Before introducing any step that touches an external system,
channel, distribution mechanism, or inferred environmental
capability — verify it exists in the project and CITE the
verification source inline.**

The rule applies to publish/install/deploy/distribute steps;
sending messages (Slack, email, webhooks); posting to external
services; uploading to registries (PyPI, npm, Docker Hub,
GitHub Packages); registering anywhere; inferring CI/CD
pipelines, license terms, dependency availability, tool
presence, database schemas — any inferred capability or
channel.

Required citation form: a one-line inline reference next to
the proposed step:
- `verified via README.md:40 — install URL is git+https`
- `verified via .github/workflows/release.yml:12 — publish job exists`
- `verified via curl https://pypi.org/.../json — registered`
- `not found — not proposing` (when verification fails)

**When verification produces DISCONFIRMING evidence (no creds,
no workflow, registry 404), treat that as the SIGNAL — not as
a credential gap to fill.** "no creds" → check first whether
the channel exists in the project at all; do not jump to
"operator needs to provide creds." Operator pushback ("what
is X?") → check whether X was introduced by inference rather
than verification; do not jump to "operator unfamiliar with X."

**Anti-pattern to abort on sight:** "I'll handle X and hand
you Y at the end" framing makes invented requirements feel
inevitable. Better framing: "verified-required steps: <list
with citations>; uncertain steps: <list>; please confirm or
correct uncertain ones."

Scope: required for high-impact actions (external system,
channel, irreversible op, anything operator-visible). NOT
required for routine local-dev steps (git status, file reads,
test runs against a known repo).

## Artifact-scoped claims — no global "fixed" or "shipped"

**When making any "fixed", "shipped", "done", "complete"
claim, the claim must be ARTIFACT-SCOPED — naming the
specific version + commit/tag + install path + bundled
content + known residual defects.** Globally-quantified
claims like "X is fully shipped" or "Y is fixed" are
forbidden when the affected surface is broader than the
artifact you actually changed.

**The canonical scenario:** an immutable tag and a dev
branch with a fix are NOT the same artifact. Don't glue
them together. If you fixed something on the v1.14.1 dev
branch, the v1.14.0 tag at commit 8e0dab2 STILL ships the
defect embedded in its wheel/install — name that explicitly.

Required claim form when announcing a fix or completion:
- Name the artifact: `v1.14.1 tag at commit 5f6cfe7`
- Name what the artifact contains: "the corrected skill"
- Name the unfixed surface explicitly: "the v1.14.0 tag is
  immutable and STILL ships the buggy skill; users on
  v1.14.0 need to upgrade to v1.14.1 to get the fix"
- For multi-artifact surfaces, list each with status:
  "fixed in dev branch ✓, fixed in v1.14.1 tag ✓, v1.14.0
  tag still defective ✗"

**Forbidden phrases when surface > what you changed:**
- "X is fully shipped"
- "X is done"
- "X is fixed" (without scoping)
- "the only channel that exists" (when an artifact-content
  caveat is being suppressed)
- "all clear" / "no issues remaining"

## Non-trivial changes go through peer review

Any **non-trivial change to consensus-mcp itself** must go through
peer review (workflow #4 if design surface, workflow #3 if pure
execution). The threshold for "trivial" is small: typo fixes, doc
formatting, individual log-line tweaks. Anything touching
contributor adapters, dispatchers, tool registration, sealing, or
goal-packet validation = non-trivial.

This rule does NOT apply to the user's own project work using
consensus-mcp as a tool — there, the operator decides what's
non-trivial in their codebase.

## "Consensus" trigger word

When the user says "consensus" in a sentence about reviewing,
deciding, or analyzing — **use the consensus-mcp tools (workflow
#4 or workflow #3 as appropriate), NOT the older /council skill.**
Council was a single-Claude multi-persona simulation; consensus-mcp
is a real cross-AI workflow with sealed-provenance peer reviewers.

## Release cadence — Friday cut if anything landed

**Cut a release tag every Friday if at least one iteration closed
that week. Release-cut is a procedure with a trigger, not an
ad-hoc decision.**

The structural failure mode this prevents: iterations close
continuously (acceptance gates pass → done), but the release-cut
ceremony has no trigger of its own. So work accumulates on the
`v<X.Y.Z>` branch indefinitely and downstream pipx/PyPI users get
stale versions while the README documents features they cannot
install. This actually happened — v1.14.0 sat at 37 commits across
iter-0009..iter-0043 over 3 days with a release-ready CHANGELOG
and no tag.

**Cut sequence (apply in order on the v<X.Y.Z> branch tip):**

1. Update `CHANGELOG.md` date stamp to the cut date; add an
   addendum for any iterations that landed after the section was
   first authored.
2. Verify `pyproject.toml` `version` matches the branch.
3. Run the full test suite; surface regressions before tagging.
   Document any pre-existing known-issue flakes in CHANGELOG so
   they aren't mistaken for v<X.Y.Z> regressions.
4. `git tag -a v<X.Y.Z> -m "..."` on the branch tip.
5. `git push origin refs/heads/v<X.Y.Z> refs/tags/v<X.Y.Z>` (use
   explicit `refs/heads/` and `refs/tags/` to disambiguate; the
   branch and tag now share a name).
6. **Release is complete.** Distribution is git-tag-based — users
   install via `pipx install git+https://github.com/stgarciaarca/
   consensus-mcp.git@v<X.Y.Z>`. There is NO PyPI publish step;
   the package is not registered on PyPI. Optional: build wheel
   + sdist locally (`python -m build`) for local smoke-testing
   only, not for upload.
7. Branch the next release from the v<X.Y.Z> tip: `v<X.Y.Z+1>`
   for hot-patches, OR `v<X.(Y+1).0>` for the next minor. Bump
   `pyproject.toml` on the new branch to the new dev version
   (e.g., `1.14.1.dev0`); add a `## X.Y.Z+1 - unreleased` stub
   to CHANGELOG.md.
8. **Bump README install URLs to `@v<X.Y.Z>` (the just-cut tag,
   NOT the dev version) on the new dev branch.** This step
   prevents the bleeding-wound failure mode where the README
   continues to point users at an older defective tag after a
   fix is published. Grep `@v<old-prefix>` to catch all stale
   refs; bump them all in one commit.
9. Push the new branch: `git push -u origin v<X.Y.Z+1>`.
10. **If the just-cut tag has a known issue worth flagging to
    users on older versions** — add an entry to
    `docs/advisories.md` naming the affected versions, the
    issue, the correct upgrade target, and the user action
    required. The advisory is the long-lived record; CHANGELOG
    entries get buried as new releases ship.

**Distribution convention:** consensus-mcp ships via git tags +
pipx, NOT PyPI. README documents `pipx install
git+https://github.com/.../consensus-mcp.git@vX.Y.Z`. No
`twine upload`, no `~/.pypirc`, no PyPI workflow exists. If you
catch yourself proposing a PyPI step, stop — you're adding a
channel that is not part of this project's release flow.

**Branch convention:** Release branches are SELF-CONTAINED. `main`
is NOT progressed by release cuts — it stays at whatever its tip
was before the v<X.Y.Z> branch was cut. Each new release branches
from the previous release's tip. `main` is essentially a stable
pointer to "the divergence point of the most recently cut release."
Do NOT merge release branches back into main.

**Variations:**

- **Empty-week skip:** if zero iterations landed, no cut. Never
  cut empty releases.
- **Hot-patches between Fridays:** cut as `v<X.Y.Z+1>` whenever a
  hot-patch lands; don't wait for Friday for a fix release.
- **Operator hold:** the operator may explicitly delay a cut
  ("hold v1.14.0 until iter-0050"). Default without explicit
  hold is "cut Friday."

Release-cut is itself an operating procedure — not a design
decision and not subject to peer consensus.

## When in doubt

The conservative move is **workflow #4 with all enabled contributors**.
The extra wall-clock is bounded; the cost of ratifying a flawed
design via 2-AI audit is not.

If a dispatch is failing in a way you can't diagnose in 5 minutes,
stop and ask the operator. Don't keep retrying — there is almost
always a config or auth issue upstream, and burning more dispatches
deepens the hole.
