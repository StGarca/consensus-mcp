---
title: Visibility TUI + v1.10.5 dispatch hardening - design
type: consensus pipeline-design
created: 2026-05-10
updated: 2026-05-11
status: approved
supersedes: codex visibility-server proposal (2026-05-10, rejected - architecture inflation)
tags: [consensus pipeline, design, visibility-tui, watchdog]
---

# Visibility TUI + v1.10.5 dispatch hardening

## Verified live gaps (gating)

Each gap was checked against current `consensus_mcp/_dispatch_codex.py` on 2026-05-10. Outside-repo reads + provenance-omission both confirmed live before this design was approved.

| # | Gap | Location | Evidence |
|---|-----|----------|----------|
| 1 | Outside-repo `--review-target` reads | `_dispatch_codex.py:167-183` (`_normalize_relative_to_repo` `p.resolve()`s absolute paths with no containment check) -> `_dispatch_codex.py:1303` (`review_target_normalized.read_text(...)`) | Operator-supplied absolute path like `C:\anywhere\foo.yaml` gets read into prompt. No filesystem boundary. |
| 2 | Sealed `provenance` omits `review_target_path` + `review_target_hash` | `_dispatch_codex.py:1360-1367` | `review_target_hash` IS computed at line 1304 and embedded in prompt at line 1325, but never persisted. Audit reconstruction can't confirm which target was reviewed. |
| 3 | `dispatch_done` event omits same fields | `_dispatch_codex.py:1382-1394` | Same gap on the audit event side. Watchdog/TUI cannot display "dispatching against target X (hash Y)". |
| 4 | Stall detection: blocking `subprocess.run` window has no event emission | `_dispatch_codex.py:443` (`_invoke_codex` calls `subprocess.run` with timeout) | Between `dispatch_start` and `dispatch_done`/`dispatch_failed`, no events emit. iter-0031 went silent for an unknown duration before operator noticed. |

## Out of scope

Explicitly NOT in this design (deferred or rejected):

- **Codex-side MCP server** - codex is a one-shot subprocess invoked by `_dispatch_codex.py`, not a peer system. Rejected as architecture inflation.
- **`visibility.post_operator_note` / `visibility.wait_for_operator_reply` MCP tools** - that's bidirectional control, not visibility. MCP JSON-RPC tool calls are a wrong fit for operator-blocking waits. Defer until passive viewer proves the need.
- **`visibility.open_terminal_view` MCP tool** - agents emit events; operators choose what to watch.
- **Embedding human text in MCP stdio** - would corrupt the JSON-RPC protocol. Sidecar terminal only.

## Tier plan (merged)

**v1.10.5 hardening - lands first, gating everything below.**

1. **Containment**: `_normalize_relative_to_repo` enforces `resolved.is_relative_to(repo_root)` after resolving. Outside-repo absolute paths raise a new `OutsideRepoPathError` with operator-facing diagnostic. Applies to `--goal-packet`, `--review-target`, `--prompt-template`, `--schema`.
2. **Sealed provenance fields**: `provenance` dict at `_dispatch_codex.py:1360` gains `review_target_path` (string, relative to repo_root) + `review_target_hash` (sha256 hex).
3. **dispatch_done audit fields**: `dispatch_done` event gains the same two fields.
4. **Regression tests**: (a) outside-repo absolute path refused, (b) provenance includes both fields, (c) dispatch_done includes both fields. Smoke + gates expected to bump baseline by 3 tests.

**Tier 0+1 - TUI with stall detection (merged per design refinement).** Lands on top of populated fields.

5. New file `consensus_mcp/_visibility_tui.py` - single-file standalone script.
6. Tails `consensus-state/state/dispatch-log.jsonl` + `consensus-state/state/mcp-server-audit.jsonl` via append-only polling (1-2s tick).
7. Displays:
 - active iteration (latest `dispatch_start` without matching `dispatch_done`/`dispatch_failed`)
 - active dispatch age (wall time since `dispatch_start`)
 - target review path + hash (from the new provenance fields)
 - last event with timestamp
 - stall warning (configurable threshold; default warn at 5min, alert near `timeout_seconds`)
 - recent terminal events (last 5)
8. Plain ANSI colors. No `rich`/`textual` dependency unless already in env.
9. **No MCP tool surface added.** Operator runs `python consensus_mcp/_visibility_tui.py` in a side terminal.

**Tier 3 (deferred)** - operator control channel: only if passive TUI use surfaces a clear recurring need. If/when added, use an append-only command/response file with explicit expiry, NOT a long-held JSON-RPC call.

## Acceptance criteria

- v1.10.5: pytest passes with +3 regression tests; smoke 60/60; gates 11/11; `G_pytest_dispatch_codex` baseline bumped to match new count.
- TUI: runs against the current `dispatch-log.jsonl` without error; detects a synthetic unmatched `dispatch_start` and displays the stall warning; can be Ctrl-C'd cleanly.

## Pairing with existing tasks

- Task #43 (subagent dispatch watchdog) - this design is the visibility half; the watchdog half (auto-cleanup on stall) is still Task #43. The TUI's stall detection is observation-only.
- Task #44 (subsystem-5 review of `_self_drive.py`) - unrelated; proceeds independently.
