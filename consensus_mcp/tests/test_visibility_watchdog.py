"""Unit tests for _visibility_watchdog.

The watchdog is a read-only consumer of consensus-state/state/dispatch-log.jsonl
in default mode; in --action=mark mode it APPENDS dispatch_stalled events
(append-only audit-log invariant). These tests exercise the pure-function
pieces (orphan detection, terminal-event recognition, append semantics)
+ the CLI end-to-end.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from consensus_mcp import _visibility_watchdog  # noqa: E402


def _recent_ts(seconds_ago: float = 60.0) -> str:
    """A UTC dispatch-event timestamp ``seconds_ago`` in the past.

    Relative to now so the seeded event stays inside the watchdog's
    ``--window-days`` filter (default 7d). A hardcoded absolute date ages out of
    that window and silently disables stale detection — the time-bomb that broke
    the main()-level tests once real time advanced ~7 days past 2026-05-11.
    """
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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


def _refused_event(pass_id: str, iteration_id: str, timestamp: str, **kw) -> dict:
    base = {
        "event": "dispatch_refused",
        "pass_id": pass_id,
        "iteration_id": iteration_id,
        "timestamp_utc": timestamp,
    }
    base.update(kw)
    return base


def _stalled_event(pass_id: str, iteration_id: str, timestamp: str, **kw) -> dict:
    base = {
        "event": "dispatch_stalled",
        "pass_id": pass_id,
        "iteration_id": iteration_id,
        "timestamp_utc": timestamp,
        "stall_reason": "watchdog_timeout",
    }
    base.update(kw)
    return base


# ---- find_stalled --------------------------------------------------------


def test_find_stalled_returns_empty_when_no_orphans():
    """Start + matching done → no orphan."""
    now = datetime(2026, 5, 11, 4, 0, 0, tzinfo=timezone.utc)
    events = [
        _start_event("p1", "iter-001", "2026-05-11T03:00:00Z"),
        _done_event("p1", "iter-001", "2026-05-11T03:05:00Z"),
    ]
    stale = _visibility_watchdog.find_stalled(events, stall_threshold_seconds=600, now=now)
    assert stale == []


def test_find_stalled_returns_orphan_older_than_threshold():
    """Start without terminal, age > threshold → returned."""
    now = datetime(2026, 5, 11, 4, 0, 0, tzinfo=timezone.utc)
    # Started 30 min ago, threshold 600s (10 min) → stale.
    events = [_start_event("p2", "iter-002", "2026-05-11T03:30:00Z")]
    stale = _visibility_watchdog.find_stalled(events, stall_threshold_seconds=600, now=now)
    assert len(stale) == 1
    assert stale[0]["start_event"]["pass_id"] == "p2"
    assert stale[0]["age_seconds"] >= 600


def test_find_stalled_ignores_orphan_younger_than_threshold():
    """Start without terminal, age < threshold → not returned."""
    now = datetime(2026, 5, 11, 4, 0, 0, tzinfo=timezone.utc)
    # Started 5 min ago, threshold 600s (10 min) → not stale.
    events = [_start_event("p3", "iter-003", "2026-05-11T03:55:00Z")]
    stale = _visibility_watchdog.find_stalled(events, stall_threshold_seconds=600, now=now)
    assert stale == []


def test_find_stalled_recognizes_dispatch_stalled_as_terminal():
    """iter-0036: dispatch_stalled is itself a terminal event. Re-running
    the watchdog on a log that already contains dispatch_stalled for an
    orphan does NOT re-mark it (idempotency).
    """
    now = datetime(2026, 5, 11, 4, 0, 0, tzinfo=timezone.utc)
    events = [
        _start_event("p4", "iter-004", "2026-05-11T03:00:00Z"),
        _stalled_event("p4", "iter-004", "2026-05-11T03:15:00Z"),
    ]
    stale = _visibility_watchdog.find_stalled(events, stall_threshold_seconds=600, now=now)
    assert stale == [], (
        "dispatch_stalled must terminate the pairing; otherwise watchdog re-marks orphans"
    )


def test_find_stalled_recognizes_dispatch_refused_as_terminal():
    """iter-0036 codex-rev-001 regression: dispatch_refused must be
    recognized as terminal. A refused dispatch with no later events would
    otherwise be misclassified as an orphan, and the watchdog would append
    a false dispatch_stalled, corrupting the audit shape.
    """
    now = datetime(2026, 5, 11, 4, 0, 0, tzinfo=timezone.utc)
    events = [
        _start_event("p5", "iter-005", "2026-05-11T03:00:00Z"),
        _refused_event("p5", "iter-005", "2026-05-11T03:00:01Z",
                       error_type="smoke_env_gate"),
    ]
    stale = _visibility_watchdog.find_stalled(events, stall_threshold_seconds=600, now=now)
    assert stale == [], "dispatch_refused must terminate pairing"


def test_find_stalled_uses_iteration_id_pass_id_tuple_key():
    """Two iterations sharing pass_id 'p1' must NOT collapse: terminating
    one must not silence the other.
    """
    now = datetime(2026, 5, 11, 4, 0, 0, tzinfo=timezone.utc)
    events = [
        _start_event("p-shared", "iter-A", "2026-05-11T03:00:00Z"),
        _start_event("p-shared", "iter-B", "2026-05-11T03:00:00Z"),
        _done_event("p-shared", "iter-A", "2026-05-11T03:05:00Z"),
        # iter-B is NEVER terminated.
    ]
    stale = _visibility_watchdog.find_stalled(events, stall_threshold_seconds=600, now=now)
    stale_iters = {s["start_event"]["iteration_id"] for s in stale}
    assert stale_iters == {"iter-B"}, (
        f"only iter-B should be flagged; got {stale_iters}"
    )


# ---- emit_stalled_event --------------------------------------------------


def test_emit_stalled_event_appends_to_log(tmp_path):
    """emit_stalled_event writes a single jsonl line with the right shape."""
    log = tmp_path / "dispatch-log.jsonl"
    pre = '{"event":"some_pre_existing","x":1}\n'
    log.write_text(pre, encoding="utf-8")

    now = datetime(2026, 5, 11, 4, 0, 0, tzinfo=timezone.utc)
    orphan = {
        "start_event": _start_event("p1", "iter-001", "2026-05-11T03:00:00Z"),
        "age_seconds": 3600,
        "start_timestamp_utc": "2026-05-11T03:00:00Z",
    }
    emitted = _visibility_watchdog.emit_stalled_event(log, orphan, 600, now)
    assert emitted["event"] == "dispatch_stalled"
    assert emitted["pass_id"] == "p1"
    assert emitted["iteration_id"] == "iter-001"
    assert emitted["stall_reason"] == "watchdog_timeout"
    assert emitted["age_seconds"] == 3600
    assert emitted["stall_threshold_seconds"] == 600

    # Pre-existing line preserved byte-for-byte.
    after = log.read_text(encoding="utf-8")
    assert after.startswith(pre), "append must not alter pre-existing content"
    # New line is the second one.
    lines = after.strip().splitlines()
    assert len(lines) == 2
    parsed = json.loads(lines[1])
    assert parsed["event"] == "dispatch_stalled"
    assert parsed["pass_id"] == "p1"


def test_emit_stalled_event_does_not_modify_existing_events(tmp_path):
    """Append-only: existing lines must remain byte-identical."""
    log = tmp_path / "dispatch-log.jsonl"
    pre_lines = [
        '{"event":"dispatch_start","pass_id":"px","iteration_id":"ix","timestamp_utc":"2026-05-11T03:00:00Z"}',
        '{"event":"some_other","x":42}',
        '{"event":"dispatch_done","pass_id":"py","iteration_id":"iy","timestamp_utc":"2026-05-11T03:05:00Z"}',
    ]
    log.write_text("\n".join(pre_lines) + "\n", encoding="utf-8")
    pre_bytes = log.read_bytes()

    now = datetime(2026, 5, 11, 4, 0, 0, tzinfo=timezone.utc)
    orphan = {
        "start_event": json.loads(pre_lines[0]),
        "age_seconds": 3600,
        "start_timestamp_utc": "2026-05-11T03:00:00Z",
    }
    _visibility_watchdog.emit_stalled_event(log, orphan, 600, now)

    after_bytes = log.read_bytes()
    # Pre-existing bytes must be a prefix of post-append bytes.
    assert after_bytes.startswith(pre_bytes), (
        "emit_stalled_event must be strictly append-only"
    )


# ---- main() CLI end-to-end ----------------------------------------------


def _scaffold_state_dir(tmp_path):
    state = tmp_path / "consensus-state" / "state"
    state.mkdir(parents=True)
    return state / "dispatch-log.jsonl"


def test_main_report_mode_does_not_mutate_log(tmp_path, capsys):
    """--action=report writes no events to the log."""
    log_path = _scaffold_state_dir(tmp_path)
    log_path.write_text(
        json.dumps(_start_event("p1", "iter-001", _recent_ts(60))) + "\n",
        encoding="utf-8",
    )
    pre_bytes = log_path.read_bytes()
    rc = _visibility_watchdog.main([
        "--repo-root", str(tmp_path),
        "--stall-after", "1",
        "--action", "report",
    ])
    out = capsys.readouterr().out
    assert rc == 1, f"orphan present => rc=1; got rc={rc}; out={out!r}"
    assert log_path.read_bytes() == pre_bytes, (
        "--action=report must not mutate the log"
    )
    parsed = json.loads(out)
    assert parsed["stale_count"] == 1
    assert "recommended_redispatch" in parsed["stale"][0]
    assert "_dispatch_codex" in parsed["stale"][0]["recommended_redispatch"]


def test_main_mark_mode_appends_dispatch_stalled(tmp_path, capsys):
    """--action=mark appends exactly one dispatch_stalled per orphan."""
    log_path = _scaffold_state_dir(tmp_path)
    log_path.write_text(
        json.dumps(_start_event("p2", "iter-002", _recent_ts(60))) + "\n",
        encoding="utf-8",
    )
    rc = _visibility_watchdog.main([
        "--repo-root", str(tmp_path),
        "--stall-after", "1",
        "--action", "mark",
    ])
    out = capsys.readouterr().out
    assert rc == 1
    parsed = json.loads(out)
    assert parsed["stale"][0].get("dispatch_stalled_appended") is True

    # The log now has 2 lines: original start + appended stalled.
    after_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(after_lines) == 2
    appended = json.loads(after_lines[1])
    assert appended["event"] == "dispatch_stalled"
    assert appended["pass_id"] == "p2"
    assert appended["stall_reason"] == "watchdog_timeout"


def test_main_mark_is_idempotent(tmp_path, capsys):
    """Second --action=mark run on the same log must NOT re-mark — the
    dispatch_stalled from the first run is terminal and pairs the orphan.
    """
    log_path = _scaffold_state_dir(tmp_path)
    log_path.write_text(
        json.dumps(_start_event("p3", "iter-003", _recent_ts(60))) + "\n",
        encoding="utf-8",
    )
    # First mark: appends.
    rc1 = _visibility_watchdog.main([
        "--repo-root", str(tmp_path),
        "--stall-after", "1",
        "--action", "mark",
    ])
    capsys.readouterr()
    line_count_after_first = len(log_path.read_text(encoding="utf-8").strip().splitlines())

    # Second mark: should NOT append again.
    rc2 = _visibility_watchdog.main([
        "--repo-root", str(tmp_path),
        "--stall-after", "1",
        "--action", "mark",
    ])
    out2 = capsys.readouterr().out
    line_count_after_second = len(log_path.read_text(encoding="utf-8").strip().splitlines())

    assert rc1 == 1, "first run sees an orphan → rc=1"
    assert rc2 == 0, f"second run must see zero stale (terminal exists); got rc={rc2}; out={out2!r}"
    assert line_count_after_first == line_count_after_second == 2, (
        f"idempotency violated: lines went {line_count_after_first} -> {line_count_after_second}"
    )


def test_main_returns_0_when_no_orphans(tmp_path, capsys):
    """Clean state → rc=0."""
    log_path = _scaffold_state_dir(tmp_path)
    log_path.write_text(
        json.dumps(_start_event("p4", "iter-004", _recent_ts(120))) + "\n"
        + json.dumps(_done_event("p4", "iter-004", _recent_ts(60))) + "\n",
        encoding="utf-8",
    )
    rc = _visibility_watchdog.main([
        "--repo-root", str(tmp_path),
        "--stall-after", "1",
        "--action", "report",
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"no orphans → rc=0; got rc={rc}; out={out!r}"


def test_main_missing_log_file_returns_0(tmp_path, capsys):
    """Missing log file (fresh repo) should not crash; report 0 stale."""
    (tmp_path / "consensus-state" / "state").mkdir(parents=True)
    # No log file written.
    rc = _visibility_watchdog.main([
        "--repo-root", str(tmp_path),
        "--action", "report",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["stale_count"] == 0


def test_recommended_redispatch_uses_external_codex_cli(tmp_path, capsys):
    """The recommended re-dispatch command must use _dispatch_codex
    (external codex-cli), NOT in-process subagent dispatch. This is the
    operator-locked external-process fallback policy.
    """
    log_path = _scaffold_state_dir(tmp_path)
    # Include reviewer_id explicitly so the suggestion is well-formed
    # (production dispatch_start events always carry reviewer_id).
    log_path.write_text(
        json.dumps(_start_event(
            "p5", "iter-005", _recent_ts(60),
            reviewer_id="codex-iter-005-1",
        )) + "\n",
        encoding="utf-8",
    )
    rc = _visibility_watchdog.main([
        "--repo-root", str(tmp_path),
        "--stall-after", "1",
        "--action", "report",
    ])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    cmd = parsed["stale"][0]["recommended_redispatch"]
    assert "consensus_mcp._dispatch_codex" in cmd, (
        f"recommendation must use external codex-cli path; got {cmd!r}"
    )
    assert "Agent" not in cmd, "must not suggest in-process subagent dispatch"
    assert "--iteration-dir consensus-state/active/iter-005" in cmd
    assert "--reviewer-id codex-iter-005-1" in cmd
    assert "codex-iter-005-1-pass2" in cmd, "pass_id must be bumped to avoid collision"


def test_main_skips_orphan_older_than_window(tmp_path, capsys):
    """An orphan older than --window-days is intentionally NOT flagged (the
    window skips ancient history); --window-days 0 disables the filter and the
    same orphan IS flagged. Documents the semantics the _recent_ts fix relies
    on (regression guard against the time-bomb returning).
    """
    log_path = _scaffold_state_dir(tmp_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_path.write_text(
        json.dumps(_start_event("pw", "iter-win", old_ts)) + "\n", encoding="utf-8")

    rc_windowed = _visibility_watchdog.main([
        "--repo-root", str(tmp_path), "--stall-after", "1", "--action", "report",
    ])
    parsed_windowed = json.loads(capsys.readouterr().out)
    assert rc_windowed == 0 and parsed_windowed["stale_count"] == 0, \
        "an orphan older than the default 7d window must be skipped"

    rc_unfiltered = _visibility_watchdog.main([
        "--repo-root", str(tmp_path), "--stall-after", "1",
        "--window-days", "0", "--action", "report",
    ])
    parsed_unfiltered = json.loads(capsys.readouterr().out)
    assert rc_unfiltered == 1 and parsed_unfiltered["stale_count"] == 1, \
        "--window-days 0 disables filtering; the ancient orphan is then flagged"


# ---- perf-rev-005: streaming reader + time-window cutoff -----------------


def test_streaming_reader_skips_old_events_outside_window(tmp_path):
    """_read_jsonl_streaming must drop events whose timestamp_utc is older
    than ``since_ts``. This is the perf-rev-005 fix: stalled-dispatch
    detection only needs recent unmatched starts, so the watchdog must
    not parse and yield every event in a multi-MB dispatch log.
    """
    log = tmp_path / "dispatch-log.jsonl"
    old_event = _start_event("p-old", "iter-old", "2020-01-01T00:00:00Z")
    recent_event = _start_event("p-new", "iter-new", "2026-05-11T03:55:00Z")
    log.write_text(
        json.dumps(old_event) + "\n" + json.dumps(recent_event) + "\n",
        encoding="utf-8",
    )

    # since_ts halfway between the two events: only the recent one survives.
    since_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = list(_visibility_watchdog._read_jsonl_streaming(log, since_ts=since_ts))
    pass_ids = [ev.get("pass_id") for ev in out]
    assert pass_ids == ["p-new"], (
        f"expected only the recent event to survive the window cutoff; got {pass_ids}"
    )

    # No cutoff → both events come through (back-compat with _read_jsonl).
    out_all = list(_visibility_watchdog._read_jsonl_streaming(log, since_ts=None))
    assert [ev.get("pass_id") for ev in out_all] == ["p-old", "p-new"]


def test_streaming_reader_keeps_events_with_missing_timestamps(tmp_path):
    """Events without a parseable timestamp are always yielded (we can't
    prove they're old, and find_stalled already drops them).
    """
    log = tmp_path / "dispatch-log.jsonl"
    no_ts = {"event": "dispatch_start", "pass_id": "p-x", "iteration_id": "i-x"}
    log.write_text(json.dumps(no_ts) + "\n", encoding="utf-8")
    since_ts = datetime(2026, 5, 11, tzinfo=timezone.utc)
    out = list(_visibility_watchdog._read_jsonl_streaming(log, since_ts=since_ts))
    assert len(out) == 1


def test_legacy_read_jsonl_still_returns_list(tmp_path):
    """_read_jsonl is preserved as a list-returning thin wrapper around
    the streaming reader so any caller depending on the old API keeps
    working.
    """
    log = tmp_path / "dispatch-log.jsonl"
    log.write_text(
        json.dumps(_start_event("p1", "iter-1", "2026-05-11T03:00:00Z")) + "\n",
        encoding="utf-8",
    )
    out = _visibility_watchdog._read_jsonl(log)
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["pass_id"] == "p1"


# ---- xplat-rev-006: _default_repo_root walks parents looking for markers --


def _scaffold_repo_markers(root: Path) -> None:
    """Create the directories that _has_repo_markers checks for."""
    (root / "consensus-state").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "consensus_mcp").mkdir(parents=True, exist_ok=True)
    (root / "consensus_mcp" / "validators").mkdir(parents=True, exist_ok=True)


def test_default_repo_root_walks_to_marker(tmp_path, monkeypatch):
    """Synthesize a temp tree with repo markers and verify _default_repo_root
    finds it from a nested cwd. The xplat-rev-006 fix replaced the unsafe
    ``parent.parent.parent`` fallback (which lands in site-packages after
    a non-editable install) with a marker-validated parent walk.
    """
    repo = tmp_path / "fake-repo"
    _scaffold_repo_markers(repo)
    nested = repo / "scripts" / "consensus_mcp" / "tests" / "deeply" / "nested"
    nested.mkdir(parents=True)

    # Operator did not supply env var; cwd is somewhere inside the fake repo.
    monkeypatch.delenv("CONSENSUS_MCP_REPO_ROOT", raising=False)
    monkeypatch.chdir(nested)

    resolved = _visibility_watchdog._default_repo_root()
    assert resolved == repo.resolve()


def test_default_repo_root_honors_env_var_when_valid(tmp_path, monkeypatch):
    """CONSENSUS_MCP_REPO_ROOT takes precedence when it validates."""
    repo = tmp_path / "env-repo"
    _scaffold_repo_markers(repo)
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(repo))
    resolved = _visibility_watchdog._default_repo_root()
    assert resolved == repo.resolve()


def test_default_repo_root_raises_when_env_var_invalid(tmp_path, monkeypatch):
    """Operator-supplied env var is authoritative; if it doesn't validate
    we fail loudly rather than silently fall through to a wrong tree.
    """
    bogus = tmp_path / "not-a-repo"
    bogus.mkdir()
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(bogus))
    with pytest.raises(_visibility_watchdog.RepoRootResolutionError):
        _visibility_watchdog._default_repo_root()


def test_default_repo_root_raises_when_no_markers_anywhere(tmp_path, monkeypatch):
    """Walk reaches filesystem root without finding markers → raise."""
    monkeypatch.delenv("CONSENSUS_MCP_REPO_ROOT", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    # The walk also inspects parents of __file__, which on this checkout
    # IS a valid repo (the real one). To isolate the failure mode we
    # patch _has_repo_markers to always return False.
    monkeypatch.setattr(_visibility_watchdog, "_has_repo_markers", lambda _p: False)
    with pytest.raises(_visibility_watchdog.RepoRootResolutionError):
        _visibility_watchdog._default_repo_root()


# ---- xplat-rev-007: cross-process locked append --------------------------


def test_locked_append_writes_payload(tmp_path):
    """Basic sanity: _locked_append appends the payload to the file."""
    log = tmp_path / "out.jsonl"
    _visibility_watchdog._locked_append(log, '{"a":1}\n')
    _visibility_watchdog._locked_append(log, '{"a":2}\n')
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"a":1}', '{"a":2}']


def test_locked_append_creates_parent_dir(tmp_path):
    """Append into a not-yet-existing parent dir."""
    log = tmp_path / "nested" / "deep" / "out.jsonl"
    _visibility_watchdog._locked_append(log, '{"x":true}\n')
    assert log.exists()
    assert log.read_text(encoding="utf-8") == '{"x":true}\n'


def test_locked_append_serializes_concurrent_writes(tmp_path):
    """Spawn N threads each calling _locked_append; verify ALL writes are
    persisted as complete JSONL records (no torn lines). This is best-effort
    — we can't easily force a race on a single host — but a 50-thread fan-out
    is sensitive enough to catch obviously-broken locking on Windows
    (xplat-rev-007 fix).
    """
    log = tmp_path / "concurrent.jsonl"
    payloads = [json.dumps({"i": i, "filler": "x" * 200}) + "\n" for i in range(50)]

    def writer(payload: str) -> None:
        _visibility_watchdog._locked_append(log, payload)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All N lines present, each parseable as JSON, all distinct i values.
    raw = log.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == len(payloads), (
        f"expected {len(payloads)} lines, got {len(lines)}; raw={raw!r}"
    )
    seen_i = set()
    for line in lines:
        ev = json.loads(line)  # raises if torn
        seen_i.add(ev["i"])
    assert seen_i == set(range(len(payloads)))


def test_emit_stalled_event_uses_locked_append(tmp_path, monkeypatch):
    """emit_stalled_event must route through _locked_append so any future
    refactor doesn't silently regress xplat-rev-007.
    """
    log = tmp_path / "dispatch-log.jsonl"
    called: list[tuple[Path, str]] = []

    def fake_locked_append(p: Path, payload: str) -> None:
        called.append((p, payload))
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(payload)

    monkeypatch.setattr(_visibility_watchdog, "_locked_append", fake_locked_append)

    now = datetime(2026, 5, 11, 4, 0, 0, tzinfo=timezone.utc)
    orphan = {
        "start_event": _start_event("p-lock", "iter-lock", "2026-05-11T03:00:00Z"),
        "age_seconds": 3600,
        "start_timestamp_utc": "2026-05-11T03:00:00Z",
    }
    _visibility_watchdog.emit_stalled_event(log, orphan, 600, now)

    assert len(called) == 1
    assert called[0][0] == log
    assert "dispatch_stalled" in called[0][1]


# ---- perf-rev-005: --window-days CLI flag --------------------------------


def test_main_window_days_filters_old_events(tmp_path, capsys):
    """--window-days drops events older than the window. An ancient orphan
    must NOT be flagged when the window excludes it.
    """
    log_path = _scaffold_state_dir(tmp_path)
    log_path.write_text(
        json.dumps(_start_event("p-ancient", "iter-old", "2020-01-01T00:00:00Z")) + "\n",
        encoding="utf-8",
    )
    rc = _visibility_watchdog.main([
        "--repo-root", str(tmp_path),
        "--stall-after", "1",
        "--window-days", "7",
        "--action", "report",
    ])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 0, f"ancient orphan must be filtered by window; got {out!r}"
    assert parsed["stale_count"] == 0


def test_main_window_days_zero_disables_filter(tmp_path, capsys):
    """--window-days 0 keeps the legacy behaviour (no filtering)."""
    log_path = _scaffold_state_dir(tmp_path)
    log_path.write_text(
        json.dumps(_start_event("p-ancient", "iter-old", "2020-01-01T00:00:00Z")) + "\n",
        encoding="utf-8",
    )
    rc = _visibility_watchdog.main([
        "--repo-root", str(tmp_path),
        "--stall-after", "1",
        "--window-days", "0",
        "--action", "report",
    ])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 1
    assert parsed["stale_count"] == 1
