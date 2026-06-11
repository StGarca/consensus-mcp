"""Tests for the architect.loop_step supervisor (workflow D)."""
from __future__ import annotations

from pathlib import Path

import consensus_mcp.config as cfg
from consensus_mcp.contributors.base import FakeAlwaysApprove
from consensus_mcp.workflow_engine import WorkflowEngine


def _abd_engine_config():
    c = cfg.default_config()
    c["workflow"]["mode"] = cfg.WORKFLOW_ARCHITECT_BUILD
    c["contributors"]["enabled"] = ["claude", "codex"]
    c["roles"] = {"architect": "claude", "builder": "codex", "reviewer": "codex"}
    return cfg.normalize(c)


def test_run_iteration_refuses_architect_build(tmp_path: Path):
    config = _abd_engine_config()
    engine = WorkflowEngine(
        config=config,
        adapters={"claude": FakeAlwaysApprove(), "codex": FakeAlwaysApprove()},
        repo_root=tmp_path,
    )
    goal = tmp_path / "goal_packet.yaml"
    goal.write_text("pilot_id: x\n", encoding="utf-8")
    target = tmp_path / "problem.md"
    target.write_text("problem\n", encoding="utf-8")
    outcome = engine.run_iteration(tmp_path / "iter", goal, target)
    assert outcome.error is not None
    assert "architect-build" in outcome.error
    assert "loop_step" in outcome.error


import json
import os
import subprocess
import sys

import pytest
import yaml

from consensus_mcp import _architect_lane as lane_mod
from consensus_mcp import _architect_paths as ap
from consensus_mcp import _dispatch_builder as db
from consensus_mcp.tools import architect_gates as gates
from consensus_mcp.tools import architect_loop_step as als


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"], ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    (repo / ".gitignore").write_text(".consensus/architect/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "ignore goal dirs"], cwd=repo, check=True,
        capture_output=True,
    )
    return repo


def _write_config(repo: Path, verification: str = "", max_cycles: int = 3,
                  enabled: list | None = None, roles: dict | None = None,
                  profiles: dict | None = None, wall: int = 0) -> Path:
    cdir = repo / ".consensus"
    cdir.mkdir(exist_ok=True)
    cfg_path = cdir / "config.yaml"
    contributors: dict = {"enabled": enabled or ["claude", "codex"]}
    if profiles:
        contributors["profiles"] = profiles
    cfg_path.write_text(yaml.safe_dump({
        "workflow": {"mode": "architect-build"},
        "contributors": contributors,
        "roles": roles or {
            "architect": "claude", "builder": "codex", "reviewer": "codex"
        },
        "architect_loop": {
            "max_cycles": max_cycles,
            "verification": verification,
            "lane_branch_prefix": "arch-lane/",
            "max_wall_clock_minutes": wall,
        },
    }), encoding="utf-8")
    return cfg_path


def _new_goal(repo: Path, goal_id: str = "g1") -> Path:
    goal = ap.goal_dir(repo, goal_id)
    goal.mkdir(parents=True)
    (goal / ap.PROBLEM_FILENAME).write_text("solve X\n", encoding="utf-8")
    return goal


def _step(goal: Path, repo: Path, **kw):
    return als.handle(goal_dir=str(goal), config_path=str(repo / ".consensus" / "config.yaml"), **kw)


def _fake_builder(monkeypatch, lane_effect=None, pushback=None):
    def fake(*, repo_root, lane, prompt, codex_bin="codex", timeout_seconds=0):
        if lane_effect:
            lane_effect(Path(lane))
        return {"summary": "did work", "pushback": pushback, "notes": ""}
    monkeypatch.setattr(als, "_dispatch_builder_fn", fake)


def test_no_spec_is_needs_spec(tmp_path: Path):
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    r = _step(goal, repo)
    assert r["ok"] and r["state"] == "needs_spec"
    assert "spec.yaml" in r["next_action"]


def test_spec_without_approval_awaits_gate(tmp_path: Path):
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    r = _step(goal, repo)
    assert r["state"] == "awaiting_spec_approval"
    assert "approve_spec" in r["next_action"]


def test_full_green_cycle_to_delivery_gate(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))

    _fake_builder(
        monkeypatch,
        lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"),
    )
    r = _step(goal, repo)
    assert r["state"] == "built"  # action taken this step
    build = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).read_text(encoding="utf-8")
    )
    assert len(build["lane_head_sha"]) == 40

    # verification configured empty -> skipped; next is review
    r = _step(goal, repo)
    assert r["state"] == "needs_review"
    ap.seal_artifact(
        ap.cycle_dir(goal, 1) / ap.REVIEW_FILENAME,
        {"verdict": "lgtm", "lane_head_sha": build["lane_head_sha"]},
    )
    r = _step(goal, repo)
    assert r["state"] == "needs_ruling"
    ap.seal_artifact(
        ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
        {"disposition": "accept", "lane_head_sha": build["lane_head_sha"]},
    )
    r = _step(goal, repo)
    assert r["state"] == "awaiting_delivery_approval"
    assert (goal / ap.HANDOFF_FILENAME).exists()
    # state 14 binds the exact lane HEAD + base sha for the delivery mint
    recheck = [a for a in r["actions_taken"]
               if a["action"] == "delivery_integrity_recheck"]
    assert recheck
    assert recheck[0]["lane_head_sha"] == build["lane_head_sha"]
    approval = yaml.safe_load(
        (goal / ap.SPEC_APPROVAL_FILENAME).read_text(encoding="utf-8")
    )
    assert recheck[0]["base_sha"] == approval["base_sha"]


