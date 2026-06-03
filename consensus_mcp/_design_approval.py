"""Design-approval marker mint/verify - the cross-family-sealed gate that the
PreToolUse hook validates before allowing implementation tool calls.

Parallel to `_delivery_readiness.py`: that module gates DELIVERY (a finished
artifact is consensus-vetted); this module gates IMPLEMENTATION (you may not
Edit/Write until a Workflow A consult has SEALED a converged plan covering the
scope you are about to touch).

Trust model (FIX TRACK 1 / decision B1 - UNANIMOUS):
  The `.consensus/design-approved` marker is a POINTER/CACHE, **not** the trust
  root. It does NOT carry a self-asserted `cross_family_sealed: true` boolean
  (that was forgeable: any process could write `true` in plain YAML and defeat
  the closure invariant). Instead the marker only *points* at a consensus
  iteration, and `verify_design_approval` ALWAYS re-validates that pointer
  against the LIVE consensus-state/ seal:

    1. `resolve_consensus_ref(ref, repo_root)` (from `_delivery_readiness`) must
       report the referenced iteration as a real CLOSED/SEALED iteration. A
       hand-written marker pointing at a non-existent or non-sealed ref FAILS.
    2. the sealed iteration must carry >=2 NON-CLAUDE reviewer artifacts
       (mirrors the >=2-non-claude rule in `mint_delivery_token`) - no
       single-family self-approval.
    3. the marker's `converged_plan_sha256` must match the iteration's
       `converged-plan.yaml` hash (`compute_artifact_hash`) - tamper guard.
    4. the edit target is repo-confined (resolve(strict=False); reject anything
       outside `repo_root` or any `..`/absolute-unsafe ref) and the
       repo-relative path must fnmatch `scope_glob`.

  Forging an accepted approval therefore requires forging the sealed cross-family
  artifacts - exactly what the T6 seal + closure invariant already protect.

Marker contract (fixed - `consensus_pretooluse_gate.py` codes against this):
  `.consensus/design-approved` is a YAML mapping with fields
  {schema_version, design_consensus_ref, converged_plan_sha256, scope_glob,
   repo_root_id}.

Fail-closed: any error (missing/unparseable/wrong-type/path-resolution/seal
re-validation failure) yields a NOT-approved Result with a reason - never raises
out of `verify_*` / `marker_is_sealed`.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import re
from pathlib import Path

from consensus_mcp._atomic_io import atomic_write_text

import yaml

from consensus_mcp._delivery_readiness import (
    compute_artifact_hash,
    resolve_consensus_ref,
)

MARKER_RELPATH = Path(".consensus") / "design-approved"
SCHEMA_VERSION = 1

# scope_globs that are too broad to be a meaningful confinement (decision B3,
# kimi): a sealed plan must name the files it covers, not "everything".
_OVERBROAD_SCOPE_GLOBS = frozenset({"*", "**", "**/*", "*/*"})

# Minimum DISTINCT non-claude reviewer families a sealed iteration must carry for
# the marker to authorize implementation (mirrors mint_delivery_token's
# >=2-non-claude rule).
_MIN_NON_CLAUDE_REVIEWERS = 2


@dataclasses.dataclass
class Result:
    """Outcome of a marker verification. `ok` is the deny/allow signal; `reason`
    is always a human-readable explanation (for the PreToolUse stderr block)."""

    ok: bool
    reason: str


def _marker_path(repo_root: Path) -> Path:
    return Path(repo_root) / MARKER_RELPATH


def _is_overbroad_scope(scope_glob: str) -> bool:
    return scope_glob.strip() in _OVERBROAD_SCOPE_GLOBS


def mint_design_approval(
    repo_root: Path,
    design_consensus_ref: str,
    scope_glob: str,
    converged_plan_sha256: str,
    repo_root_id: str | None = None,
) -> dict:
    """Write the POINTER marker `.consensus/design-approved`.

    Called when a Workflow A consult seals a converged plan. The marker carries
    NO trust by itself - it merely points at `design_consensus_ref`, which
    `verify_design_approval` re-validates against the live seal on every check.

    Rejects an overly-broad `scope_glob` ('*'/'**'/...) at mint time (decision B3):
    a sealed plan must confine itself to the files it actually covers.

    Returns the marker dict that was written.
    """
    if not isinstance(scope_glob, str) or not scope_glob.strip():
        raise ValueError("scope_glob must be a non-empty string")
    if _is_overbroad_scope(scope_glob):
        raise ValueError(
            f"scope_glob {scope_glob!r} is too broad to confine a sealed plan - "
            f"name the files the converged plan covers (e.g. 'consensus_mcp/_x.py')"
        )
    repo_root = Path(repo_root)
    marker = {
        "schema_version": SCHEMA_VERSION,
        "design_consensus_ref": design_consensus_ref,
        "converged_plan_sha256": converged_plan_sha256,
        "scope_glob": scope_glob,
        "repo_root_id": repo_root_id if repo_root_id is not None else repo_root.name,
    }
    path = _marker_path(repo_root)
    # kimi-rev-001 / gemini-rev-001: the design-approved marker is the TRUST
    # POINTER - write it through the SINGLE blessed symlink-safe atomic writer
    # (O_EXCL + unpredictable temp name + fsync + os.replace), the same one the
    # init wizard and session marker use, so a crash/concurrent reader never sees
    # a torn marker and a pre-planted temp symlink cannot redirect the write.
    atomic_write_text(path, yaml.safe_dump(marker, sort_keys=True))
    return marker


def _load_marker(repo_root: Path) -> tuple[dict | None, Result | None]:
    """Load + minimally validate the marker mapping. Returns (data, None) on
    success, or (None, deny-Result) on any failure. Fail-closed."""
    path = _marker_path(repo_root)
    if not path.exists():
        return None, Result(
            False,
            f"no design-approval marker at {MARKER_RELPATH} "
            f"(scope not consensus-sealed - run a Workflow A consult)",
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, Result(False, f"design-approval marker unparseable (fail-closed): {exc}")
    if not isinstance(data, dict):
        return None, Result(False, "design-approval marker is not a YAML mapping (fail-closed)")
    return data, None


# Matches a sealed review artifact filename, capturing the reviewer family.
# Accepts both the bare `<fam>-review.yaml` AND the round/pass-keyed
# `<fam>-review-<N>.yaml` form an adapter writes when reviewers seal under
# distinct pass_ids (the parallel-dispatch H3 seal-collision fix). `<fam>` is
# non-greedy so `kimi-review-4.yaml` -> 'kimi', not 'kimi-review-4'.
_REVIEW_FILE_RE = re.compile(r"^(?P<fam>.+?)-review(?:-.+)?\.yaml$")


def _review_family(filename: str) -> str | None:
    """Reviewer family from a sealed-review filename, or None if not a review
    artifact. Handles `<fam>-review.yaml` and `<fam>-review-<N>.yaml`."""
    m = _REVIEW_FILE_RE.match(filename)
    if not m:
        return None
    return m.group("fam").strip().lower() or None


def _count_non_claude_reviewers(iter_dir: Path) -> int:
    """Count DISTINCT non-claude reviewer families in a sealed iteration by its
    `<family>-review[-N].yaml` artifacts (codex-review.yaml, gemini-review.yaml,
    kimi-review-4.yaml, ...). Round/pass-keyed names count once per family.
    Mirrors the >=2-non-claude reviewer rule in
    `_delivery_readiness.mint_delivery_token`."""
    families: set[str] = set()
    try:
        for art in iter_dir.glob("*-review*.yaml"):
            fam = _review_family(art.name)
            if fam and "claude" not in fam:
                families.add(fam)
    except OSError:
        return 0
    return len(families)


def _revalidate_seal(data: dict, repo_root: Path) -> tuple[bool, str]:
    """The TRUST ROOT. Re-validate the marker's pointer against the live seal:
    (a) `resolve_consensus_ref` reports the iteration SEALED, (b) >=2 non-claude
    reviewer artifacts, (c) marker `converged_plan_sha256` matches the
    iteration's converged-plan.yaml hash. Returns (ok, reason). Fail-closed."""
    repo_root = Path(repo_root)
    ref = data.get("design_consensus_ref")
    if not isinstance(ref, str) or not ref:
        return False, "design-approval marker has no design_consensus_ref (fail-closed)"

    # (a) the iteration MUST resolve as a real CLOSED/SEALED consensus iteration.
    status = resolve_consensus_ref(ref, repo_root)
    if not status.sealed:
        return False, (
            f"design_consensus_ref {ref!r} is NOT a sealed consensus iteration: "
            f"{status.detail} - a hand-written marker cannot self-approve "
            f"(the seal is the trust root, not the marker)"
        )

    iter_dir = repo_root / "consensus-state" / "active" / ref

    # (b) >=2 distinct non-claude reviewer families (no single-family approval).
    n = _count_non_claude_reviewers(iter_dir)
    if n < _MIN_NON_CLAUDE_REVIEWERS:
        return False, (
            f"sealed iteration {ref!r} has only {n} non-claude reviewer "
            f"artifact(s); need >={_MIN_NON_CLAUDE_REVIEWERS} different families "
            f"(no single-family self-approval)"
        )

    # (c) converged_plan_sha256 must match the iteration's converged-plan.yaml.
    plan = iter_dir / "converged-plan.yaml"
    if not plan.exists():
        return False, f"sealed iteration {ref!r} has no converged-plan.yaml (fail-closed)"
    marker_sha = data.get("converged_plan_sha256")
    if not isinstance(marker_sha, str) or not marker_sha:
        return False, "design-approval marker has no converged_plan_sha256 (fail-closed)"
    try:
        actual_sha = compute_artifact_hash(plan)
    except Exception as exc:  # fail-closed
        return False, f"could not hash converged-plan.yaml (fail-closed): {exc}"
    if marker_sha != actual_sha:
        return False, (
            f"converged_plan_sha256 mismatch for {ref!r} "
            f"(marker={marker_sha[:12]}... live={actual_sha[:12]}...) - re-mint the marker"
        )
    return True, f"seal re-validated (sealed iteration {ref!r}, {n} non-claude reviewers)"


