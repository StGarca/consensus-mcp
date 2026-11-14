# Cold-start onboarding remediation plan

**Date:** 2026-06-02
**Status:** Draft -> pending consensus consult ratification (anchored)
**Basis:** 9-facet + 2-critic cold-start UX analysis (workflow wf_d8026658-140,
11 agents). Every claim below is file:line-grounded in that analysis.

## The scenario (no self-as-example)

The ONLY things that exist: a fresh Claude Code install (no tailored CLAUDE.md,
no memories, no consensus SessionStart directive beyond what consensus-mcp ships)
and a freshly pipx-installed consensus-mcp. A user who has NEVER used consensus
just ran `consensus init` in a clean project. Goal: neither the AI nor the user
ever has to GUESS how to set up or use consensus.

## Target: the guess-free golden path

1. `consensus init` finishes and tells the user (and arms the AI) the EXACT next
   move + whether enforcement is actually on.
2. On the first session the AI has a GUARANTEED-visible runbook (managed CLAUDE.md
   block + a dormant-safe SessionStart breadcrumb + a top-of-skill COLD-START
   section), not 770 lines of doctrine it must mine.
3. "run a consensus review on X" -> ONE deterministic path: scaffold -> fan out in
   parallel -> read ALL families' packets -> synthesize -> `consensus-mcp-approve`
   -> the marker ARMS the gate -> edits unblock.
4. Failure paths (partial panel, auth-fail mid-consult, crashed session) have a
   documented recovery.

## P0 - VERIFIED CODE BLOCKERS (bugs, not just docs; some shipped in v1.40)

P0.1 **`consensus-mcp-approve` does not arm the gate.** It mints
`.consensus/design-approved` but never writes the session-active marker, so
`session_active` -> `gate_should_enforce` stays False and edits are silently
ALLOWED after "approval." The v1.40 codified happy path therefore does NOT
actually enforce. Fix: `_approve_consult.approve_consult` writes the session
marker (mirror `_seal_iteration.py:299`) on successful mint. (Both critics: #1
blocker.)

P0.2 **Inspector tools silently drop kimi/grok reviews.**
`consensus_get_iteration_outcome._KNOWN_CONTRIB_FILES` and
`review_read_post_seal`'s reviewer allowlist cover only codex/claude(/gemini), so
a cold AI reading back a 4-AI panel LOSES kimi + grok. Same bug class as the
v1.40.1 `_count_non_claude_reviewers` glob fix. Fix: drive reviewer coverage from
the contributor registry, not hardcoded lists.

P0.3 **Stop gate names a phantom command + no delivery-token CLI.** The Stop gate
orders "invoke consensus-verify / mint a delivery token", but `consensus-verify`
exists in zero `[project.scripts]` and zero modules, and `mint_delivery_token` has
no CLI (only a hand-written `python -c`). Fix: ship `consensus-mcp-deliver`
(wrapping `mint_delivery_token`), point the Stop gate at it, delete the phantom.

P0.4 **No console-script presence guard.** A stale/editable install can lack
`consensus-mcp-approve` in entry_points with no smoke test. Fix: a test asserting
every `[project.scripts]` entry is importable AND present in installed metadata.

## P0-OPS - CONSULT-MACHINERY HARDENING (field-discovered LIVE this session)

These are the uncontrolled operational variables that broke a real consult run on
2026-06-02 (twice each, in some cases). A cold AI running its first consult WILL
hit them. Every one must be locked down (fixed + codified + tested) so the consult
machinery is deterministic.

P0-OPS.1 **pass_id is a GLOBAL collision namespace.** The T6 seal index keys on
pass_id across ALL iterations ever (content-identity tamper guard). Reusing any
pass_id - especially bare integers `1`/`2`/`3` - collides with a cryptic
`index_collision` that even cites an unrelated iteration's file. FIXED 2026-06-02:
`derive_pass_id(iteration, packet, contributor)` (a hash) is now the dispatcher
DEFAULT; omit `--pass-id`. Codified in the skill. (Downstream: the family-counter
regex was broadened to accept the hash suffix in the sealed-mirror filename.)

