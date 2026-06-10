"""architect.approve_spec + architect.cleanup - thin human gates (workflow D).

Consult Q5: a DEDICATED spec seal, never consensus_approve (whose >=2
non-claude-reviewer + converged-plan preconditions architect-build cannot
meet at spec time). Mirrors the delivery_gate multi-tool module pattern.
"""
from __future__ import annotations

from pathlib import Path

from consensus_mcp import _architect_lane as lane_mod
from consensus_mcp import _architect_paths as ap

APPROVE_SCHEMA = {
    "name": "architect.approve_spec",
    "description": (
        "Human spec gate for architect-build: seals spec-approval.yaml "
        "binding spec_sha256 + base_sha (the main HEAD the lane branches "
        "from) + approver. Refuses if no sealed spec or already approved."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal_dir": {"type": "string"},
            "approver": {"type": "string"},
            "repo_root": {"type": ["string", "null"]},
        },
        "required": ["goal_dir", "approver"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "spec_sha256": {"type": ["string", "null"]},
            "base_sha": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["ok"],
        "additionalProperties": False,
    },
}

CLEANUP_SCHEMA = {
    "name": "architect.cleanup",
    "description": (
        "Lane lifecycle for a CLOSED architect-build goal: optionally prunes "
        "the lane worktree + branch. Killed goals retain their lane for "
        "forensics (consult Q7)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal_dir": {"type": "string"},
            "repo_root": {"type": ["string", "null"]},
            "prune_lane": {"type": ["boolean", "null"]},
        },
        "required": ["goal_dir"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "pruned": {"type": ["boolean", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["ok"],
        "additionalProperties": False,
    },
}


def _repo_root(repo_root: str | None, goal: Path) -> Path | None:
    if repo_root:
        return Path(repo_root)
    # goal dir is <root>/.consensus/architect/<id>. Fail-loud doctrine: never
    # trust blind parent-hopping - a mis-shaped goal_dir would anchor git at a
    # garbage root and rev-parse would walk UP to whatever repo encloses it.
    # _derive_repo_root is the VALIDATED inversion of that layout.
    return lane_mod._derive_repo_root(ap.lane_dir(goal))


def _no_root_error(goal: Path) -> str:
    return (
        f"cannot derive repo root: goal_dir {goal} is not shaped "
        f"<root>/{'/'.join(ap.GOAL_ROOT_PARTS)}/<goal-id> and no repo_root "
        "was supplied"
    )


def handle_approve_spec(
    goal_dir: str, approver: str, repo_root: str | None = None
) -> dict:
    goal = Path(goal_dir)
    err = {"ok": False, "spec_sha256": None, "base_sha": None}
    root = _repo_root(repo_root, goal)
    if root is None:
        return dict(err, error=_no_root_error(goal))
    spec_file = ap.latest_spec_path(goal)
    spec = ap._read_yaml_or_empty(spec_file)
    if not spec.get("payload_sha256"):
        return dict(err, error=f"no sealed spec at {spec_file}")
    if (goal / ap.SPEC_APPROVAL_FILENAME).exists():
        return dict(err, error="spec already approved; the architect owns "
                               "spec evolution between gates (spec-rev-N)")
    # The hardened lane git, not a raw subprocess: scrubbed env (a GIT_DIR
    # leaked from a hook context would make rev-parse ignore cwd and seal a
    # DIFFERENT repository's HEAD), hooks neutralized, utf-8 decoding.
    try:
        base_sha = lane_mod._git(root, "rev-parse", "HEAD").strip()
    except lane_mod.LaneError as exc:
        return dict(err, error=f"cannot resolve base_sha: {exc}")
    ap.seal_artifact(
        goal / ap.SPEC_APPROVAL_FILENAME,
        {
            "spec_file": spec_file.name,
            "spec_sha256": spec["payload_sha256"],
            "base_sha": base_sha,
            "approver": approver,
        },
    )
    return {
        "ok": True, "spec_sha256": spec["payload_sha256"],
        "base_sha": base_sha, "error": None,
    }


def handle_cleanup(
    goal_dir: str, repo_root: str | None = None, prune_lane: bool | None = None
) -> dict:
    goal = Path(goal_dir)
    root = _repo_root(repo_root, goal)
    if root is None:
        return {"ok": False, "pruned": None, "error": _no_root_error(goal)}
    outcome = ap._read_yaml_or_empty(goal / ap.OUTCOME_FILENAME)
    state = outcome.get("closing_state")
    if not state:
        return {"ok": False, "pruned": None,
                "error": "no outcome.yaml closing_state - goal is still open"}
    if not ap.seal_is_intact(outcome):
        return {"ok": False, "pruned": None,
                "error": "outcome.yaml seal invalid: payload_sha256 does not "
                         "reproduce - refusing to act on a tampered outcome"}
    if state == ap.KILLED_CLOSING_STATE:
        return {"ok": False, "pruned": False,
                "error": "goal closed as killed: lane retained for forensics"}
    if state not in ap.PRUNE_ELIGIBLE_CLOSING_STATES:
        # Fail-closed allowlist (consult Q7 forensics invariant): a typo,
        # casing drift, or future closing state must never permit the
        # destructive prune by default.
        return {"ok": False, "pruned": False,
                "error": f"closing_state {state!r} is not prune-eligible "
                         f"(allowlist: "
                         f"{sorted(ap.PRUNE_ELIGIBLE_CLOSING_STATES)}); "
                         "refusing destructive prune"}
    pruned = False
    if prune_lane:
        try:
            lane_mod.remove_lane(root, goal)
            pruned = True
        except lane_mod.LaneError as exc:
            return {"ok": False, "pruned": False, "error": str(exc)}
    return {"ok": True, "pruned": pruned, "error": None}


def register(registry) -> None:
    registry.register(APPROVE_SCHEMA["name"], APPROVE_SCHEMA, handle_approve_spec)
    registry.register(CLEANUP_SCHEMA["name"], CLEANUP_SCHEMA, handle_cleanup)
