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
If the runtime is ABSENT, emit a benign "not detected - plain workflow" notice,
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

# Ensure the consensus_mcp package that ships ALONGSIDE this hook is importable
# regardless of the cwd Claude Code invokes us from (parity with
# consensus_pretooluse_gate.py / consensus_stop_gate.py - this file previously
# lacked the insert, so its new gate_should_enforce import would silently fall
# back to a stale site-packages copy). Repo root is three parents up.
_PKG_ROOT = Path(__file__).resolve().parents[3]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


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
    "consensus-mcp not detected - running the plain workflow (single-Claude). "
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


def _should_enforce(repo_root: Path) -> bool:
    """Dormant-by-default parity (v1.33 gate-consistency fix): the injector now
    shares the PreToolUse gate's activation predicate. Returns True only when a
    consensus consult is in flight (or the operator forced opt-in). On any
    import/probe error -> False (dormant = SILENT), the least-obnoxious
    direction: never impose consensus precedence framing on everyday work."""
    try:
        from consensus_mcp._session_state import gate_should_enforce
        return gate_should_enforce(repo_root)
    except Exception:
        return False


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
    # M1 (consult iteration-m1-hardening-design-4d7d2469) Q2: last-resort
    # discovery goes through the VENDORED resolver block above - the detached
    # ~/.claude hook copy cannot import consensus_mcp, and the old
    # `_self_drive._resolve_repo_root` fallback carried the site-packages
    # __file__ hazard. Hooks stay fail-open: unresolvable -> cwd, never a crash.
    try:
        return resolve_repo_root()
    except RepoRootError:
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
        # Lightweight nudge. No-op when runtime absent OR when the gate is
        # dormant (no consult in flight) - everyday prompts in any repo are not
        # nudged about consensus precedence.
        if not present:
            return 0
        if not _should_enforce(repo_root):
            return 0
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _NUDGE_TEXT,
            }
        }
        print(json.dumps(out))
        return 0

    # SessionStart (startup|clear|compact) and any other event:
    #   - runtime ABSENT  -> benign "not detected" notice (absence stays visible).
    #   - runtime present but DORMANT (no consult in flight) -> SILENT (v1.33
    #     gate-consistency fix): do not stamp consensus PRECEDENCE framing into a
    #     session doing ordinary, non-consensus work.
    #   - runtime present AND active -> full precedence context, anchored to the
    #     git-resolved repo root (H2: correct even when opened from a subdir).
    if not present:
        text = _ABSENT_TEXT
    elif not _should_enforce(repo_root):
        return 0
    else:
        text = f"{_PRECEDENCE_TEXT}\nRepo root (consensus scope): {repo_root}"
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
