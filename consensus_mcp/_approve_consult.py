"""Composed consult-approval flow (consult Q6 + Finding C/#7).

ONE command that turns a converged consult into an accepted
`.consensus/design-approved` marker, so agents/operators stop reverse-engineering
the `_design_approval` internals (the prepare -> hand-edit EDIT_ME -> mint slog).

What it does, end to end:
  1. Resolve the repo root via the SINGLE strict resolver
     (`_dispatch_base._resolve_repo_root`, env-first, fail-closed) -- the same
     one the shell binaries use, so the MCP tool and the CLI can never resolve
     different roots (Finding #7).
  2. Validate the >=2-non-claude-reviewer precondition with an ACTIONABLE error.
  3. Locate the converged plan (accepting a bare filename OR a full path -- the
     `missing_converged_plan`-on-a-full-path footgun) but always hash the
     canonical `converged-plan.yaml` the gate re-validates against.
  4. Seal the iteration outcome MECHANICALLY (closing_state + panel derived from
     the sealed reviews) -- no manual EDIT_ME step.
  5. Mint the marker via the existing `mint_design_approval` primitive and
     re-validate it against the live seal.

What it does NOT do (load-bearing trust boundary, consult Q6 constraint): it
NEVER authors or synthesizes the converged plan. The converged plan must already
exist as an explicit, host-authored artifact; this flow only validates + mints.
The trust model (>=2 non-claude families, hash match, scope confinement,
fail-closed) is unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from consensus_mcp._delivery_readiness import (
    SEALED_CLOSING_STATES,
    compute_artifact_hash,
)
from consensus_mcp._design_approval import (
    _count_non_claude_reviewers,
    _marker_path,
    _MIN_NON_CLAUDE_REVIEWERS,
    _review_family,
    _revalidate_seal,
    mint_design_approval,
)
from consensus_mcp._dispatch_base import _resolve_repo_root
from consensus_mcp._session_state import write_session_marker

# The canonical sealed state a converged Workflow-A consult closes on. Must be in
# SEALED_CLOSING_STATES (resolve_consensus_ref refuses anything else).
_DEFAULT_CLOSING_STATE = "quorum_close_passed"
_PLAN_FILENAME = "converged-plan.yaml"


def _err(error_type: str, message: str) -> dict:
    return {"ok": False, "error_type": error_type, "error": message}


def _resolve_repo(repo_root: str | os.PathLike | None) -> Path:
    """Single repo-root resolver shared by CLI + MCP (Finding #7). An explicit
    repo_root is used verbatim; otherwise the strict env-first resolver runs."""
    if repo_root:
        return Path(repo_root).resolve()
    return _resolve_repo_root()


def _resolve_iter_dir(iteration: str, repo_root: Path) -> Path:
    """Accept an absolute path, a bare iteration NAME (-> consensus-state/active/
    <name>), or a repo-relative path."""
    p = Path(iteration)
    if p.is_absolute():
        return p.resolve()
    active = repo_root / "consensus-state" / "active" / iteration
    if active.is_dir():
        return active.resolve()
    return (repo_root / iteration).resolve()


def _sealed_reviewer_families(iter_dir: Path) -> list[str]:
    """Non-claude families from sealed reviews, matching the same `<fam>-review
    [-N].yaml` forms `_count_non_claude_reviewers` does (round-keyed names too)."""
    fams: set[str] = set()
    for art in iter_dir.glob("*-review*.yaml"):
        fam = _review_family(art.name)
        if fam and "claude" not in fam:
            fams.add(fam)
    return sorted(fams)


def _ensure_sealed_outcome(iter_dir: Path, closing_state: str) -> None:
    """Write iteration-outcome.yaml with a SEALED closing_state + a panel derived
    from the sealed reviews, UNLESS a sealed outcome already exists (then respect
    it). Mechanical metadata only -- never the converged plan's content."""
    op = iter_dir / "iteration-outcome.yaml"
    if op.exists():
        try:
            existing = yaml.safe_load(op.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
        if existing.get("closing_state") in SEALED_CLOSING_STATES:
            return
    panel = ["claude"] + _sealed_reviewer_families(iter_dir)
    outcome = {
        "iteration_id": iter_dir.name,
        "closing_state": closing_state,
        "workflow": "propose-converge",
        "panel": panel,
        "converged_plan": (
            f"consensus-state/active/{iter_dir.name}/{_PLAN_FILENAME}"
        ),
        "sealed_by": "consensus-mcp-approve (composed approve flow)",
    }
    op.write_text(yaml.safe_dump(outcome, sort_keys=False), encoding="utf-8")


def approve_consult(
    iteration: str,
    scope_glob: str,
    converged_plan: str = _PLAN_FILENAME,
    repo_root: str | os.PathLike | None = None,
    closing_state: str = _DEFAULT_CLOSING_STATE,
) -> dict:
    """Validate + seal + mint a design-approval marker for a converged consult.

    Returns {'ok': True, ...} on success or {'ok': False, 'error_type', 'error'}
    with an actionable message. Does NOT author the converged plan."""
    try:
        rr = _resolve_repo(repo_root)
    except Exception as exc:  # RepoRootResolutionError etc.
        return _err("repo_root_unresolved", str(exc))

    iter_dir = _resolve_iter_dir(iteration, rr)
    if not iter_dir.is_dir():
        return _err(
            "missing_iteration",
            f"iteration directory not found: {iteration!r} (looked under "
            f"{rr / 'consensus-state' / 'active'} and as a repo-relative path).",
        )

    n = _count_non_claude_reviewers(iter_dir)
    if n < _MIN_NON_CLAUDE_REVIEWERS:
        return _err(
            "insufficient_reviewers",
            f"only {n} non-claude review(s) sealed in '{iter_dir.name}'; need "
            f">={_MIN_NON_CLAUDE_REVIEWERS} distinct families (no single-family "
            f"self-approval). Dispatch more reviewers, then re-run approve.",
        )

    # Footgun fix: --converged-plan may be a bare filename OR a full path, but the
    # gate ALWAYS re-validates against the canonical converged-plan.yaml in the
    # iteration dir, so that is what we hash. A differently-named file is rejected
    # with a clear message rather than minting a marker that verify will reject.
    given = Path(converged_plan)
    if given.name != _PLAN_FILENAME:
        return _err(
            "non_canonical_converged_plan",
            f"the gate re-validates against '{_PLAN_FILENAME}', but "
            f"--converged-plan named '{given.name}'. Rename/copy the converged "
            f"plan to '{_PLAN_FILENAME}' in the iteration dir.",
        )
    plan = iter_dir / _PLAN_FILENAME
    if not plan.exists():
        return _err(
            "missing_converged_plan",
            f"'{_PLAN_FILENAME}' not found in {iter_dir}. The converged plan must "
            f"be authored before approval -- this flow validates + mints but does "
            f"NOT author the plan.",
        )

    _ensure_sealed_outcome(iter_dir, closing_state)

    sha = compute_artifact_hash(plan)
    try:
        marker = mint_design_approval(
            rr,
            design_consensus_ref=iter_dir.name,
            scope_glob=scope_glob,
            converged_plan_sha256=sha,
        )
    except ValueError as exc:  # overbroad / empty scope_glob
        return _err("invalid_scope", str(exc))

    ok, reason = _revalidate_seal(marker, rr)
    if not ok:
        return _err("revalidation_failed", reason)

    # P0.1 (consult-verified #1 blocker): minting the design-approved marker is
    # NOT enough - the PreToolUse gate enforces only when the SESSION marker is
    # present (session_active -> gate_should_enforce). Without it the gate stays
    # dormant and edits are silently allowed AFTER approval, so the design marker
    # never actually scopes anything. Arm the gate here (mirrors
    # _seal_iteration.py, which writes the session marker on its mint path).
    try:
        write_session_marker(
            rr,
            iteration_id=iter_dir.name,
            scope_glob=scope_glob,
            activated_by="consensus-mcp-approve",
            activation_source="console_script",
        )
    except Exception as exc:  # marker minted but gate not armed - surface it
        return _err(
            "gate_arm_failed",
            f"design-approved marker minted but failed to ARM the gate "
            f"(session marker write failed): {exc}. The approval will not "
            f"enforce scope until the session marker is written.",
        )

    return {
        "ok": True,
        "iteration": iter_dir.name,
        "non_claude_reviewers": n,
        "converged_plan_sha256": sha,
        "scope_glob": scope_glob,
        "marker_path": str(_marker_path(rr)),
        "revalidated": reason,
        "gate_armed": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="consensus-mcp-approve",
        description=(
            "Validate a converged consult and mint the .consensus/design-approved "
            "marker (one composed step: precondition check + mechanical seal + "
            "mint + re-validate). Does NOT author the converged plan."
        ),
    )
    parser.add_argument("--iteration", required=True,
                        help="iteration name (-> consensus-state/active/<name>) or path")
    parser.add_argument("--scope-glob", required=True,
                        help="files the approval authorizes edits to (e.g. 'src/**')")
    parser.add_argument("--converged-plan", default=_PLAN_FILENAME,
                        help="converged plan file (bare name or path; canonical name required)")
    parser.add_argument("--repo-root", default=None,
                        help="repo root override (default: CONSENSUS_MCP_REPO_ROOT / cwd / in-tree)")
    parser.add_argument("--closing-state", default=_DEFAULT_CLOSING_STATE)
    args = parser.parse_args(argv)

    result = approve_consult(
        iteration=args.iteration,
        scope_glob=args.scope_glob,
        converged_plan=args.converged_plan,
        repo_root=args.repo_root,
        closing_state=args.closing_state,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
