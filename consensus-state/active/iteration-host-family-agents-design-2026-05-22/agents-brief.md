# Consult: focus the host-family specialist-agent writeup (Workflow A)

You are one of several AI contributors in a propose-converge consult. PROPOSE the
focused DESIGN for adding host-family specialist agents to consensus-mcp, correct
for this project's strict, Python-only, sealed-provenance architecture. The
SETTLED decisions below are not up for debate — focus on HOW to build it right.

## Settled (do not relitigate)
A same-family **specialist software-engineer reviewer** (host's own AI family,
e.g. Claude when Claude hosts) is worth adding as a SUPPLEMENTARY contributor:
- blind (no peer output visible at dispatch) + FRESH/independent context (NOT the
  host's polluted conversation),
- distinct reviewer system prompt (adversarial correctness / spec-conformance /
  edge-case focus),
- **provenance-tagged as same-family**, **weighted as supplementary**, and
  **EXCLUDED from the cross-family closure invariant** (it can never be the
  different-family signer-off — same family shares blind spots; same-family
  agreement is the shared-prior trap, low information). It augments cross-family
  review, never replaces it.

## The CRUX to resolve — invocation + integration mechanism
The host (Claude running the loop) is the IN-PROCESS orchestrator; there is no
`claude` CLI dispatch the way codex/gemini have shell binaries. So: **how does a
same-family agent get invoked, produce a SEALED, provenance-tagged review
artifact like every other contributor, satisfy blind-first-reveal, and stay
excluded from the cross-family gate?** Weigh concrete options against the embedded
code (contributors/base.py = the ContributorAdapter/DispatchPacket interface;
_engine_factory.py = build_adapters + the registry + _BUILTIN_ADAPTERS;
profile_adapter.py = the v1.18.0 generic ProfileAdapter precedent; kimi.yaml = a
built-in profile):
- (a) a `claude_artifact_callback`-style host callback invoked a SECOND time with
  a reviewer prompt + a guaranteed-fresh context;
- (b) a new adapter `kind: host_peer` in _engine_factory the host fulfills via a
  fresh sub-invocation of its own family;
- (c) the host spawns a subagent (fresh context + reviewer system prompt) whose
  output is sealed as a contributor artifact;
- (d) something else.
Name the integration point, the fresh-context guarantee, the blindness guarantee,
the provenance tagging (family == host), and the gate-exclusion wiring.

## Role split question (answer it)
Should the host ALSO run as a distinct specialized **orchestrator agent** (role:
neutral scoping, synthesis, gate-enforcement, anti-anchoring) — SEPARATE from the
SWE-reviewer agent — so the agent that authors/synthesizes is not the one that
blind-reviews? How do orchestrator-role and reviewer-role relate within the host
family? (The project already has an anchoring linter for orchestrator framing
bias — factor that in.)

## Strict-system constraints (MUST hold)
- Sealed provenance for every review artifact (hashes, independence_attestation).
- The cross-family closure invariant (a DIFFERENT family than the mutator signs
  off) MUST NOT be weakened — host_peer cannot satisfy it.
- Blind-first-reveal independence (no peer visible at dispatch).
- Conform to the open-contributor / profile model (config-driven where possible).
- Fail-closed gates; minimal, consistent with existing patterns.
- 100% Python; no new heavy deps.

## Your task
Propose ONE focused design: the invocation/integration mechanism (pick from
a/b/c/d + justify), the role prompts (orchestrator vs SWE-reviewer), the
fresh-context + blindness + provenance + gate-exclusion wiring, config surface,
and whether/how to split orchestrator vs reviewer roles. Put it in
deliverable_scope (files to touch, key decisions, acceptance gates, risks). State
the differential you reason from. Honest structural_abstention beats confabulation.
