"""Visibility TUI for consensus-mcp dispatch + audit event streams.

Tier 0+1 of the 2026-05-10 visibility-TUI design doc. Read-only tail of
the existing JSONL event streams with stall detection. NO MCP tool surface
added, NO new event generation, NO operator control channel — those are
deliberately out of scope (see design doc).

Usage
-----
    # Live tail (refresh every 2s; Ctrl-C to exit)
    python consensus_mcp/_visibility_tui.py

    # One-shot snapshot (renders once, exits 0 if no stall, 1 if any active
    # dispatch is past the warning threshold). Used by CI / smoke / manual checks.
    python consensus_mcp/_visibility_tui.py --once

    # Custom poll + threshold
    python consensus_mcp/_visibility_tui.py --tick 1.0 --warn-after 180

What it shows
-------------
- Active dispatches: dispatch_start events with no matching dispatch_done /
  dispatch_failed. Age, target file (v1.10.5), timeout, stall status.
- Recent terminal events: last 5 dispatch_done / dispatch_failed entries.
- Stall warning: dispatch age >= warn_after_seconds.
- Stall alert: dispatch age >= 0.9 * timeout_seconds (or warn_after if no timeout).

What it does NOT do
-------------------
- No MCP tool registration.
- No event generation. Read-only consumer of the existing JSONL streams.
- No watchdog actions (cleanup, retry, abort). That's Task #43.
- No operator reply channel.
- No human text on the MCP stdio pipe (we don't touch the MCP pipe at all).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# --- Color helpers (ANSI; no rich/textual dependency) -----------------------

_ANSI_ENABLED = sys.stdout.isatty()
_RESET = "\x1b[0m" if _ANSI_ENABLED else ""
_DIM = "\x1b[2m" if _ANSI_ENABLED else ""
_BOLD = "\x1b[1m" if _ANSI_ENABLED else ""
_GREEN = "\x1b[32m" if _ANSI_ENABLED else ""
_YELLOW = "\x1b[33m" if _ANSI_ENABLED else ""
_RED = "\x1b[31m" if _ANSI_ENABLED else ""
_CYAN = "\x1b[36m" if _ANSI_ENABLED else ""


def _color(s: str, code: str) -> str:
    return f"{code}{s}{_RESET}" if _ANSI_ENABLED else s


# --- File reading -----------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file. Returns empty list on missing/unreadable. Skips
    malformed lines (defensive against partial writes in tail mode).
    """
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
            # Partial write or corrupt line; skip without aborting the TUI.
            continue
    return events


# --- Time helpers -----------------------------------------------------------

def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Tolerate both Z and +00:00 forms.
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except (ValueError, TypeError):
        return None


def _humanize_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# --- Dispatch state assembly -----------------------------------------------

def _assemble_dispatches(events: list[dict]) -> dict:
    """Walk the dispatch-log event stream; pair start events with terminal
    (done/failed/refused) events by (iteration_id, pass_id). Returns a dict shape:
        {
          "active":   [
              {"start": ev, "last_line": ev_or_None, "last_heartbeat": ev_or_None},
              ...
          ],
          "recent":   [end_event, ...],     # last 5 terminal events, newest first
        }

    iter-0033 codex-rev-002 fix: key by (iteration_id, pass_id) tuple so a
    pass_id reused across iterations doesn't collapse two dispatches into
    one. iteration_id may be missing on legacy events; the (None, pass_id)
    bucket preserves the pre-fix behavior for those events without leaking
    new collisions.

    iter-0037 follow-on: also capture the most recent `dispatch_streamed_line`
    and `dispatch_heartbeat` event per (iteration_id, pass_id) so the renderer
    can surface streaming state for active dispatches.
    """
    by_key: dict[tuple, dict] = {}
    for ev in events:
        pass_id = ev.get("pass_id") or ev.get("reviewer_id")
        if not pass_id:
            continue
        iter_id = ev.get("iteration_id")
        key = (iter_id, pass_id)
        entry = by_key.setdefault(
            key,
            {"start": None, "end": None, "last_line": None, "last_heartbeat": None},
        )
        kind = ev.get("event")
        if kind == "dispatch_start":
            entry["start"] = ev
        elif kind in ("dispatch_done", "dispatch_failed", "dispatch_refused",
                      "dispatch_stalled", "dispatch_aborted"):
            # iter-0036: dispatch_stalled is the watchdog's retroactive
            # terminal marker. dispatch_aborted is iter-0037 operator-signal-file
            # / heartbeat-silence work.
            entry["end"] = ev
        elif kind == "dispatch_streamed_line":
            # iter-0037 follow-on: keep the latest streamed line. Events are
            # processed in file order; later writes overwrite earlier ones.
            entry["last_line"] = ev
        elif kind == "dispatch_heartbeat":
            # iter-0037 follow-on: keep the latest heartbeat.
            entry["last_heartbeat"] = ev

    active = [
        {
            "start": v["start"],
            "last_line": v["last_line"],
            "last_heartbeat": v["last_heartbeat"],
        }
        for v in by_key.values()
        if v["start"] and not v["end"]
    ]
    recent_terminal = [
        v["end"] for v in by_key.values() if v["end"]
    ]
    # Sort by timestamp descending.
    def _sort_key(ev):
        return _parse_ts(ev.get("timestamp_utc")) or datetime.min.replace(tzinfo=timezone.utc)
    recent_terminal.sort(key=_sort_key, reverse=True)
    active.sort(key=lambda entry: _sort_key(entry["start"]), reverse=True)
    return {"active": active, "recent": recent_terminal[:5]}


