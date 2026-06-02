"""Tests for the composed consult-approval flow (consult Q6 + Finding C/#7)."""
from __future__ import annotations

import yaml

from consensus_mcp import _approve_consult as ac


def _make_consult(repo_root, name="iter-test", families=("codex", "gemini"),
                  with_plan=True):
    """Build a synthetic post-consult iteration: sealed reviews + converged-plan."""
    iter_dir = repo_root / "consensus-state" / "active" / name
    iter_dir.mkdir(parents=True)
    for fam in families:
        (iter_dir / f"{fam}-review.yaml").write_text(
            yaml.safe_dump({"reviewer_id": fam, "goal_satisfied": True}),
            encoding="utf-8",
        )
    if with_plan:
        (iter_dir / "converged-plan.yaml").write_text(
            yaml.safe_dump({"decision": "RATIFIED", "iteration_id": name}),
            encoding="utf-8",
        )
    return iter_dir


def test_approve_happy_path_mints_and_revalidates(tmp_path):
    _make_consult(tmp_path)
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is True, res
    assert res["non_claude_reviewers"] == 2
    # marker written + re-validates against the live seal
    marker = tmp_path / ".consensus" / "design-approved"
    assert marker.exists()
    assert "re-validated" in res["revalidated"]
    # outcome sealed MECHANICALLY (no manual EDIT_ME)
    outcome = yaml.safe_load(
        (tmp_path / "consensus-state" / "active" / "iter-test"
         / "iteration-outcome.yaml").read_text()
    )
    assert outcome["closing_state"] in ac.SEALED_CLOSING_STATES
    assert outcome["panel"] == ["claude", "codex", "gemini"]


def test_approve_insufficient_reviewers_actionable_error(tmp_path):
    _make_consult(tmp_path, families=("codex",))  # only 1 non-claude
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "insufficient_reviewers"
    assert "need >=2" in res["error"]
    assert not (tmp_path / ".consensus" / "design-approved").exists()


def test_approve_does_not_author_missing_converged_plan(tmp_path):
    _make_consult(tmp_path, with_plan=False)
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "missing_converged_plan"
    # the flow must NOT have created the plan
    assert not (tmp_path / "consensus-state" / "active" / "iter-test"
                / "converged-plan.yaml").exists()


def test_approve_rejects_overbroad_scope(tmp_path):
    _make_consult(tmp_path)
    res = ac.approve_consult("iter-test", scope_glob="**", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "invalid_scope"


def test_approve_rejects_non_canonical_plan_name(tmp_path):
    _make_consult(tmp_path)
    res = ac.approve_consult("iter-test", scope_glob="src/**",
                             converged_plan="my-plan.yaml", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "non_canonical_converged_plan"


def test_approve_honors_env_repo_root_finding7(tmp_path, monkeypatch):
    """Finding #7: with no explicit repo_root, the flow resolves via the SAME
    strict CONSENSUS_MCP_REPO_ROOT-first resolver the shell binaries use."""
    _make_consult(tmp_path)
    # repo markers so the strict resolver accepts tmp_path
    (tmp_path / "consensus_mcp" / "validators").mkdir(parents=True)
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    res = ac.approve_consult("iter-test", scope_glob="src/**")  # no repo_root arg
    assert res["ok"] is True, res
    assert (tmp_path / ".consensus" / "design-approved").exists()


def test_approve_accepts_full_path_converged_plan_footgun(tmp_path):
    """The --converged-plan full-path form must not trip a false missing error."""
    iter_dir = _make_consult(tmp_path)
    full = str(iter_dir / "converged-plan.yaml")
    res = ac.approve_consult("iter-test", scope_glob="src/**",
                             converged_plan=full, repo_root=tmp_path)
    assert res["ok"] is True, res