def _confine_to_repo(target_path: Path, repo_root: Path) -> tuple[str | None, str]:
    """Repo-confine the target (decision B3): canonicalize with
    resolve(strict=False); REJECT anything that resolves outside `repo_root`.
    Returns (repo-relative-posix, "") on success, or (None, reason) on rejection.
    """
    repo_root = Path(repo_root).resolve(strict=False)
    tp = Path(target_path)
    if not tp.is_absolute():
        tp = repo_root / tp
    tp = tp.resolve(strict=False)
    try:
        rel = tp.relative_to(repo_root)
    except ValueError:
        return None, (
            f"target {Path(target_path).as_posix()!r} resolves OUTSIDE the repo "
            f"({repo_root}) - out-of-repo edits are not consensus-sealable"
        )
    return rel.as_posix(), ""


def verify_design_approval(target_path: Path, repo_root: Path) -> Result:
    """Validate the design-approval marker against `target_path`. Fail-closed.

    Order: (a) load marker; (b) re-validate the pointer against the LIVE seal
    (`resolve_consensus_ref` sealed + >=2 non-claude reviewers + matching
    converged-plan hash); (c) repo-confine the target; (d) fnmatch the
    repo-relative path against `scope_glob`. Any failure -> NOT approved.
    """
    try:
        data, deny = _load_marker(repo_root)
        if deny is not None:
            return deny

        ok, reason = _revalidate_seal(data, repo_root)
        if not ok:
            return Result(False, reason)

        scope_glob = data.get("scope_glob")
        if not isinstance(scope_glob, str) or not scope_glob:
            return Result(False, "design-approval marker has no scope_glob (fail-closed)")

        rel, reject = _confine_to_repo(target_path, repo_root)
        if rel is None:
            return Result(False, reject)

        if not fnmatch.fnmatch(rel, scope_glob):
            return Result(
                False,
                f"{rel} is OUT OF SCOPE for the sealed plan (scope_glob={scope_glob!r})",
            )
        return Result(
            True,
            f"consensus-sealed: {rel} matches scope_glob={scope_glob!r}; {reason}",
        )
    except Exception as exc:  # fail-closed
        return Result(False, f"design-approval verify error (fail-closed): {exc}")