def test_red_verification_seals_mechanical_revise(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path)
    _write_config(repo, verification="false")  # /usr/bin/false -> RED
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)              # build
    r = _step(goal, repo)          # verification runs RED
    assert r["state"] == "verification_red"
    ruling = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.RULING_FILENAME).read_text(encoding="utf-8")
    )
    assert ruling["disposition"] == "revise"
    assert ruling["reason"] == "verification_failed"
    assert ruling["mechanical"] is True
    # loop advanced: next step starts cycle 2 build
    r = _step(goal, repo)
    assert r["state"] == "built" and r["cycle"] == 2


def test_pushback_raised_routes_to_architect(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, pushback="spec is contradictory")
    _step(goal, repo)              # build returns pushback
    r = _step(goal, repo)
    assert r["state"] == "pushback_raised"
    assert "ruling" in r["next_action"]


def test_max_cycles_stop_rule(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    # fabricate 3 closed revise cycles (max_cycles=3)
    for n in (1, 2, 3):
        c = ap.cycle_dir(goal, n); c.mkdir(parents=True, exist_ok=True)
        ap.seal_artifact(c / ap.BUILD_RESULT_FILENAME, {"summary": "w", "pushback": None, "lane_head_sha": "0" * 40})
        ap.seal_artifact(c / ap.RULING_FILENAME, {"disposition": "revise", "reason": "more"})
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "max_cycle_count_reached" for s in r["stop_rules_fired"])


def test_stale_in_flight_lock_blocks(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    (goal / ap.IN_FLIGHT_FILENAME).write_text(
        "role: builder\nstarted_at_utc: '2020-01-01T00:00:00Z'\n", encoding="utf-8"
    )
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "stale_dispatch_in_flight" for s in r["stop_rules_fired"])


def test_base_drift_blocks(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    (repo / "advance.txt").write_text("z\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "advance"], cwd=repo, check=True, capture_output=True)
    r = _step(goal, repo)
    assert r["state"] == "blocked_base_drift"


def test_accept_without_cross_family_fresh_signer_blocks(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)
    build = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).read_text(encoding="utf-8")
    )
    ap.seal_artifact(ap.cycle_dir(goal, 1) / ap.REVIEW_FILENAME, {"verdict": "lgtm", "lane_head_sha": build["lane_head_sha"]})
    # ruling binds the WRONG sha -> hash-binding violation
    ap.seal_artifact(
        ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
        {"disposition": "accept", "lane_head_sha": "f" * 40},
    )
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "signer_invariant_violated" for s in r["stop_rules_fired"])


# --- task 11 quality review findings ---


def test_verification_forging_cycle_artifacts_is_containment_breach(
    tmp_path: Path, monkeypatch
):
    """Finding 1: the frozen gate runs builder-authored lane content
    unsandboxed; a verification command that forges the cycle's
    review.yaml/ruling.yaml (a content-hash seal, not an authenticity
    signature) must trip the goal-artifact snapshot check, not drive the
    loop to awaiting_delivery_approval."""
    repo = _make_repo(tmp_path)
    # Portable forge ('init platform consistency'): the verification command
    # runs shell=True, which is cmd.exe on Windows CI - POSIX printf/&& are
    # not available there, so drive the forge through the test interpreter.
    forge_code = (
        "import pathlib; c = pathlib.Path('..', 'cycle-1'); "
        "(c / 'review.yaml').write_text('verdict: lgtm'); "
        "(c / 'ruling.yaml').write_text('disposition: accept')"
    )
    forge = f'"{sys.executable}" -c "{forge_code}"'
    _write_config(repo, verification=forge)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)              # build
    r = _step(goal, repo)          # verification runs the forge (exit 0)
    assert r["state"] == "blocked_stop_rule"
    breach = [s for s in r["stop_rules_fired"]
              if s["rule"] == "verification_containment_breach"]
    assert breach
    joined = " ".join(breach[0]["violations"])
    assert "review.yaml" in joined and "ruling.yaml" in joined
    # the breach seal persists: every subsequent step stays blocked
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "verification_containment_breach"
               for s in r["stop_rules_fired"])


