# iter-0036 proposed patches — pre-implementation review target

Claude-authored proposal for Task #43 (subagent dispatch watchdog +
external-process fallback policy). Pre-implementation review by codex
per operator-preferred workflow #4.

Reviewer task: assess the proposed watchdog design and the TUI integration
for (a) correctness, (b) regression risk on existing TUI behavior, (c)
audit-log append-only invariant preservation, (d) test coverage adequacy,
(e) policy completeness for the external-process fallback. NO code on
disk reflects this proposal yet.

---

## Background

iter-0031 stalled because a claude in-process subagent was dispatched to
do codex re-review and never returned. The audit log retained a
`dispatch_start` event for `codex-iter0031-2-pass1` with no matching
terminal event. Subsequent visibility-TUI work (iter-0032/0033) surfaces
this orphan as `ACTIVE age=3h…` but takes no action.

The watchdog closes Task #43: it converts orphan dispatch_start events
into structured terminal events (`dispatch_stalled`) after a configurable
wall-time threshold, restoring audit-log shape and clearing the TUI's
active-dispatch list. It does NOT auto-re-dispatch — that's an operator
decision — but it includes a recommended re-dispatch command using the
external codex-cli pattern (the proven recovery path; in-process subagent
re-dispatch is the failure mode iter-0031 documented).

---

## Patch 1 — NEW file `scripts/agent_loop_mcp/_visibility_watchdog.py`

Standalone module. Companion to `_visibility_tui.py` (observation half;
this is the action half). Append-only audit-log mutation policy: never
modifies existing events; only appends `dispatch_stalled` records.

