"""Standalone CLI: scan iterations for closure-cross-verification-and-freshness invariant compliance.

Usage
-----
  # Scan all iterations under consensus-state/active/
  python -m consensus_mcp._validate_closure_invariant

  # Scan a specific iteration
  python -m consensus_mcp._validate_closure_invariant --iteration iteration-0009-mcp-wrapper-v1-1-x

  # Scan a custom active dir (used by tests)
  python -m consensus_mcp._validate_closure_invariant --active-dir /some/path

Output
------
JSON to stdout. Exit 0 if no non_compliant; 1 if any non_compliant.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

def _resolve_repo_root() -> Path:
    """Resolve repo root via CONSENSUS_MCP_REPO_ROOT env override (installed-wheel
    case where __file__ lands inside python_env/Lib/site-packages) -> fallback to
    in-tree __file__ walk (source-tree case).
    """
    import os
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent


_REPO_ROOT = _resolve_repo_root()
_DEFAULT_ACTIVE_DIR = _REPO_ROOT / "consensus-state" / "active"

# Import from sibling module (no relative import so CLI -m works).
from consensus_mcp._closure_invariant import (  # noqa: E402
    check_closure_invariant,
    last_mutation_from_audit,
    _parse_utc_timestamp,
)
from consensus_mcp._iteration_paths import canonical_review_name  # noqa: E402


# ---------------------------------------------------------------------------
# Review file parsing - maps on-disk yaml to closing_verdict shape.
# ---------------------------------------------------------------------------

def _load_review_yaml(path: Path) -> Optional[dict]:
    """Parse a claude-review.yaml or codex-review.yaml into closing_verdict shape.

    On-disk fields:
      reviewer_id       -> actor.id
      sealed_at_utc     -> created_at_utc
      packet_sha256     -> review_target_hash
      actor.model_family (when structured actor present) -> actor.model_family
      otherwise: derive from reviewer_id prefix or filename (claude-* / codex-*)

    Returns None if file missing or unreadable.

    Per v5 Finding 1, model_family is now part of the cross-family check; for
    historical reviews that don't carry a structured actor object, derive
    model_family from reviewer_id prefix or the review filename so the
    historical validator can still produce a verdict.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"_load_review_yaml: unreadable review file {path}: {exc}", file=sys.stderr)
        return None
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        print(f"_load_review_yaml: malformed YAML in {path}: {exc}", file=sys.stderr)
        return None
    structured_actor = data.get("actor") if isinstance(data.get("actor"), dict) else None
    reviewer_id = data.get("reviewer_id") or data.get("agent")
    if not reviewer_id and structured_actor is not None:
        reviewer_id = structured_actor.get("id")
    sealed_at = data.get("sealed_at_utc") or data.get("created_at_utc")
    target_hash = data.get("packet_sha256") or data.get("review_target_hash")
    if not (reviewer_id and sealed_at):
        return None

    # Prefer the structured actor object when present (post-iter-0018 reviews).
    family: Optional[str] = None
    if structured_actor is not None:
        family = structured_actor.get("model_family")
    if not family:
        # iter-0024 F3-005: tighten reviewer_id prefix heuristic to require a
        # dash boundary so 'codexica-bot' / 'claudette' don't silently route
        # to codex/claude family. Real reviewer_ids in this codebase always
        # have a dash (codex-iter0023-1, claude-iter0023-1); unmatched ids
        # leave family=None which fails cross_family closed (correct).
        rid = str(reviewer_id).lower()
        if rid.startswith("codex-"):
            family = "codex"
        elif rid.startswith("claude-"):
            family = "claude"
        else:
            # Fallback: file naming convention (same dash boundary for
            # consistency; codex-review.yaml / claude-review.yaml).
            fname = path.name.lower()
            if fname.startswith("codex-"):
                family = "codex"
            elif fname.startswith("claude-"):
                family = "claude"

    actor: dict = {"id": reviewer_id}
    if family:
        actor["model_family"] = family

    return {
        "actor": actor,
        "review_target_hash": target_hash,
        "created_at_utc": sealed_at,
    }


