#!/usr/bin/env python3
"""Stop verification SOFT gate (Claude Code).

SOFT, not a hard deny: the converged plan records that a hard Stop deny is NOT
verified in Claude Code, so this gate INJECTS A BLOCKING DIRECTIVE (context)
rather than refusing to stop. The PreToolUse gate is the hard backstop; this
catches "completion before verification".

Behaviour:
  - runtime absent (`shutil.which("consensus-init") is None`) -> no-op (fail-open).
  - else: `git diff --name-only HEAD`; for each modified NON-TEST SOURCE file
    call `_delivery_readiness.verify_delivery_token`; if any lacks a valid token,
    print a directive naming the file(s):
      "STOP - verification not satisfied for <file>: mint a delivery token via
       consensus-mcp-deliver"

Test/runtime overrides (env): same as the PreToolUse gate
  (CONSENSUS_MCP_FORCE_RUNTIME_ABSENT / _PRESENT, CONSENSUS_MCP_REPO_ROOT).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Ensure the consensus_mcp package shipping alongside this hook is importable
# regardless of cwd (see consensus_pretooluse_gate.py for the rationale).
_PKG_ROOT = Path(__file__).resolve().parents[3]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# File suffixes treated as "source" for the verification gate.
_SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".sh", ".mjs",
})


def _runtime_present() -> bool:
    if os.environ.get("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT"):
        return False
    if os.environ.get("CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"):
        return True
    return shutil.which("consensus-init") is not None


def _git_toplevel(start: Path) -> Path | None:
    """Resolve the git working-tree root containing `start` via git rev-parse.

    Returns the resolved top-level Path, or None if git is unavailable / `start`
    is not inside a worktree. This fixes H2: using the raw event cwd broke the
    gate whenever Claude Code ran from a SUBDIRECTORY of the repo (markers and
    `git diff` were resolved against the subdir, not the repo root).
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start), capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    top = (out.stdout or "").strip()
    return Path(top).resolve() if top else None


def _repo_root(event: dict) -> Path:
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        # Honor the override but still climb to the git toplevel so a subdir
        # override resolves to the repo root (H2).
        top = _git_toplevel(Path(override))
        return top if top is not None else Path(override)
    cwd = event.get("cwd")
    if cwd:
        top = _git_toplevel(Path(cwd))
        return top if top is not None else Path(cwd)
    from consensus_mcp._self_drive import _resolve_repo_root
    return _resolve_repo_root()


def _is_test_path(rel: str) -> bool:
    parts = Path(rel).parts
    name = Path(rel).name
    if any(p in ("tests", "test", "__tests__") for p in parts):
        return True
    if name.startswith("test_") or name.endswith("_test.py") or ".test." in name:
        return True
    return False


def _is_source(rel: str) -> bool:
    return Path(rel).suffix.lower() in _SOURCE_SUFFIXES and not _is_test_path(rel)


def _git_names(args: list[str], repo_root: Path) -> list[str]:
    try:
        out = subprocess.run(
            args, cwd=str(repo_root), capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def _modified_files(repo_root: Path) -> list[str]:
    """Files that need verification before completion.

    L1 fix: it is not enough to check only the working-tree diff vs HEAD - a
    Claude step that COMMITS its change leaves `git diff --name-only HEAD` empty
    yet the committed source is still unverified. So we combine:
      - working-tree changes:  git diff --name-only HEAD
      - the most recent commit's files: git show --name-only --pretty=format: HEAD
    Deduped, order-stable. (The caller filters to source + existing files and
    re-validates each against its delivery token, so naming a committed file
    that was later verified does no harm.)
    """
    files: list[str] = []
    seen: set[str] = set()
    for rel in (
        _git_names(["git", "diff", "--name-only", "HEAD"], repo_root)
        + _git_names(
            ["git", "show", "--name-only", "--pretty=format:", "HEAD"], repo_root
        )
    ):
        if rel and rel not in seen:
            seen.add(rel)
            files.append(rel)
    return files


def main(argv=None) -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    if not _runtime_present():
        return 0  # no-op, fail-open.

    repo_root = _repo_root(event)

    # Dormant-by-default parity (v1.33 gate-consistency fix): the Stop gate now
    # shares the PreToolUse gate's activation predicate (consensus_mcp.
    # _session_state.gate_should_enforce). When NO consensus consult is in flight
    # the completion check is a NO-OP - everyday work in any repo is never nagged
    # for a delivery token it never needed. Probe failure fails OPEN (dormant): a
    # SOFT directive must never block completion on a probe error.
    try:
        from consensus_mcp._session_state import gate_should_enforce
        if not gate_should_enforce(repo_root):
            return 0
    except Exception:
        return 0

    try:
        from consensus_mcp import _delivery_readiness as dr
    except Exception:
        return 0  # cannot load gate -> soft gate stays silent (fail-open).

    unverified: list[str] = []
    for rel in _modified_files(repo_root):
        if not _is_source(rel):
            continue
        artifact = repo_root / rel
        if not artifact.exists():
            continue
        # L2: a bug/exception in verify_delivery_token must not crash the hook.
        # Fail soft - log to stderr and treat the file as unverified (the safe,
        # gate-asserting direction for a SOFT directive that only injects text).
        try:
            res = dr.verify_delivery_token(artifact, repo_root=repo_root)
        except Exception as exc:  # noqa: BLE001 - deliberate fail-soft
            print(
                f"consensus_stop_gate: verify_delivery_token raised for {rel}: "
                f"{exc!r}; treating as unverified",
                file=sys.stderr,
            )
            unverified.append(rel)
            continue
        if not res.get("ok"):
            unverified.append(rel)

    if unverified:
        files = ", ".join(unverified)
        directive = (
            f"STOP - verification not satisfied for {files}: mint a delivery token "
            f"before claiming completion. Run, per file:\n"
            f"  consensus-mcp-deliver --file <file> --design-consensus-ref <sealed-iteration> --vetted-by <fam1>,<fam2>\n"
            f"Each modified source file must carry a valid delivery-readiness token "
            f"(consensus-vetted by >=2 non-claude reviewers, hash-current, sealed)."
        )
        print(directive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