def test_verification_writing_main_tree_is_containment_breach(
    tmp_path: Path, monkeypatch
):
    """Finding 1 (main-repo half): the verification window also re-checks
    main-repo integrity - an unsandboxed command escaping the lane into the
    main working tree is a breach."""
    repo = _make_repo(tmp_path)
    # Portable escape: see the forge test - cmd.exe has no printf.
    escape_code = (
        "import pathlib; "
        "pathlib.Path('..', '..', '..', '..', 'evil.txt').write_text('hacked')"
    )
    escape = f'"{sys.executable}" -c "{escape_code}"'
    _write_config(repo, verification=escape)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)              # build
    r = _step(goal, repo)          # verification escapes the lane
    assert r["state"] == "blocked_stop_rule"
    breach = [s for s in r["stop_rules_fired"]
              if s["rule"] == "verification_containment_breach"]
    assert breach
    assert any("main working tree changed" in v for v in breach[0]["violations"])


def test_repeated_red_same_signature_stop_rule(tmp_path: Path):
    """Finding 2: a RED verification seals a mechanical revise in the SAME
    step, closing the cycle - so at stop-rule time `cycle` is the next OPEN
    cycle. The window must scan the last 3 CLOSED cycles."""
    repo = _make_repo(tmp_path); _write_config(repo, max_cycles=8)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    sig = "a" * 64
    for n in (1, 2, 3):
        c = ap.cycle_dir(goal, n); c.mkdir(parents=True, exist_ok=True)
        ap.seal_artifact(c / ap.BUILD_RESULT_FILENAME, {"summary": "w", "pushback": None, "lane_head_sha": "0" * 40})
        ap.seal_artifact(c / ap.VERIFICATION_FILENAME, {"command": "false", "passed": False, "signature": sig, "output_tail": ""})
        ap.seal_artifact(c / ap.RULING_FILENAME, {"disposition": "revise", "reason": "verification_failed", "mechanical": True})
    r = _step(goal, repo)
    assert r["cycle"] == 4
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "repeated_verification_failure_same_signature"
               for s in r["stop_rules_fired"])


def test_two_red_cycles_do_not_fire_repeated_signature_rule(tmp_path: Path):
    """Finding 2 negative window: 2 identical RED closed cycles are below
    the 3-cycle threshold - the loop proceeds to the next build."""
    repo = _make_repo(tmp_path); _write_config(repo, max_cycles=8)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    sig = "a" * 64
    for n in (1, 2):
        c = ap.cycle_dir(goal, n); c.mkdir(parents=True, exist_ok=True)
        ap.seal_artifact(c / ap.BUILD_RESULT_FILENAME, {"summary": "w", "pushback": None, "lane_head_sha": "0" * 40})
        ap.seal_artifact(c / ap.VERIFICATION_FILENAME, {"command": "false", "passed": False, "signature": sig, "output_tail": ""})
        ap.seal_artifact(c / ap.RULING_FILENAME, {"disposition": "revise", "reason": "verification_failed", "mechanical": True})
    r = _step(goal, repo, auto_dispatch=False)
    assert r["state"] == "needs_build" and r["cycle"] == 3
    assert r["stop_rules_fired"] == []


