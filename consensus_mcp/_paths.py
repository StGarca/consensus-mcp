"""Lazy path resolution helpers.

Per the iter-0024 converged plan (SHIP-PHASED, Phase A): introduce a single
source of truth for resolving repo / state / archive / audit-log paths
that re-reads environment state on every call so tests can
`monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))` AFTER import
and have it actually work.

This module is PURELY ADDITIVE in Phase A - no existing tool is touched.
Tools migrate one at a time in iter-0026+ (Phase B). iter-0019's
`_isolate_archive_root` test helper continues to monkeypatch the cached
module-level attributes of un-migrated tools.

Resolution semantics mirror `server.py`'s `_resolve_*` functions exactly,
since v1.13.0 (iter-0007) already split REPO_ROOT into three orthogonal
concerns (spec / state / project). This module is the reusable embodiment
of that split.

Environment variable precedence (per resolver):
  - `resolve_repo_root()` - M1: THE blessed repo-root resolver (env keys ->
                            cwd-ancestor containment-marker walk -> RepoRootError)
  - `repo_root()`         - legacy/lenient shim: delegates to
                            resolve_repo_root(require_markers=False) - NO
                            `__file__` fallback (M1-remediation Q5)
  - `spec_path()`         - `CONSENSUS_MCP_SPEC_PATH` -> legacy under repo_root -> walked-up -> packaged template
  - `state_root()`        - `CONSENSUS_MCP_STATE_ROOT` -> legacy under repo_root -> cwd
  - `project_root()`      - `CONSENSUS_MCP_PROJECT_ROOT` -> legacy repo_root -> cwd

Derived paths build on the resolvers and inherit their laziness.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# === BEGIN CONSENSUS REPO-ROOT RESOLVER (vendored block; source of truth: consensus_mcp/_paths.py) ===
# M1 (consult iteration-m1-hardening-design-4d7d2469) Q2: the ONE blessed
# repo-root resolver. Precedence: CONSENSUS_MCP_REPO_ROOT >
# CONSENSUS_MCP_PROJECT_ROOT > cwd-ancestor walk to the nearest containment
# marker > RepoRootError. NEVER Path(__file__)-derived - a pipx install must
# fail loud rather than silently anchor to site-packages. The detached hook
# copies under ~/.claude/hooks/ cannot import the package, so each hook
# source under consensus_mcp/claude_extensions/hooks/ carries a
# byte-identical vendored copy of this block (stamped by the installer's
# hook-copy step); the pytest drift guard
# (consensus_mcp/tests/test_repo_root_resolver.py) fails the suite if any
# copy diverges. Edit HERE, then mirror the block verbatim into the hooks.

REPO_ROOT_ENV_KEYS = ("CONSENSUS_MCP_REPO_ROOT", "CONSENSUS_MCP_PROJECT_ROOT")

# kimi-rev-002 (binding design change): DEFAULT markers are the project
# containment dirs ONLY - `.consensus/` then `consensus-state/`. Generic
# `.git/` is NOT a default marker (it would silently anchor any subdirectory
# of an ordinary git repo - the exact authority-widening class Q2 removes);
# it is available solely via the explicit allow_git_marker=True opt-in,
# justified in a comment at the call site.
REPO_ROOT_MARKERS = (".consensus", "consensus-state")


class RepoRootError(RuntimeError):
    """Repo root could not be resolved (no env override, no marker found)."""


def _repo_root_marker_hit(candidate: Path, marker: str) -> bool:
    """Containment markers must be real directories; `.git` (opt-in only) may
    also be a FILE (linked-worktree gitlink)."""
    probe = candidate / marker
    if marker == ".git":
        return probe.exists()
    return probe.is_dir()


def resolve_repo_root(
    *,
    env_keys: tuple = REPO_ROOT_ENV_KEYS,
    require_markers: bool = True,
    allow_cwd_walk: bool = True,
    allow_git_marker: bool = False,
    on_failure: str = "raise",
) -> Path:
    """Resolve the governed project root per the M1 blessed precedence table.

    1. First set (non-empty) env key in `env_keys` wins, verbatim-resolved.
       The operator/test override is authoritative and NOT marker-validated
       here; call sites whose documented contract additionally validates the
       env path (e.g. _dispatch_base) do so themselves.
    2. cwd-ancestor walk (when allow_cwd_walk) to the NEAREST directory
       containing a containment marker: `.consensus/` or `consensus-state/`
       (plus `.git` ONLY when allow_git_marker=True).
    3. require_markers=False (declared-lenient call sites only): fall back
       to cwd instead of failing closed.
    4. Otherwise raise RepoRootError with an actionable message naming the
       env keys and markers searched. NEVER a Path(__file__)-derived root.
    """
    if on_failure != "raise":
        raise ValueError(
            f"resolve_repo_root: unsupported on_failure={on_failure!r} "
            f"(only 'raise' is defined by the M1 design)"
        )
    for key in env_keys:
        value = os.environ.get(key)
        if value:
            return Path(value).resolve()
    markers = list(REPO_ROOT_MARKERS)
    if allow_git_marker:
        markers.append(".git")
    cwd = Path.cwd().resolve()
    if allow_cwd_walk:
        for candidate in (cwd, *cwd.parents):
            if any(_repo_root_marker_hit(candidate, m) for m in markers):
                return candidate
    if not require_markers:
        return cwd
    raise RepoRootError(
        "cannot resolve the consensus repo root: none of the env overrides ("
        + ", ".join(env_keys or ("<none>",))
        + ") are set and no ancestor of the current directory ("
        + str(cwd)
        + ") contains a containment marker ("
        + ", ".join(m + "/" for m in markers)
        + "). Set CONSENSUS_MCP_REPO_ROOT to the project root, or run from "
        "inside an initialized project (`consensus init` creates .consensus/)."
    )
# === END CONSENSUS REPO-ROOT RESOLVER ===


# ---------------------------------------------------------------------------
# Resolvers - read env state on every call.
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """Resolve repo root (legacy/lenient shim; retained for back-compat).

    M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q5: this
    function is now a thin shim over the ONE blessed resolver. Its prior
    ``Path(__file__).resolve().parent.parent`` fallback is GONE - that was the
    site-packages-anchoring class the resolver census bans (under a pipx/wheel
    install the walk-up lands in site-packages). Delegating here means a stray
    ``from _paths import repo_root`` can never silently reintroduce that class.

    Precedence (inherited from resolve_repo_root):
      1. `CONSENSUS_MCP_REPO_ROOT` / `CONSENSUS_MCP_PROJECT_ROOT` env override
      2. cwd-ancestor walk to the nearest containment marker
         (`.consensus/` or `consensus-state/`)
      3. Current working directory (``require_markers=False`` keeps this
         lenient shim from raising) - NEVER a `__file__`-derived root.
    """
    return resolve_repo_root(require_markers=False)


def spec_path() -> Path:
    """Resolve spec path (orchestration-spec.md or packaged template).

    Precedence:
      1. `CONSENSUS_MCP_SPEC_PATH` env override
      2. Legacy under CONSENSUS_MCP_REPO_ROOT/docs/architecture/orchestration-spec.md
      3. Walked-up dev-checkout spec
      4. Packaged `spec_template.md` (frozen-wheel fallback)
    """
    override = os.environ.get("CONSENSUS_MCP_SPEC_PATH")
    if override:
        return Path(override).resolve()
    repo_root_env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if repo_root_env:
        legacy = (
            Path(repo_root_env).resolve()
            / "docs" / "architecture" / "orchestration-spec.md"
        )
        if legacy.exists():
            return legacy
    walked = (
        Path(__file__).resolve().parent.parent
        / "docs" / "architecture" / "orchestration-spec.md"
    )
    if walked.exists():
        return walked
    return Path(__file__).resolve().parent / "spec_template.md"


def state_root() -> Path:
    """Resolve state root (consensus-state/ location).

    Precedence:
      1. `CONSENSUS_MCP_STATE_ROOT` env override
      2. Legacy under `CONSENSUS_MCP_REPO_ROOT`
      3. `<cwd>/consensus-state`
    """
    override = os.environ.get("CONSENSUS_MCP_STATE_ROOT")
    if override:
        return Path(override).resolve()
    repo_root_env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if repo_root_env:
        return Path(repo_root_env).resolve() / "consensus-state"
    return Path.cwd().resolve() / "consensus-state"


def project_root() -> Path:
    """Resolve project root (reviewable-file root for goal_packet.allowed_files).

    Precedence:
      1. `CONSENSUS_MCP_PROJECT_ROOT` env override
      2. Legacy `CONSENSUS_MCP_REPO_ROOT`
      3. Current working directory
    """
    override = os.environ.get("CONSENSUS_MCP_PROJECT_ROOT")
    if override:
        return Path(override).resolve()
    repo_root_env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if repo_root_env:
        return Path(repo_root_env).resolve()
    return Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Derived paths - compose the resolvers above; inherit laziness.
# ---------------------------------------------------------------------------


def archive_dir() -> Path:
    """consensus-state/archive/review-passes/ - sealed review-pass archive."""
    return state_root() / "archive" / "review-passes"


def index_path() -> Path:
    """consensus-state/archive/review-passes/index.yaml - pass index."""
    return archive_dir() / "index.yaml"


def active_dir() -> Path:
    """consensus-state/active/ - per-iteration working directories."""
    return state_root() / "active"


def audit_log_path() -> Path:
    """consensus-state/state/audit-log.jsonl - append-only audit event log."""
    return state_root() / "state" / "audit-log.jsonl"


def dispatch_log_path() -> Path:
    """consensus-state/state/dispatch-log.jsonl - append-only dispatch event log."""
    return state_root() / "state" / "dispatch-log.jsonl"


# ---------------------------------------------------------------------------
# Path containment - shared fail-closed guard for operator/AI-supplied paths.
# 2026-05-22 consensus security review (CR-1/CR-3/CR-5, H-2): every caller that
# joins an untrusted relative/absolute path to a base directory before reading
# or writing MUST route it through resolve_contained() so a `../` traversal,
# an absolute path, or an in-base symlink-to-outside fails closed.
# ---------------------------------------------------------------------------


class PathTraversalError(ValueError):
    """A caller-supplied path resolves outside its permitted base directory."""


def is_contained(resolved: Path, base_resolved: Path) -> bool:
    """True iff ``resolved`` is inside ``base_resolved`` (both already resolved).

    Mirrors ``_author_review_packet._is_contained``, including the Windows
    case-fold fallback: ``Path.relative_to`` is a case-sensitive string
    compare, but Windows filesystems are case-insensitive, so a mixed-case
    base vs path would otherwise trigger a false-positive rejection.
    """
    try:
        resolved.relative_to(base_resolved)
        return True
    except ValueError:
        if sys.platform == "win32":
            resolved_lc = str(resolved).lower().replace("\\", "/")
            base_lc = str(base_resolved).lower().replace("\\", "/")
            if resolved_lc == base_lc or resolved_lc.startswith(base_lc.rstrip("/") + "/"):
                return True
        return False


def resolve_contained(base: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``base`` and confirm it stays inside ``base``.

    Resolves symlinks and ``..`` BEFORE the containment check (fail-closed):
    an absolute path, a ``../`` traversal, or an in-base symlink whose real
    target is outside ``base`` all raise :class:`PathTraversalError`. Returns
    the resolved absolute path on success.
    """
    base_resolved = Path(base).resolve()
    candidate = (base_resolved / rel).resolve()
    if not is_contained(candidate, base_resolved):
        raise PathTraversalError(
            f"{rel!r} resolves to {candidate} which is outside {base_resolved}"
        )
    return candidate
