"""DECISIVE acceptance test (2026-05-22): "will a clean install work with
2 or 20 or 200 AIs?"

1.17 consensus review (codex-003 / kimi-002,003): this test now goes through the
REAL shipped path - `register_contributor` + `build_adapters` - not a
hand-built adapters dict, and N=50 actually BUILDS (not just validates). It
registers N contributors with ARBITRARY names (none built-in), builds via the
registry, and runs an iteration to convergence with ZERO per-contributor
special-casing. The registry is process-global, so every test unregisters in a
finally (test isolation, codex-005).
"""
from copy import deepcopy

import pytest

from consensus_mcp import config as cfg
from consensus_mcp import _engine_factory as ef
from consensus_mcp.contributors.base import FakeAlwaysApprove
from consensus_mcp.workflow_engine import WorkflowEngine

_APPROVE_CB = lambda packet: {  # noqa: E731 - claude orchestrator artifact (post-review: not dispatched)
    "findings": [], "goal_satisfied": True, "blocking_objections": []}


def _names(n: int) -> list[str]:
    return ["claude"] + [f"ai-{i}" for i in range(1, n)]


def _config(n: int):
    names = _names(n)
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = names
    c["contributors"]["adapters"] = {nm: {} for nm in names}
    c["workflow"]["mode"] = cfg.WORKFLOW_POST_REVIEW
    c["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    c["convergence"]["rule"] = cfg.CONVERGE_STRICT_MAJ  # all approve -> > N/2 -> converges
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING
    cfg.validate(c)  # must NOT raise for arbitrary names / any N
    return c, names


def _register_arbitrary(names: list[str]) -> list[str]:
    """Register a distinct adapter class for each NON-built-in name (the real
    open-registry path). Returns the names registered (for cleanup)."""
    registered = []
    for nm in names:
        if nm in ("claude", "codex", "gemini"):
            continue
        cls = type(f"Adapter_{nm.replace('-', '_')}", (FakeAlwaysApprove,), {"name": nm})
        ef.register_contributor(nm, cls)
        registered.append(nm)
    return registered


@pytest.mark.parametrize("n", [2, 20])
def test_clean_install_n_arbitrary_contributors_converges(tmp_path, n):
    config, names = _config(n)
    registered = _register_arbitrary(names)
    try:
        # REAL path: build_adapters resolves via the open registry.
        adapters = ef.build_adapters(config, claude_artifact_callback=_APPROVE_CB)
        assert set(adapters) == set(names), set(adapters) ^ set(names)
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
        for nm in names:
            if nm != "claude":
                assert nm in outcome.contributor_artifacts, f"peer {nm!r} absent at N={n}"
    finally:
        for nm in registered:
            ef.unregister_contributor(nm)


def test_large_N_builds_through_registry_no_cap(tmp_path):
    """N=50 is actually BUILT via the registry (not merely validated) - proves no
    upper cap on the real construction path."""
    config, names = _config(50)
    registered = _register_arbitrary(names)
    try:
        adapters = ef.build_adapters(config, claude_artifact_callback=_APPROVE_CB)
        assert len(adapters) == 50
    finally:
        for nm in registered:
            ef.unregister_contributor(nm)


def test_strict_majority_does_not_converge_when_half_block(tmp_path):
    """Negative case (codex-003: don't only test all-approve). With a blocking
    minority below the strict-majority line, convergence must be False."""
    from consensus_mcp.contributors.base import FakeAlwaysBlock
    config, names = _config(5)  # claude + ai-1..ai-4
    registered = _register_arbitrary(names)
    try:
        adapters = ef.build_adapters(config, claude_artifact_callback=_APPROVE_CB)
        # Make 2 of the 4 peers block -> 2 approve / 2 block among peers -> not > N/2.
        for nm in ("ai-1", "ai-2"):
            b = FakeAlwaysBlock(); b.name = nm; adapters[nm] = b
        engine = WorkflowEngine(config, adapters, tmp_path)
        iter_dir = tmp_path / "iter"; iter_dir.mkdir()
        (iter_dir / "goal_packet.yaml").write_text("pilot: t\n", encoding="utf-8")
        (iter_dir / "review-target.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        outcome = engine.run_iteration(iter_dir, iter_dir / "goal_packet.yaml",
                                       iter_dir / "review-target.yaml")
        assert outcome.convergence is not None
        assert outcome.convergence.converged is False, (
            f"should NOT converge with a blocking minority: {outcome.convergence}")
    finally:
        for nm in registered:
            ef.unregister_contributor(nm)
