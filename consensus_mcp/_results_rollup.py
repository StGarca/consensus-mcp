"""consensus results - read-only project scorecard rollup.

Reads the project ledger ``consensus-state/state/results-v1.jsonl`` (one JSON
object per line, each conforming to ``schemas/results-v1.schema.json``) and
aggregates it into a PROJECT SCORECARD:

  * total findings by severity
  * dispositions: validated / dismissed_refuted / deferred / open
  * fixes_applied
  * iteration count
  * convergence rate (converged / total)
  * date span (first -> last record_updated_utc)

Forward-compat / trust rules (docs/design-consults/v1.19.0-result-logging.md):

  * Records whose ``consensus_results_schema_version`` != 1 are SKIPPED with a
    warning to stderr - never silently dropped, never miscounted.
  * Backfilled records (``backfilled is True`` or ``confidence ==
    "best-effort"``) are SEGREGATED into a separate section. They are
    best-effort reconstructions and must NEVER contaminate the authoritative
    (forward) totals.

This module is READ-ONLY: it never writes the ledger. It exposes both a
human-readable table (``render_table``) and a machine-readable dict
(``build_scorecard``) consumed by ``--json`` and the MCP tool.

Ledger path resolution mirrors the rest of the codebase (see
``consensus_mcp._paths.state_root``):

  CONSENSUS_MCP_STATE_ROOT  ->  CONSENSUS_MCP_REPO_ROOT/consensus-state  ->
  <cwd>/consensus-state

then ``/state/results-v1.jsonl``.

CLI:
  consensus results            # print the human-readable table
  consensus results --json     # print the machine-readable JSON dict
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from consensus_mcp._paths import state_root as _state_root
from consensus_mcp import _outcome_ledger
from consensus_mcp import _contributor_weights as _cw

# The schema version this rollup understands. Lines carrying any other value
# are forward/backward-incompatible and are skipped with a warning.
SUPPORTED_SCHEMA_VERSION = 1

# Severity ordering for stable, human-meaningful table output.
_SEVERITY_ORDER = ["critical", "blocking", "high", "medium", "low"]

# Disposition -> scorecard key.
_DISPOSITION_KEYS = {
    "validated_fixed": "validated",
    "dismissed_refuted": "dismissed_refuted",
    "deferred": "deferred",
    "open": "open",
}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _ledger_path(state_root: Optional[Path] = None) -> Path:
    """Resolve ``<state_root>/state/results-v1.jsonl``.

    When ``state_root`` is not supplied, fall back to the shared lazy resolver
    (CONSENSUS_MCP_STATE_ROOT -> CONSENSUS_MCP_REPO_ROOT/consensus-state -> cwd).
    """
    root = Path(state_root) if state_root is not None else _state_root()
    return root / "state" / "results-v1.jsonl"


def _outcome_ledger_path(state_root: Optional[Path] = None) -> Path:
    """Resolve ``<state_root>/state/outcome-ledger.jsonl`` - the external, append-only,
    AI-adjudicator-rejecting ledger that the contributor SCORECARD reads."""
    root = Path(state_root) if state_root is not None else _state_root()
    return root / "state" / "outcome-ledger.jsonl"


def render_contributor_scorecard(state_root: Optional[Path] = None) -> str:
    """Render the per-contributor performance SCORECARD (decision-support for declaring an
    AI lean) from the external outcome ledger. DESCRIPTIVE only - it measures how much good
    work each contributor produced; it sets no weight/lean (the operator declares the lean).
    Below 5 outcomes a contributor shows 'insufficient data' (no misleading rate). Returns
    an empty string when no outcome ledger exists yet."""
    outcomes = _outcome_ledger.read_outcomes(_outcome_ledger_path(state_root))
    if not outcomes:
        return ""
    card = _cw.build_scorecard(outcomes)
    lines: List[str] = ["", "-" * 60, "CONTRIBUTOR PERFORMANCE (decision-support; declare the lean from this)", "-" * 60]
    # rank by useful_rate (None/insufficient last), then by useful count
    def _rank(item):
        _c, v = item
        rate = v.get("useful_rate")
        enough = v["total"] >= 5
        return (-(rate if (rate is not None and enough) else -1.0), -v["useful"])
    for contributor, v in sorted(card.items(), key=_rank):
        if v["total"] >= 5 and v.get("useful_rate") is not None:
            rate = f"{v['useful_rate']:.0%}"
        else:
            rate = "insufficient data"
        lines.append(f"  {contributor:<18} useful {v['useful']}/{v['total']}   rate {rate}")
    lines.append("  (descriptive track-record only - score never judges an individual finding)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ledger loading
# ---------------------------------------------------------------------------

def _is_backfilled(record: Dict[str, Any]) -> bool:
    """A record is best-effort/backfilled if either flag says so."""
    return bool(record.get("backfilled")) or record.get("confidence") == "best-effort"


def _load_records(
    ledger: Path,
    *,
    warn: Any = None,
) -> Dict[str, Any]:
    """Read the JSONL ledger, partitioning into forward vs backfilled.

    Returns ``{"forward": [...], "backfilled": [...], "skipped_unknown": int}``.
    Unknown-schema_version lines are counted and a warning is emitted via the
    ``warn`` callable (defaults to stderr writes). Malformed JSON lines are also
    skipped with a warning.
    """
    if warn is None:
        def warn(msg: str) -> None:  # pragma: no cover - trivial default
            sys.stderr.write(msg + "\n")

    forward: List[Dict[str, Any]] = []
    backfilled: List[Dict[str, Any]] = []
    skipped_unknown = 0

    if not ledger.exists():
        return {"forward": forward, "backfilled": backfilled, "skipped_unknown": 0}

    with ledger.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as exc:
                warn(f"results-rollup: skipping malformed JSON on line {lineno}: {exc}")
                continue
            if not isinstance(rec, dict):
                warn(f"results-rollup: skipping non-object record on line {lineno}")
                continue

            version = rec.get("consensus_results_schema_version")
            if version != SUPPORTED_SCHEMA_VERSION:
                skipped_unknown += 1
                iter_id = rec.get("iteration_id", "<unknown>")
                warn(
                    "results-rollup: skipping record with unsupported "
                    f"consensus_results_schema_version={version!r} "
                    f"(iteration_id={iter_id}, line {lineno}); "
                    f"this rollup understands version {SUPPORTED_SCHEMA_VERSION}."
                )
                continue

            if _is_backfilled(rec):
                backfilled.append(rec)
            else:
                forward.append(rec)

    return {
        "forward": forward,
        "backfilled": backfilled,
        "skipped_unknown": skipped_unknown,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _empty_section() -> Dict[str, Any]:
    return {
        "iterations": 0,
        "total_findings": 0,
        "by_severity": {},
        "validated": 0,
        "dismissed_refuted": 0,
        "deferred": 0,
        "open": 0,
        "fixes_applied": 0,
        "converged": 0,
        "convergence_rate": None,
        "date_span": {"first": None, "last": None},
    }


def _aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate a list of (already schema-1, already-partitioned) records.

    Counts findings directly from each record's ``findings`` list (deduped by
    ``id`` within the iteration, mirroring the write-path invariant) so the
    rollup is robust even if a record's pre-computed ``counts`` block is stale
    or absent.
    """
    section = _empty_section()
    section["iterations"] = len(records)
    if not records:
        return section

    dates: List[str] = []
    converged = 0

    for rec in records:
        conv = rec.get("convergence") or {}
        if conv.get("converged") is True:
            converged += 1

        d = rec.get("record_updated_utc") or rec.get("sealed_at_utc")
        if isinstance(d, str) and d:
            dates.append(d)

        findings = rec.get("findings") or []
        seen_ids = set()
        for finding in findings:
            fid = finding.get("id")
            if fid is not None:
                if fid in seen_ids:
                    continue  # dedup by id within an iteration
                seen_ids.add(fid)

            section["total_findings"] += 1

            sev = finding.get("severity")
            if sev:
                section["by_severity"][sev] = section["by_severity"].get(sev, 0) + 1

            disp = finding.get("disposition")
            key = _DISPOSITION_KEYS.get(disp)
            if key:
                section[key] += 1

            # fixes_applied: count findings that carry a structured fix.
            if disp == "validated_fixed" and finding.get("fix"):
                section["fixes_applied"] += 1

    section["converged"] = converged
    section["convergence_rate"] = converged / len(records)

    if dates:
        dates_sorted = sorted(dates)
        section["date_span"] = {"first": dates_sorted[0], "last": dates_sorted[-1]}

    return section


