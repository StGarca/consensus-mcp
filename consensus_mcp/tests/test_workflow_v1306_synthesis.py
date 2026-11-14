"""v1.30.6 - synthesis-aware propose-converge (Path B guard + Path A helper)."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from consensus_mcp import config as cfg
from consensus_mcp.contributors.base import (
    FakeAlwaysApprove, FakeAlwaysBlock, SealedArtifact,
)
from consensus_mcp.workflow_engine import (
    ConvergenceOutcome, IterationOutcome, WorkflowEngine,
)


def _config(mode=cfg.WORKFLOW_PROPOSE_CONVERGE, rule=cfg.CONVERGE_STRICT_MAJ) -> dict:
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = ["claude", "codex", "gemini"]
    c["workflow"]["mode"] = mode
    c["convergence"]["rule"] = rule
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING
    cfg.validate(c)
    return c


def _engine(tmp_path, approve=True) -> WorkflowEngine:
    adapters = {n: (FakeAlwaysApprove() if approve else FakeAlwaysBlock())
                for n in ["claude", "codex", "gemini"]}
    return WorkflowEngine(_config(), adapters, tmp_path)


def _goal(tmp_path, body: str) -> Path:
    g = tmp_path / "goal_packet.yaml"
    g.write_text(body, encoding="utf-8")
    return g


# ---------- Task 1: _requires_synthesis reader ----------

def test_requires_synthesis_true_when_declared(tmp_path):
    eng = _engine(tmp_path)
    g = _goal(tmp_path, "convergence:\n  requires_synthesis: true\n")
    assert eng._requires_synthesis(g) is True


def test_requires_synthesis_false_when_absent(tmp_path):
    eng = _engine(tmp_path)
    g = _goal(tmp_path, "goal:\n  summary: x\n")
    assert eng._requires_synthesis(g) is False


def test_requires_synthesis_false_on_nonbool_or_unreadable(tmp_path):
    eng = _engine(tmp_path)
    g = _goal(tmp_path, "convergence:\n  requires_synthesis: maybe\n")
    assert eng._requires_synthesis(g) is False
    assert eng._requires_synthesis(tmp_path / "nope.yaml") is False


# ---------- Task 2: Path B fail-loud guard ----------

def test_path_b_fails_loud_on_requires_synthesis(tmp_path):
    eng = _engine(tmp_path)
    iter_dir = tmp_path / "iter-synth"; iter_dir.mkdir()
    g = _goal(iter_dir, "convergence:\n  requires_synthesis: true\n")
    target = iter_dir / "problem.yaml"; target.write_text("schema_version: 1\n", encoding="utf-8")
    outcome = eng.run_iteration(iter_dir, g, target)
    assert outcome.error is not None
    assert "requires_synthesis" in outcome.error
    assert "Path A" in outcome.error
    # it must NOT have entered the bundle-vote loop: no convergence-packet written
    assert not list(iter_dir.glob("convergence-packet-round-*.yaml"))


def test_path_b_unchanged_without_flag(tmp_path):
    # No flag -> normal propose-converge runs (FakeAlwaysApprove -> converges).
    eng = _engine(tmp_path, approve=True)
    iter_dir = tmp_path / "iter-normal"; iter_dir.mkdir()
    g = _goal(iter_dir, "goal:\n  summary: agree on approach\n")
    target = iter_dir / "problem.yaml"; target.write_text("schema_version: 1\n", encoding="utf-8")
    outcome = eng.run_iteration(iter_dir, g, target)
    assert outcome.error is None
    assert outcome.convergence is not None and outcome.convergence.converged


# ---------- Task 3: Path A evaluate + seal helpers ----------

def _outcome(iter_id="i", config_dir: Path | None = None) -> IterationOutcome:
    return IterationOutcome(
        iteration_id=iter_id, workflow_mode=cfg.WORKFLOW_PROPOSE_CONVERGE,
        effective_config_path=(config_dir or Path("/tmp")) / "ec.yaml",
    )


def _review_artifact(contributor: str, *, satisfied: bool, blocking=None) -> SealedArtifact:
    return SealedArtifact(
        contributor=contributor, phase="converge",
        pass_id=f"{contributor}-pass1", sealed_path=Path(f"/tmp/{contributor}-review.yaml"),
        archive_sealed_path=None, packet_sha256="",
        parsed={"goal_satisfied": satisfied, "blocking_objections": blocking or []},
    )


def _outcome_with(arts) -> IterationOutcome:
    outcome = _outcome()
    for a in arts:
        outcome.contributor_artifacts.setdefault(a.contributor, []).append(a)
    return outcome


def test_evaluate_plan_convergence_clean_converges(tmp_path):
    eng = _engine(tmp_path)
    arts = [_review_artifact(c, satisfied=True) for c in ["claude", "codex", "gemini"]]
    conv = eng.evaluate_plan_convergence(arts, _outcome_with(arts))
    assert isinstance(conv, ConvergenceOutcome) and conv.converged


def test_evaluate_plan_convergence_block_does_not_converge(tmp_path):
    eng = _engine(tmp_path)
    arts = [_review_artifact("claude", satisfied=True),
            _review_artifact("codex", satisfied=False, blocking=["codex-b1"]),
            _review_artifact("gemini", satisfied=False, blocking=["gemini-b1"])]
    conv = eng.evaluate_plan_convergence(arts, _outcome_with(arts))
    assert not conv.converged


def test_seal_plan_iteration_seals_THE_PLAN_not_a_bundle(tmp_path):
    eng = _engine(tmp_path)
    iter_dir = tmp_path / "iter-plan"; iter_dir.mkdir()
    plan = iter_dir / "converged-plan.yaml"
    plan.write_text("decision:\n  do: ship the thing\nfeasibility: {a: ok}\n", encoding="utf-8")
    plan_before = plan.read_text(encoding="utf-8")
    arts = [_review_artifact(c, satisfied=True) for c in ["claude", "codex", "gemini"]]
    conv = eng.evaluate_plan_convergence(arts, _outcome_with(arts))

    sealed = eng.seal_plan_iteration(iter_dir, plan, conv, round_number=1)
    assert sealed == plan                                   # the PLAN is the sealed artifact
    assert plan.read_text(encoding="utf-8") == plan_before  # NOT overwritten with a summary
    oc = yaml.safe_load((iter_dir / "iteration-outcome.yaml").read_text(encoding="utf-8"))
    from consensus_mcp._delivery_readiness import SEALED_CLOSING_STATES
    assert oc["closing_state"] in SEALED_CLOSING_STATES     # mintable sealed iteration


def test_seal_plan_iteration_none_when_not_converged(tmp_path):
    eng = _engine(tmp_path)
    iter_dir = tmp_path / "iter-plan2"; iter_dir.mkdir()
    plan = iter_dir / "converged-plan.yaml"; plan.write_text("decision: {}\n", encoding="utf-8")
    arts = [_review_artifact("claude", satisfied=False, blocking=["b1"]),
            _review_artifact("codex", satisfied=False, blocking=["b2"]),
            _review_artifact("gemini", satisfied=True)]
    conv = eng.evaluate_plan_convergence(arts, _outcome_with(arts))
    assert eng.seal_plan_iteration(iter_dir, plan, conv, round_number=1) is None
    assert not (iter_dir / "iteration-outcome.yaml").exists()