def test_overrule_pushback_advances_to_next_cycle_build(tmp_path: Path, monkeypatch):
    """Finding 3: an overrule ruling on builder pushback closes the cycle
    (like revise) and the next step re-dispatches the builder with the
    architect's rationale as feedback - no needs_ruling livelock."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, pushback="spec is contradictory")
    _step(goal, repo)              # build returns pushback
    r = _step(goal, repo)
    assert r["state"] == "pushback_raised"
    ap.seal_artifact(
        ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
        {"disposition": "overrule", "reason": "spec stands; build it"},
    )
    seen = {}

    def fake(*, repo_root, lane, prompt, codex_bin="codex", timeout_seconds=0):
        seen["prompt"] = prompt
        (Path(lane) / "f.py").write_text("a=1\n", encoding="utf-8")
        return {"summary": "did work", "pushback": None, "notes": ""}

    monkeypatch.setattr(als, "_dispatch_builder_fn", fake)
    r = _step(goal, repo)
    assert r["state"] == "built" and r["cycle"] == 2
    assert "spec stands; build it" in seen["prompt"]


def test_misshaped_goal_dir_is_goal_invalid(tmp_path: Path):
    """Finding 4: loop_step must use the VALIDATED root derivation
    (_derive_repo_root), never blind parent-hopping - a mis-shaped goal_dir
    surfaces as goal_invalid instead of anchoring git at a garbage root."""
    repo = _make_repo(tmp_path)
    cfg_path = _write_config(repo)
    rogue = tmp_path / "elsewhere" / "g1"
    rogue.mkdir(parents=True)
    (rogue / ap.PROBLEM_FILENAME).write_text("solve X\n", encoding="utf-8")
    # explicit config_path: config loads, but the root is still underivable
    r = als.handle(goal_dir=str(rogue), config_path=str(cfg_path))
    assert not r["ok"] and r["state"] == "goal_invalid"
    assert "cannot derive repo root" in r["error"]
    # no config_path: same refusal BEFORE any blind <goal>/../../.. config probe
    r = als.handle(goal_dir=str(rogue))
    assert not r["ok"] and r["state"] == "goal_invalid"
    assert "cannot derive repo root" in r["error"]


def test_same_family_overlay_reviewer_routes_signer_to_architect(
    tmp_path: Path, monkeypatch
):
    """Quality finding 1: config validation PERMITS a reviewer whose NAME
    differs from the builder but whose profile family: overlay matches it
    (the architect supplies the cross-family floor). The gate-eligible
    signer must be resolved by FAMILY over the merged profiles - the
    architect's RULING is then the only true cross-family attestation, so
    an accept whose ruling is not hash-bound blocks even when the
    same-family reviewer binds the build perfectly."""
    repo = _make_repo(tmp_path)
    _write_config(
        repo,
        enabled=["claude", "codex", "gemini"],
        roles={"architect": "claude", "builder": "codex", "reviewer": "gemini"},
        profiles={
            "gemini": {
                "name": "gemini",
                "kind": "cli_reviewer",
                "family": "codex",  # same family as the builder
                "detect": {"command": "gemini --version"},
                "invoke": {"transport": "stdin"},
                "output": {"format": "json"},
            },
        },
    )
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)
    # quality finding 3: the supervisor threads the MERGED profiles into
    # write_handoff, so the consult-Q2 transparency NOTE reflects the
    # overlay family, not builtin-only data.
    handoff = (goal / ap.HANDOFF_FILENAME).read_text(encoding="utf-8")
    assert "ONLY cross-family signer" in handoff
    build = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).read_text(encoding="utf-8")
    )
    # the same-family reviewer binds the build PERFECTLY - it still must
    # not satisfy the cross-family gate
    ap.seal_artifact(
        ap.cycle_dir(goal, 1) / ap.REVIEW_FILENAME,
        {"verdict": "lgtm", "lane_head_sha": build["lane_head_sha"]},
    )
    # the architect's ruling is NOT hash-bound -> accept must block
    ap.seal_artifact(
        ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
        {"disposition": "accept"},
    )
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "signer_invariant_violated"
               for s in r["stop_rules_fired"])
    # a hash-bound architect ruling satisfies the gate
    ap.seal_artifact(
        ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
        {"disposition": "accept", "lane_head_sha": build["lane_head_sha"]},
    )
    r = _step(goal, repo)
    assert r["state"] == "awaiting_delivery_approval"
    handoff = (goal / ap.HANDOFF_FILENAME).read_text(encoding="utf-8")
    assert "ONLY cross-family signer" in handoff


def test_containment_breach_filename_is_single_sourced():
    """Quality finding 4: the breach artifact name is _architect_paths-owned
    ('inline f-strings of artifact names are forbidden') - the supervisor
    imports the constant and the layout docstring lists the file."""
    assert ap.CONTAINMENT_BREACH_FILENAME == "containment-breach.yaml"
    assert ap.CONTAINMENT_BREACH_FILENAME in (ap.__doc__ or "")
    source = Path(als.__file__).read_text(encoding="utf-8")
    assert "containment-breach.yaml" not in source


def test_kill_seals_outcome(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)
    build = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).read_text(encoding="utf-8")
    )
    ap.seal_artifact(ap.cycle_dir(goal, 1) / ap.REVIEW_FILENAME, {"verdict": "bad", "lane_head_sha": build["lane_head_sha"]})
    ap.seal_artifact(ap.cycle_dir(goal, 1) / ap.RULING_FILENAME, {"disposition": "kill", "lane_head_sha": build["lane_head_sha"]})
    r = _step(goal, repo)
    assert r["state"] == "killed"
    outcome = yaml.safe_load((goal / ap.OUTCOME_FILENAME).read_text(encoding="utf-8"))
    assert outcome["closing_state"] == "killed"
    assert ap.lane_dir(goal).exists()  # forensics


def test_pushback_accept_ruling_is_blocked(tmp_path: Path, monkeypatch):
    # Final-quality finding: an accept ruling on a PUSHBACK cycle must never
    # reach delivery - the cycle has no verification and no review.
    repo = _make_repo(tmp_path)
    _write_config(repo, verification="false")  # would be RED if it ever ran
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, pushback="spec is contradictory")
    _step(goal, repo)  # build returns pushback
    assert _step(goal, repo)["state"] == "pushback_raised"
    build = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).read_text(encoding="utf-8")
    )
    ap.seal_artifact(
        ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
        {"disposition": "accept", "lane_head_sha": build["lane_head_sha"]},
    )
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "pushback_accept_forbidden" for s in r["stop_rules_fired"])
    # neither gate artifact exists - the bypass would have been real
    assert not (ap.cycle_dir(goal, 1) / ap.VERIFICATION_FILENAME).exists()
    assert not (ap.cycle_dir(goal, 1) / ap.REVIEW_FILENAME).exists()


def test_verification_timeout_is_red_not_raise(tmp_path: Path, monkeypatch):
    # Timeout must terminate the PROCESS TREE, seal a RED verification +
    # mechanical revise, and never raise.
    repo = _make_repo(tmp_path)
    _write_config(repo, verification="sleep 30")
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)  # build
    monkeypatch.setattr(als, "_VERIFICATION_TIMEOUT_SECONDS", 1)
    r = _step(goal, repo)
    assert r["state"] == "verification_red"
    v = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.VERIFICATION_FILENAME).read_text(encoding="utf-8")
    )
    assert v["passed"] is False
    assert "timed out" in v["output_tail"]


def test_verification_undecodable_output_is_replaced(tmp_path: Path, monkeypatch):
    # cp1252/utf-8 hostile bytes in verification output must not raise
    # UnicodeDecodeError through the never-raises boundary. Driven through
    # the test interpreter, NOT printf+';' (shell=True is cmd.exe on Windows
    # CI, where ';' is no separator and Git-for-Windows printf.exe would
    # swallow 'exit 1' as arguments and exit 0 - the forge tests' precedent).
    repo = _make_repo(tmp_path)
    bad_code = (
        "import os, sys; "
        "os.write(sys.stdout.fileno(), b'\\xff\\xfe bad bytes'); sys.exit(1)"
    )
    _write_config(repo, verification=f'"{sys.executable}" -c "{bad_code}"')
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)  # build
    r = _step(goal, repo)
    assert r["state"] == "verification_red"  # exit 1 -> RED, decoded with replacement
    v = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.VERIFICATION_FILENAME).read_text(encoding="utf-8")
    )
    assert v["passed"] is False
    assert "\ufffd" in v["output_tail"]  # the hostile bytes really arrived


def test_verification_machinery_failure_blocks(tmp_path: Path, monkeypatch):
    # A LaneError from the snapshot machinery mid-verification surfaces as
    # blocked_stop_rule, never as an unhandled exception.
    repo = _make_repo(tmp_path)
    _write_config(repo, verification="true")
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)  # build (uses the real snapshot machinery)

    def boom(*a, **k):
        raise lane_mod.LaneError("git exploded mid-verification")
    monkeypatch.setattr(als.lane_mod, "snapshot_main_integrity", boom)
    r = _step(goal, repo)
    assert r["ok"] is False
    assert r["state"] == "blocked_stop_rule"
    assert any(
        s["rule"] == "verification_machinery_failed" for s in r["stop_rules_fired"]
    )


# --- final-review findings (2026-06-10) ---


def test_tampered_spec_blocks_build(tmp_path: Path, monkeypatch):
    """Finding: the build consumes the latest spec at point of use - a
    spec whose body was edited after sealing (payload_sha256 no longer
    reproduces) must never drive the builder."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    spec = yaml.safe_load(ap.spec_path(goal).read_text(encoding="utf-8"))
    spec["body"] = "EVIL: do something else entirely"
    ap.spec_path(goal).write_text(yaml.safe_dump(spec), encoding="utf-8")
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "spec_seal_invalid" for s in r["stop_rules_fired"])
    # refused BEFORE any lane work or lock litter
    assert not ap.lane_dir(goal).exists()
    assert not (goal / ap.IN_FLIGHT_FILENAME).exists()


