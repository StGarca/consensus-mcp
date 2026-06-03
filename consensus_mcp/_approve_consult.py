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
import fnmatch

from consensus_mcp._dispatch_base import (
    _normalize_for_compare,
    _resolve_repo_root,
    validate_explicit_repo_root,
)
from consensus_mcp._session_state import write_session_marker

# The canonical sealed state a converged Workflow-A consult closes on. Must be in
# SEALED_CLOSING_STATES (resolve_consensus_ref refuses anything else).
_DEFAULT_CLOSING_STATE = "quorum_close_passed"
_PLAN_FILENAME = "converged-plan.yaml"
_GOAL_PACKET_FILENAME = "goal_packet.yaml"


def _err(error_type: str, message: str) -> dict:
    return {"ok": False, "error_type": error_type, "error": message}


def _resolve_repo(repo_root: str | os.PathLike | None) -> Path:
    """Single repo-root resolver shared by CLI + MCP (Finding #7). An explicit
    repo_root is VALIDATED against the same marker contract auto-discovery uses
    (codex finding: never accept an explicit --repo-root verbatim); otherwise the
    strict env-first resolver runs."""
    if repo_root:
        return validate_explicit_repo_root(repo_root)
    return _resolve_repo_root()


def _glob_segments(glob: str) -> list[str]:
    """Split a forward-slash glob into path segments, dropping empty parts so a
    leading/trailing '/' does not introduce phantom segments. '**' and '*' remain
    their own segments; literal segments may carry intra-segment metachars."""
    return [seg for seg in str(glob).strip().split("/") if seg != ""]


def _glob_subset(narrow: list[str], broad: list[str]) -> bool:
    """True iff EVERY path matched by glob `narrow` is also matched by `broad`
    (segment-wise language containment). Wildcards: '*' matches exactly one
    segment (any chars, no '/'); '**' matches zero or more segments.

    This is the scope-confinement primitive: an approval scope is acceptable only
    when it is a subset of the goal_packet's authorized files (narrowing is fine;
    expansion is a scope escalation). Sound (no false 'subset' verdicts) for the
    glob shapes consensus uses; conservative (returns False) when unsure."""
    if not narrow and not broad:
        return True
    if not broad:
        # broad exhausted but narrow can still produce >=1 segment -> not covered.
        return False
    if broad[0] == "**":
        rest = broad[1:]
        if not rest:
            # TRAILING '**' matches ONE OR MORE segments (paths strictly UNDER the
            # prefix), never zero - consistent with the gate's matcher, which does
            # NOT match the bare prefix 'src' against 'src/**'. So an exhausted
            # narrow ('src' vs 'src/**') is NOT a subset (kimi-rev-001); a narrow
            # that still has >=1 segment (incl. its own '**' tail) is.
            return len(narrow) >= 1
        # MIDDLE '**' may absorb ZERO segments (skip it) ...
        if _glob_subset(narrow, rest):
            return True
        # ... or one-or-more narrow segments.
        if narrow and _glob_subset(narrow[1:], broad):
            return True
        return False
    if not narrow:
        # broad still needs >=1 concrete segment but narrow produced none.
        return False
    n0, b0 = narrow[0], broad[0]
    if n0 == "**":
        # narrow '**' can emit MANY segments; a single non-'**' broad segment
        # cannot cover all of them -> not a subset.
        return False
    if b0 == "*":
        # '*' covers any single segment (narrow's n0 is one segment here).
        return _glob_subset(narrow[1:], broad[1:])
    if n0 == "*":
        # narrow '*' emits an arbitrary segment; a literal broad segment can't
        # cover all of them.
        return False
    # Both literal segments (each may carry fnmatch metachars, e.g. '*.py').
    if n0 == b0 or fnmatch.fnmatchcase(n0, b0):
        return _glob_subset(narrow[1:], broad[1:])
    return False


def _scope_within_allowed(scope_glob: str, allowed_files: list) -> bool:
    """True iff `scope_glob` is a subset of AT LEAST ONE allowed_files pattern."""
    scope_segs = _glob_segments(scope_glob)
    for allowed in allowed_files:
        if not isinstance(allowed, str) or not allowed.strip():
            continue
        if _glob_subset(scope_segs, _glob_segments(allowed)):
            return True
    return False


