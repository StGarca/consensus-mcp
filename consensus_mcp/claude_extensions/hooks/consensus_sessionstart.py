#!/usr/bin/env python3
"""SessionStart precedence injector + UserPromptSubmit nudge (Claude Code).

SessionStart: probe the consensus runtime; if present, emit JSON
  {"hookSpecificOutput": {"hookEventName": "SessionStart",
                          "additionalContext": "<precedence text>"}}
mapping Superpowers gates -> consensus:
  - brainstorming approval        -> Workflow A (the converged-plan IS the approval)
  - requesting/receiving-code-review -> Workflow B (sealed cross-family audit)
  - verification-before-completion -> sealed gate + delivery token
  - Edit/Write blocked until `.consensus/design-approved` (cross-family sealed)
  - completion blocked until delivery tokens exist
If the runtime is ABSENT, emit a benign "not detected — plain workflow" notice,
so the absence is visible (not silently unguarded) and the workflow degrades.

UserPromptSubmit: a lightweight nudge (same precedence summary, shorter). The
event type is taken from the stdin event's `hook_event_name` (Claude Code
populates this); falls back to SessionStart shape. Always fail-open.

Test/runtime overrides (env): CONSENSUS_MCP_FORCE_RUNTIME_ABSENT / _PRESENT.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_PRECEDENCE_TEXT = (
    "consensus-mcp is active. Consensus has PRECEDENCE at every decision gate; "
    "defer to it instead of barreling ahead:\n"
    "- Brainstorming/design approval -> run a consensus Workflow A consult. The "
    "converged plan IS the approval (consensus is the approver, not a single Claude).\n"
    "- Requesting/receiving code review -> Workflow B: dispatch the cross-family "
    "reviewer panel (codex/gemini/kimi). The sealed panel is the audit, not a "
    "single-Claude pass.\n"
    "- Verification before completion -> the sealed gate + a delivery-readiness "
    "token (consensus_mcp/_delivery_readiness.py).\n"
    "ENFORCEMENT (hooks): Edit/Write/MultiEdit/NotebookEdit and file-modifying "
    "Bash are BLOCKED until `.consensus/design-approved` (cross-family sealed) "
    "covers the scope. Completion is flagged until every modified source file has "
    "a valid delivery token."
)

_ABSENT_TEXT = (
    "consensus-mcp not detected — running the plain workflow (single-Claude). "
    "Consensus gates are NOT enforced this session; install consensus-mcp "
    "(consensus-init) to enable cross-family design approval + delivery gating."
)

_NUDGE_TEXT = (
    "Reminder: consensus has precedence. Before implementing, seal a Workflow A "
    "plan (.consensus/design-approved); before claiming done, mint a delivery "
    "token. Edits/file-modifying Bash are gated until the design is sealed."
)


def _runtime_present() -> bool:
    if os.environ.get("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT"):
        return False
    if os.environ.get("CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"):
        return True
    return shutil.which("consensus-init") is not None


def _git_toplevel(start: Path) -> Path | None:
    """Resolve the git working-tree root containing `start` via git rev-parse.

    Returns None if git is unavailable / `start` is not inside a worktree.
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
    """Resolve the repo root for this session.

    H2 fix: resolve via `git rev-parse --show-toplevel` (from the override or
    the event cwd) so a session opened from a SUBDIRECTORY still resolves to the
    repo root. Fall back to the event cwd, then the runtime resolver.
    """
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        top = _git_toplevel(Path(override))
        return top if top is not None else Path(override)
    cwd = event.get("cwd")
    if cwd:
        top = _git_toplevel(Path(cwd))
        return top if top is not None else Path(cwd)
    try:
        from consensus_mcp._self_drive import _resolve_repo_root
        return _resolve_repo_root()
    except Exception:
        return Path.cwd()


def _event_name(event: dict) -> str:
    return (
        event.get("hook_event_name")
        or event.get("hookEventName")
        or "SessionStart"
    )


def main(argv=None) -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    name = _event_name(event)
    present = _runtime_present()
    # H2: resolve the repo root via git rev-parse so a session/prompt opened
    # from a subdirectory still anchors to the repo root. Computed here so the
    # precedence injection is repo-correct; failures fall back gracefully and
    # never block the (fail-open) hook.
    repo_root = _repo_root(event)

    if name == "UserPromptSubmit":
        # Lightweight nudge. No-op when runtime absent.
        if not present:
            return 0
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _NUDGE_TEXT,
            }
        }
        print(json.dumps(out))
        return 0

    # SessionStart (startup|clear|compact) and any other event: emit precedence
    # context (present) or the benign absence notice. When present, append the
    # git-resolved repo root so the injected context is anchored correctly even
    # when the session was opened from a subdirectory (H2).
    if present:
        text = f"{_PRECEDENCE_TEXT}\nRepo root (consensus scope): {repo_root}"
    else:
        text = _ABSENT_TEXT
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
