#!/usr/bin/env python3
"""PreToolUse design gate (Claude Code) — THE HARD BACKSTOP.

Modelled on `contrib/delivery_gate_pretooluse.py` (verified exit-2 block
pattern): reads the PreToolUse event JSON on stdin; blocks a tool call by
exiting 2 with a reason on stderr, allows by exiting 0.

Contract enforced (see `consensus_mcp/_design_approval.py`):
  Implementation tools (Edit/Write/MultiEdit/NotebookEdit) and file-modifying
  Bash commands are DENIED until a VALIDATED `.consensus/design-approved` marker
  covers the scope being touched. A `cross_family_sealed=False` marker is
  advisory only -> still denied (no single-Claude self-approval).

Graceful degradation (converged-plan invariant): if the consensus runtime is
absent (`shutil.which("consensus-init") is None`) the gate FAILS OPEN (exit 0)
so the integrated workflow is never worse than the plain workflow.

stdin event shape (subset used):
  {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"}, "cwd": "..."}
  {"tool_name": "Bash", "tool_input": {"command": "sed -i ..."}, "cwd": "..."}

Test/runtime overrides (env):
  CONSENSUS_MCP_FORCE_RUNTIME_ABSENT=1  -> force fail-open (simulate no runtime)
  CONSENSUS_MCP_FORCE_RUNTIME_PRESENT=1 -> force runtime present (simulate
                                            install without consensus-init on PATH)
  CONSENSUS_MCP_REPO_ROOT=<path>        -> repo root for marker lookup
                                            (else the event `cwd`, else _self_drive)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

# Ensure the consensus_mcp package that ships ALONGSIDE this hook is importable,
# regardless of the cwd Claude Code invokes us from (this file lives at
# consensus_mcp/claude_extensions/hooks/, so the repo root is three parents up).
_PKG_ROOT = Path(__file__).resolve().parents[3]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# Tools whose every invocation modifies a file at `tool_input.file_path`.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# Conservative file-modifying Bash classifier. Keep this LIST conservative
# (the converged plan: "keep regex conservative, log false positives"). Any
# match means the command is treated as scope-modifying and requires a marker.
_FILE_MODIFYING_BASH = (
    re.compile(r"\bsed\b[^\n|;]*\s-i\b"),          # sed -i (in-place)
    re.compile(r"\btee\b"),                          # tee (writes a file)
    re.compile(r">>?"),                              # > or >> redirection
    re.compile(r"\bmv\b"),
    re.compile(r"\bcp\b"),
    re.compile(r"\brm\b"),
    re.compile(r"\bgit\b\s+(commit|tag|push)\b"),    # git commit/tag/push
    re.compile(r"\b(release|deploy|publish)\b"),      # release/deploy/publish
    re.compile(r"\btruncate\b"),
    re.compile(r"\bdd\b"),
)


def _runtime_present() -> bool:
    """Probe whether the consensus runtime is installed. Env overrides win so
    tests can deterministically simulate either branch."""
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
    # Last resort: the package's own repo root.
    from consensus_mcp._self_drive import _resolve_repo_root
    return _resolve_repo_root()


def _classify_bash(command: str) -> bool:
    """True iff the Bash command is conservatively classified as file-modifying
    (and therefore requires a sealed design marker)."""
    if not command:
        return False
    return any(rx.search(command) for rx in _FILE_MODIFYING_BASH)


def _deny(reason: str) -> int:
    print(f"[consensus-design-gate] BLOCKED: {reason}\n"
          f"Seal a Workflow A converged plan covering this scope, then mint "
          f"`.consensus/design-approved` (cross_family_sealed=true).",
          file=sys.stderr)
    return 2


def main(argv=None) -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        # Unreadable payload: FAIL OPEN. The design gate must never brick a
        # session on a malformed/foreign event (UX-parity invariant); the
        # delivery gate is the fail-closed backstop for finished artifacts.
        return 0

    if not _runtime_present():
        return 0  # FAIL OPEN — plain workflow, never worse.

    tool = event.get("tool_name") or event.get("toolName") or ""
    tool_input = event.get("tool_input") or event.get("toolInput") or {}

    # Lazy import so a missing module also fails OPEN here (runtime "present"
    # via PATH but package import broken should not brick editing).
    try:
        from consensus_mcp import _design_approval as da
    except Exception:
        return 0

    repo_root = _repo_root(event)

    if tool in EDIT_TOOLS:
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not file_path:
            return 0  # nothing to gate
        res = da.verify_design_approval(Path(file_path), repo_root=repo_root)
        return 0 if res.ok else _deny(res.reason)

    if tool == "Bash":
        command = tool_input.get("command") or ""
        if not _classify_bash(command):
            return 0  # read-only command (ls, cat, grep, git status/diff) -> allow
        # File-modifying Bash requires a sealed marker. The target scope cannot
        # be pinned to a single path, so we require only that a cross-family-
        # sealed plan is in force (marker_is_sealed), not a per-path scope match.
        res = da.marker_is_sealed(repo_root)
        if res.ok:
            return 0
        return _deny(f"file-modifying Bash command requires a sealed design "
                     f"marker: {res.reason}")

    return 0  # other tools (Read, Grep, Glob, Task, ...) -> allow.


if __name__ == "__main__":
    raise SystemExit(main())
