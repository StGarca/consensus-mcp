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
  - `repo_root()`         - `CONSENSUS_MCP_REPO_ROOT` -> walked-up `__file__` -> cwd
  - `spec_path()`         - `CONSENSUS_MCP_SPEC_PATH` -> legacy under repo_root -> walked-up -> packaged template
  - `state_root()`        - `CONSENSUS_MCP_STATE_ROOT` -> legacy under repo_root -> cwd
  - `project_root()`      - `CONSENSUS_MCP_PROJECT_ROOT` -> legacy repo_root -> cwd

Derived paths build on the resolvers and inherit their laziness.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Resolvers - read env state on every call.
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """Resolve repo root.

    Precedence:
      1. `CONSENSUS_MCP_REPO_ROOT` env var (operator override)
      2. Walked-up `__file__` (dev checkout layout: this file's parent.parent
         is the repo root)
      3. Current working directory (installed wheel / unusual layouts)
    """
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override).resolve()
    walked = Path(__file__).resolve().parent.parent
    # Dev checkout: walked-up parent contains repo markers (pyproject.toml
    # or .git). In an installed wheel, walked = site-packages/ which has
    # `consensus_mcp/` as a sibling but neither pyproject.toml nor .git, so
    # we must NOT return site-packages as the repo root.
    if (walked / "pyproject.toml").exists() or (walked / ".git").exists():
        return walked
    # Installed wheel / unusual layouts: cwd is the right fallback so the
    # orchestrator can still locate consensus-state/.
    return Path.cwd().resolve()


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
