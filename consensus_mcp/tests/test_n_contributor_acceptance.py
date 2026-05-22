"""DECISIVE acceptance test (2026-05-22): "will a clean install work with
2 or 20 or 200 AIs?"

This is the gate for the open-contributor work. It registers N contributors with
ARBITRARY names (none of the built-in claude/codex/gemini), builds the engine,
and runs an iteration to convergence — with ZERO special-casing per contributor.
If this passes for N=2 and N=20, the closed-enum / fixed-adapter-dict bias is
genuinely gone and the system is min-2 / max-N / any-combination.
"""
from copy import deepcopy

import pytest

from consensus_mcp import config as cfg
from consensus_mcp.contributors.base import FakeAlwaysApprove
from consensus_mcp.workflow_engine import WorkflowEngine


def _n_contributor_config(n: int):
    """A clean config with claude (orchestrator) + n-1 ARBITRARILY-NAMED peers."""
    names = ["claude"] + [f"ai-{i}" for i in range(1, n)]
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = names
    c["contributors"]["adapters"] = {nm: {} for nm in names}
    c["workflow"]["mode"] = cfg.WORKFLOW_POST_REVIEW
    c["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    c["convergence"]["rule"] = cfg.CONVERGE_STRICT_MAJ  # all approve -> > N/2 -> converges
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING
    cfg.validate(c)  # must NOT raise for arbitrary names / any N (no closed enum, no cap)
    return c, names


@pytest.mark.parametrize("n", [2, 20])
def test_clean_install_n_arbitrary_contributors_converges(tmp_path, n):
    config, names = _n_contributor_config(n)
    # ARBITRARY contributors — generic adapters, distinct names, no special-casing.
    adapters = {}
    for nm in names:
        a = FakeAlwaysApprove()
        a.name = nm
        adapters[nm] = a
    engine = WorkflowEngine(config, adapters, tmp_path)
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    goal = iter_dir / "goal_packet.yaml"
    goal.write_text("pilot: accept-test\n", encoding="utf-8")
    target = iter_dir / "review-target.yaml"
    target.write_text("schema_version: 1\n", encoding="utf-8")

    outcome = engine.run_iteration(iter_dir, goal, target)

    assert outcome.error is None, outcome.error
    assert outcome.convergence is not None
    assert outcome.convergence.converged is True, (
        f"N={n} arbitrary contributors failed to converge: {outcome.convergence}")
    # Every non-orchestrator peer actually participated (claude orchestrates,
    # doesn't dispatch, in post-review).
    for nm in names:
        if nm != "claude":
            assert nm in outcome.contributor_artifacts, (
                f"peer {nm!r} did not produce an artifact at N={n}")


def test_validate_accepts_large_N_no_upper_cap():
    """No upper cap: a 50-contributor config validates."""
    config, names = _n_contributor_config(50)
    assert len(names) == 50  # validate() inside did not raise
