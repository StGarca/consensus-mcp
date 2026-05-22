# Delivery-readiness gate (anti-self-judge enforcement) — consensus-mcp 1.16.0

A fail-closed gate ensuring an operator-facing artifact has been **consensus-vetted**,
not self-judged by the agent. **Headline feature for consensus-mcp `1.16.0`**
(minor bump from 1.15.10) — it fixes a major logic flaw (agent self-judging
soundness instead of routing through consensus) that caused real-world bad
deliverables and work stoppage.

## Why
An agent repeatedly failed to *invoke/utilize* consensus-mcp: it built before
vetting, **self-judged soundness** ("I tested it myself, it's sound"), and
delivered before it was checked. Behavioral memories did not bind it. Only a
mechanism does. Source: internal consensus iterations
`iteration-antistall-protocol-2026-05-22` (4/4) +
`iteration-antistall-impl-2026-05-22` (4/4).

## The invariant (unforgeable by the agent)
You cannot mint a delivery token unless `design_consensus_ref` resolves to a
**CLOSED/SEALED consensus iteration** (`closing_state` in the same accepted set
used by `_release_gate_check.gate_real_iter`:
`{quorum_close_passed, implementation_ready_apply_landed}`). An agent that
self-judged has **no sealed iteration to point at**, so `mint_delivery_token`
refuses and `verify_delivery_token` fails closed. Soundness *must* route through
consensus.

Additional fail-closed conditions:
- artifact `sha256` must match the current file (no stale/edited delivery);
- `known_flaws` must be `[]` unless `operator_ack: true` (no caveat-and-ship);
- `vetted_by` must include **>= 2 non-claude reviewers** from that sealed
  iteration (the agent cannot self-vet).

## Files
- `consensus_mcp/_delivery_readiness.py` — token schema, `mint_delivery_token`,
  `verify_delivery_token`, `resolve_consensus_ref`, `compute_artifact_hash`, CLI.
- `tests/test_delivery_readiness.py` — fail-closed coverage (7 tests).
- `hooks/delivery_gate_pretooluse.py` — optional Claude-Code PreToolUse template.

## Enforcement surfaces (portable-first)
1. **MCP tool (primary, portable across Kimi CLI / Cursor / Claude Code).**
   Register in `server.py`:
   ```python
   from consensus_mcp import _delivery_readiness as _dr

   @mcp.tool()
   def request_delivery(artifact_path: str) -> dict:
       """Fail-closed delivery gate: returns {ok, reason, token_id?}.
       ok=True only if the artifact is consensus-vetted (sealed design ref,
       current hash, no unacked flaws, >=2 non-claude reviewers)."""
       return _dr.verify_delivery_token(artifact_path)

   @mcp.tool()
   def mint_delivery_readiness(artifact_path: str, design_consensus_ref: str,
                               vetted_by: list[str], known_flaws: list[str] | None = None,
                               operator_ack: bool = False) -> dict:
       try:
           tok = _dr.mint_delivery_token(artifact_path,
                   design_consensus_ref=design_consensus_ref, vetted_by=vetted_by,
                   known_flaws=known_flaws, operator_ack=operator_ack)
           return {"ok": True, "token_id": tok["token_id"]}
       except _dr.DeliveryReadinessError as exc:
           return {"ok": False, "error": str(exc)}
   ```
2. **CLI:**
   ```
   python -m consensus_mcp._delivery_readiness mint <artifact> \
       --design-consensus-ref iteration-foo-2026-05-22 --vetted-by codex,gemini,kimi
   python -m consensus_mcp._delivery_readiness verify <artifact>   # exit 0 iff ready
   ```
3. **PreToolUse hook (Claude-Code only, contrib convenience).** See
   `hooks/delivery_gate_pretooluse.py`; wire in `.claude/settings.json` to block
   operator-facing tool calls unless `verify` passes. Note: harness-specific —
   invisible to Kimi CLI / Cursor, hence secondary to the MCP tool.

## Flow
1. A consensus iteration vets the artifact's DESIGN and closes (sealed).
2. After implementing + testing, mint a token citing that sealed iteration +
   its non-claude reviewers.
3. Delivery (operator-facing send) is gated on `verify_delivery_token` ok=True.
4. Editing the artifact invalidates the token (hash drift) → re-vet.

## Decisive test
`test_mint_refused_without_sealed_ref`: an unsealed `design_consensus_ref` is
rejected — proving self-judged delivery is impossible without a prior real
consensus.
