"""Unit tests for consensus_mcp.workflow_engine.

Uses Fake* adapters from consensus_mcp.contributors.base for fast tests
without spawning real codex/gemini subprocesses.
"""
from __future__ import annotations

import hashlib
import threading
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from consensus_mcp import config as cfg
from consensus_mcp.contributors import DispatchError
from consensus_mcp.contributors.base import (
    ContributorAdapter,
    DispatchPacket,
    FakeAlwaysApprove,
    FakeAlwaysBlock,
    FakeRaisesDispatchError,
    SealedArtifact,
)
from consensus_mcp.workflow_engine import (
    ConvergenceOutcome,
    IterationOutcome,
    WorkflowEngine,
    WorkflowError,
)


# ---------- helpers ----------


def _three_contributor_config(mode=cfg.WORKFLOW_PROPOSE_CONVERGE, rule=cfg.CONVERGE_STRICT_MAJ) -> dict:
    c = deepcopy(cfg.default_config())
    # Pin to 3 named contributors (helper is "three_contributor"); default_config
    # now derives a dynamic 4-AI independent set (incl kimi), which these tests do
    # not provide adapters for.
    c["contributors"]["enabled"] = ["claude", "codex", "gemini"]
    c["workflow"]["mode"] = mode
    c["convergence"]["rule"] = rule
    # propose-converge forces all-or-nothing in validation; respect that
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING
    cfg.validate(c)
    return c


def _make_iter_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    iter_dir = tmp_path / "iter-test"
    iter_dir.mkdir()
    goal = iter_dir / "goal_packet.yaml"
    goal.write_text("pilot: iter-test\n", encoding="utf-8")
    target = iter_dir / "review-target.yaml"
    target.write_text("schema_version: 1\n", encoding="utf-8")
    return iter_dir, goal, target


# ---------- advisory weights: engine-level weights-off equivalence ----------


def test_convergence_packet_advisory_weights_reorder_but_set_is_identical(tmp_path):
    """S3 decisive experiment (next-step converged plan): contributor_weights in
    _build_convergence_packet change the proposal READING-ORDER but never the SET of
    proposals - so the gate/convergence (which operate on the set, never on weights)
    are byte-identical regardless. Proves weights-off equivalence at the engine level."""
    config = _three_contributor_config()
    adapters = {n: FakeAlwaysApprove() for n in ["claude", "codex", "gemini"]}
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir = tmp_path / "iter-w"
    iter_dir.mkdir()
    paths = []
    for c in ["gemini", "codex", "claude"]:  # deliberately not weight-order
        p = iter_dir / f"{c}-proposal.yaml"
        p.write_text(f"contributor: {c}\n", encoding="utf-8")
        paths.append(str(p))

    pkt_off = engine._build_convergence_packet(iter_dir, paths, 1)  # no weights = identity
    pkt_on = engine._build_convergence_packet(
        iter_dir, paths, 2,
        contributor_weights={"codex": 1.0, "claude": 0.6, "gemini": 0.25},
    )
    files_off = yaml.safe_load(pkt_off.read_text(encoding="utf-8"))["defect_target"]["files"]
    files_on = yaml.safe_load(pkt_on.read_text(encoding="utf-8"))["defect_target"]["files"]

    assert sorted(files_off) == sorted(files_on)        # same SET (weights-off equivalence)
    assert files_off != files_on                        # weights DID reorder (advisory effect real)
    assert files_on[0].endswith("codex-proposal.yaml")  # highest weight read first
    assert files_off[0].endswith("gemini-proposal.yaml")  # default = original order


# ---------- construction ----------

def test_engine_construction_requires_all_enabled_adapters(tmp_path):
    config = _three_contributor_config()
    # Only provide two adapters; one missing.
    adapters = {"claude": FakeAlwaysApprove(), "codex": FakeAlwaysApprove()}
    with pytest.raises(WorkflowError, match="missing for enabled contributors"):
        WorkflowEngine(config, adapters, tmp_path)


def test_engine_constructs_with_all_adapters(tmp_path):
    config = _three_contributor_config()
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysApprove(),
        "gemini": FakeAlwaysApprove(),
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    assert engine.repo_root == tmp_path


# ---------- effective_config ----------

