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
                  profiles: dict | None = None) -> Path:
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
            "max_wall_clock_minutes": 0,
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
    # UnicodeDecodeError through the never-raises boundary.
    repo = _make_repo(tmp_path)
    _write_config(repo, verification="printf '\\377\\376 bad bytes'; exit 1")
    goal = _new_goal(repo)
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "do it"})
    gates.handle_approve_spec(goal_dir=str(goal), approver="op", repo_root=str(repo))
    _fake_builder(monkeypatch, lane_effect=lambda lane: (lane / "f.py").write_text("a=1\n", encoding="utf-8"))
    _step(goal, repo)  # build
    r = _step(goal, repo)
    assert r["state"] == "verification_red"  # exit 1 -> RED, decoded with replacement


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
