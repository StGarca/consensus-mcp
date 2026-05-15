"""Watchdog for stalled dispatches (Task #43; iter-0031 abandonment lesson).

Detects dispatch_start events in consensus-state/state/dispatch-log.jsonl that
lack a matching terminal event after a configurable wall-time threshold.
In --action=mark mode, appends a `dispatch_stalled` audit event for each
stale orphan so the audit shape closes and the visibility TUI clears it
from "active".

THE AUDIT LOG IS APPEND-ONLY. The watchdog NEVER edits or deletes existing
events. It only appends new `dispatch_stalled` records that share the
orphan's identifiers (iteration_id / reviewer_id / pass_id).

Terminal events recognized for pairing (per iter-0036 codex-rev-001 review
+ workflow taxonomy):
  - dispatch_done       (success)
  - dispatch_failed     (helper-side exception or codex non-zero exit)
  - dispatch_refused    (env-gate / pre-flight refusal)
  - dispatch_aborted    (operator-signal-file or watchdog-silence, iter-0037+)
  - dispatch_stalled    (this watchdog's own retroactive marker)

Including dispatch_refused matters: refused dispatches already have a
terminal event; treating them as orphans would corrupt the audit shape
by appending a synthetic dispatch_stalled.

EXTERNAL-PROCESS FALLBACK POLICY:
Any codex re-dispatch following a stalled in-process subagent dispatch
MUST use the external codex-cli pattern via `python -m
consensus_mcp._dispatch_codex …`. In-process Claude subagent re-dispatch
is the failure mode iter-0031 documented; see
`memory/feedback_external_process_fallback.md`.

Usage
-----
    # Default: read-only report; rc=1 if any orphan is stale, rc=0 otherwise.
    python consensus_mcp/_visibility_watchdog.py

    # Mutate: append dispatch_stalled events for each stale orphan.
    python consensus_mcp/_visibility_watchdog.py --action mark

    # Custom thresholds (seconds).
    python consensus_mcp/_visibility_watchdog.py --stall-after 1800
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

# v1.15.8 Q1(d): `_locked_append` is the sealed-provenance/audit +
# watchdog integrity primitive. Its OS file lock (msvcrt.locking /
# fcntl.flock) serializes CROSS-process writers, but the observed
# loss (windows-py3.10 CI: 46/50 lines from a 50-thread, ONE-process
# fan-out) was INTRA-process thread contention — for which an OS file
# lock is the wrong primitive (same lesson as v1.15.7
# `_dispatch_base._log_dispatch`). A process-wide lock serializes all
# in-process callers deterministically; the OS lock then only matters
# for genuine cross-process contention. Converged via Workflow A
# (claude+codex+gemini, weighted-synthesis, shared-prior check
# PASSED): iteration-v1158-flaky-ci-and-locked-append.
_APPEND_LOCK = threading.Lock()
from pathlib import Path
from typing import Iterator

WATCHDOG_VERSION = "1.1.0"

# Per iter-0036 codex-rev-001: include dispatch_refused as terminal so a
# refused dispatch (env-gate failure, etc.) isn't reclassified as an
# orphan. dispatch_aborted reserved for iter-0037 streaming/abort work.
_TERMINAL_EVENTS = {
    "dispatch_done",
    "dispatch_failed",
    "dispatch_refused",
    "dispatch_aborted",
    "dispatch_stalled",
}

# Markers identifying the consensus-mcp repo root (mirrors
# _dispatch_codex._REPO_ROOT_MARKERS, kept local to avoid an import cycle).
_REPO_ROOT_MARKERS = ("consensus-state", "consensus_mcp", "consensus_mcp/validators")


class RepoRootResolutionError(RuntimeError):
    """Raised when repo_root cannot be resolved to a valid repo (no markers found)."""


def _has_repo_markers(candidate: Path) -> bool:
    """Return True iff candidate contains all _REPO_ROOT_MARKERS as subpaths."""
    return all((candidate / marker).is_dir() for marker in _REPO_ROOT_MARKERS)


def _default_repo_root() -> Path:
    """Resolve the repo root using marker validation (xplat-rev-006 fix).

    The prior heuristic ``Path(__file__).resolve().parent.parent`` lands
    inside site-packages after a non-editable install, making the watchdog scan
    the wrong tree. Walk parents looking for repo markers; raise if none match
    and no env var was supplied.

    Resolution order (first candidate with all repo markers wins):
      1. CONSENSUS_MCP_REPO_ROOT env var (must validate; authoritative)
      2. Path.cwd() (operator usually invokes from repo root)
      3. Walk parents of Path(__file__) until filesystem root
    """
    candidates_tried: list[tuple[str, Path]] = []

    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        candidate = Path(override).resolve()
        candidates_tried.append(("CONSENSUS_MCP_REPO_ROOT", candidate))
        if _has_repo_markers(candidate):
            return candidate
        # Operator-supplied env var is authoritative — don't silently fall
        # through. Mirrors _dispatch_codex behaviour.
        raise RepoRootResolutionError(
            f"CONSENSUS_MCP_REPO_ROOT={override!r} was set but the path "
            f"{candidate} does not contain all required repo markers "
            f"{_REPO_ROOT_MARKERS}. Either fix the path or unset the env "
            f"var to use automatic discovery."
        )

    seen: set[Path] = set()

    def _walk(start: Path, label: str) -> Path | None:
        """Walk start and its parents looking for repo markers."""
        node = start
        while node not in seen:
            seen.add(node)
            candidates_tried.append((f"{label} ({node.name or node})", node))
            if _has_repo_markers(node):
                return node
            if node.parent == node:
                return None
            node = node.parent
        return None

    # 1. Walk cwd and its parents (operator usually invokes from inside the repo).
    found = _walk(Path.cwd().resolve(), "cwd-walk")
    if found is not None:
        return found

    # 2. Walk parents of __file__ (handles in-tree installs; fails on site-packages
    #    because no parent up to filesystem root contains the marker set).
    found = _walk(Path(__file__).resolve().parent, "__file__-walk")
    if found is not None:
        return found

    tried_msg = "; ".join(f"{name}={path}" for name, path in candidates_tried)
    raise RepoRootResolutionError(
        f"Cannot resolve consensus-mcp repo root. None of the candidates contain "
        f"all required markers {_REPO_ROOT_MARKERS}. Set CONSENSUS_MCP_REPO_ROOT "
        f"to the repo root directory. Candidates tried: {tried_msg}"
    )


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL; tolerate missing file + malformed lines (mirrors TUI).

    Legacy non-streaming API. Kept for any caller that depends on a list
    return type. New code should prefer ``_read_jsonl_streaming``.
    """
    return list(_read_jsonl_streaming(path))