P0-OPS.2 **Stale .pyc / install drift / Python-version skew.** The pipx venv runs
Python 3.13; an import-sanity check under the repo `.venv` (3.12) only refreshed
3.12 bytecode, so the dispatch ran STALE 3.13 bytecode - the source had the fix but
the binary ran the old code (WSL mtime invalidation did not fire). A source fix is
NOT live until the matching `.pyc` refreshes. Fix: the console-script test must
verify VERSION/BEHAVIOR (not just importability); `--install-claude-code` / init
should `compileall -f` (or stamp + check a version), and surface install-drift.

P0-OPS.3 **kimi's integrity check is incompatible with parallel dispatch.** kimi
content-snapshots the WHOLE repo and REJECTS its own review if ANY file changed
during its run - including a SIBLING reviewer's output (grok's, this run). Running
all reviewers in parallel trips it. Fix: SCOPE kimi's integrity check to ignore
`consensus-state/`, untracked files, and sibling `*-review*.yaml` / reviewer
scratch (it should guard against the kimi SUBPROCESS mutating tracked source, not
against concurrent siblings); AND/OR codify that kimi runs LAST / alone with the
repo quiescent. The current whole-repo snapshot is too broad.

P0-OPS.4 **grok writes its proposal to the launch cwd (repo root).** This run grok
wrote a 28KB `grok_coldstart_proposal.json` into the repo root - polluting the repo
AND triggering P0-OPS.3. Fix: the grok dispatch must confine grok's cwd to its
disposable temp / iteration dir and never let it write to the repo root; capture
its output from the sealed artifact, not a stray cwd file.

P0-OPS.5 **MCP `reviewer_dispatch_*` wrappers have a 45s cold-start timeout** that
kills real reviews. CODIFIED: use the shell binaries (`consensus-mcp-dispatch-*`),
which honor `CONSENSUS_MCP_STALL_SILENCE_SECONDS` (set 300); never the wrappers for
a real consult. (Or: fix the wrappers to honor the same threshold.)

P0-OPS.6 **Dispatch ergonomics - codified.** One Bash call PER reviewer (its own
observable shell, never a bundled `& ... wait`), so a hang/failure is isolated and
diagnosable. Omit `--pass-id` (auto-hash). These are now in the skill.

P0-OPS.7 **Partial-panel / mid-consult failure is NORMAL, not exceptional.** This
single run hit a revoked codex token, a kimi integrity rejection, and a grok stray
write. The flow MUST define recovery: proceed with the >=2 families that sealed,
re-dispatch only the failed family (alone, repo quiescent for kimi), and never
silently drop a family. (See P3.1.)

## P1 - DETERMINISTIC SURFACES EVERY COLD AI/USER SEES

P1.1 **Per-project `consensus init` wires no hooks.** settings.json hook wiring is
gated behind the SEPARATE global `--install-claude-code`, and the init output
never says so -> a cold user believes consensus is set up while gating/precedence
are inert. Fix: per-project init either auto-wires the hooks OR prints a loud
"enforcement is OFF until you run `consensus-init --install-claude-code`" warning.

P1.2 **Managed CLAUDE.md block has zero consensus content.** The injected
`CLAUDE.md`/`AGENTS.md` block is generic upstream coding guidelines
(`contributor_instructions/base.md`); a restarted cold AI reading it learns
nothing about how to run consensus. Fix: prepend a 5-8 line consensus operating
preamble (consensus is active here; to run a review say X; to approve run Y;
the runbook lives in the consensus-workflow skill).

P1.3 **Post-init output dead-ends at "restart".** `_print_status_summary` ends at
"Restart Claude Code ... to activate" and never gives a first-consult trigger
phrase or the approve command. Fix: extend the Next-steps block with the literal
phrase to type + the one-line `consensus-mcp-approve` usage.

P1.4 **SessionStart is silent when dormant.** In an init'd repo with no consult in
flight the injector returns 0 (injects nothing), so the AI gets no "how to start".
Fix: a dormant-safe branch that injects a SHORT "consensus is installed; to start
a review do X" breadcrumb (distinct from the active-consult precedence framing).

P1.5 **No single canonical runbook the AI is guaranteed to see.** The real
procedure is buried at `consensus-workflow/SKILL.md:141` under ~140 lines of
doctrine, and the trigger description does not match natural phrases like the
README's own "get a consensus review on this change". Fix: a top-of-file
COLD-START runbook section + broadened trigger synonyms.

