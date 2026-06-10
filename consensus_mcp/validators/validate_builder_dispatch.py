"""Write-enabled builder dispatch canon - SEPARATE from the read-only canon.

2026-06-10 consult Q6 (unanimous): write-enabled shapes never live in the
read-only dispatch-canon allowlist - merging them risks promoting
workspace-write into review dispatches. This module is the ONLY authority on
what a builder argv may look like, and the builder dispatcher MUST call it
before Popen (fail-closed: any violation aborts the dispatch).

v1 canon (codex only):
  codex exec --skip-git-repo-check --cd <lane> --sandbox workspace-write ...

Rules (consult Q1/Q6 union):
  R1 binary basename is exactly the builder CLI ('codex' / 'codex.exe')
  R2 'exec' subcommand present
  R3 exactly one --sandbox flag, value exactly 'workspace-write'
  R4 exactly one --cd flag whose RESOLVED path (symlinks followed) is a
     'lane' directory under <repo>/.consensus/architect/<goal-id>/
  R5 no argv token is 'git' (supervisor-owned git, consult Q1)
  R6 no shell metacharacters in any token (argv is exec'd, never shell'd,
     but defense-in-depth against future wrapper drift)
"""
from __future__ import annotations

import re
from pathlib import Path

_ALLOWED_BINARIES = {"codex", "codex.exe"}
_SHELL_META_RE = re.compile(r"[;&|`$<>\n]")


def _flag_values(argv: list[str], flag: str) -> list[str]:
    vals = []
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv):
            vals.append(argv[i + 1])
    return vals


def validate_builder_argv(argv: list[str], repo_root: Path) -> list[str]:
    """Return a list of violation strings; empty list means the shape is
    canon. Callers MUST treat any violation as a hard abort."""
    violations: list[str] = []
    if not argv:
        return ["empty argv"]

    binary = Path(argv[0]).name.lower()
    if binary not in _ALLOWED_BINARIES:
        violations.append(
            f"binary {argv[0]!r} is not a builder_capable CLI "
            f"(allowed: {sorted(_ALLOWED_BINARIES)})"
        )
    if "exec" not in argv[1:2]:
        violations.append("second token must be the 'exec' subcommand")

    sandboxes = _flag_values(argv, "--sandbox")
    if sandboxes != ["workspace-write"]:
        violations.append(
            f"--sandbox must appear exactly once with value "
            f"'workspace-write'; got {sandboxes!r}"
        )

    cds = _flag_values(argv, "--cd")
    if len(cds) != 1:
        violations.append(f"--cd must appear exactly once; got {len(cds)}")
    else:
        try:
            resolved = Path(cds[0]).resolve(strict=True)
        except OSError:
            resolved = None
        root = Path(repo_root).resolve() / ".consensus" / "architect"
        ok = (
            resolved is not None
            and resolved.name == "lane"
            and resolved.parent.parent == root
        )
        if not ok:
            violations.append(
                f"--cd {cds[0]!r} does not resolve to a lane directory "
                f"under {root} (symlinks are resolved before the check)"
            )

    for tok in argv:
        if tok.lower() == "git":
            violations.append(
                "argv token 'git' is forbidden: lane git operations are "
                "supervisor-owned (consult Q1)"
            )
        if _SHELL_META_RE.search(tok):
            violations.append(f"shell metacharacter in token {tok!r}")

    return violations
