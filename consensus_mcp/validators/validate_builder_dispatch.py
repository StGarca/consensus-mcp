"""Write-enabled builder dispatch canon - SEPARATE from the read-only canon.

2026-06-10 consult Q6 (unanimous): write-enabled shapes never live in the
read-only dispatch-canon allowlist - merging them risks promoting
workspace-write into review dispatches. This module is the ONLY authority on
what a builder argv may look like, and the builder dispatcher MUST call it
before Popen (fail-closed: any violation aborts the dispatch).

Canon model: an EXACT POSITIONAL ALLOWLIST, not a rule list. The converged
plan's Q6 resolution prescribes "exact argv shape per builder_capable CLI";
the quality review of the first cut proved why - a rule list (one --sandbox,
one --cd, no 'git' token) is smuggle-able through clap's alternate spellings
(-s/-C shorts, --sandbox=X/--cd=X equals forms) and through OTHER flags that
promote sandbox posture without touching --sandbox at all
(-c sandbox_mode=..., --dangerously-bypass-approvals-and-sandbox,
--full-auto, --profile). An exact-shape allowlist kills the whole class
structurally: there are no extra token positions for anything to hide in.

v1 canon (codex only) - exactly 12 tokens:

  [0] <codex binary>          variable: stem 'codex', ext per Windows set
  [1] exec                    fixed
  [2] --skip-git-repo-check   fixed
  [3] --cd                    fixed
  [4] <lane path>             variable: must RESOLVE (symlinks followed)
                              to a lane dir under the goal root
                              (layout from _architect_paths, the single
                              source of truth)
  [5] --sandbox               fixed
  [6] workspace-write         fixed
  [7] --output-schema         fixed
  [8] <schema path>           variable
  [9] -o                      fixed
  [10] <output path>          variable
  [11] -                      fixed (prompt via stdin)

Anything longer, shorter, or positionally different is rejected. Variable
slots are additionally screened for shell metacharacters and the token
'git' (supervisor-owned git, consult Q1) as defense-in-depth - argv is
exec'd, never shell'd, but wrapper drift is exactly what this module exists
to refuse.
"""
from __future__ import annotations

import re
from pathlib import Path

from consensus_mcp._architect_paths import GOAL_ROOT_PARTS, LANE_DIRNAME

_BUILDER_CLI_STEM = "codex"
# Must match the extension set _resolve_codex_bin emits ('init platform
# consistency'): on Windows it tries .exe/.cmd/.bat/.ps1 for a bare name,
# so rejecting any of them would fail-close every legitimate Windows
# builder dispatch.
_ALLOWED_BINARY_SUFFIXES = {"", ".exe", ".cmd", ".bat", ".ps1"}
_SHELL_META_RE = re.compile(r"[;&|`$<>\n]")

# index -> required literal token. Indices 0, 4, 8, 10 are variable slots.
_FIXED_TOKENS = {
    1: "exec",
    2: "--skip-git-repo-check",
    3: "--cd",
    5: "--sandbox",
    6: "workspace-write",
    7: "--output-schema",
    9: "-o",
    11: "-",
}
_CANON_LEN = 12
_CANON_TEMPLATE = (
    "[<codex-bin>, exec, --skip-git-repo-check, --cd, <lane>, --sandbox, "
    "workspace-write, --output-schema, <schema>, -o, <out>, -]"
)


def validate_builder_argv(argv: list[str], repo_root: Path) -> list[str]:
    """Return a list of violation strings; empty list means the shape is
    canon. Callers MUST treat any violation as a hard abort."""
    violations: list[str] = []
    if not argv:
        return ["empty argv"]

    if len(argv) != _CANON_LEN:
        # One message carries the whole template so the caller (and the
        # pin tests) see exactly which canon was violated and by what.
        return [
            f"argv must be exactly the {_CANON_LEN}-token canon "
            f"{_CANON_TEMPLATE}; got {len(argv)} tokens: {argv!r}"
        ]

    for i, expected in _FIXED_TOKENS.items():
        if argv[i] != expected:
            violations.append(
                f"argv[{i}] must be {expected!r}; got {argv[i]!r}"
            )

    binary = Path(Path(argv[0]).name.lower())
    if not (
        binary.stem == _BUILDER_CLI_STEM
        and binary.suffix in _ALLOWED_BINARY_SUFFIXES
    ):
        violations.append(
            f"binary {argv[0]!r} is not a builder_capable CLI "
            f"(allowed: {_BUILDER_CLI_STEM!r} with extension in "
            f"{sorted(_ALLOWED_BINARY_SUFFIXES)})"
        )

    try:
        resolved = Path(argv[4]).resolve(strict=True)
    except OSError:
        resolved = None
    root = Path(repo_root).resolve().joinpath(*GOAL_ROOT_PARTS)
    ok = (
        resolved is not None
        and resolved.name == LANE_DIRNAME
        and resolved.parent.parent == root
    )
    if not ok:
        violations.append(
            f"--cd {argv[4]!r} does not resolve to a lane directory "
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
