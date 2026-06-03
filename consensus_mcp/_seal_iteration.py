"""consensus-mcp-seal-iteration - Path A iteration close-and-seal CLI.

Converged consult iteration-debrief-2026-05-26 (5-AI open-contest, deep
tier, unanimous P1): a SINGLE consolidated console script with four
subcommands. THIN 1:1 wrapper around existing library functions - NO
new logic, NO new trust-root semantics.

Subcommands:
  prepare    Canonicalize per-family review YAML names + scaffold
             iteration-outcome.yaml from a template.
  lint       Parse every YAML in the iteration directory; refuse on
             parse error with file:line pointers. Catches embedded
             unquoted ':' (Section 3.7 friction) BEFORE the verifier.
  mint       Compute canonical converged-plan hash via the package's
             own `compute_artifact_hash` (NOT `hashlib.sha256` on raw
             bytes - Section 3.8 friction) and write the
             `.consensus/design-approved` marker via the package's own
             `mint_design_approval`. Refuses overbroad scope_glob.
  verify     Call `verify_design_approval` on a path; returns the
             Result.ok + reason verbatim. Useful as a smoke-check
             after mint or as a CI gate.

USAGE
-----
  consensus-mcp-seal-iteration prepare --iteration-dir <dir>
  consensus-mcp-seal-iteration lint    --iteration-dir <dir>
  consensus-mcp-seal-iteration mint    --iteration-dir <dir> \\
                                       --closing-state quorum_close_passed \\
                                       --scope-glob 'docs/consensus/**'
  consensus-mcp-seal-iteration verify  --target-path <path>

The mint command is the load-bearing one. The others are diagnostic /
ergonomics conveniences. ALL trust-root invariants (sealed
closing_state, >=2 distinct non-claude reviewers, canonical hash
match, scope confinement) are enforced by the underlying library
functions, NOT by this CLI.

Per converged-plan D5: this CLI MUST NOT create reviewer artifacts,
iteration-outcome.yaml content other than a skeleton, or the
design-approved marker without explicit operator input. Stub creation
is non-authoritative.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from consensus_mcp._design_approval import (
    mint_design_approval,
    verify_design_approval,
    _is_overbroad_scope,
    MARKER_RELPATH,
)
from consensus_mcp._delivery_readiness import (
    compute_artifact_hash,
    SEALED_CLOSING_STATES,
)
from consensus_mcp._dispatch_base import _resolve_repo_root
from consensus_mcp._session_state import (
    write_session_marker,
    clear_session_marker,
    read_session_marker,
)


# Default scope_glob when the operator passes --writing-plans-followup
# (the brainstorming -> writing-plans pipeline produces files under
# docs/consensus/plans/, so the spec-file glob is too narrow - debrief
# Section 3.9 friction). Default chosen by the converged-plan
# D1+G_default.
_DEFAULT_WRITING_PLANS_SCOPE_GLOB = "docs/consensus/**"


# ----- prepare -----

_FAMILY_SUFFIX_RE = re.compile(r"^(?P<family>[a-zA-Z0-9_-]+?)-review-(?P<rest>.+)\.yaml$")


def _prepare(iter_dir: Path) -> dict:
    """Canonicalize per-family `<family>-review-<pass>.yaml` files into
    bare `<family>-review.yaml` so `_count_non_claude_reviewers` counts
    them (Section 3.6 friction).

    Returns a dict summarising what was copied; does NOT modify
    iteration-outcome.yaml unless one is missing entirely (in which
    case writes a minimal skeleton naming a TODO closing_state - the
    operator must still edit + supply a real closing_state before
    mint).

    Returns {'ok': True, 'copied': [...], 'skeleton_written': bool,
    'detail': '...'}.
    """
    copied: list[str] = []
    skipped: list[str] = []
    for f in sorted(iter_dir.glob("*-review-*.yaml")):
        m = _FAMILY_SUFFIX_RE.match(f.name)
        if not m:
            continue
        family = m.group("family")
        canonical = iter_dir / f"{family}-review.yaml"
        if canonical.exists():
            # Don't overwrite - operator may have intentionally
            # produced a per-pass file alongside a canonical one.
            skipped.append(f"{f.name} -> {canonical.name} (canonical exists; not overwriting)")
            continue
        shutil.copy2(f, canonical)
        copied.append(f"{f.name} -> {canonical.name}")

    # Scaffold iteration-outcome.yaml only if absent (NON-authoritative
    # closing_state placeholder; the operator must edit before mint).
    outcome = iter_dir / "iteration-outcome.yaml"
    skeleton_written = False
    if not outcome.exists():
        iter_id = iter_dir.name
        outcome.write_text(
            "# Skeleton authored by consensus-mcp-seal-iteration prepare.\n"
            "# EDIT before `mint`: closing_state MUST be in\n"
            f"# {sorted(SEALED_CLOSING_STATES)} for the marker to verify.\n"
            f"iteration_id: {iter_id}\n"
            f"closed_at_utc: 'EDIT_ME'\n"
            f"closing_state: 'EDIT_ME_TO_A_SEALED_STATE'\n"
            f"workflow: 'EDIT_ME'\n"
            f"goal: 'EDIT_ME'\n",
            encoding="utf-8",
        )
        skeleton_written = True

    return {
        "ok": True,
        "copied": copied,
        "skipped": skipped,
        "skeleton_written": skeleton_written,
        "iteration_outcome_path": str(outcome.relative_to(_resolve_repo_root())),
    }


# ----- lint -----

def _lint(iter_dir: Path) -> dict:
    """Parse every *.yaml in iter_dir; report parse failures with
    file:line pointers. Catches embedded ':' in unquoted scalars
    BEFORE the marker mint step (Section 3.7 friction).

    Returns {'ok': True/False, 'errors': [{file, line, col, msg}],
    'parsed': N}.
    """
    errors: list[dict] = []
    parsed = 0
    for f in sorted(iter_dir.glob("*.yaml")):
        try:
            yaml.safe_load(f.read_text(encoding="utf-8"))
            parsed += 1
        except yaml.YAMLError as exc:
            line = col = None
            mark = getattr(exc, "problem_mark", None)
            if mark is not None:
                line = mark.line + 1  # YAML marks are 0-indexed
                col = mark.column + 1
            errors.append({
                "file": str(f.relative_to(_resolve_repo_root())),
                "line": line,
                "col": col,
                "msg": str(exc).split("\n")[0],
            })
    return {"ok": not errors, "errors": errors, "parsed": parsed}


# ----- mint -----

def _mint(
    iter_dir: Path,
    closing_state: str,
    scope_glob: str,
    converged_plan_filename: str = "converged-plan.yaml",
    repo_root_id: str | None = None,
) -> dict:
    """Compute canonical hash of converged-plan.yaml + write the
    design-approved marker via mint_design_approval. THIN wrapper.

    Pre-flight: lint pass (refuses on YAML parse error). Refuses on
    overbroad scope_glob. The mint_design_approval call itself
    enforces trust-root invariants (sealed iteration, >=2 non-claude,
    matching hash).
    """
    # Pre-flight YAML lint (G4 acceptance gate).
    lint_result = _lint(iter_dir)
    if not lint_result["ok"]:
        return {
            "ok": False,
            "error_type": "lint_failed",
            "error": "YAML parse errors in iteration_dir; fix before mint.",
            "lint_errors": lint_result["errors"],
        }

    # Refuse overbroad scope (mint_design_approval would too, but earlier
    # rejection gives a cleaner error).
    if _is_overbroad_scope(scope_glob):
        return {
            "ok": False,
            "error_type": "overbroad_scope",
            "error": (
                f"scope_glob {scope_glob!r} is overbroad - name the files the "
                f"converged plan covers (e.g. 'docs/consensus/**' or "
                f"'consensus_mcp/_x.py')."
            ),
        }

    # Refuse closing_state not in the sealed set (the marker would fail
    # verify anyway; reject early with a clearer message).
    outcome = iter_dir / "iteration-outcome.yaml"
    if not outcome.exists():
        return {
            "ok": False,
            "error_type": "missing_iteration_outcome",
            "error": (
                f"{outcome.name} is required before mint. Run "
                f"`consensus-mcp-seal-iteration prepare --iteration-dir "
                f"{iter_dir.name}` first, then edit the closing_state."
            ),
        }
    try:
        outcome_data = yaml.safe_load(outcome.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return {
            "ok": False,
            "error_type": "iteration_outcome_unparseable",
            "error": f"{outcome}: {exc}",
        }
    declared_state = outcome_data.get("closing_state")
    if not isinstance(declared_state, str) or declared_state not in SEALED_CLOSING_STATES:
        return {
            "ok": False,
            "error_type": "closing_state_not_sealed",
            "error": (
                f"iteration-outcome.yaml declares closing_state="
                f"{declared_state!r}; must be one of "
                f"{sorted(SEALED_CLOSING_STATES)}. The marker re-validates "
                f"against this - a mismatched value fails fail-closed."
            ),
        }
    # closing_state passed in via --closing-state must match what's already
    # written in iteration-outcome.yaml (the file is authoritative).
    if closing_state != declared_state:
        return {
            "ok": False,
            "error_type": "closing_state_mismatch",
            "error": (
                f"--closing-state={closing_state!r} but iteration-outcome.yaml "
                f"declares closing_state={declared_state!r}. The outcome file "
                f"is authoritative - either pass --closing-state={declared_state} "
                f"or edit the outcome file."
            ),
        }

    # Compute canonical hash + mint the marker.
    plan = iter_dir / converged_plan_filename
    if not plan.exists():
        return {
            "ok": False,
            "error_type": "missing_converged_plan",
            "error": f"{plan.name} not found in {iter_dir}.",
        }
    try:
        sha = compute_artifact_hash(plan)
    except Exception as exc:
        return {
            "ok": False,
            "error_type": "hash_failed",
            "error": f"compute_artifact_hash({plan}) raised {type(exc).__name__}: {exc}",
        }

    repo_root = _resolve_repo_root()
    try:
        marker = mint_design_approval(
            repo_root,
            design_consensus_ref=iter_dir.name,
            scope_glob=scope_glob,
            converged_plan_sha256=sha,
            repo_root_id=repo_root_id,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "error_type": "mint_refused",
            "error": str(exc),
        }

    # v1.32.1 (consult iteration-v133-gate-scope-shift-2026-05-26):
    # mint ALSO writes the session-active marker (D3 - primary
    # activation trigger). The gate's session_active() probe then
    # treats this iteration as the active scope.
    try:
        session_path = write_session_marker(
            repo_root,
            iteration_id=iter_dir.name,
            scope_glob=scope_glob,
            activated_by=f"consensus-mcp-seal-iteration-mint",
            activation_source="console_script",
        )
        session_path_rel = str(session_path.relative_to(repo_root))
    except Exception as exc:
        # codex-rev-003 / kimi-rev-001: arming the session marker AFTER minting the
        # design-approved marker leaves a HALF-STATE on failure - a stale
        # design-approved trust pointer with no live session. Returning ok=True
        # with only a warning (the prior behavior) papered over an inconsistent
        # repo and diverged from `_approve_consult`, which makes approval
        # all-or-nothing. Match it: ROLL BACK the just-minted design-approved
        # marker and fail loudly so the caller re-runs cleanly.
        rollback_note = "design-approved marker rolled back"
        try:
            marker_file = repo_root / MARKER_RELPATH
            if marker_file.exists():
                marker_file.unlink()
        except OSError as rb_exc:
            rollback_note = (
                f"FAILED to roll back design-approved marker ({rb_exc}); remove "
                f"{repo_root / MARKER_RELPATH} manually before re-running mint"
            )
        return {
            "ok": False,
            "error_type": "gate_arm_failed",
            "error": (
                f"design-approved minted but arming the session-active marker "
                f"failed: {type(exc).__name__}: {exc}. {rollback_note}. No partial "
                f"approval remains; re-run mint once the cause is fixed."
            ),
        }

    return {
        "ok": True,
        "marker_path": str((repo_root / MARKER_RELPATH).relative_to(repo_root)),
        "design_consensus_ref": iter_dir.name,
        "scope_glob": scope_glob,
        "converged_plan_sha256": sha,
        "marker": marker,
        "session_marker_path": session_path_rel,
    }


# ----- close (v1.32.1) -----

def _git_modified_relpaths(repo_root: Path) -> set[str] | None:
    """Repo-relative POSIX paths git reports as CHANGED (modified, added, or
    untracked) under `repo_root`, or None when git is unavailable / this is not a
    git work tree (so the caller can fall back to a conservative all-in-scope
    check). Used to scope the close-time delivery-token check to the actual change
    delta (grok-rev-001)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None  # not a git repo / git error -> conservative fallback
    changed: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        # Porcelain v1: 'XY <path>' or 'XY <old> -> <new>' for renames.
        path = line[3:] if len(line) > 3 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.add(path.strip().strip('"').replace("\\", "/"))
    return changed


