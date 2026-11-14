"""Regressions for review report D1-D9 and D13-D15; no live reviewers."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import pytest
import yaml

from consensus_mcp import config as cfg
from consensus_mcp.contributors.base import DispatchError, FakeAlwaysApprove
from consensus_mcp.workflow_engine import IterationOutcome, WorkflowEngine
from consensus_mcp.tools import review_write_and_seal as seal
from consensus_mcp.tests.sealed_review_helpers import write_sealed_reviews


def _engine(tmp_path, rule="inclusive-majority", policy="shrink-quorum"):
    config = cfg.default_config()
    config["contributors"]["enabled"] = ["codex", "gemini"]
    config["workflow"].update(mode="post-review", timeout_policy=policy)
    config["convergence"]["rule"] = rule
    return WorkflowEngine(config, {n: FakeAlwaysApprove() for n in ("codex", "gemini")}, tmp_path)


def test_d1_parallel_seals_keep_index_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path / "consensus-state"))
    iteration = tmp_path / "consensus-state/active/parallel"
    iteration.mkdir(parents=True)
    barrier = threading.Barrier(12)

    def write(i):
        barrier.wait(timeout=10)
        packet = dict(iteration_id="parallel", reviewer_id=f"reviewer-{i}",
                      pass_id=f"pass-{i}", findings=[], independence_attestation={})
        return seal.handle("parallel", f"reviewer-{i}", f"pass-{i}", packet)

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(write, range(12)))
    assert all("error" not in r for r in results), results
    index = yaml.safe_load((tmp_path / "consensus-state/archive/review-passes/index.yaml").read_text())
    audit = yaml.safe_load((iteration / "independence-audit.yaml").read_text())
    assert {e["id"] for e in index["passes"]} == {f"pass-{i}" for i in range(12)}
    assert {e["actor"] for e in audit["audit_log"]} == {f"reviewer-{i}" for i in range(12)}


def test_d1_seals_across_processes_and_iterations(tmp_path):
    env = dict(os.environ, CONSENSUS_MCP_PROJECT_ROOT=str(tmp_path),
               CONSENSUS_MCP_STATE_ROOT=str(tmp_path / "consensus-state"))
    for iteration in ("one", "two"):
        (tmp_path / "consensus-state/active" / iteration).mkdir(parents=True)
    script = (
        "import json, sys; from consensus_mcp.tools.review_write_and_seal import handle; "
        "iteration, name = sys.argv[1:]; "
        "packet = dict(iteration_id=iteration, reviewer_id=name, pass_id=name, "
        "findings=[], independence_attestation={}); "
        "print(json.dumps(handle(iteration, name, name, packet)))"
    )

    def write(i):
        result = subprocess.run([sys.executable, "-I", "-c", script,
                                 "one" if i % 2 else "two", f"review-{i}"],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "error" not in json.loads(result.stdout), result.stdout

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(6)))
    index = yaml.safe_load((tmp_path / "consensus-state/archive/review-passes/index.yaml").read_text())
    assert {e["id"] for e in index["passes"]} == {f"review-{i}" for i in range(6)}
    for iteration in ("one", "two"):
        audit = yaml.safe_load((tmp_path / "consensus-state/active" / iteration / "independence-audit.yaml").read_text())
        assert len(audit["audit_log"]) == 3


@pytest.mark.parametrize("rule", ["host-decides", "unanimous", "strict-majority", "inclusive-majority", "advisory"])
@pytest.mark.parametrize("policy", ["treat-as-no-vote", "treat-as-blocking", "shrink-quorum"])
def test_d2_empty_panel_never_converges(tmp_path, rule, policy):
    engine = _engine(tmp_path, rule, policy)
    outcome = IterationOutcome("empty", "post-review", tmp_path / "config")
    result = engine._evaluate_convergence([], outcome)
    assert result.converged is False
    assert result.rationale == "no reviews returned"
    assert result.contributors_timed_out == ["codex", "gemini"]


@pytest.mark.parametrize("kind,message", [
    ("timeout", "review timed out after 10s"),
    ("quota", "usage limit reached; quota exhausted"),
    ("rate_limit", "HTTP 429 too many requests"),
    ("auth", "HTTP 401 unauthorized"),
    ("parse", "CodexOutputParseError: invalid JSON"),
    ("crash", "process exited 7"),
    ("integrity", "KimiIntegrityError: repo mutated"),
    ("quota", "You've reached your Fable limit"),
    ("timeout", "kimi stuck: no output for 240s"),
])
def test_d3_failure_cause_kept_on_partial_panel(tmp_path, kind, message):
    engine = _engine(tmp_path)

    def fail(*args, **kwargs):
        raise DispatchError(message)

    engine.adapters["gemini"].review = fail
    goal = tmp_path / "goal.yaml"
    goal.write_text("goal: test\n")
    outcome = engine.run_iteration(tmp_path / "iter", goal, goal)
    assert outcome.error is None
    assert outcome.convergence.converged is True
    assert outcome.convergence.block_votes == []
    assert outcome.convergence.contributors_timed_out == ["gemini"]
    assert outcome.contributors_failed["gemini"] == {"class": kind, "message": message}


def test_d4_kimi_scrubs_python_host_env(monkeypatch):
    from consensus_mcp._dispatch_kimi import _kimi_subprocess_env
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        monkeypatch.setenv(key, "host-python")
    env = _kimi_subprocess_env()
    assert all(key not in env for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"))
    assert os.environ["PYTHONPATH"] == "host-python"
    assert env["PYTHONUTF8"] == "1"


def test_d5_full_convergence_target(tmp_path):
    target = tmp_path / "target.txt"
    content = "x" * 40000 + "\nLAST ACCEPTANCE GATE\n"
    target.write_text(content, encoding="utf-8")
    packet = _engine(tmp_path)._build_convergence_packet(tmp_path, [], 1, target_path=target)
    data = yaml.safe_load(packet.read_text())
    assert data["defect_target"]["touched_files_contents"]["target.txt"] == content


@pytest.mark.parametrize("goal_satisfied,objections", [(False, []), (True, ["blocking-1"])])
@pytest.mark.parametrize("reviewer", ["codex", "gemini"])
def test_d6_negative_bound_review_blocks_accept(tmp_path, goal_satisfied, objections, reviewer):
    from consensus_mcp import _architect_paths as ap
    from consensus_mcp.tools.architect_loop_step import _signer_violations
    cycle = ap.cycle_dir(tmp_path, 1)
    cycle.mkdir(parents=True)
    binding = dict(lane_head_sha="a" * 40)
    ap.seal_artifact(cycle / ap.BUILD_RESULT_FILENAME, binding)
    ap.seal_artifact(cycle / ap.REVIEW_FILENAME,
                     dict(binding, goal_satisfied=goal_satisfied, blocking_objections=objections))
    ap.seal_artifact(cycle / ap.RULING_FILENAME, dict(binding, disposition="accept"))
    violations = _signer_violations(tmp_path, 1,
                                   dict(builder="codex", reviewer=reviewer, architect="claude"), {})
    assert any("verdict" in v for v in violations), violations


def _delivery(tmp_path):
    iteration = tmp_path / "consensus-state/active/delivery"
    iteration.mkdir(parents=True)
    (iteration / "iteration-outcome.yaml").write_text("closing_state: quorum_close_passed\n")
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("done")
    return iteration, artifact


def _review(iteration, name):
    write_sealed_reviews(iteration, [name])


def test_d7_duplicate_reviewer_cannot_mint(tmp_path):
    from consensus_mcp import _delivery_readiness as dr
    iteration, artifact = _delivery(tmp_path)
    _review(iteration, "codex")
    with pytest.raises(dr.DeliveryReadinessError, match="non-claude"):
        dr.mint_delivery_token(artifact, design_consensus_ref="delivery",
                               vetted_by=["codex", "codex"], repo_root=tmp_path)


def test_d7_token_deduplicates_vetted_names(tmp_path):
    from consensus_mcp import _delivery_readiness as dr
    iteration, artifact = _delivery(tmp_path)
    write_sealed_reviews(iteration)
    token = dr.mint_delivery_token(artifact, design_consensus_ref="delivery",
                                   vetted_by=["codex", "gemini", "codex"], repo_root=tmp_path)
    assert token["vetted_by"] == ["codex", "gemini"]


@pytest.mark.parametrize("invalid", ["missing", "unsealed", "tampered", "wrong-iteration", "proposal"])
def test_d7_each_name_needs_sealed_review(tmp_path, invalid):
    from consensus_mcp import _delivery_readiness as dr
    iteration, artifact = _delivery(tmp_path)
    _review(iteration, "codex")
    if invalid != "missing":
        _review(iteration, "gemini")
        p = iteration / "gemini-review.yaml"
        packet = yaml.safe_load(p.read_text())
        if invalid == "unsealed":
            packet.pop("packet_sha256")
        elif invalid == "wrong-iteration":
            packet["iteration_id"] = "other"
            packet.pop("packet_sha256")
            packet["packet_sha256"] = seal._canonical_yaml_sha256(packet)
        elif invalid == "tampered":
            packet["findings"] = ["changed"]
        p.write_text(yaml.safe_dump(packet))
        if invalid == "proposal":
            p.rename(iteration / "gemini-proposal.yaml")
    with pytest.raises(dr.DeliveryReadinessError, match="sealed review"):
        dr.mint_delivery_token(artifact, design_consensus_ref="delivery",
                               vetted_by=["codex", "gemini"], repo_root=tmp_path)


@pytest.mark.parametrize("key", ["CONSENSUS_MCP_PROJECT_ROOT", "CONSENSUS_MCP_STATE_ROOT"])
def test_d8_self_drive_env_root(tmp_path, monkeypatch, key):
    from consensus_mcp._self_drive import _resolve_repo_root
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path / "legacy"))
    monkeypatch.setenv(key, str(tmp_path / "consensus-state" if key.endswith("STATE_ROOT") else tmp_path))
    assert _resolve_repo_root() == tmp_path.resolve()


@pytest.mark.parametrize("phase", ["propose", "review", "converge"])
def test_d9_attestation_matches_phase(phase):
    from consensus_mcp._dispatch_base import _build_sealed_packet
    packet = _build_sealed_packet({}, "iter", f"codex-iter-{phase}-1", "pass",
                                  provenance={"phase": phase})
    attestation = packet["independence_attestation"]
    assert attestation["no_peer_review_visible_at_dispatch"] is (phase != "converge")
    assert attestation["reviewer_isolated_by_construction"] is (phase != "converge")


def test_d13_ambient_env_isolated(tmp_path):
    env = dict(os.environ, CONSENSUS_MCP_GATE_DISABLE="1", AI_AGENT="1", CLAUDECODE="1",
               CI="1", CMCP_HERMETIC_PROBE="1", CMCP_PARENT_TEMP=tempfile.gettempdir())
    result = subprocess.run([sys.executable, "-m", "pytest", __file__, "-q", "-k",
                             "d13_child", "-p", "no:cacheprovider"], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.environ.get("CMCP_HERMETIC_PROBE") != "1", reason="child probe")
def test_d13_child():
    # Ambient CONSENSUS_MCP_* (the parent sets GATE_DISABLE) is stripped; the
    # only one left is conftest's hermetic sensitive-data default.
    assert {k: v for k, v in os.environ.items() if k.startswith("CONSENSUS_MCP_")} == {
        "CONSENSUS_MCP_SENSITIVE_DATA": "no"}
    assert not any(k in os.environ for k in ("AI_AGENT", "CLAUDECODE", "CI"))
    assert tempfile.gettempdir() != os.environ["CMCP_PARENT_TEMP"]
    assert os.environ["TEMP"] == tempfile.gettempdir()


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_d14_profile_requires_boolean(value):
    from consensus_mcp.contributors.profile_adapter import ProfileAdapter
    adapter = object.__new__(ProfileAdapter)
    adapter.profile = {}
    adapter.name = "test"
    with pytest.raises(DispatchError, match="goal_satisfied.*bool"):
        adapter._parse(json.dumps(dict(findings=[], goal_satisfied=value, blocking_objections=[])))


def test_d15_kimi_accepts_git_worktree_file(tmp_path, monkeypatch, capsys):
    from consensus_mcp import _dispatch_kimi as kimi
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    monkeypatch.setattr(kimi, "_resolve_repo_root", lambda: tmp_path)
    # Stop immediately AFTER preflight, before any reviewer invocation.
    def stop(*args, **kwargs):
        raise RuntimeError("passed worktree preflight")
    monkeypatch.setattr(kimi, "_normalize_relative_to_repo", stop)
    with pytest.raises(RuntimeError, match="passed worktree preflight"):
        kimi.main(["--goal-packet", "goal.yaml", "--iteration-dir", "iter"])


def test_d2_empty_advisory_run(tmp_path):
    from consensus_mcp.contributors.base import FakeRaisesDispatchError
    engine = _engine(tmp_path)
    engine.config["workflow"]["mode"] = "advisory"
    engine.adapters = {name: FakeRaisesDispatchError() for name in engine.adapters}
    goal = tmp_path / "goal.yaml"
    goal.write_text("goal: test\n")
    outcome = engine.run_iteration(tmp_path / "iter", goal, goal)
    assert outcome.convergence.converged is False
    assert outcome.convergence.rationale == "no reviews returned"


def test_d3_run_iteration_response_keeps_failure(tmp_path, monkeypatch):
    from consensus_mcp.tools import consensus_run_iteration as tool
    engine = _engine(tmp_path)

    def fail(*args, **kwargs):
        raise DispatchError("quota exhausted")

    engine.adapters["gemini"].review = fail
    monkeypatch.setattr(tool, "_load_config", lambda *args: engine.config)
    monkeypatch.setattr(tool.factory, "build_engine", lambda *args, **kwargs: engine)
    goal = tmp_path / "goal.yaml"
    goal.write_text("goal: test\n")
    result = tool.handle(str(tmp_path / "iter"), str(goal), str(goal),
                         repo_root=str(tmp_path), rigor_tier="quick")
    assert result["ok"] is True, result
    assert result["contributors_timed_out"] == ["gemini"]
    assert result["contributors_failed"] == {"gemini": {"class": "quota", "message": "quota exhausted"}}


def test_d2_no_blind_proposals_reports_no_reviews(tmp_path):
    from consensus_mcp.contributors.base import FakeRaisesDispatchError
    engine = _engine(tmp_path)
    engine.config["workflow"]["mode"] = "propose-converge"
    engine.adapters = {name: FakeRaisesDispatchError() for name in engine.adapters}
    goal = tmp_path / "goal.yaml"
    goal.write_text("goal: test\n")
    outcome = engine.run_iteration(tmp_path / "iter", goal, goal)
    assert outcome.convergence is not None
    assert outcome.convergence.converged is False
    assert outcome.convergence.rationale == "no reviews returned"
