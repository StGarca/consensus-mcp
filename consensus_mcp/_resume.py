"""consensus_mcp._resume — read-only operating-context snapshot for orchestrators.

Implements docs/specs/consensus-resume-spec.md (v2, sealed by codex-iter0002-2-pass1
on 2026-05-11). Addresses pass-2 findings codex-rev-001..005 inline:

  rev-001: output includes optional watermark_unchanged_since_prior +
           snapshot_watermark_drift fields (per §8 fast-path).
  rev-002: recent_activity.kind enum includes bundle-mutation kinds
           (patch_applied, review_packet_rebundled, operator_force_bundle_rewrite).
  rev-003: current_bundle_sha is computed BEFORE review classification (the
           spec's §5 step ordering was unimplementable as written; fixed here).
  rev-004: when no bundle_mutation event is present, current_bundle_sha falls
           back to review-packet.yaml's defect_target.base_sha.
  rev-005: recent_activity is sorted (timestamp_utc DESC, line_number DESC)
           so equal timestamps have a stable tiebreaker.

Read-only by construction: this module never writes to consensus-state/, never
signals processes, never spawns dispatches. The `snapshot()` function is the
sole public entry point.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from consensus_mcp._closure_invariant import check_closure_invariant
from consensus_mcp._self_drive import _scope_signature

SCHEMA_VERSION = 2

# Mirror of _dispatch_codex._invoke_codex's stall_silence_seconds default (45s).
# Per spec §6 / pass-1 codex-rev-007 Q1: single source of truth via import.
# _invoke_codex's value lives as a kwarg default; we capture it by inspecting
# the function signature at module-load time so renames or default-bumps in
# _dispatch_codex propagate automatically.
def _resolve_stall_threshold() -> float:
    try:
        from consensus_mcp import _dispatch_codex
        import inspect
        sig = inspect.signature(_dispatch_codex._invoke_codex)
        return float(sig.parameters["stall_silence_seconds"].default)
    except Exception:
        return 45.0


STALL_SILENCE_SECONDS = _resolve_stall_threshold()

# rev-002: bundle-mutation kinds are admitted into recent_activity. They ALSO
# populate bundle_mutation; the two surfaces are not mutually exclusive (one
# is the focused pointer, the other is the bounded audit stream).
BUNDLE_MUTATION_KINDS = frozenset({
    "patch_applied",
    "review_packet_rebundled",
    "operator_force_bundle_rewrite",
})

RECENT_ACTIVITY_KINDS = BUNDLE_MUTATION_KINDS | frozenset({
    "dispatch_started",
    "dispatch_heartbeat",
    "dispatch_streamed_line",
    "dispatch_completed",
    "dispatch_aborted",
    "review_packet_authored",
    "review_sealed",
    "goal_packet_authored",
    "operator_abort_signaled",
})

REVIEW_CLASSIFICATIONS = frozenset({"open", "closing", "consumed", "superseded", "invalid"})


# ---------------------------------------------------------------------------
# repo root resolution (mirrors _dispatch_codex._resolve_repo_root semantics).
# ---------------------------------------------------------------------------

_REPO_ROOT_MARKERS = ("consensus-state", "consensus_mcp", "consensus_mcp/validators")


def _has_repo_markers(candidate: Path) -> bool:
    return all((candidate / m).is_dir() for m in _REPO_ROOT_MARKERS)


def _resolve_repo_root(override: Path | None = None) -> Path:
    if override is not None:
        cand = Path(override).resolve()
        if _has_repo_markers(cand):
            return cand
        raise RuntimeError(f"override repo_root {cand} missing markers {_REPO_ROOT_MARKERS}")
    env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if env:
        cand = Path(env).resolve()
        if _has_repo_markers(cand):
            return cand
    cand = Path.cwd().resolve()
    if _has_repo_markers(cand):
        return cand
    for p in Path(__file__).resolve().parents:
        if _has_repo_markers(p):
            return p
    raise RuntimeError(
        "could not resolve repo_root; set CONSENSUS_MCP_REPO_ROOT or run from repo root"
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# auto-detection (§3): authorized_at_utc DESC primary, dir name DESC tiebreaker.
# ---------------------------------------------------------------------------

def _list_active_iterations(active_dir: Path) -> list[tuple[Path, str | None]]:
    """Return [(iter_dir, authorized_at_utc), ...] sorted DESC by auth time then name DESC."""
    if not active_dir.exists() or not active_dir.is_dir():
        return []
    entries: list[tuple[Path, str | None]] = []
    for child in active_dir.iterdir():
        if not child.is_dir():
            continue
        gp = child / "goal_packet.yaml"
        if not gp.is_file():
            continue
        auth_at: str | None = None
        try:
            data = yaml.safe_load(gp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                auth = data.get("authorization") or {}
                auth_at = auth.get("authorized_at_utc")
        except Exception:
            auth_at = None
        entries.append((child, auth_at))
    # Sort: authorized_at_utc DESC primary (None last), then dir name DESC.
    entries.sort(key=lambda t: (t[1] is None, t[1] or "", t[0].name), reverse=False)
    # The default ascending sort with reverse=False above sorts None-first by
    # the t[1] is None key; flip carefully:
    #   - want None timestamps LAST → sort key (t[1] is None, -timestamp, -name).
    # Achieve with a two-pass approach:
    with_ts = [(d, ts) for d, ts in entries if ts is not None]
    without_ts = [(d, ts) for d, ts in entries if ts is None]
    with_ts.sort(key=lambda t: (t[1], t[0].name), reverse=True)
    without_ts.sort(key=lambda t: t[0].name, reverse=True)
    return with_ts + without_ts


# ---------------------------------------------------------------------------
# dispatch-log walking.
# ---------------------------------------------------------------------------

def _walk_dispatch_log(log_path: Path, iteration_id: str) -> tuple[list[dict], list[str]]:
    """Return (events, warnings). Events are JSONL-parsed dicts for this iteration only.

    Each event has _line_number injected (rev-005 tiebreaker). Malformed lines
    are skipped with a warning citing the line number (not the content).
    """
    events: list[dict] = []
    warnings: list[str] = []
    if not log_path.is_file():
        return events, warnings
    try:
        text = log_path.read_text(encoding="utf-8")
    except Exception as exc:
        warnings.append(f"dispatch-log.jsonl read failed: {type(exc).__name__}")
        return events, warnings
    for idx, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"dispatch-log.jsonl line {idx}: malformed JSON skipped")
            continue
        if not isinstance(e, dict):
            warnings.append(f"dispatch-log.jsonl line {idx}: non-object skipped")
            continue
        if e.get("iteration_id") != iteration_id:
            continue
        e["_line_number"] = idx
        events.append(e)
    return events, warnings


def _event_kind(event: dict) -> str | None:
    """Dispatch-log events use 'event' key for kind."""
    return event.get("event")


# ---------------------------------------------------------------------------
# rev-003 + rev-004: current_bundle_sha computed BEFORE classification.
# ---------------------------------------------------------------------------

def _compute_current_bundle_sha(
    events: list[dict],
    review_packet_path: Path,
) -> tuple[str | None, str]:
    """Return (current_bundle_sha, source_label).

    source_label is one of:
      - "dispatch_log_bundle_mutation" — found a bundle-mutation event
      - "review_packet_base_sha"       — fell back to review-packet.yaml
      - "none"                          — neither source available
    """
    # rev-003: scan events for the latest bundle-mutation kind.
    mutation_events = [e for e in events if _event_kind(e) in BUNDLE_MUTATION_KINDS]
    if mutation_events:
        # latest by timestamp_utc, tiebreaker by line number
        mutation_events.sort(key=lambda e: (e.get("timestamp_utc", ""), e.get("_line_number", 0)))
        latest = mutation_events[-1]
        sha = latest.get("bundle_sha256") or latest.get("review_target_hash")
        if isinstance(sha, str) and sha:
            return sha, "dispatch_log_bundle_mutation"
        # codex-iter0003-3 rev-001 fix: a bundle-mutation event WITHOUT a usable
        # hash means we cannot fall through to the review-packet base_sha — that
        # would classify post-mutation state against the pre-mutation hash and
        # leave stale reviews looking "open" when they are actually superseded.
        # Report unknown + force the caller to surface a warning.
        return None, "mutation_event_missing_hash"
    # rev-004: fallback to review-packet.yaml base_sha ONLY when no mutation event exists.
    if review_packet_path.is_file():
        try:
            rp = yaml.safe_load(review_packet_path.read_text(encoding="utf-8"))
            if isinstance(rp, dict):
                dt = rp.get("defect_target") or {}
                sha = dt.get("base_sha")
                if isinstance(sha, str) and sha:
                    return sha, "review_packet_base_sha"
        except Exception:
            pass
    return None, "none"


def _compute_bundle_mutation(events: list[dict]) -> dict | None:
    """Return the latest bundle-mutation event projected into the §4 bundle_mutation shape."""
    candidates = [e for e in events if _event_kind(e) in BUNDLE_MUTATION_KINDS]
    if not candidates:
        return None
    candidates.sort(key=lambda e: (e.get("timestamp_utc", ""), e.get("_line_number", 0)))
    e = candidates[-1]
    return {
        "actor": e.get("actor") or {"id": e.get("actor_id"), "model_family": e.get("model_family")},
        "kind": _event_kind(e),
        "timestamp_utc": e.get("timestamp_utc"),
        "bundle_sha256": e.get("bundle_sha256") or e.get("review_target_hash"),
    }


# ---------------------------------------------------------------------------
# recent_activity (rev-002 + rev-005)
# ---------------------------------------------------------------------------

def _compute_recent_activity(events: list[dict], max_count: int = 10) -> list[dict]:
    """rev-002: enum includes bundle-mutation kinds. rev-005: (ts DESC, line DESC) sort."""
    out: list[dict] = []
    for e in events:
        kind = _event_kind(e)
        if kind not in RECENT_ACTIVITY_KINDS:
            continue
        out.append({
            "event_id": e.get("event_id") or f"line:{e.get('_line_number')}",
            "kind": kind,
            "actor_id": e.get("actor_id") or e.get("reviewer_id"),
            "timestamp_utc": e.get("timestamp_utc"),
            "_line_number": e.get("_line_number"),
        })
    # rev-005: stable tiebreaker.
    out.sort(key=lambda r: (r.get("timestamp_utc") or "", r.get("_line_number") or 0), reverse=True)
    out = out[:max_count]
    # Strip the internal sort key before returning.
    for r in out:
        r.pop("_line_number", None)
    return out


# ---------------------------------------------------------------------------
# in_flight_dispatches
# ---------------------------------------------------------------------------

def _compute_in_flight(
    events: list[dict],
    iter_dir: Path,
    *,
    include_streamed: bool = False,
    max_streamed_lines: int = 50,
) -> list[dict]:
    """Per pass_id: dispatch_started without matching completed/aborted is in-flight.

    codex-iter0003-1 rev-002 fix: when include_streamed is True, attach a bounded
    streamed_lines list to each in-flight entry (newest-last, capped at max_streamed_lines).
    """
    by_pass: dict[str, dict] = {}
    streams: dict[str, list[dict]] = {}
    for e in events:
        pid = e.get("pass_id")
        if not pid:
            continue
        kind = _event_kind(e)
        slot = by_pass.setdefault(pid, {"start": None, "end": None, "last_line": None, "last_hb": None})
        if kind == "dispatch_started":
            slot["start"] = e
        elif kind in ("dispatch_completed", "dispatch_done", "dispatch_aborted", "dispatch_failed"):
            slot["end"] = e
        elif kind == "dispatch_streamed_line":
            slot["last_line"] = e
            if include_streamed:
                streams.setdefault(pid, []).append({
                    "timestamp_utc": e.get("timestamp_utc"),
                    "line": (e.get("line") or "")[:200],
                })
        elif kind == "dispatch_heartbeat":
            slot["last_hb"] = e

    now = datetime.now(timezone.utc)
    capped_lines = max(0, min(int(max_streamed_lines), 500))
    result: list[dict] = []
    for pid, slot in by_pass.items():
        if slot["start"] is None or slot["end"] is not None:
            continue
        started = slot["start"].get("timestamp_utc")
        last_line_evt = slot["last_line"] or slot["last_hb"] or slot["start"]
        last_line_ts = last_line_evt.get("timestamp_utc") if last_line_evt else None
        seconds_since = None
        if last_line_ts:
            try:
                t = datetime.fromisoformat(last_line_ts.replace("Z", "+00:00"))
                seconds_since = max(0.0, (now - t).total_seconds())
            except Exception:
                seconds_since = None
        abort_signal_path = iter_dir.parent.parent / f"abort-dispatch-{pid}.signal"
        entry = {
            "pass_id": pid,
            "reviewer_id": slot["start"].get("reviewer_id"),
            "started_at_utc": started,
            "last_heartbeat_utc": (slot["last_hb"] or {}).get("timestamp_utc"),
            "seconds_since_last_line": seconds_since,
            "stall_silence_threshold_seconds": STALL_SILENCE_SECONDS,
            "abort_signal_path": str(abort_signal_path),
            "abort_signal_present": abort_signal_path.is_file(),
            "looks_stuck": (seconds_since is not None and seconds_since >= STALL_SILENCE_SECONDS),
        }
        if include_streamed:
            entry["streamed_lines"] = (streams.get(pid) or [])[-capped_lines:]
        result.append(entry)
    result.sort(key=lambda r: r["pass_id"])
    return result


# ---------------------------------------------------------------------------
# review classification (uses current_bundle_sha — rev-003 prereq satisfied).
# ---------------------------------------------------------------------------

def _parse_review(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _review_target_hash(review: dict) -> str | None:
    prov = review.get("dispatch_provenance") or {}
    h = prov.get("review_target_hash")
    if isinstance(h, str):
        return h
    # fallback: some review shapes carry it at top level
    h = review.get("review_target_hash")
    return h if isinstance(h, str) else None


def _classify_review(
    review: dict,
    current_bundle_sha: str | None,
    closure_cert: dict | None,
    iteration_state: str,
) -> tuple[str, str | None, str | None]:
    """Return (classification, superseded_by_pass_id, closure_source_path)."""
    pid = review.get("pass_id")
    hash_ = _review_target_hash(review)

    if closure_cert is not None:
        closer_pids = closure_cert.get("closing_review_pass_ids") or []
        if isinstance(closer_pids, list) and pid in closer_pids:
            if iteration_state in ("closed_passed", "closed_failed"):
                return ("consumed", None, closure_cert.get("_path"))
            return ("closing", None, closure_cert.get("_path"))

    # codex-iter0003-4 rev-001 fix: when current_bundle_sha is unknown, do NOT
    # default to "open" — there is no way to verify the review is current. Mark
    # as invalid so the orchestrator does not act on the review's findings.
    if current_bundle_sha is None:
        return ("invalid", None, None)

    if hash_ is not None and hash_ != current_bundle_sha:
        return ("superseded", None, None)

    # invalid: review claims a hash but it's None on disk OR it parses but lacks required fields
    if hash_ is None:
        return ("invalid", None, None)

    return ("open", None, None)


# ---------------------------------------------------------------------------
# watermark (§8 / rev-001)
# ---------------------------------------------------------------------------

def _compute_watermark(iter_dir: Path, log_path: Path) -> str:
    parts: list[str] = []
    try:
        parts.append(str(iter_dir.stat().st_mtime_ns))
    except FileNotFoundError:
        parts.append("0")
    try:
        parts.append(str(log_path.stat().st_mtime_ns))
    except FileNotFoundError:
        parts.append("0")
    yaml_parts: list[str] = []
    if iter_dir.is_dir():
        for p in sorted(iter_dir.glob("*.yaml")):
            try:
                yaml_parts.append(f"{p.name}:{p.stat().st_mtime_ns}")
            except FileNotFoundError:
                continue
    parts.append("|".join(yaml_parts))
    # codex-iter0003-5 rev-001 fix: include abort-dispatch-*.signal file
    # presence + mtimes in the watermark. Otherwise a cached fast-path snapshot
    # can report stale abort_signal_present after an operator writes or removes
    # an abort signal. Signals live in consensus-state/ (sibling of active/).
    signal_dir = iter_dir.parent.parent
    signal_parts: list[str] = []
    if signal_dir.is_dir():
        for sig in sorted(signal_dir.glob("abort-dispatch-*.signal")):
            try:
                signal_parts.append(f"{sig.name}:{sig.stat().st_mtime_ns}")
            except FileNotFoundError:
                continue
    parts.append("|".join(signal_parts))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# iteration_state, closure_invariant_status, expected_next_action
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _compute_iteration_state(
    goal_packet: dict | None,
    iteration_outcome: dict | None,
    closure_cert: dict | None,
) -> str:
    if goal_packet is None:
        return "unknown"
    if closure_cert is not None:
        verdict = closure_cert.get("verdict")
        if verdict == "quorum_close_passed":
            return "closed_passed"
        if verdict == "quorum_close_failed":
            return "closed_failed"
    if iteration_outcome is not None:
        state = iteration_outcome.get("state")
        if state == "blocked_needs_operator":
            return "blocked_operator"
    return "open"


def _compute_closure_invariant_status(
    bundle_mutation: dict | None,
    open_reviews: list[dict],
    current_bundle_sha: str | None,
) -> dict:
    last_mut_family = (bundle_mutation or {}).get("actor", {}).get("model_family")
    valid_closers = []
    if last_mut_family:
        if last_mut_family == "claude":
            valid_closers.append("codex")
        elif last_mut_family == "codex":
            valid_closers.append("claude")

    satisfiable_now = False
    blockers: list[str] = []
    validated_closer_pass_id: str | None = None
    if bundle_mutation is None:
        # No mutation yet; closure is trivially satisfiable per check_closure_invariant.
        # But there's nothing to close either.
        blockers.append("no_bundle_mutation_to_close")
    else:
        # Look for an open review whose closure-invariant self-check would pass.
        for r in open_reviews:
            if r.get("classification") != "open":
                continue
            if r.get("model_family") and r["model_family"] in valid_closers:
                if r.get("review_target_hash") == current_bundle_sha:
                    check = check_closure_invariant(
                        last_mutation=bundle_mutation,
                        closing_verdict={
                            "actor": {"id": r.get("reviewer_id"), "model_family": r.get("model_family"), "pass_id": r.get("pass_id")},
                            "review_target_hash": r.get("review_target_hash"),
                            "created_at_utc": r.get("sealed_at_utc"),
                        },
                    )
                    if check["ok"]:
                        satisfiable_now = True
                        validated_closer_pass_id = r.get("pass_id")
                        break
        if not satisfiable_now:
            blockers.append("needs_cross_family_reviewer")

    return {
        "satisfiable_now": satisfiable_now,
        "blockers": blockers,
        "last_bundle_mutation_family": last_mut_family,
        "valid_closer_families": valid_closers,
        "valid_closer_review_target_hash": current_bundle_sha,
        # codex-iter0003-1 rev-003 fix: carry the specific closer's pass_id forward
        # so expected_next_action's close_iteration_payload references the validated
        # closer, not the first review by pass_id sort order.
        "validated_closer_pass_id": validated_closer_pass_id,
    }


def _compute_expected_next_action(
    iteration_state: str,
    closure_inv: dict,
    open_reviews: list[dict],
    in_flight: list[dict],
    iter_dir: Path,
    goal_packet_path: Path,
    review_packet_path: Path,
) -> dict:
    """Exhaustive decision tree per spec §5 step 8. Every branch returns."""
    base = {
        "kind": None,
        "rationale": "",
        "suggested_command": None,
        "wait_for_dispatch_payload": None,
        "apply_proposed_patch_payload": None,
        "close_iteration_payload": None,
    }
    if iteration_state in ("closed_passed", "closed_failed"):
        base["kind"] = "operator_decision_required"
        base["rationale"] = f"iteration already {iteration_state}; start a new iteration"
        return base
    if iteration_state == "blocked_operator":
        base["kind"] = "operator_decision_required"
        base["rationale"] = "iteration is blocked; consult iteration-outcome.yaml"
        return base
    if iteration_state == "unknown":
        base["kind"] = "operator_decision_required"
        base["rationale"] = "goal_packet missing or unparseable"
        return base

    if closure_inv.get("satisfiable_now"):
        # codex-iter0003-1 rev-003 fix: use the validated closer pass_id surfaced
        # by _compute_closure_invariant_status, not "first open review by pass_id."
        # The first open review might not be the one that actually satisfied the
        # cross-family + hash-match + freshness check.
        base["kind"] = "close_iteration"
        base["rationale"] = "closure invariant satisfiable now"
        base["close_iteration_payload"] = {
            "closing_review_pass_id": closure_inv.get("validated_closer_pass_id"),
        }
        return base

    # patch_proposal check: any open review with at least one finding carrying patch_proposal?
    for r in open_reviews:
        if r.get("classification") != "open":
            continue
        for f in r.get("findings_summary", []):
            if f.get("has_patch_proposal"):
                base["kind"] = "apply_proposed_patch"
                base["rationale"] = "an open review carries a patch_proposal awaiting verification"
                base["apply_proposed_patch_payload"] = {
                    "finding_id": f.get("id"),
                    "patch_id": f.get("patch_id"),
                }
                return base

    healthy = [d for d in in_flight if not d.get("looks_stuck")]
    if healthy:
        d = min(healthy, key=lambda x: x.get("started_at_utc") or "")
        base["kind"] = "wait_for_dispatch"
        base["rationale"] = f"dispatch {d['pass_id']} is in-flight and streaming"
        base["wait_for_dispatch_payload"] = {
            "pass_id": d["pass_id"],
            "seconds_since_last_line": d.get("seconds_since_last_line"),
            "stall_silence_threshold_seconds": d.get("stall_silence_threshold_seconds"),
            "looks_stuck": False,
        }
        return base

    stuck = [d for d in in_flight if d.get("looks_stuck")]
    if stuck:
        base["kind"] = "operator_decision_required"
        base["rationale"] = (
            f"dispatch {stuck[0]['pass_id']} appears stalled; watchdog will auto-abort "
            "or operator may write abort-signal file"
        )
        return base

    # codex-iter0003-3 rev-002 fix: when suggesting a cross-family dispatch,
    # pick the actual valid closer family from closure_invariant_status — not
    # a hard-coded codex command. After a codex-authored bundle mutation the
    # valid closer is claude (not codex); recommending another codex dispatch
    # would create a stuck loop. Today only codex has a dispatcher CLI; if
    # the required family is claude (or any other), fall back to
    # operator_decision_required with the family name in the rationale.
    valid_closers = closure_inv.get("valid_closer_families") or []
    if "codex" in valid_closers:
        base["kind"] = "dispatch_cross_family_reviewer"
        base["rationale"] = "no in-flight pass; closure invariant requires cross-family review (codex)"
        base["suggested_command"] = (
            f"python -m consensus_mcp._dispatch_codex "
            f"--goal-packet {goal_packet_path} "
            f"--iteration-dir {iter_dir} "
            f"--reviewer-id codex-{iter_dir.name}-N "
            f"--review-target {review_packet_path}"
        )
        return base
    if valid_closers:
        base["kind"] = "operator_decision_required"
        base["rationale"] = (
            f"closure invariant requires a {valid_closers[0]} reviewer, but consensus-mcp's "
            f"built-in dispatcher only supports codex today. Operator must invoke a "
            f"{valid_closers[0]} reviewer manually (e.g., via the upcoming `consensus run`)."
        )
        return base
    # codex-iter0003-5 rev-002 fix: when there's no mutation event yet (fresh
    # iteration) but a review-packet exists, the natural next action is to
    # dispatch a cross-family reviewer — that creates the first peer review.
    # Defaulting codex as the suggested adapter (the only one with a CLI
    # dispatcher today; the upcoming `consensus run` will generalize this).
    if review_packet_path.is_file():
        base["kind"] = "dispatch_cross_family_reviewer"
        base["rationale"] = (
            "fresh iteration: review-packet authored but no review has been dispatched yet; "
            "kick off the cross-family review"
        )
        base["suggested_command"] = (
            f"python -m consensus_mcp._dispatch_codex "
            f"--goal-packet {goal_packet_path} "
            f"--iteration-dir {iter_dir} "
            f"--reviewer-id codex-{iter_dir.name}-1 "
            f"--review-target {review_packet_path}"
        )
        return base
    # Truly nothing to act on.
    base["kind"] = "operator_decision_required"
    base["rationale"] = "no bundle mutation, no review-packet, no in-flight dispatch; nothing for the resume tool to suggest"
    return base


def _compute_previous_iteration_summary(repo_root: Path, current_auth_at: str | None) -> dict | None:
    archive = repo_root / "consensus-state" / "archive"
    if not archive.is_dir():
        return None
    # closure certificates live somewhere under archive; search for closure-certificate.yaml
    best: tuple[str, Path] | None = None
    for cert in archive.rglob("closure-certificate.yaml"):
        data = _load_yaml(cert)
        if not data:
            continue
        closed_at = data.get("closed_at_utc") or data.get("sealed_at_utc")
        if not closed_at:
            continue
        if current_auth_at and closed_at >= current_auth_at:
            continue
        if best is None or closed_at > best[0]:
            best = (closed_at, cert)
    if not best:
        return None
    data = _load_yaml(best[1]) or {}
    return {
        "iteration_id": data.get("iteration_id"),
        "iteration_state": data.get("verdict") if data.get("verdict", "").startswith("quorum_close") else None,
        "closed_at_utc": best[0],
        "closure_certificate_path": str(best[1]),
    }


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def snapshot(
    iteration_id: str | None = None,
    *,
    include_streamed_lines: bool = False,
    max_streamed_lines: int = 50,
    prior_snapshot_watermark: str | None = None,
    repo_root: Path | None = None,
) -> dict:
    """Return a read-only operating-context snapshot. See docs/specs/consensus-resume-spec.md §5."""
    repo = _resolve_repo_root(repo_root)
    active = repo / "consensus-state" / "active"
    log_path = repo / "consensus-state" / "state" / "dispatch-log.jsonl"
    warnings: list[str] = []
    multiple: list[str] = []

    # --- §3 iteration resolution ---
    if iteration_id is not None:
        # codex-iter0003-6 rev-001 fix: path-traversal hardening. The MCP input
        # is documented as an iteration DIRECTORY NAME (a single path component),
        # not a path. Reject absolute paths, separators, and parent traversal
        # before any disk read.
        if (
            not iteration_id
            or iteration_id in (".", "..")
            or "/" in iteration_id
            or "\\" in iteration_id
            or Path(iteration_id).is_absolute()
        ):
            raise ValueError(f"iteration_id must be a single directory name: {iteration_id!r}")
        iter_dir = active / iteration_id
        # Defense-in-depth: confirm the resolved path stays inside active/.
        try:
            iter_dir.resolve().relative_to(active.resolve())
        except ValueError as exc:
            raise ValueError(
                f"iteration_id {iteration_id!r} resolves outside consensus-state/active/"
            ) from exc
        if not iter_dir.is_dir():
            raise ValueError(f"iteration_id not found: {iteration_id}")
        selected = iteration_id
    else:
        listed = _list_active_iterations(active) if active.exists() else []
        if not listed:
            return {
                "schema_version": SCHEMA_VERSION,
                "snapshot_taken_at_utc": _utc_now_iso(),
                "snapshot_watermark": "",
                "selected_iteration_id": None,
                "iteration_state": "unknown",
                "goal": None,
                "bundle_mutation": None,
                "recent_activity": [],
                "open_reviews": [],
                "in_flight_dispatches": [],
                "closure_invariant_status": {
                    "satisfiable_now": False,
                    "blockers": ["no_active_iteration"],
                    "last_bundle_mutation_family": None,
                    "valid_closer_families": [],
                    "valid_closer_review_target_hash": None,
                },
                "expected_next_action": {
                    "kind": "operator_decision_required",
                    "rationale": "no active iteration found in consensus-state/active/",
                    "suggested_command": None,
                    "wait_for_dispatch_payload": None,
                    "apply_proposed_patch_payload": None,
                    "close_iteration_payload": None,
                },
                "previous_iteration_summary": None,
                "warnings": [
                    "consensus-state/active/ contains no iteration directories with goal_packet.yaml"
                ] if active.exists() else ["consensus-state/active/ does not exist"],
                "multiple_active_iterations": [],
            }
        iter_dir = listed[0][0]
        selected = iter_dir.name
        if len(listed) > 1:
            multiple = [d.name for d, _ in listed]

    # --- §8 watermark fast-path (rev-001 fields) ---
    watermark = _compute_watermark(iter_dir, log_path)
    if prior_snapshot_watermark is not None and prior_snapshot_watermark == watermark:
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_taken_at_utc": _utc_now_iso(),
            "snapshot_watermark": watermark,
            "watermark_unchanged_since_prior": True,
            "selected_iteration_id": selected,
        }

    # --- §5 step 2: load goal_packet ---
    gp_path = iter_dir / "goal_packet.yaml"
    goal_packet = _load_yaml(gp_path)
    goal: dict | None = None
    if goal_packet is not None:
        auth = goal_packet.get("authorization") or {}
        recorded_sig = auth.get("scope_signature")
        expected_sig = None
        sig_valid = False
        try:
            expected_sig = _scope_signature(goal_packet)
            sig_valid = (expected_sig == recorded_sig)
        except Exception as exc:
            warnings.append(f"scope_signature recomputation failed: {type(exc).__name__}")
        goal = {
            "summary": (goal_packet.get("goal") or {}).get("summary"),
            "scope_signature": recorded_sig,
            "scope_signature_valid": sig_valid,
            "authorized_by": auth.get("authorized_by"),
            "authorized_at_utc": auth.get("authorized_at_utc"),
            "max_iterations": goal_packet.get("max_iterations"),
            "iterations_used": 0,  # not tracked on disk yet; placeholder
        }
    else:
        warnings.append(f"goal_packet.yaml missing or unparseable in {iter_dir.name}")

    # --- §5 step 3: walk dispatch log ---
    events, log_warnings = _walk_dispatch_log(log_path, selected)
    warnings.extend(log_warnings)

    # --- rev-003: compute current bundle sha BEFORE classifying reviews ---
    rp_path = iter_dir / "review-packet.yaml"
    current_bundle_sha, _bundle_source = _compute_current_bundle_sha(events, rp_path)
    # codex-iter0003-4 rev-001 fix: surface non-OK bundle-sha sources as warnings
    # so the orchestrator knows the review classification is unverified.
    if _bundle_source == "mutation_event_missing_hash":
        warnings.append(
            "bundle-mutation event found but lacks bundle_sha256/review_target_hash; "
            "reviews will be classified 'invalid' until a hashed mutation event is logged"
        )
    elif _bundle_source == "none" and any(_event_kind(e) for e in events):
        warnings.append(
            "current_bundle_sha unknown (no mutation event with hash and no review-packet); "
            "reviews cannot be verified against the working bundle state"
        )

    # --- §5 step 5: bundle_mutation ---
    bundle_mutation = _compute_bundle_mutation(events)

    # --- recent_activity (rev-002 enum, rev-005 tiebreaker) ---
    recent_activity = _compute_recent_activity(events, max_count=10)

    # --- in_flight_dispatches ---
    in_flight = _compute_in_flight(
        events,
        iter_dir,
        include_streamed=include_streamed_lines,
        max_streamed_lines=max_streamed_lines,
    )

    # --- closure cert + iteration-outcome ---
    closure_cert_path = iter_dir / "closure-certificate.yaml"
    closure_cert = _load_yaml(closure_cert_path)
    if closure_cert is not None:
        closure_cert["_path"] = str(closure_cert_path)
    iteration_outcome_path = iter_dir / "iteration-outcome.yaml"
    iteration_outcome = _load_yaml(iteration_outcome_path)

    # --- iteration_state ---
    iteration_state = _compute_iteration_state(goal_packet, iteration_outcome, closure_cert)

    # --- §5 step 4: classify reviews (now we have current_bundle_sha) ---
    open_reviews: list[dict] = []
    for review_path in sorted(iter_dir.glob("*-review.yaml")):
        review = _parse_review(review_path)
        if review is None:
            warnings.append(f"review file {review_path.name} failed to parse")
            continue
        classification, superseded_by, closure_source = _classify_review(
            review, current_bundle_sha, closure_cert, iteration_state,
        )
        # Project findings into summary
        findings_summary: list[dict] = []
        for f in review.get("findings", []) or []:
            pp = f.get("patch_proposal")
            findings_summary.append({
                "id": f.get("id"),
                "severity": f.get("severity"),
                "has_patch_proposal": isinstance(pp, dict),
                "patch_id": (pp or {}).get("patch_id"),
            })
        actor = review.get("actor") or {}
        open_reviews.append({
            "pass_id": review.get("pass_id"),
            "reviewer_id": review.get("reviewer_id") or actor.get("id"),
            "model_family": actor.get("model_family") or _infer_model_family(review.get("reviewer_id") or ""),
            "sealed_at_utc": review.get("sealed_at_utc"),
            "blocking_objections": review.get("blocking_objections") or [],
            "goal_satisfied": review.get("goal_satisfied"),
            "review_target_hash": _review_target_hash(review),
            "classification": classification,
            "superseded_by_pass_id": superseded_by,
            "closure_source": closure_source,
            "findings_summary": findings_summary,
        })
    open_reviews.sort(key=lambda r: r.get("pass_id") or "")

    # --- closure_invariant_status ---
    closure_inv = _compute_closure_invariant_status(bundle_mutation, open_reviews, current_bundle_sha)

    # --- expected_next_action ---
    expected_next = _compute_expected_next_action(
        iteration_state, closure_inv, open_reviews, in_flight, iter_dir, gp_path, rp_path,
    )

    # --- previous_iteration_summary ---
    prev_summary = _compute_previous_iteration_summary(
        repo, (goal or {}).get("authorized_at_utc"),
    )

    # --- §8 drift check: re-compute watermark; warn if changed ---
    end_watermark = _compute_watermark(iter_dir, log_path)
    snapshot_drift = None
    if end_watermark != watermark:
        warnings.append("watermark_drifted_during_snapshot; recommend retry")
        snapshot_drift = {"start": watermark, "end": end_watermark}

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_taken_at_utc": _utc_now_iso(),
        "snapshot_watermark": end_watermark,
        "snapshot_watermark_drift": snapshot_drift,
        "selected_iteration_id": selected,
        "iteration_state": iteration_state,
        "goal": goal,
        "bundle_mutation": bundle_mutation,
        "recent_activity": recent_activity,
        "open_reviews": open_reviews,
        "in_flight_dispatches": in_flight,
        "closure_invariant_status": closure_inv,
        "expected_next_action": expected_next,
        "previous_iteration_summary": prev_summary,
        "warnings": sorted(warnings),
        "multiple_active_iterations": multiple,
    }


def _infer_model_family(reviewer_id: str) -> str | None:
    rid = (reviewer_id or "").lower()
    if "codex" in rid:
        return "codex"
    if "claude" in rid:
        return "claude"
    return None
