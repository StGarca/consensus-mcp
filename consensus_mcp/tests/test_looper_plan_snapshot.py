"""Decisive zero-diff guard (consult Q2 / Task 8): Build's EXISTING architect-tree
snapshot already covers inside-goal-dir looper artifacts (so no supervisor change
is needed), and a post-baseline mutation is a containment violation.

If these fail, the 'zero diff to Build' claim is refuted - STOP and escalate to
the operator before any supervisor edit (per the converged plan)."""
from consensus_mcp import _architect_lane as lane
from consensus_mcp import _architect_paths as ap


def _goal(tmp_path):
    g = tmp_path / ".consensus" / "architect" / "g1"
    (g / "looper-plan").mkdir(parents=True)
    (g / "looper-plan" / "loop.yaml").write_text("version: 1\n", encoding="utf-8")
    (g / "looper-plan" / "LOOP.md").write_text("# preview\n", encoding="utf-8")
    (g / "problem.md").write_text("# coached\n", encoding="utf-8")
    return g


def test_architect_tree_snapshot_includes_looper_files(tmp_path):
    g = _goal(tmp_path)
    snap = lane.snapshot_architect_tree(tmp_path, exclude_lane=g / ap.LANE_DIRNAME)
    assert any("looper-plan/loop.yaml" in k for k in snap)
    assert any(k.endswith("problem.md") for k in snap)


def test_post_baseline_mutation_is_a_violation(tmp_path):
    g = _goal(tmp_path)
    before = lane.snapshot_architect_tree(tmp_path, exclude_lane=g / ap.LANE_DIRNAME)
    (g / "problem.md").write_text("# coached EDITED\n", encoding="utf-8")
    violations = lane.check_architect_tree(tmp_path, before, exclude_lane=g / ap.LANE_DIRNAME)
    assert any("problem.md" in v and "modified" in v for v in violations)


def test_goal_artifact_snapshot_also_covers_looper_files(tmp_path):
    g = _goal(tmp_path)
    snap = lane.snapshot_goal_artifacts(g)
    assert any("looper-plan/loop.yaml" in k for k in snap)
    assert "problem.md" in snap
