"""Per-iteration cost + outcome telemetry (sp-consensus-optimization B5).

"Measure the measurement": both 2026-05 consults flagged that we assert the
discipline catches bugs but never track cost-per-caught-defect by tier. This is
the substrate - an append-only JSONL of one record per closed iteration - plus a
rollup that reports cost-per-blocking-finding by tier, so the operator can SEE
whether DEEP-tier rigor pays off versus STANDARD. Pure I/O + aggregation; no AI
judgement involved.
"""
from __future__ import annotations

import json
from pathlib import Path

_REQUIRED = ("iteration_id", "tier")
_NUMERIC = ("panel_size", "n_dispatches", "wall_clock_s", "retries",
            "smokes_run", "blocking_findings")


def _nonneg_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def _record_is_valid(record: dict) -> bool:
    """A stored row is valid iff required fields are present and every numeric field
    that IS present is a non-negative number. Used at the READ boundary so a
    hand-edited/corrupt row never reaches the rollup (codex-rev-002)."""
    if not isinstance(record, dict):
        return False
    if any(not record.get(f) for f in _REQUIRED):
        return False
    return all(_nonneg_number(record[k]) for k in _NUMERIC if k in record)


def record_iteration(telemetry_path: str | Path, record: dict) -> None:
    """Append one per-iteration telemetry record (append-only JSONL).

    Required: iteration_id, tier. Numeric fields default to 0 if absent. Numeric
    values must be non-negative numbers."""
    missing = [f for f in _REQUIRED if not record.get(f)]
    if missing:
        raise ValueError(f"telemetry record missing required fields: {missing}")
    out = {"iteration_id": record["iteration_id"], "tier": record["tier"]}
    for k in _NUMERIC:
        v = record.get(k, 0)
        if not _nonneg_number(v):
            raise ValueError(f"telemetry field {k!r} must be a non-negative number, got {v!r}")
        out[k] = v
    path = Path(telemetry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(out, sort_keys=True) + "\n")


def read_iterations(telemetry_path: str | Path) -> list[dict]:
    """Read all telemetry records (empty list if absent; malformed rows skipped)."""
    path = Path(telemetry_path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _record_is_valid(record):  # skip hand-edited/corrupt rows (codex-rev-002)
            out.append(record)
    return out


def summarize_by_tier(records: list[dict]) -> dict[str, dict]:
    """Roll up per-tier: iteration count, total dispatches, total wall-clock, total
    blocking findings, and cost-per-blocking-finding (dispatches / blocking). The
    last is None when a tier caught zero blocking findings (cost with no payoff -
    the signal the operator wants to see)."""
    by_tier: dict[str, dict] = {}
    for r in records:
        t = r.get("tier", "unknown")
        agg = by_tier.setdefault(t, {"iterations": 0, "dispatches": 0,
                                     "wall_clock_s": 0.0, "blocking_findings": 0})
        agg["iterations"] += 1
        agg["dispatches"] += r.get("n_dispatches", 0)
        agg["wall_clock_s"] += r.get("wall_clock_s", 0)
        agg["blocking_findings"] += r.get("blocking_findings", 0)
    for agg in by_tier.values():
        bf = agg["blocking_findings"]
        agg["dispatches_per_blocking_finding"] = (agg["dispatches"] / bf) if bf else None
    return by_tier