# --- Rendering --------------------------------------------------------------

def _render(dispatch_events: list[dict], audit_events: list[dict], warn_after: float, now: datetime) -> tuple[str, bool]:
    """Render the TUI screen. Returns (text, any_stalled).

    any_stalled is True if at least one active dispatch is past warn_after_seconds.
    """
    state = _assemble_dispatches(dispatch_events)
    lines: list[str] = []
    any_stalled = False

    lines.append(_color("consensus-mcp visibility TUI", _BOLD + _CYAN))
    lines.append(_color(f"  now={now.isoformat(timespec='seconds')}  warn_after={int(warn_after)}s", _DIM))
    lines.append("")

    # Active dispatches
    if state["active"]:
        lines.append(_color(f"ACTIVE DISPATCHES ({len(state['active'])}):", _BOLD))
        for entry in state["active"]:
            ev = entry["start"]
            start_ts = _parse_ts(ev.get("timestamp_utc"))
            age_s = (now - start_ts).total_seconds() if start_ts else 0.0
            timeout = ev.get("timeout_seconds") or 0
            alert_threshold = max(warn_after, 0.9 * timeout) if timeout else warn_after
            stalled = age_s >= warn_after
            alert = age_s >= alert_threshold and timeout > 0
            any_stalled = any_stalled or stalled

            tag = _color("OK    ", _GREEN)
            if alert:
                tag = _color("ALERT ", _RED + _BOLD)
            elif stalled:
                tag = _color("STALL ", _YELLOW + _BOLD)

            iter_id = ev.get("iteration_id", "?")
            pass_id = ev.get("pass_id", "?")
            target = ev.get("review_target_path") or _color("(no review-target)", _DIM)
            timeout_disp = f"timeout={timeout}s" if timeout else "timeout=?"
            lines.append(
                f"  {tag} {iter_id} / {pass_id}"
            )
            lines.append(
                f"        age={_humanize_age(age_s)}  {timeout_disp}  target={target}"
            )

            # iter-0037 follow-on: surface streaming sub-state. The renderer
            # truncates content to keep terminal width sane (200-char audit
            # truncation is too wide for typical terminals).
            last_line_ev = entry.get("last_line")
            if last_line_ev:
                seq = last_line_ev.get("seq", "?")
                content = last_line_ev.get("line_truncated", "")
                if len(content) > 80:
                    content = content[:77] + "..."
                lines.append(
                    f"        last_line_seq={seq}: \"{content}\""
                )

            last_hb_ev = entry.get("last_heartbeat")
            if last_hb_ev:
                hb_age = last_hb_ev.get("age_seconds")
                silence_age = last_hb_ev.get("last_streamed_line_age_seconds")
                # Color the silence age by tier:
                #   <60s  GREEN healthy
                #   60-90 YELLOW warning
                #   >=90  RED critical / about to auto-abort
                if isinstance(silence_age, (int, float)):
                    if silence_age >= 90:
                        silence_color = _RED + _BOLD
                    elif silence_age >= 60:
                        silence_color = _YELLOW
                    else:
                        silence_color = _GREEN
                    silence_disp = _color(f"silence: {_humanize_age(float(silence_age))} ago", silence_color)
                else:
                    silence_disp = "silence: (unknown)"
                if isinstance(hb_age, (int, float)):
                    hb_disp = f"last_heartbeat: {_humanize_age(float(hb_age))} ago"
                else:
                    hb_disp = "last_heartbeat: (unknown)"
                lines.append(f"        {hb_disp}; {silence_disp}")
    else:
        lines.append(_color("ACTIVE DISPATCHES (0): idle", _DIM))
    lines.append("")

    # Recent terminal events
    lines.append(_color(f"RECENT TERMINAL EVENTS (last {len(state['recent'])}):", _BOLD))
    if not state["recent"]:
        lines.append(_color("  (none)", _DIM))
    for ev in state["recent"]:
        ts = ev.get("timestamp_utc", "?")
        kind = ev.get("event", "?")
        iter_id = ev.get("iteration_id", "?")
        pass_id = ev.get("pass_id", "?")
        if kind == "dispatch_done":
            marker = _color("done   ", _GREEN)
        elif kind == "dispatch_failed":
            marker = _color("failed ", _RED)
        elif kind == "dispatch_refused":
            marker = _color("refused", _YELLOW)
        elif kind == "dispatch_stalled":
            marker = _color("stalled", _RED + _BOLD)
        elif kind == "dispatch_aborted":
            marker = _color("aborted", _RED + _BOLD)
        else:
            marker = _color(f"{kind:7}", _DIM)
        rt_hash = ev.get("review_target_hash")
        hash_disp = f"  target_hash={rt_hash[:12]}…" if rt_hash else ""
        lines.append(f"  {marker}  {ts}  {iter_id} / {pass_id}{hash_disp}")
    lines.append("")

    # MCP server boot history (from audit-log). NOTE: the MCP server in this
    # project is NOT a long-running daemon — it boots on demand and exits
    # when the caller releases stdio. "mcp_server_stopped" means "the last
    # boot finished", not "the server is currently down." Suppress entirely
    # if the last event is >24h old to avoid implying a current outage.
    server_events = [e for e in audit_events if e.get("event", "").startswith("mcp_server_")]
    if server_events:
        last_server = server_events[-1]
        last_ts = _parse_ts(last_server.get("timestamp_utc"))
        age_hours = (now - last_ts).total_seconds() / 3600 if last_ts else 999
        if age_hours < 24:
            lines.append(_color("MCP SERVER (boot history — not a daemon):", _BOLD))
            lines.append(
                f"  last_boot_event={last_server.get('event','?')}  "
                f"at={last_server.get('timestamp_utc','?')}  "
                f"({_humanize_age(age_hours * 3600)} ago)"
            )
            lines.append("")

    return "\n".join(lines), any_stalled