def _architect_goals() -> List[Dict[str, Any]]:
    """Read-only architect-build goal listing (consult Q3,
    iteration-architect-hardening-2026-06-11). Empty list when the project
    has no `.consensus/architect/` tree. Derivation is the SHARED loop_step
    helper - never a duplicated state map. Repo root honors
    CONSENSUS_MCP_REPO_ROOT (the same env-first convention as the state
    root), falling back to cwd."""
    import os

    from consensus_mcp import _architect_paths as ap

    root = Path(os.environ.get("CONSENSUS_MCP_REPO_ROOT") or os.getcwd())
    arch = root.joinpath(*ap.GOAL_ROOT_PARTS)
    if not arch.is_dir():
        return []
    # architect-build verification gate configured? (advisory distinction
    # between needs_verification and needs_review; degrade on any failure)
    verification_configured = False
    try:
        import yaml

        raw = yaml.safe_load(
            (root / ".consensus" / "config.yaml").read_text(encoding="utf-8")
        ) or {}
        verification_configured = bool(
            (raw.get("architect_loop") or {}).get("verification") or ""
        )
    except Exception:
        pass
    from consensus_mcp.tools.architect_loop_step import (
        derive_goal_public_state,
    )

    goals: List[Dict[str, Any]] = []
    for entry in sorted(p for p in arch.iterdir() if p.is_dir()):
        try:
            goals.append(
                derive_goal_public_state(entry, verification_configured)
            )
        except Exception as exc:  # never let one bad goal dir kill results
            goals.append({"goal_id": entry.name, "cycle": None,
                          "state": f"unreadable:{exc}",
                          "last_handoff_utc": None})
    return goals


