"""interaction_surface declaration check (sp-consensus-optimization B1).

The gate<->init miss (v1.29.4) was a cross-cutting interaction nobody named in the
consult packet. This validator makes that surface EXPLICIT: a goal_packet must
declare an ``interaction_surface`` (the gates/hooks/CLIs/dispatch/distribution it
may perturb), and "none" must be an explicit assertion — never silent omission. A
path heuristic flags a *reflexive* "none" when the change actually touches
interaction-bearing files (hooks/gates/dispatchers/engine).

Pure function returning warnings; NOT yet wired into the live goal_packet loader
(that is a deliberate, operator-reviewed step). Complements the governed-project
integration smoke (B2).
"""
from __future__ import annotations

# Substrings that mark a path as interaction-surface-bearing (live governance machinery).
_INTERACTION_PATH_MARKERS = (
    "claude_extensions/hooks/", "_dispatch", "pretooluse", "stop_gate",
    "workflow_engine", "gate", "_design_approval", "_delivery_readiness",
    "settings.json", ".mcp.json", ".consensus/",  # .consensus config IS governance machinery (codex-rev-001)
)


def _is_none_assertion(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "none", "n/a"}
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def _touches_interaction_surface(changed_paths) -> list[str]:
    hits = []
    for p in changed_paths or []:
        pl = str(p).replace("\\", "/").lower()
        if any(marker in pl for marker in _INTERACTION_PATH_MARKERS):
            hits.append(p)
    return hits


def validate_interaction_surface(goal_packet: dict, changed_paths=None) -> list[str]:
    """Return a list of warning strings (empty == clean).

    - missing ``interaction_surface`` key entirely -> warning (must be explicit).
    - an explicit "none"/empty assertion while ``changed_paths`` touches
      interaction-bearing files -> reflexive-none warning naming the files.
    A declared non-empty interaction_surface, or a genuine "none" with no
    interaction-bearing paths, is clean."""
    warnings: list[str] = []
    if "interaction_surface" not in (goal_packet or {}):
        warnings.append(
            "interaction_surface is missing — declare it explicitly (the surfaces this "
            "change may perturb: hooks/gates/CLIs/dispatch/distribution), or assert 'none'."
        )
        return warnings
    value = goal_packet["interaction_surface"]
    if _is_none_assertion(value):
        hits = _touches_interaction_surface(changed_paths)
        if hits:
            warnings.append(
                "interaction_surface asserts 'none' but the change touches "
                f"interaction-bearing files: {hits}. Name the surface (e.g. the "
                "PreToolUse gate / dispatcher) instead of a reflexive 'none'."
            )
    return warnings