# --- CLI --------------------------------------------------------------------

def _default_repo_root() -> Path:
    """Locate repo_root via the same env-var override as _dispatch_codex.
    Falls back to a __file__-relative walk; if that fails the TUI exits with
    a clear diagnostic rather than tailing nothing.
    """
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override).resolve()
    # Walk up from this file: consensus_mcp/_visibility_tui.py -> repo root
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Visibility TUI for consensus-mcp dispatch streams.")
    parser.add_argument("--once", action="store_true", help="Render once and exit (non-tail mode).")
    parser.add_argument("--tick", type=float, default=2.0, help="Tail-mode refresh interval in seconds (default 2.0).")
    parser.add_argument("--warn-after", type=float, default=300.0,
                        help="Seconds before an active dispatch is flagged STALL (default 300 = 5min).")
    parser.add_argument("--repo-root", default=None,
                        help="Override repo root (default: CONSENSUS_MCP_REPO_ROOT or __file__ walk).")
    ns = parser.parse_args(argv)

    repo_root = Path(ns.repo_root).resolve() if ns.repo_root else _default_repo_root()
    dispatch_log = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"
    audit_log = repo_root / "consensus-state" / "state" / "mcp-server-audit.jsonl"

    if not dispatch_log.parent.exists():
        print(f"ERROR: state dir not found: {dispatch_log.parent}", file=sys.stderr)
        return 2

    def render_once() -> bool:
        dispatch_events = _read_jsonl(dispatch_log)
        audit_events = _read_jsonl(audit_log)
        text, stalled = _render(dispatch_events, audit_events, ns.warn_after, datetime.now(timezone.utc))
        if ns.once:
            print(text)
        else:
            # Clear screen before each render. ANSI escape works in modern
            # terminals (PowerShell 7+, Windows Terminal, most Linux/macOS)
            # but PowerShell 5.1 + default Windows console may not interpret
            # it depending on VT_PROCESSING state. Fall back to os.system on
            # Windows so the screen reliably clears either way.
            if sys.platform == "win32":
                os.system("cls")
            else:
                sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.write(text)
            sys.stdout.write("\n")
            sys.stdout.flush()
        return stalled

    if ns.once:
        stalled = render_once()
        return 1 if stalled else 0

    try:
        while True:
            render_once()
            time.sleep(ns.tick)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