def build_scorecard(state_root: Optional[Path] = None) -> Dict[str, Any]:
    """Read the ledger and build the full project scorecard dict.

    Shape::

        {
          "authoritative": {<section>},   # forward, trustworthy totals
          "backfilled":    {<section>},   # best-effort, segregated
          "skipped_unknown_schema": int,  # count of unsupported-version lines
          "ledger_path": str,
        }

    where each ``<section>`` is the structure produced by :func:`_aggregate`.
    """
    ledger = _ledger_path(state_root)
    loaded = _load_records(ledger)

    return {
        "authoritative": _aggregate(loaded["forward"]),
        "backfilled": _aggregate(loaded["backfilled"]),
        "skipped_unknown_schema": loaded["skipped_unknown"],
        "ledger_path": str(ledger),
        "architect_goals": _architect_goals(),
    }


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------

def _render_section(title: str, section: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    lines.append(title)
    lines.append("-" * len(title))
    lines.append(f"  iterations:       {section['iterations']}")
    lines.append(f"  total findings:   {section['total_findings']}")

    # by severity, in a stable, meaningful order (known first, then any extras).
    by_sev = section["by_severity"]
    if by_sev:
        lines.append("  by severity:")
        ordered = [s for s in _SEVERITY_ORDER if s in by_sev]
        extras = [s for s in sorted(by_sev) if s not in _SEVERITY_ORDER]
        for sev in ordered + extras:
            lines.append(f"      {sev:<9} {by_sev[sev]}")
    else:
        lines.append("  by severity:      (none)")

    lines.append("  dispositions:")
    lines.append(f"      validated      {section['validated']}")
    lines.append(f"      dismissed      {section['dismissed_refuted']}")
    lines.append(f"      deferred       {section['deferred']}")
    lines.append(f"      open           {section['open']}")
    lines.append(f"  fixes applied:    {section['fixes_applied']}")

    rate = section["convergence_rate"]
    rate_str = "n/a" if rate is None else f"{rate:.0%} ({section['converged']}/{section['iterations']})"
    lines.append(f"  convergence rate: {rate_str}")

    span = section["date_span"]
    if span["first"] or span["last"]:
        lines.append(f"  date span:        {span['first']}  ->  {span['last']}")
    else:
        lines.append("  date span:        (none)")

    return lines


def render_table(scorecard: Dict[str, Any]) -> str:
    """Render the scorecard dict as a human-readable string table."""
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("consensus-mcp project scorecard")
    lines.append("=" * 60)
    lines.append(f"ledger: {scorecard.get('ledger_path', '(unknown)')}")
    lines.append("")

    lines.extend(_render_section("AUTHORITATIVE (forward)", scorecard["authoritative"]))
    lines.append("")

    backfilled = scorecard["backfilled"]
    lines.extend(_render_section("BACKFILLED (best-effort, NOT in totals)", backfilled))

    arch_goals = scorecard.get("architect_goals") or []
    if arch_goals:
        lines.append("")
        title = "ARCHITECT GOALS (Consensus Build, workflow D - preview)"
        lines.append(title)
        lines.append("-" * len(title))
        for g in arch_goals:
            handoff = g.get("last_handoff_utc") or "-"
            lines.append(
                f"  {g.get('goal_id')}: {g.get('state')} "
                f"(cycle {g.get('cycle')}, last handoff {handoff})"
            )

    skipped = scorecard.get("skipped_unknown_schema", 0)
    if skipped:
        lines.append("")
        lines.append(
            f"note: skipped {skipped} record(s) with an unsupported "
            "schema version (see warnings above)."
        )

    contributor_card = render_contributor_scorecard()
    if contributor_card:
        lines.append(contributor_card)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """``consensus results`` / ``consensus-results`` console-script entry point.

    ``results``         -> print the human-readable table.
    ``results --json``  -> print the machine-readable JSON dict.

    A leading ``results`` token is stripped so that the ``consensus results``
    invocation form (which forwards the literal subcommand) routes here, mirroring
    how ``_init_wizard.main`` strips a leading ``init``.
    """
    if argv is None:
        raw = sys.argv[1:]
    else:
        raw = list(argv)
    if raw and raw[0] == "results":
        raw = raw[1:]

    parser = argparse.ArgumentParser(
        prog="consensus results",
        description="Read-only project scorecard from consensus-state/state/results-v1.jsonl",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the scorecard as machine-readable JSON instead of a table",
    )
    args = parser.parse_args(raw)

    scorecard = build_scorecard()

    if args.json:
        sys.stdout.write(json.dumps(scorecard, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_table(scorecard) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