def _read_jsonl_streaming(path: Path, since_ts: datetime | None = None) -> Iterator[dict]:
    """Yield parsed events from a JSONL file (perf-rev-005 fix).

    Streams line-by-line instead of slurping the whole file. Events whose
    ``timestamp_utc`` parses to a value strictly older than ``since_ts``
    are skipped; events with missing/unparseable timestamps are always
    yielded (we can't prove they're old, and ``find_stalled`` already
    handles unparseable timestamps).
    """
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_ts is not None:
                    ts = _parse_ts(ev.get("timestamp_utc"))
                    if ts is not None and ts < since_ts:
                        continue
                yield ev
    except OSError:
        return


def _locked_append(path: Path, payload: str) -> None:
    """Append ``payload`` to ``path`` under an OS-appropriate exclusive lock
    (xplat-rev-007 fix).

    Concurrency model (v1.15.8 Q1(d), Workflow A converged):
      - INTRA-process (threads of one process): serialized by the
        module-level ``_APPEND_LOCK`` held for the whole append. This
        is the verified-observed contention class (F1: 50 threads /
        one process lost 4 lines). Deterministic; no scheduler luck.
      - CROSS-process: the OS lock (``msvcrt.locking`` byte-range on
        Windows / ``fcntl.flock`` on POSIX) still serializes distinct
        processes.
      - **Fail-LOUD (independent safeguard):** if the OS lock cannot
        be acquired we DO NOT silently write unlocked — that would
        let the sealed-provenance/audit log be silently incomplete
        (the F1 defect: ``except OSError: pass`` + unlocked write).
        We raise so integrity loss is observable, never silent. This
        protects audit integrity regardless of *why* the OS lock
        failed.
      - If neither lock module is importable (rare platform), the
        in-process lock still serializes threads; cross-process is
        unsupportable there, so we warn (unchanged) rather than fail
        the platform outright.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload.encode("utf-8")
    try:
        import msvcrt  # type: ignore[import-not-found]
    except ImportError:
        msvcrt = None  # type: ignore[assignment]
    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:
        fcntl = None  # type: ignore[assignment]

    with _APPEND_LOCK:
        if msvcrt is None and fcntl is None:
            sys.stderr.write(
                "WARNING: _locked_append: neither msvcrt nor fcntl "
                "available; cross-process serialization unavailable "
                "(in-process writers are still serialized).\n"
            )
            with path.open("ab") as f:
                f.write(data)
            return

        with path.open("ab") as f:
            if msvcrt is not None:
                # Windows byte-range lock (1 byte at the writer's
                # offset). LK_LOCK blocks with internal retries; the
                # lock auto-releases on handle close.
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                except OSError as exc:
                    # Fail LOUD: never a silent unlocked write to an
                    # integrity log. (Was: `except OSError: pass` +
                    # unlocked write — the F1 audit-loss defect.)
                    raise OSError(
                        f"_locked_append: could not acquire OS lock on "
                        f"{path} ({exc!r}); refusing a silent unlocked "
                        f"write to a sealed-provenance/audit log"
                    ) from exc
                f.write(data)
            else:
                # POSIX advisory lock; auto-released on close.
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(data)


def find_stalled(events, stall_threshold_seconds: float, now: datetime) -> list[dict]:
    """Return orphan dispatch_start events older than stall_threshold_seconds.

    Pairing uses (iteration_id, pass_id) tuple key (per iter-0033 codex-rev-002
    fix). dispatch_stalled is itself a terminal event, so re-running the
    watchdog after a previous --action=mark run does NOT re-mark the same
    orphan.

    ``events`` may be any iterable of dicts (list or generator from
    ``_read_jsonl_streaming``).
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