def test_write_effective_config(tmp_path):
    config = _three_contributor_config()
    adapters = {n: FakeAlwaysApprove() for n in ["claude", "codex", "gemini"]}
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    out = engine.write_effective_config(iter_dir)
    assert out.exists()
    assert out.name == "effective-config.yaml"
    loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


# ---------- workflow #3 ----------

def test_workflow_3_all_approve_converges(tmp_path):
    config = _three_contributor_config(mode=cfg.WORKFLOW_POST_REVIEW)
    config["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    config["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING
    cfg.validate(config)
    adapters = {n: FakeAlwaysApprove() for n in ["claude", "codex", "gemini"]}
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    assert outcome.error is None
    assert outcome.convergence is not None
    assert outcome.convergence.converged is True
    # Claude is the orchestrator in workflow #3, so doesn't dispatch.
    assert "codex" in outcome.contributor_artifacts
    assert "gemini" in outcome.contributor_artifacts
    assert "claude" not in outcome.contributor_artifacts


def test_workflow_3_one_blocks_strict_majority_holds(tmp_path):
    """codex blocks, gemini approves -> strict-majority of 2 non-claude review = 1 of 2 = NOT majority -> fails."""
    config = _three_contributor_config(mode=cfg.WORKFLOW_POST_REVIEW, rule=cfg.CONVERGE_STRICT_MAJ)
    config["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    cfg.validate(config)
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysBlock(),
        "gemini": FakeAlwaysApprove(),
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    # Convergence rule operates on the 2 non-claude artifacts; strict-majority
    # of 2 = 2 (must have all). codex blocked, so 1/2 approve -> not converged.
    # ALSO blocking_objections from codex prevents convergence regardless.
    assert outcome.convergence.converged is False
    assert "codex" in outcome.convergence.block_votes
    assert "gemini" in outcome.convergence.approve_votes


# ---------- workflow #4 ----------

def test_workflow_4_all_approve_converges_round_1(tmp_path):
    config = _three_contributor_config()  # propose-converge + strict-majority
    adapters = {n: FakeAlwaysApprove() for n in ["claude", "codex", "gemini"]}
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    assert outcome.error is None
    assert outcome.convergence.converged is True
    # All three contributed; blind phase + at least one convergence round.
    for c in ("claude", "codex", "gemini"):
        assert c in outcome.contributor_artifacts
        # Each contributor has propose + at least one converge artifact.
        assert len(outcome.contributor_artifacts[c]) >= 2
    # Final converged-plan written.
    assert outcome.final_artifact_path is not None
    assert outcome.final_artifact_path.name == "converged-plan.yaml"
    assert outcome.final_artifact_path.exists()


def test_workflow_4_max_rounds_exceeded(tmp_path):
    config = _three_contributor_config()
    config["workflow"]["max_convergence_rounds"] = 2
    cfg.validate(config)
    # All three block - never converges.
    adapters = {n: FakeAlwaysBlock() for n in ["claude", "codex", "gemini"]}
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    assert outcome.error is not None
    assert "convergence not reached" in outcome.error
    assert outcome.convergence.converged is False


def test_workflow_4_one_blocks_strict_majority_fails(tmp_path):
    """3 contributors, strict-majority threshold = 2; if codex blocks (and emits
    blocking_objections), convergence fails regardless of approve count."""
    config = _three_contributor_config()
    config["workflow"]["max_convergence_rounds"] = 1
    cfg.validate(config)
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysBlock(),
        "gemini": FakeAlwaysApprove(),
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    # 2 approve, 1 block. Strict-majority of 3 = 2. n_approve=2 meets threshold,
    # BUT blocking_objections is non-empty -> not converged.
    assert outcome.convergence.converged is False
    assert outcome.convergence.blocking_objection_ids  # non-empty


# ---------- advisory ----------

def test_advisory_always_converges(tmp_path):
    config = _three_contributor_config(mode=cfg.WORKFLOW_ADVISORY, rule=cfg.CONVERGE_ADVISORY)
    config["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    cfg.validate(config)
    adapters = {n: FakeAlwaysBlock() for n in ["claude", "codex", "gemini"]}
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    # Advisory always converges (claude decides regardless).
    assert outcome.convergence.converged is True
    assert outcome.convergence.rule == cfg.CONVERGE_ADVISORY


class _BarrierAdapter(ContributorAdapter):
    """Advisory concurrency probe (mirrors
    test_dispatch_phase_parallel.test_phase_parallel_runs_concurrently): every
    dispatch must rendezvous at a shared N-party barrier before it returns. A
    SERIAL dispatch loop can never release the barrier -- the first waiter blocks
    to the timeout and raises BrokenBarrierError -- so only a true parallel
    fan-out crosses it and lets all contributors complete."""

    def __init__(self, name: str, barrier: threading.Barrier):
        super().__init__()
        self.name = name
        self._barrier = barrier

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        # BrokenBarrierError (propagates) if peers never arrive => serial.
        self._barrier.wait()
        out_path = packet.iteration_dir / f"{self.name}-advisory-{packet.phase}.yaml"
        parsed = {
            "iteration_id": packet.iteration_dir.name,
            "reviewer_id": f"{self.name}-{packet.contributor}-1",
            "pass_id": f"{self.name}-{packet.contributor}-1-pass1",
            "findings": [],
            "goal_satisfied": True,
            "goal_satisfied_rationale": "barrier-crossed",
            "blocking_objections": [],
        }
        out_path.write_text(
            yaml.safe_dump(parsed, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        return SealedArtifact(
            contributor=self.name,
            phase=packet.phase,
            pass_id=parsed["pass_id"],
            sealed_path=out_path,
            archive_sealed_path=None,
            packet_sha256=hashlib.sha256(out_path.read_bytes()).hexdigest(),
            parsed=parsed,
        )


def test_advisory_dispatches_in_parallel(tmp_path):
    """Q8 (consult iteration-path-to-a-remediation-260caad1): advisory mode fans
    reviewers out CONCURRENTLY via _dispatch_phase_parallel, exactly like
    workflow #3. An N-party barrier that every dispatch must reach proves it: a
    serial for-loop would block the first waiter to the barrier timeout
    (BrokenBarrierError -> the run raises), while concurrent dispatch releases the
    barrier so all N complete and are recorded responsive."""
    config = _three_contributor_config(mode=cfg.WORKFLOW_ADVISORY, rule=cfg.CONVERGE_ADVISORY)
    config["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    cfg.validate(config)
    names = ["claude", "codex", "gemini"]
    barrier = threading.Barrier(len(names), timeout=5)
    adapters = {n: _BarrierAdapter(n, barrier) for n in names}
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    # If dispatch were serial the barrier would never release and the run would
    # surface an error instead of a clean advisory convergence.
    assert outcome.error is None
    assert outcome.convergence is not None
    assert outcome.convergence.converged is True
    assert outcome.convergence.rule == cfg.CONVERGE_ADVISORY
    assert sorted(outcome.convergence.contributors_responsive) == sorted(names)
    assert not outcome.convergence.contributors_timed_out
    # Every contributor produced exactly one advisory artifact.
    for n in names:
        assert len(outcome.contributor_artifacts[n]) == 1


# ---------- convergence rule evaluation ----------

def test_convergence_unanimous_requires_all(tmp_path):
    config = _three_contributor_config(rule=cfg.CONVERGE_UNANIMOUS)
    config["workflow"]["max_convergence_rounds"] = 1
    cfg.validate(config)
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysApprove(),
        "gemini": FakeAlwaysBlock(),
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    # 2/3 approve, 1 block -> unanimous requires ALL -> fails.
    assert outcome.convergence.converged is False


def test_convergence_inclusive_majority_passes(tmp_path):
    """With 2 contributors approving under inclusive-majority, convergence passes.

    (Renamed from test_convergence_inclusive_majority_ties_pass per
    gemini-rev-003 - the prior name implied tie testing that the body didn't
    actually exercise.)"""
    config = _three_contributor_config(rule=cfg.CONVERGE_INCL_MAJ)
    config["contributors"]["enabled"] = ["claude", "codex"]
    config["workflow"]["max_convergence_rounds"] = 1
    cfg.validate(config)
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeAlwaysApprove(),
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    assert outcome.convergence.converged is True


# ---------- timeout policy ----------

def test_timeout_policy_no_vote_default(tmp_path):
    """treat-as-no-vote: timed-out contributors don't count as 'no'.
    With 3 enabled, codex times out, claude+gemini approve -> strict-maj of 3 = 2 approve -> passes."""
    config = _three_contributor_config()
    config["workflow"]["timeout_policy"] = cfg.TIMEOUT_NO_VOTE
    config["workflow"]["max_convergence_rounds"] = 1
    cfg.validate(config)
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeRaisesDispatchError(),  # times out
        "gemini": FakeAlwaysApprove(),
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    # 2 approve out of 3 enabled = strict-majority threshold (3//2)+1 = 2 -> passes.
    assert outcome.convergence.converged is True
    assert "codex" in outcome.convergence.contributors_timed_out


def test_timeout_policy_shrink_quorum(tmp_path):
    """shrink-quorum: timed-out contributors REDUCE N. With 3 enabled, codex
    times out -> N=2, strict-maj of 2 = 2 (everyone must approve)."""
    config = _three_contributor_config()
    config["workflow"]["timeout_policy"] = cfg.TIMEOUT_SHRINK
    config["workflow"]["max_convergence_rounds"] = 1
    cfg.validate(config)
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeRaisesDispatchError(),
        "gemini": FakeAlwaysApprove(),
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    # responsive N=2, both approve, strict-maj threshold (2//2)+1=2, 2>=2 -> passes.
    assert outcome.convergence.converged is True


def test_timeout_policy_treat_as_blocking(tmp_path):
    """treat-as-blocking: timed-out contributors count as block votes."""
    config = _three_contributor_config(rule=cfg.CONVERGE_UNANIMOUS)
    config["workflow"]["timeout_policy"] = cfg.TIMEOUT_BLOCKING
    config["workflow"]["max_convergence_rounds"] = 1
    cfg.validate(config)
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeRaisesDispatchError(),
        "gemini": FakeAlwaysApprove(),
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    # Unanimous requires all approve; codex counted as block under treat-as-blocking -> fails.
    assert outcome.convergence.converged is False
    assert "codex" in outcome.convergence.block_votes


def test_timeout_policy_blocking_vetoes_strict_majority(tmp_path):
    """H-7: under TIMEOUT_BLOCKING a timed-out contributor MUST veto even a
    strict-majority. 3 enabled, strict-majority, codex times out, claude+gemini
    approve. Two approvals reach the strict-maj threshold ((3//2)+1 == 2), so the
    approve count alone would converge - but the operator chose treat-as-blocking,
    so codex's non-response is a block that vetoes the majority."""
    config = _three_contributor_config(rule=cfg.CONVERGE_STRICT_MAJ)
    config["workflow"]["timeout_policy"] = cfg.TIMEOUT_BLOCKING
    config["workflow"]["max_convergence_rounds"] = 1
    cfg.validate(config)
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeRaisesDispatchError(),  # times out -> no artifact
        "gemini": FakeAlwaysApprove(),
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    assert "codex" in outcome.convergence.block_votes
    assert outcome.convergence.converged is False


def test_timeout_blocking_inclusive_majority_two_party(tmp_path):
    """H-7: under TIMEOUT_BLOCKING a timeout vetoes an inclusive-majority too.
    enabled=[claude,codex], inclusive-majority, claude approves, codex times out.
    inclusive-maj threshold ceil(2/2) == 1, so claude's lone approval would
    otherwise converge - but treat-as-blocking makes codex's timeout a veto."""
    config = _three_contributor_config(rule=cfg.CONVERGE_INCL_MAJ)
    config["contributors"]["enabled"] = ["claude", "codex"]
    config["workflow"]["timeout_policy"] = cfg.TIMEOUT_BLOCKING
    config["workflow"]["max_convergence_rounds"] = 1
    cfg.validate(config)
    adapters = {
        "claude": FakeAlwaysApprove(),
        "codex": FakeRaisesDispatchError(),  # times out -> no artifact
    }
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    assert "codex" in outcome.convergence.block_votes
    assert outcome.convergence.converged is False


# ---------- failure mode: all contributors blow up ----------

def test_workflow_4_all_contributors_fail(tmp_path):
    config = _three_contributor_config()
    config["workflow"]["max_convergence_rounds"] = 1
    cfg.validate(config)
    adapters = {n: FakeRaisesDispatchError() for n in ["claude", "codex", "gemini"]}
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    outcome = engine.run_iteration(iter_dir, goal, target)
    # All three failed blind phase; engine raises WorkflowError captured in outcome.
    assert outcome.error is not None
    assert "no contributors produced blind proposals" in outcome.error