def _most_recent_closing_verdict(
    iteration_dir: Path,
    last_mutation: Optional[dict],
) -> tuple[Optional[dict], bool]:
    """Find the most recent review (claude or codex) with created_at_utc > last_mutation.timestamp.

    Returns (verdict_dict | None, closing_verdict_present: bool).
    present=True means a fresh-enough review exists. present=False means
    reviews exist but are all stale (or absent).
    """
    mut_ts: Optional[str] = None
    if last_mutation is not None:
        mut_ts = last_mutation.get("timestamp") or last_mutation.get("timestamp_utc")
    mut_dt: Optional[datetime] = _parse_utc_timestamp(mut_ts) if mut_ts is not None else None
    mutation_timestamp_missing = mut_ts is None or mut_dt is None

    candidates: list[tuple[Optional[datetime], dict]] = []
    for fname in (canonical_review_name("claude"), canonical_review_name("codex")):
        v = _load_review_yaml(iteration_dir / fname)
        if v is None:
            continue
        v_dt = _parse_utc_timestamp(v.get("created_at_utc"))
        if mutation_timestamp_missing:
            candidates.append((v_dt, v))
        elif v_dt is not None and mut_dt is not None and v_dt > mut_dt:
            candidates.append((v_dt, v))

    if not candidates:
        return None, False
    # Pick most recent by normalized timestamp. Invalid verdict timestamps sort
    # oldest and will fail the downstream freshness check.
    floor = datetime.min.replace(tzinfo=timezone.utc)
    best = max(candidates, key=lambda item: item[0] or floor)[1]
    return best, True


# ---------------------------------------------------------------------------
# Per-iteration scan
# ---------------------------------------------------------------------------

def scan_iteration(iteration_dir: Path) -> dict:
    """Return a result dict for a single iteration directory."""
    iteration_id = iteration_dir.name
    audit_path = iteration_dir / "independence-audit.yaml"

    if not audit_path.exists():
        return {
            "iteration_id": iteration_id,
            "has_apply_step_landed": False,
            "last_mutation": None,
            "closing_verdict_present": False,
            "invariant_check": None,
            "verdict": "n/a (no audit)",
        }

    try:
        audit_text = audit_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"scan_iteration: unreadable audit file {audit_path}: {exc}", file=sys.stderr)
        return {
            "iteration_id": iteration_id,
            "has_apply_step_landed": False,
            "last_mutation": None,
            "closing_verdict_present": False,
            "invariant_check": None,
            "verdict": "n/a (audit unreadable)",
        }
    try:
        audit_data = yaml.safe_load(audit_text) or {}
    except yaml.YAMLError as exc:
        print(f"scan_iteration: malformed YAML in audit file {audit_path}: {exc}", file=sys.stderr)
        return {
            "iteration_id": iteration_id,
            "has_apply_step_landed": False,
            "last_mutation": None,
            "closing_verdict_present": False,
            "invariant_check": None,
            "verdict": "n/a (audit malformed)",
        }

    audit_log = audit_data.get("audit_log") or []
    last_mutation = last_mutation_from_audit(audit_log)

    if last_mutation is None:
        return {
            "iteration_id": iteration_id,
            "has_apply_step_landed": False,
            "last_mutation": None,
            "closing_verdict_present": False,
            "invariant_check": None,
            "verdict": "n/a (no mutation)",
        }

    closing_verdict, cv_present = _most_recent_closing_verdict(iteration_dir, last_mutation)

    if not cv_present:
        return {
            "iteration_id": iteration_id,
            "has_apply_step_landed": True,
            "last_mutation": last_mutation,
            "closing_verdict_present": False,
            "invariant_check": None,
            "verdict": "in_flight",
        }

    inv = check_closure_invariant(last_mutation, closing_verdict)
    verdict = "compliant" if inv["ok"] else "non_compliant"

    return {
        "iteration_id": iteration_id,
        "has_apply_step_landed": True,
        "last_mutation": last_mutation,
        "closing_verdict_present": True,
        "invariant_check": inv,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Multi-iteration scan
# ---------------------------------------------------------------------------

def scan_all(iteration_dirs: list[Path]) -> dict:
    """Scan a list of iteration dirs and return output dict with summary."""
    results = [scan_iteration(d) for d in sorted(iteration_dirs, key=lambda p: p.name)]

    counts = {"compliant": 0, "non_compliant": 0, "n/a": 0, "in_flight": 0}
    for r in results:
        v = r["verdict"]
        if v.startswith("n/a"):
            counts["n/a"] += 1
        elif v in counts:
            counts[v] += 1

    return {
        "iterations_scanned": results,
        "summary": {
            "total": len(results),
            **counts,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan iterations for closure-invariant compliance."
    )
    parser.add_argument(
        "--iteration",
        metavar="ITERATION_ID",
        help="Scan a single iteration by name (relative to --active-dir).",
    )
    parser.add_argument(
        "--active-dir",
        metavar="PATH",
        default=str(_DEFAULT_ACTIVE_DIR),
        help="Directory containing iteration-* subdirs (default: consensus-state/active/).",
    )
    args = parser.parse_args()

    active_dir = Path(args.active_dir)

    if args.iteration:
        dirs = [active_dir / args.iteration]
    else:
        dirs = sorted(active_dir.glob("iteration-*"))

    output = scan_all(dirs)
    print(json.dumps(output, indent=2, default=str))

    has_non_compliant = output["summary"]["non_compliant"] > 0
    sys.exit(1 if has_non_compliant else 0)


if __name__ == "__main__":
    main()
