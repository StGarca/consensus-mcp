# Codex diff review — v1.20.0 host-family specialist agents

This change shipped via 4-way Workflow A design but WITHOUT a Workflow B audit
(operator chose skip-B for the design→impl gate). This codex pass IS the
verification that it landed as expected. Be adversarial.

## What to review
The full feature diff is embedded as `v1.20.0-feature.diff` (cf4fe11..0e47cb6).
The converged design it must match is embedded as
`v1.20.0-host-peer-agent.md`.

## Confirm (and flag any deviation/bug/risk, with severity)
1. **LOAD-BEARING SAFETY — the cross-family closure invariant must NOT be
   weakened.** `_closure_invariant.py` adds `closer_gate_excluded =
   closing_verdict.get("gate_eligible") is False` and ANDs `not
   closer_gate_excluded` into `cross_family`. Verify: (a) a host_peer
   (gate_eligible=false) can NEVER be the different-family signer that closes a
   mutation — even when its family DIFFERS from the mutator's; (b) the change is
   purely additive — every existing closer (no gate_eligible key, or
   gate_eligible truthy/None) behaves EXACTLY as before (`is False` is
   literal-only); (c) no other closure logic (hash_match, freshness) is touched.
2. **HostPeerAdapter** (`contributors/host_peer_adapter.py`, NEW): blind — invoked
   with ONLY the phase DispatchPacket, no peer artifacts; seals a normal artifact;
   provenance authoritatively stamped (cannot be overridden by the callback):
   family==host, role=swe_reviewer, weight=supplementary, gate_eligible=false,
   independence_attestation{method,fresh_context,no_peer_review_visible}; NEVER
   shells out; fails closed when no callback / no family.
3. **_engine_factory**: routes kind:host_peer → HostPeerAdapter; threads a
   DEDICATED host_peer review callback (separate from claude_artifact_callback);
   builds host_peer ONLY when that callback is wired (graceful absence — existing
   configs/flows unchanged).
4. **Design conformance**: dedicated callback (not reusing claude_artifact_callback);
   supplementary weighting does not count toward the cross-family quorum; results
   record tags supplementary same-family reviewers separately; wizard caveat is
   accurate ("NOT independent multi-AI consensus, same model as host").
5. **Anything else**: bugs, edge cases, schema mismatches, provenance that could
   be spoofed by a malicious/buggy callback, gate_eligible bypasses, test gaps.

## Output
Per-finding: severity (blocking/high/medium/low), file:line citation, the issue,
and a recommended fix. If everything landed correctly, say so explicitly per
item. The #1 question: can a same-family host_peer EVER satisfy cross-family
closure? If yes anywhere, that's BLOCKING.
