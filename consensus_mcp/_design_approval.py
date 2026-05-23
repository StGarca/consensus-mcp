"""Design-approval marker mint/verify — the cross-family-sealed gate that the
PreToolUse hook validates before allowing implementation tool calls.

Parallel to `_delivery_readiness.py`: that module gates DELIVERY (a finished
artifact is consensus-vetted); this module gates IMPLEMENTATION (you may not
Edit/Write until a Workflow A consult has SEALED a converged plan covering the
scope you are about to touch).

Marker contract (fixed — `consensus_pretooluse_gate.py` codes against this):
  `.consensus/design-approved` is a YAML file with fields
  {iteration_id, scope_glob, converged_plan_sha256, sealed_at_utc,
   cross_family_sealed}.

A marker is VALID iff:
  1. the file parses (YAML mapping), AND
  2. `cross_family_sealed` is true, AND
  3. the edited repo-relative path matches `scope_glob` (fnmatch).

A marker with `cross_family_sealed=False` is ADVISORY ONLY = treated as NOT
approved. This preserves the cross-family closure invariant: a single-Claude
step can't self-approve its own implementation (no laundering).

Fail-closed: any error (missing/unparseable/wrong-type/path-resolution) yields
a NOT-approved Result with a reason — never raises out of `verify_*`.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import fnmatch
from pathlib import Path

import yaml

MARKER_RELPATH = Path(".consensus") / "design-approved"


@dataclasses.dataclass
class Result:
    """Outcome of a marker verification. `ok` is the deny/allow signal; `reason`
    is always a human-readable explanation (for the PreToolUse stderr block)."""

    ok: bool
    reason: str


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker_path(repo_root: Path) -> Path:
    return Path(repo_root) / MARKER_RELPATH


def mint_design_approval(
    repo_root: Path,
    iteration_id: str,
    scope_glob: str,
    converged_plan_sha256: str,
    cross_family_sealed: bool,
) -> dict:
    """Write `.consensus/design-approved` with the marker-contract fields.

    Called when a Workflow A consult seals a converged plan. `cross_family_sealed`
    must be True for the marker to actually authorize implementation; a False
    value is written but is advisory only (verify treats it as NOT approved).

    Returns the marker dict that was written.
    """
    repo_root = Path(repo_root)
    marker = {
        "iteration_id": iteration_id,
        "scope_glob": scope_glob,
        "converged_plan_sha256": converged_plan_sha256,
        "sealed_at_utc": _utcnow(),
        "cross_family_sealed": bool(cross_family_sealed),
    }
    path = _marker_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(marker, sort_keys=True), encoding="utf-8")
    return marker


def _rel_to_repo(target_path: Path, repo_root: Path) -> str:
    """Return the repo-relative POSIX path for `target_path`. Accepts absolute
    or relative targets; falls back to the raw string if it is not under the
    repo root (so an out-of-repo edit still gets a deterministic, non-matching
    string rather than an exception)."""
    repo_root = Path(repo_root).resolve()
    tp = Path(target_path)
    if not tp.is_absolute():
        tp = (repo_root / tp)
    tp = tp.resolve(strict=False)
    try:
        rel = tp.relative_to(repo_root)
    except ValueError:
        # Outside the repo; use the path as-given (POSIX) so scope_glob can
        # still (not) match deterministically.
        return Path(target_path).as_posix()
    return rel.as_posix()


def marker_is_sealed(repo_root: Path) -> Result:
    """Path-agnostic check: a `.consensus/design-approved` marker exists, parses,
    and is `cross_family_sealed=True`. Used by the PreToolUse gate for Bash
    file-modifying commands, whose target scope cannot be pinned to a single
    path — so we require only that a cross-family-sealed plan is in force. Edit
    tools instead use `verify_design_approval` for full per-path scope checks.
    Fail-closed."""
    try:
        path = _marker_path(repo_root)
        if not path.exists():
            return Result(False, f"no design-approval marker at {MARKER_RELPATH} "
                                 f"(scope not consensus-sealed — run a Workflow A consult)")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            return Result(False, f"design-approval marker unparseable (fail-closed): {exc}")
        if not isinstance(data, dict):
            return Result(False, "design-approval marker is not a YAML mapping (fail-closed)")
        if data.get("cross_family_sealed") is not True:
            return Result(False, "design-approval marker is ADVISORY ONLY "
                                 "(cross_family_sealed not true) — a single-Claude step "
                                 "cannot self-approve implementation")
        return Result(True, f"cross-family-sealed design marker in force "
                            f"(iteration {data.get('iteration_id')})")
    except Exception as exc:  # fail-closed
        return Result(False, f"design-approval verify error (fail-closed): {exc}")


def verify_design_approval(target_path: Path, repo_root: Path) -> Result:
    """Validate the design-approval marker against `target_path`. Fail-closed.

    NOT approved (ok=False) if any of: marker missing, unparseable, not a
    mapping, `cross_family_sealed` not true (advisory-only / single-Claude), or
    the repo-relative target does not fnmatch `scope_glob`. Any unexpected error
    is caught and returned as a fail-closed deny.
    """
    try:
        path = _marker_path(repo_root)
        if not path.exists():
            return Result(False, f"no design-approval marker at {MARKER_RELPATH} "
                                 f"(scope not consensus-sealed — run a Workflow A consult)")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            return Result(False, f"design-approval marker unparseable (fail-closed): {exc}")
        if not isinstance(data, dict):
            return Result(False, "design-approval marker is not a YAML mapping (fail-closed)")
        if data.get("cross_family_sealed") is not True:
            return Result(False, "design-approval marker is ADVISORY ONLY "
                                 "(cross_family_sealed not true) — a single-Claude step "
                                 "cannot self-approve implementation")
        scope_glob = data.get("scope_glob")
        if not isinstance(scope_glob, str) or not scope_glob:
            return Result(False, "design-approval marker has no scope_glob (fail-closed)")
        rel = _rel_to_repo(target_path, repo_root)
        if not fnmatch.fnmatch(rel, scope_glob):
            return Result(False, f"{rel} is OUT OF SCOPE for the sealed plan "
                                 f"(scope_glob={scope_glob!r})")
        return Result(True, f"consensus-sealed: {rel} matches scope_glob={scope_glob!r} "
                            f"(iteration {data.get('iteration_id')})")
    except Exception as exc:  # fail-closed
        return Result(False, f"design-approval verify error (fail-closed): {exc}")
