---
name: external-process fallback for codex re-dispatch
description: Any codex re-dispatch following a stalled in-process subagent dispatch MUST use the external codex-cli pattern; in-process subagent re-dispatch is the failure mode iter-0031 documented and iter-0036 reaffirmed.
type: feedback
originSessionId: 3dc1e744-0c21-449b-80ee-09dff754acb7
---
When a codex dispatch stalls (e.g., in-process Claude subagent never returns; codex-cli wrapper hangs past wall-time timeout; subprocess.run buffers indefinitely), the recovery path is **external codex-cli only**:

```
python_env/python.exe -m consensus_mcp._dispatch_codex \
 --goal-packet consensus-state/active/<iter-id>/goal_packet.yaml \
 --iteration-dir consensus-state/active/<iter-id> \
 --reviewer-id codex-<iter-id>-N \
 --pass-id codex-<iter-id>-N-pass<M> \
 --codex-bin codex \
 --review-target consensus-state/active/<iter-id>/review-packet.yaml \
 --timeout-seconds 900
```

**Why:** In-process Claude subagent re-dispatch (via the `Agent` tool) is unreliable for long-running codex work. iter-0031 abandoned because a claude subagent dispatched to do codex re-review reported "Waiting for codex re-dispatch" and never returned — no timeout, no error, no audit-log terminal event. iter-0036 first pre-review dispatch hung 29 minutes past its 15-min internal timeout via `subprocess.run` with no visibility (no streaming, no heartbeat) — proving that even the external pattern needs bidirectional monitoring (iter-0037 work in progress). The external codex-cli path is the proven recovery primitive: it has a wall-time timeout, writes structured `dispatch_done` / `dispatch_failed` events to `consensus-state/state/dispatch-log.jsonl`, and produces a sealed `codex-review.yaml` even when codex emits zero findings.

**How to apply:**

1. **NEVER re-dispatch codex via the `Agent` tool** (in-process Claude subagent) for codex review/patch work. Use the external CLI pattern above.
2. When the visibility TUI shows an ACTIVE dispatch in ALERT state, run `python consensus_mcp/_visibility_watchdog.py --action mark` to close the audit shape with a `dispatch_stalled` event.
3. To re-dispatch after stall, look at the watchdog's `recommended_redispatch` output for the exact command line. The pass_id will be auto-bumped to `<reviewer_id>-pass2` to avoid collision with the stalled pass1.
4. iter-0037 will add streaming + heartbeats + abort-signal-file to make in-progress dispatches visible/recoverable. Until then, watch the TUI and the dispatch-log; if a dispatch has no terminal event after 10 minutes, it's stuck — kill the bash task and run the watchdog.

**When this rule does NOT apply:** initial dispatch (always external codex-cli already, via `_dispatch_codex`); subagent dispatch for non-codex work (those are short and the Agent tool is fine).