P1.6 **Two contradictory shipped authorities.** `first-consult-quickstart.md`
teaches `seal-iteration` EDIT_ME + hand-authored goal_packets + one schema; the
skill teaches `consensus-mcp-approve` + a different schema; `goal_packet_schema.yaml`
is titled "Phase 4 bounded self-drive" and disagrees with the quickstart's
required fields. Fix: single-source the seal/approve step (rewrite the quickstart
to the v1.40 surface) AND the goal_packet schema.

## P2 - PACKET HANDLING + SCAFFOLDING (kill the "how do I start / decode" guesses)

P2.1 **No scaffold; everything required, nothing created.** `consensus_run_iteration`
requires iteration_dir + goal_packet_path + target_path, none of which exist on a
cold start, and nothing scaffolds them. Fix: a cold-start entrypoint
(`consensus.start_consult` MCP tool + console script) that creates
`consensus-state/active/<iter>/`, writes a schema-valid goal_packet (with
scope_signature), and writes the session marker - the one self-documenting "call
this FIRST" surface.

P2.2 **No decode/synthesis recipe.** Nothing tells a cold AI that a dispatch
returns a `sealed_path` (a PATH, not content), which tool reads it, the packet
field schema, or HOW to turn N packets into a converged-plan. Fix: a "Decode the
returned packets" skill section + a per-field packet->converged-plan synthesis
recipe.

P2.3 **scope_glob is required with no derivation.** Fix: `consensus-mcp-approve`
suggests a glob derived from the iteration's touched files on missing/overbroad
input.

## P3 - ROBUSTNESS / RECOVERY (the failure paths every facet assumed away)

P3.1 **Partial-panel / auth-failure-mid-consult is undefined.** A revoked/expired
token is a non-retryable hard fail with no documented "proceed with >=2 vs
re-dispatch one family vs abandon" procedure. (Live example this session: codex's
token is revoked.) Fix: define + document the recovery policy.

P3.2 **Crashed-consult resume + stale-marker hygiene.** `session_active` returns
True for ANY unsealed dir under `active/`, so an abandoned prior consult leaves
the gate armed against an unrelated scope; `consensus.resume` cannot bootstrap.
Fix: stale-marker detection/cleanup + a cold-safe resume that returns a happy-path
bootstrap message instead of `iteration_id_not_found`.

P3.3 **Version-skew undetectable.** Vendored skills/quickstart are copied into
`~/.claude` at `--install-claude-code` time and never re-synced; no
skill-vs-binary compatibility signal. Fix: a `--version`-stamped skill + a drift
check.

## P4 - WIZARD POLISH

P4.1 Workflow C (autonomous-execute) is selectable but raises NotImplementedError
-> flag/suppress it in the prompt + skill.
P4.2 Single-AI user can't escape the ">=2 required" re-prompt -> a dedicated
banner ("claude-host counts; or install a 2nd CLI: <cmd>").
P4.3 Per-tuning-prompt one-line plain-language gloss; grok exact per-OS install
command; README `consensus-results` hyphen fix (the space form aliases the init
wizard and silently no-ops).

## Sequencing

P0 first (they make the codified path actually WORK + correct). P1 next (the
deterministic surfaces are what remove the guesses for every cold session). P2/P3
make the path self-scaffolding and crash-safe. P4 is polish. P0 + P1 alone deliver
the guess-free golden path for the happy case.

## Open questions for the consult

1. Is the P0/P1/P2/P3/P4 prioritization right - in particular, are P0.1
   (gate-arming) and P0.2 (dropped reviewers) correctly the top blockers?
2. Single-source-of-truth: should the ONE canonical runbook live in the skill, a
   shipped doc, or the managed CLAUDE.md block - which is the AI most guaranteed to
   see on a cold start?
3. Scaffold tool (P2.1): one new `consensus.start_consult` entrypoint vs teaching
   the AI to compose existing tools - which better removes the guess without
   bloating the tool surface?
4. Should per-project `consensus init` AUTO-WIRE the hooks (P1.1), or is the
   global/per-project split intentional and only the WARNING should be added?
5. Anything missing, or any item that is NOT actually a cold-start blocker?
