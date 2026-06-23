"""seed.py - Build adapter (Tasks 4-7): problem.md, taxonomy mapping, manifest,
re-coach refusal, orchestration."""
import pytest
import yaml

from consensus_mcp.looper_plan import seed

RESOLVED = {
    "meta": {"name": "g1"},
    "goal": {"statement": "Produce X", "definition_of_done": "X is done",
             "verification": [
                 {"id": "build", "type": "programmatic", "check": ["pytest", "-q"], "expect": "exit_zero"},
                 {"id": "covers", "type": "judge", "rubric": "every step has an owner"},
                 {"id": "signoff", "type": "human", "prompt": "client confirms"},
             ]},
    "loop_control": {"max_iterations": 8, "budget": {"wall_clock_min": 30}},
}


# --- Task 4: problem.md -------------------------------------------------------

def test_problem_md_has_goal_dod_and_nonautomation_banner():
    md = seed.render_problem_md(RESOLVED)
    assert "Produce X" in md and "X is done" in md
    assert "pytest" in md
    assert "NON-AUTOMATION" in md
    assert "every step has an owner" in md and "client confirms" in md


# --- Task 5: taxonomy mapping + cross-platform --------------------------------

def test_render_verification_command_uses_platform_quoting():
    cmd = seed.render_verification_command(["pytest", "-q", "tests/a b.py"])
    assert "pytest" in cmd and "a b.py" in cmd.replace('"', "").replace("'", "")


def test_map_verification_exit_zero_to_frozen_and_acceptance():
    m = seed.map_verification(RESOLVED)
    assert m["frozen_verification"]
    assert any(g["id"] == "build" for g in m["acceptance_gates"])
    assert all(g["id"] not in ("covers", "signoff") for g in m["acceptance_gates"])
    assert any(d["id"] == "covers" for d in m["design_criteria"])


def test_map_verification_non_exit_zero_flagged_for_operator():
    r = {"goal": {"verification": [
        {"id": "has", "type": "programmatic", "check": ["grep", "x", "f"],
         "expect": "stdout_contains", "contains": "x"}]}}
    m = seed.map_verification(r)
    g = [x for x in m["acceptance_gates"] if x["id"] == "has"][0]
    assert g["needs_operator_edit"] is True
    assert m["frozen_verification"] == ""   # non-exit_zero is not auto-frozen


# --- Task 6: manifest + re-coach refusal --------------------------------------

def test_seal_manifest_records_sha256(tmp_path):
    (tmp_path / "looper-plan").mkdir()
    (tmp_path / "looper-plan" / "loop.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "problem.md").write_text("# x\n", encoding="utf-8")
    man = seed.seal_manifest(tmp_path)
    assert "looper-plan/loop.yaml" in man["files"]
    assert len(man["files"]["looper-plan/loop.yaml"]) == 64


def test_assert_safe_to_coach_refuses_when_supervisor_artifact_exists(tmp_path):
    seed.assert_safe_to_coach(tmp_path)                  # clean -> ok
    (tmp_path / "spec-approval.yaml").write_text("x", encoding="utf-8")
    with pytest.raises(seed.ReCoachRefused):
        seed.assert_safe_to_coach(tmp_path)


def test_assert_safe_to_coach_refuses_when_cycle_dir_exists(tmp_path):
    (tmp_path / "cycle-1").mkdir()
    with pytest.raises(seed.ReCoachRefused):
        seed.assert_safe_to_coach(tmp_path)


# --- Task 7: orchestration ----------------------------------------------------

def test_seed_build_inputs_writes_all_artifacts(tmp_path):
    out = seed.seed_build_inputs(RESOLVED, tmp_path)
    assert (tmp_path / "problem.md").is_file()
    sug = yaml.safe_load((tmp_path / "looper-suggestions.yaml").read_text())
    assert "frozen_verification" in sug and "acceptance_gates" in sug
    assert sug["architect_loop"]["max_cycles"] == 8
    man = yaml.safe_load((tmp_path / "looper-plan-manifest.yaml").read_text())
    assert "problem.md" in man["files"]
    assert out["problem_md"].endswith("problem.md")
