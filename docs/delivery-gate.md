# Delivery-readiness gate

A fail-closed token check for an operator-facing artifact, separate from the
consult's design approval. In weighted-synthesis / proposal mode, the host AI
is the approver by design: it collects blind reviews, records each idea as
accepted (because...), rejected (proven wrong by...), or deferred in
`dispositions.yaml`, and authors `converged-plan.yaml`. `consensus-mcp-approve`
validates the table and binds its hash into the sealed outcome. Approval is
not a majority or unanimity vote. Failed reviewers supply no review or vote
against a change; approval still requires two distinct non-Claude model results.

## Why
An agent repeatedly failed to *invoke/utilize* consensus-mcp: it built before
vetting, **self-judged soundness** ("I tested it myself, it's sound"), and
delivered before it was checked. Behavioral memories did not bind it. Only a
mechanism does. Source: internal consensus iterations
`iteration-antistall-protocol-2026-05-22` (4/4) +
`iteration-antistall-impl-2026-05-22` (4/4).

## The token invariant
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
- `consensus_mcp/_delivery_readiness.py` - token schema, `mint_delivery_token`,
  `verify_delivery_token`, `resolve_consensus_ref`, `compute_artifact_hash`, CLI.
- `consensus_mcp/tests/test_delivery_readiness.py` - fail-closed coverage.
- `contrib/delivery_gate_pretooluse.py` - optional Claude-Code PreToolUse template.

## Enforcement surfaces (portable-first)
1. **MCP tools (portable across hosts).** `delivery.mint` accepts
   `artifact_path`, `design_consensus_ref`, `vetted_by`, optional `known_flaws`,
   and `operator_ack`. `delivery.request` verifies the token for `artifact_path`
   and returns `{ok, reason, token_id?}`. Both are already registered in
   `server.py`; no custom tool registration is needed.
2. **CLI:**
   ```
   python -m consensus_mcp._delivery_readiness mint <artifact> \
       --design-consensus-ref iteration-foo-2026-05-22 --vetted-by codex,gemini,kimi
   python -m consensus_mcp._delivery_readiness verify <artifact>   # exit 0 iff ready
   ```
3. **PreToolUse hook (Claude-Code only, contrib convenience).** See
   `contrib/delivery_gate_pretooluse.py`; wire in `.claude/settings.json` to block
   operator-facing tool calls unless `verify` passes. Note: harness-specific -
   invisible to Kimi CLI / Cursor, hence secondary to the MCP tool.

## Flow
1. A consensus iteration vets the artifact's DESIGN and closes (sealed).
2. After implementing + testing, mint a token citing that sealed iteration +
   its non-claude reviewers.
3. Delivery (operator-facing send) is gated on `verify_delivery_token` ok=True.
4. Editing the artifact invalidates the token (hash drift) -> re-vet.

## Decisive test
`test_mint_refused_without_sealed_ref`: an unsealed `design_consensus_ref` is
rejected - proving self-judged delivery is impossible without a prior real
consensus.