def test_accept_without_review_blocks(tmp_path: Path, monkeypatch):
    """Finding: in the canonical cheap config (reviewer==builder family) the
    architect's ruling is the cross-family signer, but the v1-REQUIRED
    reviewer must still have reviewed: an accept with NO review.yaml must
    never reach awaiting_delivery_approval."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)
    build = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).read_text(encoding="utf-8")
    )
    # a perfectly bound accept ruling, but NO review.yaml was ever sealed
    ap.seal_artifact(
        ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
        {"disposition": "accept", "lane_head_sha": build["lane_head_sha"]},
    )
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    stops = [s for s in r["stop_rules_fired"]
             if s["rule"] == "signer_invariant_violated"]
    assert stops
    assert any("review.yaml missing" in v for v in stops[0]["violations"])


def test_loop_step_ignores_git_dir_env(tmp_path: Path, monkeypatch):
    """Finding: the base-drift HEAD read must go through the scrubbed lane
    git - a GIT_DIR leaked from a hook context would make rev-parse read a
    DIFFERENT repository's HEAD and mis-fire blocked_base_drift."""
    repo = _make_repo(tmp_path); _write_config(repo)
    other = tmp_path / "other"
    other.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)
    (other / "y.txt").write_text("y\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=other, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "other"], cwd=other, check=True,
                   capture_output=True)
    repo_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True).stdout.strip()
    other_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=other, check=True,
        capture_output=True, text=True).stdout.strip()
    assert repo_head != other_head
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    r = _step(goal, repo, auto_dispatch=False)
    assert r["state"] == "needs_build"  # NOT a false blocked_base_drift