def _suggest_redispatch(orphan_start: dict) -> str:
    """Recommended external-process re-dispatch command for the given orphan.

    Uses the proven _dispatch_codex CLI pattern; never in-process subagent
    (per iter-0031 lesson + feedback_external_process_fallback.md). The
    pass_id is bumped to f"{reviewer_id}-pass2" to avoid collision with
    the orphan; operator must adjust manually if pass2 is also taken.
    """
    iter_id = orphan_start.get("iteration_id", "<iteration_id>")
    reviewer_id = orphan_start.get("reviewer_id", "<reviewer_id>")
    return (
        "python_env/python.exe -m consensus_mcp._dispatch_codex "
        f"--goal-packet consensus-state/active/{iter_id}/goal_packet.yaml "
        f"--iteration-dir consensus-state/active/{iter_id} "
        f"--reviewer-id {reviewer_id} "
        f"--pass-id {reviewer_id}-pass2 "
        "--codex-bin codex "
        f"--review-target consensus-state/active/{iter_id}/review-packet.yaml "
        "--timeout-seconds 900"
    )


def emit_stalled_event(log_path: Path, orphan: dict, stall_threshold_seconds: float, now: datetime) -> dict:
    """Append a dispatch_stalled event for the given orphan; return the event dict.

    APPEND-ONLY: never modifies or deletes existing events.

    Per iter-0036 codex-rev-002 review: stall_reason MUST be "watchdog_timeout"
    (the goal_packet-contracted value). A free-text 'detail' field carries
    any additional context.
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
        "detail": "orphan dispatch_start with no terminal event past stall threshold",
    }
    _locked_append(log_path, json.dumps(event) + "\n")
    return event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog for stalled dispatches.")
    parser.add_argument("--repo-root", default=None,
                        help="Override repo root (default: env var or __file__ walk).")
    parser.add_argument("--stall-after", type=float, default=600.0,
                        help="Seconds before an orphan is stale (default 600 = 10 min).")
    parser.add_argument("--action", choices=("report", "mark"), default="report",
                        help="report = read-only scan; mark = append dispatch_stalled events.")
    parser.add_argument("--window-days", type=float, default=7.0,
                        help="Only inspect events newer than this many days "
                             "(default 7). Stalled-dispatch detection only "
                             "needs recent unmatched starts; older history "
                             "is skipped to avoid slurping the full log. "
                             "Pass 0 (or negative) to disable filtering.")
    ns = parser.parse_args(argv)

    repo_root = Path(ns.repo_root).resolve() if ns.repo_root else _default_repo_root()
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"

    now = datetime.now(timezone.utc)
    since_ts: datetime | None = None
    if ns.window_days and ns.window_days > 0:
        since_ts = now - timedelta(days=ns.window_days)
    events = _read_jsonl_streaming(log_path, since_ts=since_ts)
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
            "recommended_redispatch": _suggest_redispatch(start),
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
