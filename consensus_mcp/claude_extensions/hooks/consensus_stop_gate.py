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
      "STOP — verification not satisfied for <file>: invoke consensus-verify /
       mint a delivery token"

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


def _repo_root(event: dict) -> Path:
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override)
    cwd = event.get("cwd")
    if cwd:
        return Path(cwd)
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


def _modified_files(repo_root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def main(argv=None) -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    if not _runtime_present():
        return 0  # no-op, fail-open.

    try:
        from consensus_mcp import _delivery_readiness as dr
    except Exception:
        return 0  # cannot load gate -> soft gate stays silent (fail-open).

    repo_root = _repo_root(event)
    unverified: list[str] = []
    for rel in _modified_files(repo_root):
        if not _is_source(rel):
            continue
        artifact = repo_root / rel
        if not artifact.exists():
            continue
        res = dr.verify_delivery_token(artifact, repo_root=repo_root)
        if not res.get("ok"):
            unverified.append(rel)

    if unverified:
        files = ", ".join(unverified)
        directive = (
            f"STOP — verification not satisfied for {files}: "
            f"invoke consensus-verify / mint a delivery token before claiming "
            f"completion. Each modified source file must carry a valid "
            f"delivery-readiness token (consensus-vetted, hash-current, sealed)."
        )
        print(directive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
