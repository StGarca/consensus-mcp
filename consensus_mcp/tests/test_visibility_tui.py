"""Unit tests for _visibility_tui.

The TUI is a read-only consumer of consensus-state/state/*.jsonl. These tests
exercise the pure-function pieces (event pairing, stall detection, render
text) so the heuristic stays correct across changes.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure the package import works whether or not the wheel is installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from consensus_mcp import _visibility_tui  # noqa: E402


def _start_event(pass_id: str, iteration_id: str, timestamp: str, **kw) -> dict:
    base = {
        "event": "dispatch_start",
        "pass_id": pass_id,
        "iteration_id": iteration_id,
        "timestamp_utc": timestamp,
        "timeout_seconds": 900,
    }
    base.update(kw)
    return base


def _done_event(pass_id: str, iteration_id: str, timestamp: str, **kw) -> dict:
    base = {
        "event": "dispatch_done",
        "pass_id": pass_id,
        "iteration_id": iteration_id,
        "timestamp_utc": timestamp,
        "exit_code": 0,
    }
    base.update(kw)
    return base


def test_assemble_pairs_start_with_done():
    """A start + matching done by pass_id => no active, one recent."""
    events = [
        _start_event("p1", "iter-001", "2026-05-10T20:00:00Z"),
        _done_event("p1", "iter-001", "2026-05-10T20:05:00Z"),
    ]
    state = _visibility_tui._assemble_dispatches(events)
    assert state["active"] == []
    assert len(state["recent"]) == 1
    assert state["recent"][0]["pass_id"] == "p1"


def test_assemble_unmatched_start_is_active():
    """A start with no matching terminal event => 1 active, 0 recent."""
    events = [_start_event("p2", "iter-002", "2026-05-10T20:00:00Z")]
    state = _visibility_tui._assemble_dispatches(events)
    assert len(state["active"]) == 1
    # iter-0037 follow-on: active entries are wrapper dicts {"start": ev, ...}.
    assert state["active"][0]["start"]["pass_id"] == "p2"
    assert state["active"][0]["last_line"] is None
    assert state["active"][0]["last_heartbeat"] is None
    assert state["recent"] == []


def test_assemble_handles_multiple_passes():
    """Multiple iterations interleaved; pairing by pass_id, not iteration_id."""
    events = [
        _start_event("p1", "iter-001", "2026-05-10T20:00:00Z"),
        _start_event("p2", "iter-002", "2026-05-10T20:01:00Z"),  # active
        _done_event("p1", "iter-001", "2026-05-10T20:05:00Z"),
        _start_event("p3", "iter-003", "2026-05-10T20:06:00Z"),  # active
    ]
    state = _visibility_tui._assemble_dispatches(events)
    active_ids = {entry["start"]["pass_id"] for entry in state["active"]}
    assert active_ids == {"p2", "p3"}
    assert state["recent"][0]["pass_id"] == "p1"


def test_render_flags_stall_when_age_exceeds_warn_after():
    """STALL marker fires when age >= warn_after_seconds."""
    now = datetime(2026, 5, 10, 20, 10, 0, tzinfo=timezone.utc)
    # Started 6 minutes ago, warn_after = 5 minutes => STALL but not ALERT.
    start_time = (now - timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
    events = [_start_event("p-stall", "iter-stall", start_time, timeout_seconds=900)]
    text, stalled = _visibility_tui._render(events, [], warn_after=300.0, now=now)
    assert stalled is True
    assert "STALL" in text or "ALERT" in text
    assert "iter-stall" in text


def test_render_flags_alert_when_age_near_timeout():
    """ALERT marker fires when age >= 0.9 * timeout_seconds (and timeout > 0)."""
    now = datetime(2026, 5, 10, 20, 10, 0, tzinfo=timezone.utc)
    # Started 15 minutes ago, timeout=900 (15min); age/timeout = 1.0 >= 0.9.
    start_time = (now - timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
    events = [_start_event("p-alert", "iter-alert", start_time, timeout_seconds=900)]
    text, stalled = _visibility_tui._render(events, [], warn_after=300.0, now=now)
    assert stalled is True
    assert "ALERT" in text
    assert "iter-alert" in text


def test_render_clean_when_no_active():
    """No active dispatches => no stall, idle message."""
    now = datetime(2026, 5, 10, 20, 10, 0, tzinfo=timezone.utc)
    events = [
        _start_event("p1", "iter-001", "2026-05-10T20:00:00Z"),
        _done_event("p1", "iter-001", "2026-05-10T20:05:00Z"),
    ]
    text, stalled = _visibility_tui._render(events, [], warn_after=300.0, now=now)
    assert stalled is False
    # Either says "idle" (no active) or shows zero active dispatches.
    assert "idle" in text or "ACTIVE DISPATCHES (0)" in text


def test_render_includes_review_target_path_when_present():
    """v1.10.5 dispatch_start carries review_target_path; render must display it."""
    now = datetime(2026, 5, 10, 20, 10, 0, tzinfo=timezone.utc)
    start_time = (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    events = [_start_event(
        "p-target",
        "iter-target",
        start_time,
        review_target_path="consensus-state/active/iter-target/review-packet.yaml",
        timeout_seconds=900,
    )]
    text, _ = _visibility_tui._render(events, [], warn_after=300.0, now=now)
    assert "consensus-state/active/iter-target/review-packet.yaml" in text


def test_read_jsonl_missing_file_returns_empty(tmp_path):
    """No file => no error, empty list."""
    missing = tmp_path / "nope.jsonl"
    assert _visibility_tui._read_jsonl(missing) == []


def test_read_jsonl_skips_malformed_lines(tmp_path):
    """Partial-write tolerance: malformed JSON lines are skipped, not fatal."""
    p = tmp_path / "log.jsonl"
    p.write_text(
        '{"event":"a","pass_id":"p1","iteration_id":"i","timestamp_utc":"2026-05-10T20:00:00Z"}\n'
        '{this is not valid json}\n'
        '{"event":"b","pass_id":"p2","iteration_id":"i","timestamp_utc":"2026-05-10T20:01:00Z"}\n',
        encoding="utf-8",
    )
    events = _visibility_tui._read_jsonl(p)
    assert [e["event"] for e in events] == ["a", "b"]


def test_assemble_treats_dispatch_stalled_as_terminal():
    """iter-0036: dispatch_stalled (the watchdog's retroactive marker)
    must terminate the pairing in _assemble_dispatches so the orphan
    moves from 'active' to 'recent'.
    """
    events = [
        _start_event("p-stalled", "iter-stalled", "2026-05-11T03:00:00Z"),
        {
            "event": "dispatch_stalled",
            "pass_id": "p-stalled",
            "iteration_id": "iter-stalled",
            "timestamp_utc": "2026-05-11T03:15:00Z",
            "stall_reason": "watchdog_timeout",
        },
    ]
    state = _visibility_tui._assemble_dispatches(events)
    assert state["active"] == [], (
        f"dispatch_stalled should terminate pairing; active={state['active']}"
    )
    assert len(state["recent"]) == 1
    assert state["recent"][0]["event"] == "dispatch_stalled"


def test_assemble_treats_dispatch_aborted_as_terminal():
    """iter-0036 forward-compat: dispatch_aborted (iter-0037 streaming /
    operator-signal-file work) is recognized as terminal.
    """
    events = [
        _start_event("p-aborted", "iter-aborted", "2026-05-11T03:00:00Z"),
        {
            "event": "dispatch_aborted",
            "pass_id": "p-aborted",
            "iteration_id": "iter-aborted",
            "timestamp_utc": "2026-05-11T03:01:00Z",
            "abort_source": "operator_signal_file",
        },
    ]
    state = _visibility_tui._assemble_dispatches(events)
    assert state["active"] == []
    assert len(state["recent"]) == 1
    assert state["recent"][0]["event"] == "dispatch_aborted"


def test_assemble_pass_id_collision_across_iterations():
    """iter-0033 codex-rev-002 regression: two iterations sharing the same
    pass_id must NOT collapse into one entry. Previously the dict was keyed
    by pass_id only; a terminal event from iter-A would close an unrelated
    active dispatch from iter-B with the same pass_id, hiding stalls.
    Post-fix: key is (iteration_id, pass_id) tuple.
    """
    events = [
        _start_event("p-shared", "iter-A", "2026-05-10T20:00:00Z"),
        _start_event("p-shared", "iter-B", "2026-05-10T20:01:00Z"),
        _done_event("p-shared", "iter-A", "2026-05-10T20:05:00Z"),
        # iter-B is NEVER terminated.
    ]
    state = _visibility_tui._assemble_dispatches(events)
    # Pre-fix: state["active"] would be empty because the iter-A done event
    # would have closed iter-B too. Post-fix: iter-B's start remains active.
    active_iters = {entry["start"].get("iteration_id") for entry in state["active"]}
    assert active_iters == {"iter-B"}, (
        f"expected iter-B active (its dispatch_start was never terminated); "
        f"got active iterations {active_iters}"
    )
    recent_iters = {ev.get("iteration_id") for ev in state["recent"]}
    assert recent_iters == {"iter-A"}, (
        f"expected only iter-A in recent (iter-B has no terminal event); "
        f"got recent iterations {recent_iters}"
    )


def _streamed_line_event(pass_id: str, iteration_id: str, timestamp: str, seq: int, content: str) -> dict:
    return {
        "event": "dispatch_streamed_line",
        "pass_id": pass_id,
        "iteration_id": iteration_id,
        "timestamp_utc": timestamp,
        "seq": seq,
        "line_truncated": content,
        "line_full_length": len(content),
        "truncated": False,
    }


def _heartbeat_event(
    pass_id: str,
    iteration_id: str,
    timestamp: str,
    age_seconds: float,
    silence_age: float,
    last_seq: int = 0,
) -> dict:
    return {
        "event": "dispatch_heartbeat",
        "pass_id": pass_id,
        "iteration_id": iteration_id,
        "timestamp_utc": timestamp,
        "age_seconds": age_seconds,
        "last_streamed_line_age_seconds": silence_age,
        "last_streamed_line_seq": last_seq,
    }


def test_active_dispatch_shows_last_streamed_line():
    """iter-0037 follow-on: an active dispatch with streamed lines surfaces
    the most recent line's content in the render output.
    """
    now = datetime(2026, 5, 10, 20, 10, 0, tzinfo=timezone.utc)
    start_time = (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    events = [
        _start_event("p-stream", "iter-stream", start_time, timeout_seconds=900),
        _streamed_line_event("p-stream", "iter-stream",
                             (now - timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
                             1, "first line content"),
        _streamed_line_event("p-stream", "iter-stream",
                             (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
                             2, "second line content"),
        _streamed_line_event("p-stream", "iter-stream",
                             (now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
                             3, "third line LATEST"),
    ]
    text, _ = _visibility_tui._render(events, [], warn_after=300.0, now=now)
    # The third (latest) line's content must appear in the render text.
    assert "third line LATEST" in text, f"expected latest streamed line in output; got: {text!r}"
    # The seq number should be surfaced too.
    assert "last_line_seq=3" in text


def test_active_dispatch_shows_heartbeat_age():
    """iter-0037 follow-on: heartbeat events are surfaced with silence age."""
    now = datetime(2026, 5, 10, 20, 10, 0, tzinfo=timezone.utc)
    start_time = (now - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    hb_time = (now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    events = [
        _start_event("p-hb", "iter-hb", start_time, timeout_seconds=900),
        _heartbeat_event("p-hb", "iter-hb", hb_time, age_seconds=55.0, silence_age=45.0),
    ]
    text, _ = _visibility_tui._render(events, [], warn_after=300.0, now=now)
    # silence age should appear in humanized form (45s).
    assert "silence:" in text, f"expected silence in render; got: {text!r}"
    assert "45s" in text, f"expected 45s silence in render; got: {text!r}"


def test_silence_color_tier_red_at_90s(monkeypatch):
    """iter-0037 follow-on: silence_age >= 90s renders with ANSI red color
    (critical / about to auto-abort tier).

    The module enables ANSI at import time based on stdout.isatty(); under
    pytest stdout is not a TTY so we monkeypatch the color sentinels for
    this test only.
    """
    monkeypatch.setattr(_visibility_tui, "_ANSI_ENABLED", True)
    monkeypatch.setattr(_visibility_tui, "_RESET", "\x1b[0m")
    monkeypatch.setattr(_visibility_tui, "_DIM", "\x1b[2m")
    monkeypatch.setattr(_visibility_tui, "_BOLD", "\x1b[1m")
    monkeypatch.setattr(_visibility_tui, "_GREEN", "\x1b[32m")
    monkeypatch.setattr(_visibility_tui, "_YELLOW", "\x1b[33m")
    monkeypatch.setattr(_visibility_tui, "_RED", "\x1b[31m")
    monkeypatch.setattr(_visibility_tui, "_CYAN", "\x1b[36m")

    now = datetime(2026, 5, 10, 20, 10, 0, tzinfo=timezone.utc)
    start_time = (now - timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    hb_time = (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    events = [
        _start_event("p-red", "iter-red", start_time, timeout_seconds=900),
        _heartbeat_event("p-red", "iter-red", hb_time, age_seconds=119.0, silence_age=95.0),
    ]
    text, _ = _visibility_tui._render(events, [], warn_after=300.0, now=now)
    # Red tier => ANSI red sequence \x1b[31m must wrap the silence line.
    assert "\x1b[31m" in text, (
        f"expected ANSI red code in render (silence_age=95s is critical tier); "
        f"got: {text!r}"
    )


def test_main_once_returns_1_when_stalled(monkeypatch, tmp_path, capsys):
    """End-to-end: --once exits 1 when any active dispatch is past warn_after.

    Uses --warn-after=1 so the historic dispatch_start (definitionally >1s old)
    is flagged. Writes a synthetic unmatched start to a tmp dispatch-log.
    """
    state_dir = tmp_path / "consensus-state" / "state"
    state_dir.mkdir(parents=True)
    dispatch_log = state_dir / "dispatch-log.jsonl"
    dispatch_log.write_text(
        '{"event":"dispatch_start","pass_id":"px","iteration_id":"ix",'
        '"timestamp_utc":"2026-05-10T20:00:00Z","timeout_seconds":900}\n',
        encoding="utf-8",
    )
    rc = _visibility_tui.main([
        "--once",
        "--warn-after", "1",
        "--repo-root", str(tmp_path),
    ])
    out = capsys.readouterr().out
    assert rc == 1, f"expected rc=1 (stalled), got rc={rc}; out={out!r}"
    assert "ix" in out