def _fnmatch_patterns_overlap(a: str, b: str) -> bool:
    """True iff the two fnmatch globs `a` and `b` share at least one common string.

    This is the EXACT intersection test under the gate's own matcher: the PreToolUse
    gate enforces scope via `fnmatch.fnmatch(path, scope_glob)`, where `*` (and `**`,
    which fnmatch treats identically) matches ANY characters INCLUDING `/`. So the
    correct overlap test is character-level wildcard intersection - NOT a
    segment-based glob algebra (the prior _glob_subset / literal-prefix attempts
    diverged from fnmatch and produced both false vetoes (codex-rev-002) AND, worse,
    MISSED partial intersections where neither pattern is a subset of the other
    (codex-rev-001, a security under-veto)). Validated exhaustively against fnmatch
    ground truth: zero false vetoes and zero missed overlaps.

    `*` matches any run of chars (incl '/'); `?` matches one char; everything else
    is literal. Memoized DP over the two strings."""
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def go(i: int, j: int) -> bool:
        ci = a[i] if i < len(a) else None
        cj = b[j] if j < len(b) else None
        if ci is None and cj is None:
            return True
        if ci == "*":
            return go(i + 1, j) or (cj is not None and go(i, j + 1))
        if cj == "*":
            return go(i, j + 1) or (ci is not None and go(i + 1, j))
        if ci is not None and cj is not None and (ci == "?" or cj == "?" or ci == cj):
            return go(i + 1, j + 1)
        return False

    result = go(0, 0)
    go.cache_clear()
    return result


def _forbidden_vetoes(scope_glob: str, forbidden_entry: str) -> bool:
    """True iff `scope_glob` overlaps the `forbidden_entry` and must be rejected.

    Uses the gate-consistent fnmatch intersection (`_fnmatch_patterns_overlap`). A
    forbidden DIRECTORY entry ('consensus-state/', or any wildcard-free path)
    denotes its whole SUBTREE, so it is expanded to both the bare path and
    '<entry>/*' (fnmatch '*' spans '/' -> the entire subtree). Veto if the scope
    overlaps any form. Security-critical direction is 'never miss an overlap', which
    the exact fnmatch test guarantees."""
    base = forbidden_entry.rstrip("/")
    forms = [forbidden_entry] if "*" in forbidden_entry else [base]
    if forbidden_entry.endswith("/") or "*" not in forbidden_entry:
        forms.append(base + "/*")  # the subtree (fnmatch '*' matches '/')
    return any(_fnmatch_patterns_overlap(scope_glob, form) for form in forms)


class _IterDirOutsideRepo(ValueError):
    """The requested iteration path resolves OUTSIDE the repo root (containment
    breach). Raised so approve_consult never reads a goal_packet / converged-plan
    from an attacker-influenced location outside the project (gemini-rev-001 path
    traversal; codex-rev-001; kimi-rev-003)."""


def _is_within(child: Path, parent: Path) -> bool:
    """True iff `child` is `parent` or a descendant, after resolution.

    Cross-platform (gemini-rev-001 blocking / grok-rev-003): a naive
    `Path.relative_to` is a CASE-SENSITIVE string compare and does not handle the
    Windows extended-length (long-path) prefix, so on Windows it false-rejects
    valid in-repo iterations (drive-letter or 8.3 case differences) and breaks
    `approve`.
    We reuse `_dispatch_base._normalize_for_compare` - the same normalization the
    dispatch containment guard uses - so the two paths agree on every platform.
    Symlink- and `..`-safe because both sides are fully resolved first."""
    c = _normalize_for_compare(child.resolve())
    p = _normalize_for_compare(parent.resolve())
    return c == p or c.startswith(p + os.sep)