```python
"""Watchdog for stalled dispatches (Task #43; iter-0031 abandonment lesson).

Detects dispatch_start events in agent-loop/state/dispatch-log.jsonl that
lack a matching terminal event (dispatch_done | dispatch_failed |
dispatch_refused | dispatch_stalled) after a configurable wall-time
threshold. In --action=mark mode, appends a `dispatch_stalled` audit
event for each stale orphan so the audit shape closes and the visibility
TUI clears it from "active".

THE AUDIT LOG IS APPEND-ONLY. The watchdog NEVER edits or deletes existing
events. It only appends new `dispatch_stalled` records that share the
orphan's identifiers (iteration_id / reviewer_id / pass_id).

External-process fallback (operator-locked policy per iter-0031 lesson):
any codex re-dispatch following a stalled in-process subagent dispatch
MUST use the external codex-cli pattern via _dispatch_codex. The watchdog
emits a recommended re-dispatch command line in its report output.

Usage
-----
    # Default: read-only report; rc=1 if any orphan is stale, rc=0 otherwise.
    python scripts/agent_loop_mcp/_visibility_watchdog.py

    # Mutate: append dispatch_stalled events for each stale orphan.
    python scripts/agent_loop_mcp/_visibility_watchdog.py --action mark

    # Custom thresholds (seconds).
    python scripts/agent_loop_mcp/_visibility_watchdog.py --stall-after 1800
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WATCHDOG_VERSION = "1.0.0"


def _default_repo_root() -> Path:
    """Same resolution as _visibility_tui._default_repo_root."""
    override = os.environ.get("AGENT_LOOP_MCP_REPO_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent.parent


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL; tolerate missing file + malformed lines (mirrors TUI)."""
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


_TERMINAL_EVENTS = {
    "dispatch_done", "dispatch_failed", "dispatch_refused", "dispatch_stalled",
}


def find_stalled(events, stall_threshold_seconds: float, now: datetime) -> list[dict]:
    """Return orphan dispatch_start events older than stall_threshold_seconds.

    Pairing uses (iteration_id, pass_id) tuple key (per iter-0033 codex-rev-002
    fix). dispatch_stalled is itself a terminal event, so re-running the
    watchdog after a previous --action=mark run does NOT re-mark the same
    orphan.
    """
    by_key: dict[tuple, dict] = {}
    for ev in events:
        pass_id = ev.get("pass_id") or ev.get("reviewer_id")
        if not pass_id:
            continue
        iter_id = ev.get("iteration_id")
        key = (iter_id, pass_id)
        entry = by_key.setdefault(key, {"start": None, "end": None})
        kind = ev.get("event")
        if kind == "dispatch_start":
            entry["start"] = ev
        elif kind in _TERMINAL_EVENTS:
            entry["end"] = ev

    stale = []
    for entry in by_key.values():
        start = entry["start"]
        if not start or entry["end"]:
            continue
        start_ts = _parse_ts(start.get("timestamp_utc"))
        if start_ts is None:
            continue
        age = (now - start_ts).total_seconds()
        if age >= stall_threshold_seconds:
            stale.append({
                "start_event": start,
                "age_seconds": age,
                "start_timestamp_utc": start.get("timestamp_utc"),
            })
    return stale


def _suggest_redispatch(orphan_start: dict, repo_root: Path) -> str:
    """Return a recommended external-process re-dispatch command line for the
    given orphan dispatch_start event. Uses the proven _dispatch_codex CLI
    pattern; never in-process subagent (per iter-0031 lesson).
    """
    iter_id = orphan_start.get("iteration_id", "<iteration_id>")
    reviewer_id = orphan_start.get("reviewer_id", "<reviewer_id>")
    # We synthesize a new pass_id suffix to avoid collision with the orphan.
    return (
        "python_env/python.exe -m agent_loop_mcp._dispatch_codex "
        f"--goal-packet agent-loop/active/{iter_id}/goal_packet.yaml "
        f"--iteration-dir agent-loop/active/{iter_id} "
        f"--reviewer-id {reviewer_id} "
        f"--pass-id {reviewer_id}-pass2 "
        "--codex-bin codex "
        f"--review-target agent-loop/active/{iter_id}/review-packet.yaml "
        "--timeout-seconds 900"
    )


def emit_stalled_event(log_path: Path, orphan: dict, stall_threshold_seconds: float, now: datetime) -> dict:
    """Append a dispatch_stalled event for the given orphan; return the event dict.

    APPEND-ONLY: never modifies or deletes existing events.
    """
    start = orphan["start_event"]
    event = {
        "timestamp_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": "dispatch_stalled",
        "iteration_id": start.get("iteration_id"),
        "reviewer_id": start.get("reviewer_id"),
        "pass_id": start.get("pass_id"),
        "stall_reason": "watchdog_timeout",
        "stall_threshold_seconds": stall_threshold_seconds,
        "age_seconds": orphan["age_seconds"],
        "original_start_timestamp_utc": orphan["start_timestamp_utc"],
        "watchdog_run_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "watchdog_version": WATCHDOG_VERSION,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    return event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog for stalled dispatches.")
    parser.add_argument("--repo-root", default=None,
                        help="Override repo root (default: env var or __file__ walk).")
    parser.add_argument("--stall-after", type=float, default=600.0,
                        help="Seconds before an orphan is stale (default 600 = 10 min).")
    parser.add_argument("--action", choices=("report", "mark"), default="report",
                        help="report = read-only scan; mark = append dispatch_stalled events.")
    ns = parser.parse_args(argv)

    repo_root = Path(ns.repo_root).resolve() if ns.repo_root else _default_repo_root()
    log_path = repo_root / "agent-loop" / "state" / "dispatch-log.jsonl"

    events = _read_jsonl(log_path)
    now = datetime.now(timezone.utc)
    stale = find_stalled(events, ns.stall_after, now)

    output = {
        "watchdog_run_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "watchdog_version": WATCHDOG_VERSION,
        "stall_after_seconds": ns.stall_after,
        "action": ns.action,
        "stale_count": len(stale),
        "stale": [],
    }
    for orphan in stale:
        start = orphan["start_event"]
        entry = {
            "iteration_id": start.get("iteration_id"),
            "reviewer_id": start.get("reviewer_id"),
            "pass_id": start.get("pass_id"),
            "age_seconds": orphan["age_seconds"],
            "original_start_timestamp_utc": orphan["start_timestamp_utc"],
            "recommended_redispatch": _suggest_redispatch(start, repo_root),
        }
        if ns.action == "mark":
            emitted = emit_stalled_event(log_path, orphan, ns.stall_after, now)
            entry["dispatch_stalled_appended"] = True
            entry["appended_event_timestamp_utc"] = emitted["timestamp_utc"]
        output["stale"].append(entry)

    print(json.dumps(output, indent=2))
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## Patch 2 — EDIT `scripts/agent_loop_mcp/_visibility_tui.py`

Recognize `dispatch_stalled` as a terminal event in `_assemble_dispatches`
so the TUI moves a stalled-then-marked dispatch from ACTIVE to RECENT
TERMINAL EVENTS. Display a clear "stalled" marker.

```diff
--- a/scripts/agent_loop_mcp/_visibility_tui.py
+++ b/scripts/agent_loop_mcp/_visibility_tui.py
@@ -118,7 +118,7 @@ def _assemble_dispatches(events: list[dict]) -> dict:
         kind = ev.get("event")
         if kind == "dispatch_start":
             entry["start"] = ev
