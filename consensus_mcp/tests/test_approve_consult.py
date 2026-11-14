"""Tests for the composed consult-approval flow (consult Q6 + Finding C/#7)."""
from __future__ import annotations

import yaml
import pytest

from consensus_mcp import _approve_consult as ac


def test_init_objections_cli_without_scope_and_user_warning(tmp_path, monkeypatch, capsys):
    import json

    iter_dir = _make_consult(tmp_path, with_plan=False)
    target = iter_dir / "plan-objections.yaml"
    monkeypatch.setattr(ac, "_init_objections", lambda path: target)
    assert ac.main(["--iteration", "iter-test", "--init-objections",
                    "--repo-root", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["plan_objections_path"] == str(target)
    assert "host must never answer for them" in result["message"]
    assert not (iter_dir / "dispositions.yaml").exists()


def test_user_decision_cli_records_repeatable_answers_without_scope(tmp_path, monkeypatch, capsys):
    import json

    iter_dir = _make_consult(tmp_path)
    recorded = []

    def record(path, decisions):
        assert path == iter_dir
        recorded.extend(decisions)
        return path / "plan-objections.yaml"

    monkeypatch.setattr(ac, "record_user_decisions", record)
    assert ac.main(["--iteration", "iter-test", "--user-decision", "one=yay",
                    "--user-decision", "two=send_back", "--repo-root", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert recorded == ["one=yay", "two=send_back"]
    assert "host must ask the user and never answer for them" in result["message"]
    assert not (iter_dir / "iteration-outcome.yaml").exists()


def test_new_marker_write_failure_rolls_back_approval(tmp_path, monkeypatch):
    iter_dir = _make_consult(tmp_path)
    monkeypatch.setattr(ac, "objections_hash", lambda path: "a" * 64)
    original = ac.atomic_write_text

    def fail_marker(path, text):
        if path.name == "design-approved":
            raise OSError("marker binding failed")
        return original(path, text)

    monkeypatch.setattr(ac, "atomic_write_text", fail_marker)
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not result["ok"] and result["error_type"] == "marker_write_failed"
    assert not (tmp_path / ".consensus" / "design-approved").exists()
    assert not (iter_dir / "iteration-outcome.yaml").exists()


@pytest.mark.parametrize("tamper", ["table", "delete", "marker", "outcome"])
def test_plan_objection_hash_is_bound_and_revalidated(tmp_path, monkeypatch, tamper):
    from consensus_mcp import _design_approval as da
    from consensus_mcp._plan_passes import objections_hash

    iter_dir = _make_consult(tmp_path)
    table = iter_dir / "plan-objections.yaml"
    table.write_text("schema_version: 1\nobjections: []\n", encoding="utf-8")
    monkeypatch.setattr(ac, "check_plan_passes", lambda path: [])
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert result["ok"], result
    sha = objections_hash(iter_dir)
    outcome = yaml.safe_load((iter_dir / "iteration-outcome.yaml").read_text(encoding="utf-8"))
    marker_path = tmp_path / ".consensus" / "design-approved"
    marker = yaml.safe_load(marker_path.read_text(encoding="utf-8"))
    assert result["plan_objections_sha256"] == sha
    assert outcome["plan_objections_sha256"] == marker["plan_objections_sha256"] == sha
    assert da.verify_design_approval("src/x.py", tmp_path).ok
    if tamper == "table":
        table.write_text(table.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    elif tamper == "delete":
        table.unlink()
    elif tamper == "marker":
        del marker["plan_objections_sha256"]
        marker_path.write_text(yaml.safe_dump(marker), encoding="utf-8")
    else:
        del outcome["plan_objections_sha256"]
        (iter_dir / "iteration-outcome.yaml").write_text(yaml.safe_dump(outcome), encoding="utf-8")
    check = da.verify_design_approval("src/x.py", tmp_path)
    assert not check.ok and "plan_objections_sha256" in check.reason


def _init_project(repo_root, governance_mode="continuous"):
    """Make `repo_root` a valid consuming-project root: write .consensus/config.yaml
    (what `consensus init` writes), so the now-validated explicit --repo-root
    resolver accepts it (codex finding)."""
    cfg = repo_root / ".consensus" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml.safe_dump({
        "schema_version": 1,
        "governance": {"mode": governance_mode},
    }), encoding="utf-8")


def _make_consult(repo_root, name="iter-test", families=("codex", "gemini"),
                  with_plan=True, allowed_files=("src/**",), with_goal_packet=True,
                  governance_mode="continuous"):
    """Build a synthetic post-consult iteration: sealed reviews + converged-plan +
    a goal_packet (carrying the authorized allowed_files the scope gate checks)."""
    _init_project(repo_root, governance_mode)
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
    if with_goal_packet:
        (iter_dir / "goal_packet.yaml").write_text(
            yaml.safe_dump({"pilot_id": name, "allowed_files": list(allowed_files)}),
            encoding="utf-8",
        )
    return iter_dir


@pytest.mark.parametrize("tier,allowed", [("quick", True), ("standard", False), ("deep", False)])
def test_approval_enforces_summary_tier_before_sealing(tmp_path, tier, allowed):
    iter_dir = _make_consult(tmp_path)
    (iter_dir / "consult-summary.yaml").write_text(
        yaml.safe_dump({"tier": tier}), encoding="utf-8")
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert result["ok"] is allowed, result
    assert (iter_dir / "iteration-outcome.yaml").exists() is allowed
    assert (tmp_path / ".consensus" / "design-approved").exists() is allowed
    if not allowed:
        assert result["error_type"] == "invalid_plan_passes"
        assert "requires a sealed plan pass" in result["error"]


def test_approval_fails_closed_on_unknown_summary_tier(tmp_path):
    iter_dir = _make_consult(tmp_path)
    (iter_dir / "consult-summary.yaml").write_text("tier: other\n", encoding="utf-8")
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not result["ok"] and result["error_type"] == "invalid_plan_passes"
    assert not (iter_dir / "iteration-outcome.yaml").exists()


def _review_plan(repo_root, tier="standard", *, objections=None, models=None):
    from consensus_mcp.tests.test_plan_passes import seal_round

    directory = _make_consult(repo_root)
    ac._init_dispositions(directory)
    (directory / "consult-summary.yaml").write_text(yaml.safe_dump({
        "tier": tier, "seats": [{"seat": "codex"}, {"seat": "gemini"}],
    }), encoding="utf-8")
    seal_round(directory, objections, models=models)
    return directory


@pytest.mark.parametrize("tier", ["standard", "deep"])
def test_approval_accepts_current_plan_review_without_objections(tmp_path, tier):
    _review_plan(tmp_path, tier)
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert result["ok"], result
    assert result["plan_objections_sha256"]


def test_approval_refuses_stale_plan_review(tmp_path):
    directory = _review_plan(tmp_path)
    (directory / "converged-plan.yaml").write_text("decision: changed\n", encoding="utf-8")
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not result["ok"] and "stale" in result["error"]
    assert not (directory / "iteration-outcome.yaml").exists()


def test_approval_refuses_plan_review_with_one_model_family(tmp_path):
    directory = _review_plan(tmp_path, models={"codex": "same-model", "gemini": "SAME-MODEL"})
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not result["ok"] and ">=2 distinct non-host model families" in result["error"]
    assert not (directory / "iteration-outcome.yaml").exists()


def test_approval_requires_current_confirmation_after_accepted_edit(tmp_path):
    from consensus_mcp.tests.test_plan_passes import respond, seal_round

    directory = _review_plan(tmp_path, objections={"codex": ["[gap] Add rollback"]})
    respond(directory, "accepted", host_response="Added rollback")
    (directory / "converged-plan.yaml").write_text("decision: include rollback\n", encoding="utf-8")
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not result["ok"] and result["error_type"] == "invalid_plan_passes"
    assert "--confirm-pass" in result["error"]
    assert not (directory / "iteration-outcome.yaml").exists()
    seal_round(directory, pass_kind="confirm")
    approved = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert approved["ok"], approved
    assert approved["plan_objections_sha256"]
    assert (directory / "iteration-outcome.yaml").exists()
    with (directory / "dispositions.yaml").open("a", encoding="utf-8") as stream:
        stream.write("# Changed after confirmation\n")
    stale = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not stale["ok"] and "--confirm-pass" in stale["error"]


def test_confirmation_holdout_approval_requires_user_decision(tmp_path):
    from consensus_mcp.tests.test_plan_passes import respond, seal_round

    directory = _review_plan(tmp_path, objections={"codex": ["[gap] Add rollback"]})
    respond(directory, "accepted", host_response="Added rollback")
    (directory / "converged-plan.yaml").write_text("decision: include rollback\n", encoding="utf-8")
    _, confirmation = seal_round(directory, {"codex": ["[unrelated_change] Unrequested step"]}, pass_kind="confirm")
    assert confirmation["status"] == "needs_user_decision"
    respond(directory, "accepted", host_response="Host says it is fine")
    refused = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not refused["ok"] and "host must ask the user" in refused["error"]
    assert not (directory / "iteration-outcome.yaml").exists()
    row = yaml.safe_load((directory / "plan-objections.yaml").read_text(encoding="utf-8"))["objections"][-1]
    recorded = ac.approve_consult("iter-test", repo_root=tmp_path, user_decisions=[row["id"] + "=nay"])
    assert recorded["ok"], recorded
    assert ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)["ok"]


@pytest.mark.parametrize("tier", ["standard", "deep"])
@pytest.mark.parametrize("response", ["pending", "rebutted"])
def test_approval_requires_user_to_dismiss_plan_holdout(tmp_path, tier, response):
    from consensus_mcp.tests.test_plan_passes import respond

    directory = _review_plan(tmp_path, tier, objections={"codex": ["[gap] Missing guard"]})
    if response == "rebutted":
        respond(directory, "rebutted", evidence="Existing guard at src/check.py:42")
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not result["ok"] and "host must ask the user" in result["error"]
    assert not (directory / "iteration-outcome.yaml").exists()
    recorded = ac.approve_consult("iter-test", repo_root=tmp_path,
                                  user_decisions=["plan-1/codex/0=nay"])
    assert recorded["ok"], recorded
    table = yaml.safe_load((directory / "plan-objections.yaml").read_text(encoding="utf-8"))
    assert table["objections"][0]["decided_by"] == "user"
    assert table["objections"][0]["user_decision"] == "nay"
    assert ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)["ok"]


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


def test_approve_on_demand_seals_results_without_enforcement(tmp_path):
    _make_consult(tmp_path, governance_mode="on-demand")
    stale_session = tmp_path / ".consensus" / "session-active"
    stale_design = tmp_path / ".consensus" / "design-approved"
    stale_session.write_text("stale", encoding="utf-8")
    stale_design.write_text("stale", encoding="utf-8")

    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)

    assert res["ok"] is True, res
    assert res["governance_mode"] == "on-demand"
    assert res["gate_armed"] is False
    assert res["marker_path"] is None
    assert not stale_session.exists()
    assert not stale_design.exists()
    outcome = yaml.safe_load(
        (tmp_path / "consensus-state" / "active" / "iter-test"
         / "iteration-outcome.yaml").read_text()
    )
    assert outcome["closing_state"] in ac.SEALED_CLOSING_STATES
    assert "No edit gate" in res["next_steps"]["2_no_enforcement"]


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
    # allowed_files=['**'] so the scope passes the escalation gate and reaches
    # mint_design_approval's absolute-overbroad guard (the behavior under test).
    _make_consult(tmp_path, allowed_files=("**",))
    res = ac.approve_consult("iter-test", scope_glob="**", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "invalid_scope"


def test_approve_rejects_scope_escalation_beyond_allowed(tmp_path):
    """kimi finding: approving a scope BROADER than the goal_packet authorized is a
    privilege escalation - reject it. allowed=['src/foo.py'], scope='**'."""
    _make_consult(tmp_path, allowed_files=("src/foo.py",))
    res = ac.approve_consult("iter-test", scope_glob="**", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "scope_escalation"
    assert not (tmp_path / ".consensus" / "design-approved").exists()


def test_approve_accepts_scope_narrowing_within_allowed(tmp_path):
    """kimi finding: NARROWING the scope is fine. allowed=['src/*'], scope=
    'src/bar.py' (a subset) must approve."""
    _make_consult(tmp_path, allowed_files=("src/*",))
    res = ac.approve_consult("iter-test", scope_glob="src/bar.py", repo_root=tmp_path)
    assert res["ok"] is True, res


def test_approve_requires_goal_packet_for_scope_check(tmp_path):
    """kimi finding: with no goal_packet there is nothing to confine the scope to;
    fail closed with an actionable error rather than minting an unbounded marker."""
    _make_consult(tmp_path, with_goal_packet=False)
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "missing_goal_packet"
    assert not (tmp_path / ".consensus" / "design-approved").exists()


def test_approve_surfaces_disarm_next_step(tmp_path):
    """gemini finding: a successful approve must surface the DISARM/close command
    so the gate is never left armed after the edits land."""
    _make_consult(tmp_path)
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is True, res
    disarm = res["next_steps"]["3_disarm_when_done"]
    assert "consensus-mcp-seal-iteration close" in disarm
    assert "iter-test" in disarm


def test_fnmatch_subset_matches_gate_containment():
    """gemini-rev-001 (round 7): scope confinement uses the gate's fnmatch
    containment. '*'/'**' match any chars incl '/'. Key cases:"""
    fs = ac._fnmatch_subset
    assert fs("src/**", "src/**") is True             # reflexive
    assert fs("src/a.py", "src/**") is True           # narrowing
    assert fs("src/sub/**", "src/**") is True
    assert fs("src", "src/**") is False               # bare dir not under src/**
    assert fs("**", "src/**") is False                # broader -> escalation
    assert fs("src/sub/x.py", "src/*") is True        # fnmatch '*' spans '/'
    # EXACT (automaton) containment accepts narrowings the conservative version
    # could not prove, with no escalation:
    assert fs("src/a/test", "src/*/test") is True
    assert fs("a/b/c", "a/**/c") is True
    assert fs("a/c", "a/**/c") is False               # '**' requires >=1 segment


def test_fnmatch_subset_no_escalation_exhaustive():
    """Security property (regression guard for the round-7 escalation bug): over an
    EXHAUSTIVE char-level matrix of wildcard patterns, _fnmatch_subset must NEVER
    declare a subset that lets a string matched by `a` escape `b` under fnmatch.
    A curated matrix missed this class once; brute-force is the real check."""
    import fnmatch as _fn
    import itertools
    alpha = ["a", "b", "/", "x"]
    strings = set()
    for n in range(1, 5):
        for c in itertools.product(alpha, repeat=n):
            strings.add("".join(c))
    patom = ["a", "b", "x", "/", "*"]
    pats = set()
    for n in range(1, 4):
        for c in itertools.product(patom, repeat=n):
            pats.add("".join(c))
    escalations = 0
    for a in pats:
        if not any(_fn.fnmatchcase(s, a) for s in strings):
            continue
        for b in pats:
            if ac._fnmatch_subset(a, b):
                for s in strings:
                    if _fn.fnmatchcase(s, a) and not _fn.fnmatchcase(s, b):
                        escalations += 1
                        break
    assert escalations == 0, f"{escalations} escalation pairs (over-accepted subset)"


def test_fnmatch_patterns_overlap_matches_gate_semantics():
    """The forbidden overlap test must use the gate's OWN fnmatch semantics, where
    '*' (and '**') matches ANY chars INCLUDING '/'. Validated cases:"""
    ov = ac._fnmatch_patterns_overlap
    assert ov("src/*", "src/a/b/c") is True          # fnmatch '*' spans '/'
    assert ov("consensus_mcp/**", "consensus-state/*") is False
    # codex-rev-001: a PARTIAL intersection where NEITHER pattern is a subset of
    # the other must still be caught (the security under-veto).
    assert ov("src/*/secret.py", "src/sub/**") is True   # share src/sub/secret.py
    assert ov("a/**/c", "**/secret.py") is False         # tails 'c' vs 'secret.py' differ
    # codex-rev-001 (round 7): bracket expressions are fail-safe -> conservative
    # overlap (never an under-veto), since the exact DP cannot model [seq] classes.
    assert ov("src/[abc].py", "src/sub/**") is True       # conservative, not missed
    assert ov("src/x.py", "src/[!q]*") is True            # bracket on either side


def test_forbidden_vetoes_uses_fnmatch_overlap():
    """The forbidden veto catches real overlaps (under fnmatch semantics) including
    partial intersections, and never vetoes a genuinely disjoint scope."""
    fv = ac._forbidden_vetoes
    # real overlaps -> veto
    assert fv("consensus-state/**", "consensus-state/") is True
    assert fv("**", "consensus-state/") is True
    assert fv("consensus-state/sub/x.py", "consensus-state/") is True
    # under fnmatch, 'src/*.py' CAN match 'src/sub/x.py' (forbidden subtree) -> veto
    assert fv("src/*.py", "src/sub/") is True
    # codex-rev-001 partial intersection (neither a subset) -> veto
    assert fv("src/*/secret.py", "src/sub/") is True
    # genuinely disjoint (different first segment) -> NO veto
    assert fv("consensus_mcp/**", "consensus-state/") is False
    assert fv("consensus_mcp/_x.py", "consensus-state/") is False


def test_approve_allows_scope_disjoint_from_forbidden(tmp_path):
    """End-to-end: a scope that does not overlap forbidden_files is NOT vetoed."""
    iter_dir = _make_consult(tmp_path, allowed_files=("consensus_mcp/**",))
    gp = iter_dir / "goal_packet.yaml"
    import yaml as _y
    data = _y.safe_load(gp.read_text())
    data["forbidden_files"] = ["consensus-state/"]
    gp.write_text(_y.safe_dump(data), encoding="utf-8")
    res = ac.approve_consult("iter-test", scope_glob="consensus_mcp/**",
                             repo_root=tmp_path)
    assert res["ok"] is True, res


def test_approve_rejects_bare_dir_scope_under_doublestar_allowed(tmp_path):
    """kimi-rev-001 at approve level: allowed=['src/**'], scope='src' must be a
    scope escalation (it would authorize editing a file literally named 'src' that
    the consult never allowed)."""
    _make_consult(tmp_path, allowed_files=("src/**",))
    res = ac.approve_consult("iter-test", scope_glob="src", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "scope_escalation"


def test_is_within_uses_xplat_normalization(tmp_path):
    """gemini-rev-001/grok-rev-003: containment must use the shared xplat
    normalizer (not a case-sensitive relative_to). A descendant is within; a
    sibling is not."""
    parent = tmp_path / "repo"
    parent.mkdir()
    (parent / "a").mkdir()
    assert ac._is_within(parent / "a", parent) is True
    assert ac._is_within(parent, parent) is True
    assert ac._is_within(tmp_path / "other", parent) is False


def test_approve_rejects_out_of_repo_iteration_path(tmp_path):
    """gemini-rev-001/codex-rev-001/kimi-rev-003: an absolute --iteration that
    resolves OUTSIDE the repo must be refused, never read from."""
    _make_consult(tmp_path)
    outside = tmp_path.parent / "evil-outside-iter"
    res = ac.approve_consult(str(outside), scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "iteration_outside_repo"


def test_approve_rejects_dotdot_in_scope(tmp_path):
    """kimi-rev-005: a '..' segment in the approval scope is a traversal smell;
    reject it outright as an invalid scope."""
    _make_consult(tmp_path, allowed_files=("**",))
    res = ac.approve_consult("iter-test", scope_glob="../etc/passwd", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "invalid_scope"


def test_approve_rejects_scope_overlapping_forbidden_files(tmp_path):
    """kimi-rev-001: allowed_files alone is not enough - a scope that overlaps the
    goal_packet's forbidden_files must be rejected (forbidden wins)."""
    iter_dir = _make_consult(tmp_path, allowed_files=("**",))
    gp = iter_dir / "goal_packet.yaml"
    import yaml as _y
    data = _y.safe_load(gp.read_text())
    data["forbidden_files"] = ["consensus-state/"]
    gp.write_text(_y.safe_dump(data), encoding="utf-8")
    res = ac.approve_consult("iter-test", scope_glob="consensus-state/**",
                             repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "forbidden_scope"


def test_approve_rolls_back_marker_on_gate_arm_failure(tmp_path, monkeypatch):
    """grok-rev-001 (blocking): if arming the session marker fails AFTER the
    design-approved marker is minted, approval must be ALL-OR-NOTHING - the
    design-approved marker is rolled back so no half-state persists."""
    _make_consult(tmp_path)

    def _boom(*a, **k):
        raise OSError("simulated session-marker write failure")
    monkeypatch.setattr(ac, "write_session_marker", _boom)
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "gate_arm_failed"
    # the just-minted design-approved marker must NOT survive the failed approve.
    assert not (tmp_path / ".consensus" / "design-approved").exists()
    # grok-rev-003: TRANSACTIONAL - the outcome this call wrote is rolled back too.
    assert not (tmp_path / "consensus-state" / "active" / "iter-test"
                / "iteration-outcome.yaml").exists()


def test_approve_arm_failure_preserves_preexisting_outcome(tmp_path, monkeypatch):
    """grok-rev-003: rollback removes only what THIS call wrote. A pre-existing
    SEALED iteration-outcome.yaml (from an earlier run) must be preserved on a
    failed approve, not deleted."""
    iter_dir = _make_consult(tmp_path)
    outcome = iter_dir / "iteration-outcome.yaml"
    sealed = next(iter(ac.SEALED_CLOSING_STATES))
    outcome.write_text(yaml.safe_dump(
        {"iteration_id": "iter-test", "closing_state": sealed,
         "sealed_by": "prior-run"}), encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("arm failure")
    monkeypatch.setattr(ac, "write_session_marker", _boom)
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is False and res["error_type"] == "gate_arm_failed"
    # the pre-existing sealed outcome survives (we did not create it).
    assert outcome.exists()
    assert yaml.safe_load(outcome.read_text())["sealed_by"] == "prior-run"


def test_approve_validates_explicit_repo_root(tmp_path):
    """codex finding: an explicit --repo-root that is not a consensus project root
    (no .consensus/config.yaml, no source markers) must be REJECTED, not accepted
    verbatim - otherwise the gate arms in an arbitrary tree."""
    bogus = tmp_path / "not-a-project"
    bogus.mkdir()
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=bogus)
    assert res["ok"] is False
    assert res["error_type"] == "repo_root_unresolved"
    assert ".consensus" in res["error"]


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


def test_approve_arms_the_gate(tmp_path):
    """Continuous mode preserves approval and session marker artifacts."""
    from consensus_mcp import _session_state as ss
    _make_consult(tmp_path)
    marker = tmp_path / ".consensus" / "session-active"
    assert ss.gate_should_enforce(tmp_path) is True
    assert not marker.exists()
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is True, res
    assert res.get("gate_armed") is True
    assert marker.exists()


def test_review_family_handles_hash_and_digit_suffixes():
    """The derive_pass_id default makes kimi's mirror `kimi-review-kimi-<hash>.yaml`;
    the family counter must recognize hash suffixes as well as digit suffixes."""
    from consensus_mcp._design_approval import _review_family
    assert _review_family("kimi-review-kimi-0c532a488f1cbfd5.yaml") == "kimi"
    assert _review_family("grok-review-4.yaml") == "grok"
    assert _review_family("codex-review.yaml") == "codex"
    assert _review_family("review-packet.yaml") is None  # not a review artifact


def test_approve_counts_round_keyed_review_filenames(tmp_path):
    """H3 interaction: when reviewers seal under distinct pass_ids, their mirror
    files can be round-keyed (e.g. 'kimi-review-4.yaml'). The >=2 precondition AND
    the panel derivation must still recognize those families, not just the bare
    '<fam>-review.yaml' form."""
    _init_project(tmp_path)
    iter_dir = tmp_path / "consensus-state" / "active" / "iter-rk"
    iter_dir.mkdir(parents=True)
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"reviewer_id": "codex"}), encoding="utf-8")
    (iter_dir / "kimi-review-4.yaml").write_text(
        yaml.safe_dump({"reviewer_id": "kimi"}), encoding="utf-8")
    (iter_dir / "converged-plan.yaml").write_text(
        yaml.safe_dump({"decision": "RATIFIED"}), encoding="utf-8")
    (iter_dir / "goal_packet.yaml").write_text(
        yaml.safe_dump({"allowed_files": ["src/**"]}), encoding="utf-8")
    res = ac.approve_consult("iter-rk", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is True, res
    assert res["non_claude_reviewers"] == 2  # codex + kimi (round-keyed) both counted
    outcome = yaml.safe_load((iter_dir / "iteration-outcome.yaml").read_text())
    assert set(outcome["panel"]) == {"claude", "codex", "kimi"}


def test_approve_accepts_full_path_converged_plan_footgun(tmp_path):
    """The --converged-plan full-path form must not trip a false missing error."""
    iter_dir = _make_consult(tmp_path)
    full = str(iter_dir / "converged-plan.yaml")
    res = ac.approve_consult("iter-test", scope_glob="src/**",
                             converged_plan=full, repo_root=tmp_path)
    assert res["ok"] is True, res


# --------------------------------------------------------------------------- #
# G3: multi-glob approvals (a single approval covering a multi-root change).
# --------------------------------------------------------------------------- #

def test_approve_multi_glob_within_allowed_mints(tmp_path):
    _make_consult(tmp_path, allowed_files=("consensus_mcp/**", "docs/**", "pyproject.toml"))
    res = ac.approve_consult(
        "iter-test",
        scope_glob=["consensus_mcp/_x.py", "docs/guide.md", "pyproject.toml"],
        repo_root=tmp_path)
    assert res["ok"] is True, res
    assert res["scope_glob"] is None          # scalar omitted for multi
    assert res["scope_globs"] == ["consensus_mcp/_x.py", "docs/guide.md", "pyproject.toml"]
    data = yaml.safe_load(
        (tmp_path / ".consensus" / "design-approved").read_text())
    assert data["schema_version"] == 2
    assert data["scope_globs"] == ["consensus_mcp/_x.py", "docs/guide.md", "pyproject.toml"]


def test_approve_single_element_list_keeps_scalar_output(tmp_path):
    _make_consult(tmp_path, allowed_files=("src/**",))
    res = ac.approve_consult("iter-test", scope_glob=["src/foo.py"], repo_root=tmp_path)
    assert res["ok"] is True, res
    assert res["scope_glob"] == "src/foo.py"   # scalar preserved for single
    assert res["scope_globs"] == ["src/foo.py"]
    data = yaml.safe_load((tmp_path / ".consensus" / "design-approved").read_text())
    assert data["schema_version"] == 1 and data["scope_glob"] == "src/foo.py"


def test_approve_multi_glob_one_entry_escalates_rejected(tmp_path):
    # docs/** is NOT in allowed_files -> the WHOLE multi-glob approval is rejected.
    _make_consult(tmp_path, allowed_files=("src/**",))
    res = ac.approve_consult(
        "iter-test", scope_glob=["src/a.py", "docs/b.md"], repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "scope_escalation"


def test_approve_multi_glob_overbroad_entry_rejected(tmp_path):
    _make_consult(tmp_path, allowed_files=("**",))
    res = ac.approve_consult(
        "iter-test", scope_glob=["src/a.py", "**"], repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "invalid_scope"


def test_approve_multi_glob_too_many_rejected(tmp_path):
    _make_consult(tmp_path, allowed_files=("**",))
    many = [f"root{i}/x.py" for i in range(9)]
    res = ac.approve_consult("iter-test", scope_glob=many, repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "invalid_scope"


def test_approve_multi_glob_dotdot_entry_rejected(tmp_path):
    _make_consult(tmp_path, allowed_files=("**",))
    res = ac.approve_consult(
        "iter-test", scope_glob=["src/a.py", "../etc/passwd"], repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "invalid_scope"
