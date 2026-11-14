# Dispatching Codex correctly

For an ordinary consult, scaffold with `consensus-mcp-start-consult`, then run
`consensus-mcp-consult --iteration <iteration> --tier standard`. The panel
runner dispatches configured seats concurrently and writes `consult-summary.yaml`;
no per-reviewer shell or Kimi-last sequence is needed. See
[hosting a consult](hosting-a-consult.md) for approval and Windows notes.

For a direct Codex dispatch, pass the scaffolded review packet:

```bash
consensus-mcp-dispatch-codex \
  --goal-packet consensus-state/active/<iteration>/goal_packet.yaml \
  --iteration-dir consensus-state/active/<iteration> \
  --reviewer-id codex \
  --review-target consensus-state/active/<iteration>/review-packet.yaml
```

Omit `--pass-id` to use the generated unique identity. The current dispatcher
also embeds a raw review target's content; the historical requirement to pass
only YAML no longer applies. Inspect `dispatch_provenance` in the sealed review
to confirm the reviewed target and actual model.

Diff-scoped packets are bounded by `packet_budget` in `.consensus/config.yaml`:
by default, 8 changed/context sections, 120 lines per section excerpt, 600 diff
lines, 20 prior open items, and full files only below 250 lines. Omitted content
is recorded explicitly. A prior packet uses its reviewed HEAD as the new base.

Cold-start and streaming silence limits are distinct, configurable controls.
Do not edit dispatcher source to change timeouts. The panel exposes
`--timeout-seconds` (per seat, default 600) and `--deadline-seconds` (whole panel,
default 900); engine tiers also supply their own timeout presets.

Failures are classified as `auth`, `quota`, `rate_limit`, `timeout`, `parse`,
`integrity`, or `crash`. Quota can select the next configured model in the same
seat; parse, integrity, crash, and timeout do not trigger model fallback.
The sealed provenance records the requested model, actual model, and fallback
reason. A failed reviewer supplies no negative vote against the change.
