#!/usr/bin/env python3
"""PreToolUse design gate (Claude Code) — THE HARD BACKSTOP.

Modelled on `contrib/delivery_gate_pretooluse.py` (verified exit-2 block
pattern): reads the PreToolUse event JSON on stdin; blocks a tool call by
exiting 2 with a reason on stderr, allows by exiting 0.

Contract enforced (see `consensus_mcp/_design_approval.py`):
  Implementation tools (Edit/Write/MultiEdit/NotebookEdit) are DENIED until a
  VALIDATED `.consensus/design-approved` marker covers the scope being touched
  (`verify_design_approval` re-validates the marker pointer against the live T6
  seal — a hand-written marker cannot self-approve).

  Bash is DEFAULT-DENY (decision B2): the old leaky blocklist (`_classify_bash`)
  is GONE. Bash is allowed ONLY if (a) the command is on a conservative
  READ-ONLY ALLOWLIST (matched on the leading command token; pipelines/`&&`
  require EVERY segment allowlisted), OR (b) a tight-scope sealed marker is in
  force (`marker_is_sealed`). An unknown command is DENIED (fail-safe — the
  inverse of the unfixable blocklist).

  THREAT MODEL: this enforces a COOPERATING agent's discipline, not a malicious
  shell. It is not a sandbox.

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

# Shell operators that split a command line into segments we evaluate
# independently (a pipeline / sequence is allowed only if EVERY segment is
# allowlisted). Conservative on purpose — anything we can't cleanly split or
# recognise is denied.
_BASH_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n")

# Conservative READ-ONLY ALLOWLIST (decision B2 + claude's usability fold-in).
# A single command segment is allowed iff its LEADING token (or a recognised
# `git <subcommand>` / `python -m pytest`) is on this list. Default-deny: an
# unknown leading token is DENIED (fail-safe).
_READ_ONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "rg",
    "echo", "pwd", "which", "pytest",
})
# NOTE: `find` is deliberately NOT allowlisted — `find -exec`/`-delete`/`-fprintf`
# mutate the filesystem (re-audit codex-rev-001). A leading-token allowlist cannot
# safely admit a command whose own primaries can write/exec.
# git subcommands that are read-only.
_READ_ONLY_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "branch", "rev-parse",
})


def _runtime_present() -> bool:
    """Probe whether the consensus runtime is installed. Env overrides win so
    tests can deterministically simulate either branch."""
    if os.environ.get("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT"):
        return False
    if os.environ.get("CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"):
        return True
    return shutil.which("consensus-init") is not None


def _repo_root(event: dict) -> Path:
    # Test/operator override always wins.
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override)
    # Decision H2: resolve the repo root via `git rev-parse --show-toplevel`
    # (anchored at the event cwd), not the raw event cwd, so the marker lookup is
    # stable regardless of which subdirectory the tool was invoked from.
    cwd = event.get("cwd")
    try:
        import subprocess
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd or None, capture_output=True, text=True, timeout=10,
        )
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip())
    except Exception:
        pass
    if cwd:
        return Path(cwd)  # fallback to event cwd
    # Last resort: the package's own repo root.
    from consensus_mcp._self_drive import _resolve_repo_root
    return _resolve_repo_root()


def _segment_is_read_only(segment: str) -> bool:
    """True iff a single command segment's leading token is on the read-only
    allowlist. Recognises `git <read-only-subcommand>` and `python[3] -m pytest`.
    Anything else (incl. redirections, unknown tokens) -> False (default-deny)."""
    seg = segment.strip()
    if not seg:
        return False
    # A redirection OR a command substitution makes a segment a potential writer
    # / arbitrary-exec, even if the leading token looks read-only
    # (e.g. `echo $(rm x)`). Re-audit (gemini-rev-001): the allowlist must reject
    # these before trusting the leading token.
    if any(marker in seg for marker in (">", "<", "$(", "`", "${")):
        return False
    try:
        import shlex
        tokens = shlex.split(seg)
    except ValueError:
        return False
    if not tokens:
        return False
    head = tokens[0]
    if head in _READ_ONLY_COMMANDS:
        return True
    if head == "git":
        if len(tokens) < 2 or tokens[1] not in _READ_ONLY_GIT_SUBCOMMANDS:
            return False
        # git config/pager/exec-path/output injection can EXECUTE arbitrary
        # commands (e.g. `git -c core.pager='!sh -c …' log`) or WRITE a file
        # (`git diff --output=f`). Reject those even on a read-only subcommand.
        for t in tokens[1:]:
            # Exact `-c` = config injection (pager exec); `--output`/`--exec-path`
            # (incl. `=value` forms) write a file / set the exec path. Use exact +
            # prefix matches that do NOT catch benign flags like `--color`.
            if t == "-c" or t.startswith("--output") or t.startswith("--exec-path"):
                return False
        return True
    if head in ("python", "python3"):
        # Allow only `python -m pytest …` (a test runner), nothing else.
        return len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] == "pytest"
    return False


def _bash_is_read_only(command: str) -> bool:
    """DEFAULT-DENY allowlist check for a whole command line. A pipeline /
    sequence (split on | || && ; newline) is read-only iff EVERY non-empty
    segment is read-only. Empty / unknown -> False (denied)."""
    if not command or not command.strip():
        return False
    segments = [s for s in _BASH_SEGMENT_SPLIT.split(command) if s.strip()]
    if not segments:
        return False
    return all(_segment_is_read_only(s) for s in segments)


def _deny(reason: str) -> int:
    print(f"[consensus-design-gate] BLOCKED: {reason}\n"
          f"Seal a Workflow A converged plan (>=2 non-claude reviewers) covering "
          f"this scope, then mint `.consensus/design-approved` pointing at that "
          f"sealed iteration. (The gate re-validates the pointer against the live "
          f"seal — a hand-written marker cannot self-approve.)",
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
        # DEFAULT-DENY (decision B2). Allow ONLY if the command is read-only
        # (every segment on the conservative allowlist) OR a tight-scope sealed
        # marker is in force. An unknown command is denied (fail-safe).
        if _bash_is_read_only(command):
            return 0  # read-only allowlist (ls, cat, grep, git status, pytest, …)
        res = da.marker_is_sealed(repo_root)
        if res.ok:
            return 0  # a tight-scope sealed plan authorises this Bash command
        return _deny(f"Bash command DENIED by default (not on the read-only "
                     f"allowlist and no tight-scope sealed marker in force): "
                     f"{res.reason}")

    return 0  # other tools (Read, Grep, Glob, Task, ...) -> allow.


if __name__ == "__main__":
    raise SystemExit(main())