def test_verification_env_scrubs_credentials(tmp_path: Path, monkeypatch):
    """Finding: the frozen gate executes builder-authored lane code; it must
    not inherit the supervisor's AI-provider credentials (the builder
    dispatch scrubs them - the verification run must too)."""
    repo = _make_repo(tmp_path)
    leak_code = (
        "import os, sys; "
        "sys.stdout.write(os.environ.get('OPENAI_API_KEY', 'SCRUBBED') + '/' "
        "+ os.environ.get('GEMINI_API_KEY', 'SCRUBBED'))"
    )
    _write_config(repo, verification=f'"{sys.executable}" -c "{leak_code}"')
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    monkeypatch.setenv("OPENAI_API_KEY", "sekret-codex")
    monkeypatch.setenv("GEMINI_API_KEY", "sekret-gemini")
    _step(goal, repo)  # build
    r = _step(goal, repo)
    assert r["state"] == "needs_review"  # gate ran green
    v = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.VERIFICATION_FILENAME).read_text(encoding="utf-8")
    )
    assert v["output_tail"] == "SCRUBBED/SCRUBBED"


def test_accept_recheck_blocks_on_main_drift(tmp_path: Path, monkeypatch):
    """Finding: spec 6.5 - the delivery gate independently re-checks the
    integrity snapshot; a main-tree delta between build and accept must
    block instead of reaching awaiting_delivery_approval on stale data."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)
    build = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).read_text(encoding="utf-8")
    )
    ap.seal_artifact(ap.cycle_dir(goal, 1) / ap.REVIEW_FILENAME,
                     {"verdict": "lgtm", "lane_head_sha": build["lane_head_sha"]})
    ap.seal_artifact(ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
                     {"disposition": "accept", "lane_head_sha": build["lane_head_sha"]})
    (repo / "drift.txt").write_text("appeared after the build\n", encoding="utf-8")
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    stops = [s for s in r["stop_rules_fired"]
             if s["rule"] == "delivery_integrity_recheck_failed"]
    assert stops
    assert any("main working tree changed" in v for v in stops[0]["violations"])


def test_accept_recheck_blocks_on_lane_head_move(tmp_path: Path, monkeypatch):
    """Finding: state 14 binds the exact lane HEAD - a lane commit landing
    AFTER the build seal (what the signer judged) must block delivery."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)
    build = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).read_text(encoding="utf-8")
    )
    ap.seal_artifact(ap.cycle_dir(goal, 1) / ap.REVIEW_FILENAME,
                     {"verdict": "lgtm", "lane_head_sha": build["lane_head_sha"]})
    ap.seal_artifact(ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
                     {"disposition": "accept", "lane_head_sha": build["lane_head_sha"]})
    lane = ap.lane_dir(goal)
    (lane / "sneak.txt").write_text("post-review change\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=lane, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "sneak"], cwd=lane, check=True,
                   capture_output=True)
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    stops = [s for s in r["stop_rules_fired"]
             if s["rule"] == "delivery_integrity_recheck_failed"]
    assert stops
    assert any("lane HEAD moved" in v for v in stops[0]["violations"])


def test_red_verification_resume_reseals_lost_mechanical_revise(
    tmp_path: Path, monkeypatch
):
    """Finding (CONFIRMED repro): verification.yaml and the mechanical revise
    ruling are two separate seals - an interrupt between them must not make
    the next step route the RED build to review. The transition is gated on
    verification CONTENT (passed), not file existence."""
    repo = _make_repo(tmp_path)
    _write_config(repo, verification="false")
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)                       # build
    r = _step(goal, repo)                   # verification RED + revise sealed
    assert r["state"] == "verification_red"
    # simulate the interrupt between the two seals: the ruling write is lost
    (ap.cycle_dir(goal, 1) / ap.RULING_FILENAME).unlink()
    r = _step(goal, repo)
    assert r["state"] == "verification_red"  # NOT needs_review
    ruling = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.RULING_FILENAME).read_text(encoding="utf-8")
    )
    assert ruling["disposition"] == "revise"
    assert ruling["reason"] == "verification_failed"
    assert ruling["mechanical"] is True
    # the loop then advances to cycle 2 like any RED cycle
    r = _step(goal, repo)
    assert r["state"] == "built" and r["cycle"] == 2