def marker_is_sealed(repo_root: Path) -> Result:
    """Path-agnostic check used by the PreToolUse Bash branch: a VALID sealed
    marker with a TIGHT scope_glob is in force. "Valid" = the pointer re-validates
    against the live seal (`_revalidate_seal`); "tight" = the scope_glob is not
    one of the overbroad globs ('*'/'**'/...). Fail-closed.

    The Bash branch cannot pin a single target path, so it requires only that a
    genuinely sealed, tightly-scoped plan is in force (not a per-path scope
    match). Edit tools instead use `verify_design_approval` for the full check.
    """
    try:
        data, deny = _load_marker(repo_root)
        if deny is not None:
            return deny

        scope_glob = data.get("scope_glob")
        if not isinstance(scope_glob, str) or not scope_glob:
            return Result(False, "design-approval marker has no scope_glob (fail-closed)")
        if _is_overbroad_scope(scope_glob):
            return Result(
                False,
                f"design-approval marker scope_glob {scope_glob!r} is too broad to "
                f"authorize Bash (a tight, file-naming scope is required)",
            )

        ok, reason = _revalidate_seal(data, repo_root)
        if not ok:
            return Result(False, reason)
        return Result(True, f"tight-scope sealed design marker in force; {reason}")
    except Exception as exc:  # fail-closed
        return Result(False, f"design-approval verify error (fail-closed): {exc}")