-        elif kind in ("dispatch_done", "dispatch_failed", "dispatch_refused"):
+        elif kind in ("dispatch_done", "dispatch_failed", "dispatch_refused", "dispatch_stalled"):
             entry["end"] = ev
```

Plus in the rendering, add the stalled marker:

```diff
@@ -178,6 +178,8 @@ def _render(dispatch_events, audit_events, warn_after, now):
         marker = _color("done  ", _GREEN) if kind == "dispatch_done" else (
                  _color("failed", _RED)   if kind == "dispatch_failed" else
+                 _color("stalled", _RED + _BOLD) if kind == "dispatch_stalled" else
                  _color("refused", _YELLOW))
```

---

## Test coverage

### `tests/test_visibility_watchdog.py` (NEW)

5+ tests covering:

1. `test_find_stalled_returns_empty_when_no_orphans` — start + matching done → no stale.
2. `test_find_stalled_returns_orphan_older_than_threshold` — start without terminal, age > threshold → returned.
3. `test_find_stalled_ignores_orphan_younger_than_threshold` — start without terminal but age < threshold → not returned.
4. `test_find_stalled_recognizes_dispatch_stalled_as_terminal` — start + dispatch_stalled → no orphan (idempotent: re-running watchdog doesn't re-mark).
5. `test_find_stalled_uses_iteration_id_pass_id_tuple_key` — two iterations sharing pass_id: one terminated, other not → only the orphan returned.
6. `test_emit_stalled_event_appends_to_log` — calling emit_stalled_event writes a properly-shaped event to dispatch-log.jsonl.
7. `test_emit_stalled_event_does_not_modify_existing_events` — pre-existing events in the log are byte-identical after the append.
8. `test_main_report_mode_does_not_mutate_log` — --action=report leaves the log untouched.
9. `test_main_mark_mode_appends_events` — --action=mark appends exactly one dispatch_stalled per orphan.
10. `test_main_returns_1_when_orphans_present` — exit-code semantics for CI integration.
11. `test_main_returns_0_when_no_orphans` — clean state exits 0.

### `tests/test_visibility_tui.py` (EDIT)

Add one regression test:

12. `test_assemble_treats_dispatch_stalled_as_terminal` — a dispatch_start
    followed by dispatch_stalled appears in `recent`, not `active`.

---

## Acceptance & verification

After implementing:
- pytest: +12 net new tests (11 in watchdog + 1 in TUI). Local 372 → 384.
- smoke: 60/60 unchanged
- gates: 11/11 unchanged after staging + commit
- Run `_visibility_watchdog.py --action=mark` ONCE to clean up the 2
  historical orphans (iter-0031-2 + 2026-05-09 smoke) — this is an
  intentional one-time hygiene action documented in iteration-outcome,
  not a regression test.

## Reviewer questions

1. **Append-only invariant**: is appending a synthetic terminal event the
   right shape vs writing a separate "watchdog-overrides" sidecar file?
   I argue append-only audit + recognize-stalled-as-terminal is cleaner;
   alternative would split the truth across two files.
2. **Threshold default**: 600s (10min) — too aggressive or too lax for
   our typical codex dispatch wall time (~30-60s)?
3. **`_suggest_redispatch` command shape**: pass_id collision avoidance
   uses `f"{reviewer_id}-pass2"` literal. If the orphan was already
   pass2, the recommendation would collide. Should it scan the log for
   existing pass_ids and pick the next available, or is the operator
   expected to adjust manually?
4. **External-process-fallback policy**: should we update CLAUDE.md or
   add a memory entry to codify "in-process subagent re-dispatch is
   forbidden post-stall"? I propose memory entry; CLAUDE.md is
   project-rules-heavy already.
5. **Idempotency under concurrent watchdog runs**: two `--action=mark`
   processes racing on the same orphan would each append. Worth a
   file-lock, or rely on the operator to not parallelize the watchdog?