def _resolve_iter_dir(iteration: str, repo_root: Path) -> Path:
    """Resolve an iteration to a directory CONFINED to repo_root.

    Accepts an absolute path, a bare iteration NAME (-> consensus-state/active/
    <name>), or a repo-relative path -- but the resolved result must live inside
    repo_root. An absolute path outside the repo, or a `..`-escaping relative
    path, raises `_IterDirOutsideRepo` (gemini-rev-001/codex-rev-001/kimi-rev-003:
    approve must never read a goal_packet/plan from outside the project tree)."""
    p = Path(iteration)
    if p.is_absolute():
        resolved = p.resolve()
    else:
        active = repo_root / "consensus-state" / "active" / iteration
        resolved = active.resolve() if active.is_dir() else (repo_root / iteration).resolve()
    if not _is_within(resolved, repo_root):
        raise _IterDirOutsideRepo(
            f"iteration {iteration!r} resolves to {resolved}, which is OUTSIDE the "
            f"repo root {repo_root.resolve()}. approve only operates on iterations "
            f"inside the project (no out-of-repo / '..'-escaping paths)."
        )
    return resolved


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

    try:
        iter_dir = _resolve_iter_dir(iteration, rr)
    except _IterDirOutsideRepo as exc:
        return _err("iteration_outside_repo", str(exc))
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

    # Scope-confinement gate (kimi finding): the approval scope_glob must be a
    # SUBSET of what the consult's goal_packet authorized. Narrowing is fine;
    # EXPANDING the scope at approval time (e.g. approving '**' for a consult
    # scoped to 'src/foo.py') is a privilege escalation - the gate would then
    # bless edits the panel never reviewed the scope of. mint_design_approval
    # only rejects the absolute-overbroad '*'/'**'; it cannot know the consult's
    # intended scope. We enforce it here, against the goal_packet of record.
    # Reject traversal in the scope itself (kimi-rev-005): a '..' segment in the
    # approved scope is meaningless for in-repo confinement and could let a glob
    # "escape" the project under a naive matcher. Forbid it outright.
    if ".." in _glob_segments(scope_glob):
        return _err(
            "invalid_scope",
            f"--scope-glob {scope_glob!r} contains a '..' segment; approval scopes "
            f"must be in-repo paths with no parent-directory traversal.",
        )

    gp_path = iter_dir / _GOAL_PACKET_FILENAME
    if not gp_path.is_file():
        return _err(
            "missing_goal_packet",
            f"'{_GOAL_PACKET_FILENAME}' not found in {iter_dir}; cannot validate "
            f"that --scope-glob is within the authorized scope. A consult started "
            f"via consensus-mcp-start-consult always writes one. Author it (with an "
            f"'allowed_files' list) before approving.",
        )
    try:
        gp = yaml.safe_load(gp_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # unreadable/non-YAML packet
        return _err(
            "missing_goal_packet",
            f"'{_GOAL_PACKET_FILENAME}' in {iter_dir} is unreadable/not valid YAML "
            f"({exc}); cannot validate scope confinement.",
        )
    allowed_files = [
        a for a in (gp.get("allowed_files") or [])
        if isinstance(a, str) and a.strip()
    ]
    if not allowed_files:
        return _err(
            "missing_goal_packet",
            f"the goal_packet in {iter_dir} declares no 'allowed_files'; there is "
            f"nothing for --scope-glob to be confined to. Add the authorized files "
            f"to the goal_packet before approving.",
        )
    if not _scope_within_allowed(scope_glob, allowed_files):
        return _err(
            "scope_escalation",
            f"--scope-glob {scope_glob!r} is NOT within the goal_packet's "
            f"allowed_files {allowed_files}. An approval may only NARROW the scope "
            f"the consult authorized, never expand it. Re-run with a scope_glob "
            f"that is a subset of the allowed_files, or start a new consult scoped "
            f"to the broader set.",
        )

    # forbidden_files veto (kimi-rev-001): allowed_files passing is necessary but
    # not sufficient - the goal_packet can ALSO declare forbidden_files the
    # approval must never cover (e.g. 'consensus-state/'). Reject if the scope
    # either falls entirely within a forbidden pattern OR swallows one (a broad
    # scope that engulfs a forbidden area). Conservative on the clear cases the
    # panel cited; the goal_packet's intent wins over a permissive allowed_files.
    forbidden_files = [
        f for f in (gp.get("forbidden_files") or [])
        if isinstance(f, str) and f.strip()
    ]
    for forb in forbidden_files:
        if _forbidden_vetoes(scope_glob, forb):
            return _err(
                "forbidden_scope",
                f"--scope-glob {scope_glob!r} overlaps the goal_packet's "
                f"forbidden_files entry {forb!r}; an approval may not authorize "
                f"edits to a path the consult explicitly forbade. Narrow the scope "
                f"to exclude {forb!r}.",
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
    except Exception as exc:  # marker minted but gate not armed - ROLL BACK
        # grok-rev-001 (blocking): minting the design-approved marker before arming
        # the session marker left a HALF-STATE on arm failure - a stale
        # design-approved trust pointer persisting with no live session, which a
        # later approve/session could trip over. Make approval ALL-OR-NOTHING: if
        # arming fails, remove the just-minted design-approved marker so the repo
        # returns to its pre-approve state. A failed approve leaves NO markers.
        rollback_note = "design-approved marker rolled back"
        try:
            marker_file = _marker_path(rr)
            if marker_file.exists():
                marker_file.unlink()
        except OSError as rb_exc:
            rollback_note = (
                f"FAILED to roll back design-approved marker ({rb_exc}); remove "
                f"{_marker_path(rr)} manually before re-running approve"
            )
        return _err(
            "gate_arm_failed",
            f"failed to ARM the gate (session marker write failed): {exc}. "
            f"{rollback_note}. No partial approval remains; re-run approve once "
            f"the cause is fixed.",
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
        # gemini finding: arming has a matching DISARM, and the lifecycle is only
        # complete when the gate returns to dormant. Surface the exact close
        # command so the gate is never left armed after the edits land. (mint a
        # delivery token per edited file first; then close.)
        "next_steps": {
            "1_edit_within_scope": (
                f"Edits to files matching {scope_glob!r} are now allowed; "
                f"out-of-scope edits stay blocked."),
            "2_deliver_each_edited_file": (
                "consensus-mcp-deliver --file <path> --design-consensus-ref "
                f"{iter_dir.name} --vetted-by <fam1>,<fam2>"),
            "3_disarm_when_done": (
                "consensus-mcp-seal-iteration close --iteration-dir "
                f"consensus-state/active/{iter_dir.name}   # clears the "
                "session-active + design-approved markers; gate back to dormant"),
        },
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