def test_in_flight_lock_is_test_and_set(tmp_path: Path, monkeypatch):
    """Finding: the in-flight lock must be an O_EXCL test-and-set, not
    read-then-act - a lock file that defeats the read check (empty YAML)
    must still refuse the dispatch instead of double-dispatching."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    # an EMPTY lock file: _read_yaml_or_empty -> {} passes the read check,
    # only the O_EXCL create can refuse it
    (goal / ap.IN_FLIGHT_FILENAME).write_text("", encoding="utf-8")
    r = _step(goal, repo)
    assert r["state"] == "dispatch_in_flight"
    assert not ap.lane_dir(goal).exists()           # no lane work happened
    assert not (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).exists()
    # the loser must NOT have clobbered the existing lock
    assert (goal / ap.IN_FLIGHT_FILENAME).read_text(encoding="utf-8") == ""


def test_fresh_in_flight_lock_reports_dispatch_in_flight(tmp_path: Path):
    """Docs state table: a FRESH lock (within TTL) is the wait state, not a
    stop rule (only the stale path had coverage)."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    ap.seal_artifact(
        goal / ap.IN_FLIGHT_FILENAME,
        {"role": "builder", "cycle": 1,
         "started_at_utc": als._utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    r = _step(goal, repo)
    assert r["state"] == "dispatch_in_flight"
    assert r["stop_rules_fired"] == []
    assert "again later" in r["next_action"]


def test_wall_clock_budget_exceeded(tmp_path: Path):
    """Docs stop-rule table: wall_clock_budget_exceeded had no coverage."""
    repo = _make_repo(tmp_path); _write_config(repo, wall=5)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    approval = yaml.safe_load(
        (goal / ap.SPEC_APPROVAL_FILENAME).read_text(encoding="utf-8")
    )
    approval["sealed_at_utc"] = "2020-01-01T00:00:00Z"
    (goal / ap.SPEC_APPROVAL_FILENAME).write_text(
        yaml.safe_dump(approval), encoding="utf-8"
    )
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "wall_clock_budget_exceeded"
               for s in r["stop_rules_fired"])


def test_delivered_outcome_reports_closed(tmp_path: Path):
    """Docs state table: a delivered outcome.yaml is the terminal 'closed'
    state (only 'killed' had coverage)."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(goal / ap.OUTCOME_FILENAME,
                     {"closing_state": "delivered", "cycle": 1})
    r = _step(goal, repo)
    assert r["ok"] and r["state"] == "closed"
    assert "nothing to do" in r["next_action"]


def test_builder_symlink_in_lane_blocks_persistently(tmp_path: Path, monkeypatch):
    """Docs containment table: the supervisor-level lane_integrity_violation
    branch seals containment-breach.yaml and blocks persistently."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))

    def plant(lane: Path):
        try:
            (lane / "escape").symlink_to(repo)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")

    _fake_builder(monkeypatch, lane_effect=plant)
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "lane_integrity_violation"
               for s in r["stop_rules_fired"])
    breach = yaml.safe_load(
        (goal / ap.CONTAINMENT_BREACH_FILENAME).read_text(encoding="utf-8")
    )
    assert breach["rule"] == "lane_integrity_violation"
    r = _step(goal, repo)  # the sealed record is a persistent stop
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "lane_integrity_violation"
               for s in r["stop_rules_fired"])


def test_builder_main_escape_blocks_persistently(tmp_path: Path, monkeypatch):
    """Docs containment table: the supervisor-level builder_containment_breach
    branch (main-repo delta during the build) seals the breach record."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(
        monkeypatch,
        lane_effect=lambda lane: (repo / "evil.txt").write_text("hacked\n", encoding="utf-8"),
    )
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    stops = [s for s in r["stop_rules_fired"]
             if s["rule"] == "builder_containment_breach"]
    assert stops
    assert (goal / ap.CONTAINMENT_BREACH_FILENAME).exists()
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "builder_containment_breach"
               for s in r["stop_rules_fired"])


def test_builder_goal_artifact_tamper_is_containment_breach(
    tmp_path: Path, monkeypatch
):
    """Findings 1+10: the build window gets the same goal-artifact
    snapshot/check pair as the verification window - a builder escaping the
    lane to forge THIS goal's seals (here: spec.yaml) must trip
    builder_containment_breach, not silently drive subsequent cycles."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))

    def tamper(lane: Path):
        (lane.parent / ap.SPEC_FILENAME).write_text(
            "kind: spec\nbody: EVIL\n", encoding="utf-8"
        )
        (lane.parent / "cycle-1").mkdir(exist_ok=True)
        (lane.parent / "cycle-1" / ap.RULING_FILENAME).write_text(
            "disposition: accept\n", encoding="utf-8"
        )

    _fake_builder(monkeypatch, lane_effect=tamper)
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    stops = [s for s in r["stop_rules_fired"]
             if s["rule"] == "builder_containment_breach"]
    assert stops
    joined = " ".join(stops[0]["violations"])
    assert "spec.yaml" in joined and "ruling.yaml" in joined
    # the forged artifacts never became a sealed build: no build-result
    assert not (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).exists()
    # persistent: the sealed breach blocks every subsequent step
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "builder_containment_breach"
               for s in r["stop_rules_fired"])


