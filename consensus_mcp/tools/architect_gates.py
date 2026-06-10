"""architect.approve_spec + architect.cleanup - thin human gates (workflow D).

Consult Q5: a DEDICATED spec seal, never consensus_approve (whose >=2
non-claude-reviewer + converged-plan preconditions architect-build cannot
meet at spec time). Mirrors the delivery_gate multi-tool module pattern.
"""
from __future__ import annotations

import subprocess
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


def _repo_root(repo_root: str | None, goal: Path) -> Path:
    if repo_root:
        return Path(repo_root)
    # goal dir is <root>/.consensus/architect/<id>
    return goal.parent.parent.parent


def handle_approve_spec(
    goal_dir: str, approver: str, repo_root: str | None = None
) -> dict:
    goal = Path(goal_dir)
    root = _repo_root(repo_root, goal)
    err = {"ok": False, "spec_sha256": None, "base_sha": None}
    spec_file = ap.latest_spec_path(goal)
    spec = ap._read_yaml_or_empty(spec_file)
    if not spec.get("payload_sha256"):
        return dict(err, error=f"no sealed spec at {spec_file}")
    if (goal / ap.SPEC_APPROVAL_FILENAME).exists():
        return dict(err, error="spec already approved; the architect owns "
                               "spec evolution between gates (spec-rev-N)")
    try:
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root), check=True,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
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
    outcome = ap._read_yaml_or_empty(goal / ap.OUTCOME_FILENAME)
    state = outcome.get("closing_state")
    if not state:
        return {"ok": False, "pruned": None,
                "error": "no outcome.yaml closing_state - goal is still open"}
    if state == "killed":
        return {"ok": False, "pruned": False,
                "error": "goal closed as killed: lane retained for forensics"}
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