def _close(
    iter_dir: Path,
    abandon: bool = False,
) -> dict:
    """v1.32.1 deactivation. Removes the session-active marker (and
    optionally the design-approved marker) so the gate returns to
    DORMANT for unrelated work.

    Default mode: VERIFY delivery tokens exist for every file
    matching the active scope_glob; refuse on any missing. On
    success, remove both `.consensus/session-active` AND
    `.consensus/design-approved`.

    --abandon mode: skip the delivery-token check; remove both
    markers unconditionally. For recovery from crashed/abandoned
    consults. Adds zero attack surface - the trust-root model
    doesn't depend on these markers' presence.
    """
    repo_root = _resolve_repo_root()
    design_marker = repo_root / MARKER_RELPATH
    # gemini-rev-001/002: session marker is cleared via clear_session_marker();
    # no local binding needed here.

    if abandon:
        cleared_session = clear_session_marker(repo_root)
        cleared_design = design_marker.exists()
        if cleared_design:
            try:
                design_marker.unlink()
            except OSError as exc:
                return {
                    "ok": False,
                    "error_type": "unlink_failed",
                    "error": f"failed to unlink {design_marker}: {exc}",
                }
        return {
            "ok": True,
            "mode": "abandon",
            "session_marker_cleared": cleared_session,
            "design_marker_cleared": cleared_design,
            "note": "abandoned iteration; trust-root invariants unaffected.",
        }

    # Standard close path: require delivery tokens for every
    # in-scope file. Without a design-approved marker we can't read
    # the scope_glob; refuse cleanly.
    if not design_marker.exists():
        return {
            "ok": False,
            "error_type": "no_design_marker",
            "error": (
                f"no {MARKER_RELPATH} present - nothing to close. Use "
                f"`--abandon` to force-clear any session marker without "
                f"the delivery-token check."
            ),
        }
    try:
        marker_data = yaml.safe_load(design_marker.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return {
            "ok": False,
            "error_type": "design_marker_unparseable",
            "error": str(exc),
        }
    scope_glob = marker_data.get("scope_glob")
    if not isinstance(scope_glob, str) or not scope_glob.strip():
        return {
            "ok": False,
            "error_type": "design_marker_missing_scope",
            "error": f"{design_marker.name} has no scope_glob",
        }

    # Enumerate in-scope files via fnmatch against the working
    # tree. We don't traverse out-of-repo; fnmatch is path-aware.
    import fnmatch
    in_scope: list[Path] = []
    for p in repo_root.rglob("*"):
        if not p.is_file():
            continue
        # Skip governance dirs (.consensus/, consensus-state/) so we
        # don't count session markers + iteration artifacts.
        try:
            rel = p.relative_to(repo_root)
        except ValueError:
            continue
        rel_str = str(rel).replace("\\", "/")
        if rel_str.startswith(".consensus/") or rel_str.startswith("consensus-state/"):
            continue
        if rel_str.startswith(".delivery-readiness/"):
            continue
        if fnmatch.fnmatch(rel_str, scope_glob):
            in_scope.append(p)

    # grok-rev-001: a delivery token proves a CHANGED file was vetted. Requiring
    # one for EVERY file matching scope_glob made a broad scope (e.g.
    # 'consensus_mcp/**') impossible to close - hundreds of UNCHANGED files would
    # each demand a token, forcing --abandon. The token check belongs on the
    # MODIFIED DELTA, not the whole scope. Restrict in_scope to files git reports
    # as changed (modified/added/untracked) when git is available; fall back to the
    # conservative all-in-scope set when it is not (no weaker than before).
    modified = _git_modified_relpaths(repo_root)
    if modified is not None:
        in_scope = [
            p for p in in_scope
            if str(p.relative_to(repo_root)).replace("\\", "/") in modified
        ]

    # Verify delivery tokens exist for each (the delivery-readiness
    # module hashes the artifact path to compute the token filename).
    from consensus_mcp._delivery_readiness import _token_path
    missing: list[str] = []
    for f in in_scope:
        token = _token_path(f, repo_root)
        if not token.exists():
            missing.append(str(f.relative_to(repo_root)))

    if missing:
        return {
            "ok": False,
            "error_type": "missing_delivery_tokens",
            "error": (
                f"{len(missing)} CHANGED in-scope file(s) lack delivery tokens; "
                f"mint one per file with `consensus-mcp-deliver --file <path> "
                f"--design-consensus-ref <iter> --vetted-by <fam1>,<fam2>` before "
                f"close, OR use `--abandon` to disarm without the token check "
                f"(e.g. a review-only consult that landed no edits - the seal + "
                f"audit trail are unaffected; markers are session caches)."
            ),
            "missing": missing[:20],  # cap output
            "missing_count": len(missing),
        }

    # All clear - remove both markers.
    cleared_session = clear_session_marker(repo_root)
    try:
        design_marker.unlink()
        cleared_design = True
    except OSError as exc:
        return {
            "ok": False,
            "error_type": "unlink_failed",
            "error": f"failed to unlink {design_marker}: {exc}",
        }

    return {
        "ok": True,
        "mode": "delivered",
        "session_marker_cleared": cleared_session,
        "design_marker_cleared": cleared_design,
        "in_scope_files_verified": len(in_scope),
        "scope_glob": scope_glob,
    }


# ----- verify -----

def _verify(target_path: Path) -> dict:
    """Call verify_design_approval against `target_path`. Returns
    {'ok': bool, 'reason': str}.
    """
    repo_root = _resolve_repo_root()
    res = verify_design_approval(target_path, repo_root=repo_root)
    return {"ok": res.ok, "reason": res.reason}


# ----- main / argparse -----

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus-mcp-seal-iteration",
        description=(
            "Path A iteration close-and-seal helper. THIN 1:1 wrapper around "
            "the package's library functions - no new trust-root semantics."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser(
        "prepare",
        help="Canonicalize per-family review YAML names + scaffold iteration-outcome.yaml.",
    )
    pp.add_argument("--iteration-dir", required=True)

    pl = sub.add_parser(
        "lint",
        help="Parse every YAML in the iteration dir; report file:line on parse error.",
    )
    pl.add_argument("--iteration-dir", required=True)

    pm = sub.add_parser(
        "mint",
        help="Compute canonical hash + write the design-approved marker.",
    )
    pm.add_argument("--iteration-dir", required=True)
    pm.add_argument(
        "--closing-state", required=True,
        choices=sorted(SEALED_CLOSING_STATES),
        help="Must match the closing_state declared in iteration-outcome.yaml.",
    )
    pm.add_argument(
        "--scope-glob", default=None,
        help=(
            "fnmatch glob for files the sealed plan covers. When --writing-"
            "plans-followup is set, defaults to 'docs/consensus/**'. "
            "Otherwise required."
        ),
    )
    pm.add_argument(
        "--writing-plans-followup", action="store_true",
        help=(
            "Use the brainstorming -> writing-plans default scope_glob "
            "'docs/consensus/**'. Eliminates Section 3.9 friction."
        ),
    )
    pm.add_argument("--converged-plan", default="converged-plan.yaml")
    pm.add_argument("--repo-root-id", default=None)

    pc = sub.add_parser(
        "close",
        help=(
            "v1.32.1 deactivation. Verify delivery tokens for every "
            "in-scope file then remove the session-active + design-"
            "approved markers, returning the gate to dormant."
        ),
    )
    pc.add_argument("--iteration-dir", required=True)
    pc.add_argument(
        "--abandon", action="store_true",
        help=(
            "Skip the delivery-token check; force-clear both markers. "
            "For recovery from abandoned/crashed iterations. Trust-root "
            "invariants are unaffected (markers are session caches, not "
            "trust artifacts)."
        ),
    )

    pv = sub.add_parser(
        "verify",
        help="Run verify_design_approval against a path.",
    )
    pv.add_argument("--target-path", required=True)

    ns = p.parse_args(argv)

    try:
        repo_root = _resolve_repo_root()
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}))
        return 4

    if ns.cmd in ("prepare", "lint", "mint", "close"):
        iter_dir_raw = Path(ns.iteration_dir)
        iter_dir = iter_dir_raw if iter_dir_raw.is_absolute() else (repo_root / iter_dir_raw)
        iter_dir = iter_dir.resolve()
        if not iter_dir.exists() or not iter_dir.is_dir():
            print(json.dumps({
                "ok": False,
                "error_type": "iteration_dir_missing",
                "error": f"iteration-dir {iter_dir} does not exist or is not a directory.",
            }))
            return 1

    if ns.cmd == "prepare":
        result = _prepare(iter_dir)
        print(json.dumps(result, indent=2))
        return 0

    if ns.cmd == "lint":
        result = _lint(iter_dir)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 2

    if ns.cmd == "mint":
        scope_glob = ns.scope_glob
        if scope_glob is None:
            if ns.writing_plans_followup:
                scope_glob = _DEFAULT_WRITING_PLANS_SCOPE_GLOB
            else:
                print(json.dumps({
                    "ok": False,
                    "error_type": "missing_scope_glob",
                    "error": (
                        "--scope-glob is required (or pass --writing-plans-followup "
                        f"to use the default {_DEFAULT_WRITING_PLANS_SCOPE_GLOB!r})."
                    ),
                }))
                return 1
        result = _mint(
            iter_dir, ns.closing_state, scope_glob,
            converged_plan_filename=ns.converged_plan,
            repo_root_id=ns.repo_root_id,
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2

    if ns.cmd == "close":
        result = _close(iter_dir, abandon=ns.abandon)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2

    if ns.cmd == "verify":
        target_raw = Path(ns.target_path)
        target = target_raw if target_raw.is_absolute() else (repo_root / target_raw)
        result = _verify(target.resolve())
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 2

    print(json.dumps({"ok": False, "error_type": "unknown_subcommand", "error": ns.cmd}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