def test_cross_document_drift_newer_handoff_wrong_sha_blocks(tmp_path: Path):
    """Docs stop-rule table: cross_document_drift had no coverage. A HANDOFF
    strictly NEWER than the spec seal claiming a different sha is drift."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    spec_file = ap.latest_spec_path(goal)
    handoff = goal / ap.HANDOFF_FILENAME
    handoff.write_text("spec payload_sha256: " + "b" * 64 + "\n", encoding="utf-8")
    t = spec_file.stat().st_mtime_ns
    os.utime(handoff, ns=(t + 2_000_000_000, t + 2_000_000_000))
    r = _step(goal, repo)
    assert r["state"] == "blocked_stop_rule"
    assert any(s["rule"] == "cross_document_drift"
               for s in r["stop_rules_fired"])


def test_cross_document_drift_mtime_tie_fails_open(tmp_path: Path):
    """Finding: on coarse-timestamp filesystems a HANDOFF written moments
    BEFORE the spec seal can TIE the spec mtime; a tie cannot distinguish
    pending-regeneration from tamper, so it must fail open (strict >)."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    spec_file = ap.latest_spec_path(goal)
    handoff = goal / ap.HANDOFF_FILENAME
    handoff.write_text("spec payload_sha256: " + "b" * 64 + "\n", encoding="utf-8")
    t = spec_file.stat().st_mtime_ns
    os.utime(handoff, ns=(t, t))
    r = _step(goal, repo)
    assert r["state"] == "awaiting_spec_approval"
    assert r["stop_rules_fired"] == []


def test_verification_signature_stable_across_volatile_output(
    tmp_path: Path, monkeypatch
):
    """Finding: the repeated-RED stop rule keys on signature EQUALITY, but a
    raw stdout+stderr hash differs every run for e.g. pytest's wall-clock
    line. Two REAL runs of a failing command with volatile output (hex id +
    duration) must produce the SAME signature over DIFFERENT tails."""
    repo = _make_repo(tmp_path)
    noisy_code = (
        "import random, sys; "
        "sys.stdout.write('1 failed at 0x' + format(random.getrandbits(64), 'x')"
        " + ' in 0.' + str(random.randint(0, 99)) + 's'); sys.exit(1)"
    )
    _write_config(repo, verification=f'"{sys.executable}" -c "{noisy_code}"',
                  max_cycles=8)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)                                   # build cycle 1
    assert _step(goal, repo)["state"] == "verification_red"
    _step(goal, repo)                                   # build cycle 2
    assert _step(goal, repo)["state"] == "verification_red"
    v1 = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.VERIFICATION_FILENAME).read_text(encoding="utf-8")
    )
    v2 = yaml.safe_load(
        (ap.cycle_dir(goal, 2) / ap.VERIFICATION_FILENAME).read_text(encoding="utf-8")
    )
    assert v1["output_tail"] != v2["output_tail"]       # genuinely volatile
    assert v1["signature"] == v2["signature"]           # normalized stable


def test_cli_main_step_and_exit_codes(tmp_path: Path, capsys):
    """Docs/pyproject surface: consensus-mcp-architect main() - arg parsing,
    --no-dispatch wiring, approve-spec wiring, ok -> exit-code mapping."""
    repo = _make_repo(tmp_path); _write_config(repo)
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    rc = als.main(["approve-spec", "--goal-dir", str(goal),
                   "--approver", "op", "--repo-root", str(repo)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True
    assert (goal / ap.SPEC_APPROVAL_FILENAME).exists()
    rc = als.main(["step", "--goal-dir", str(goal),
                   "--config", str(repo / ".consensus" / "config.yaml"),
                   "--no-dispatch"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["state"] == "needs_build"  # --no-dispatch reported, not dispatched
    rogue = tmp_path / "rogue"
    rogue.mkdir()
    (rogue / ap.PROBLEM_FILENAME).write_text("p\n", encoding="utf-8")
    rc = als.main(["step", "--goal-dir", str(rogue)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False and out["state"] == "goal_invalid"


def test_verification_window_holds_in_flight_lock(tmp_path: Path, monkeypatch):
    """Findings 8+12 (verification half): the frozen gate is the other
    long-running lane subprocess - a held lock must refuse a concurrent
    gate run via the same O_EXCL test-and-set, not double-run the command."""
    repo = _make_repo(tmp_path)
    _write_config(repo, verification="false")
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)  # build (acquires + releases the lock)
    # a concurrent step's lock, shaped to defeat the YAML read check
    (goal / ap.IN_FLIGHT_FILENAME).write_text("", encoding="utf-8")
    r = _step(goal, repo)
    assert r["state"] == "dispatch_in_flight"
    assert not (ap.cycle_dir(goal, 1) / ap.VERIFICATION_FILENAME).exists()
    (goal / ap.IN_FLIGHT_FILENAME).unlink()
    r = _step(goal, repo)  # lock released: the gate runs normally
    assert r["state"] == "verification_red"
