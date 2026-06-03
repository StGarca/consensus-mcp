"""Tests for the composed consult-approval flow (consult Q6 + Finding C/#7)."""
from __future__ import annotations

import yaml

from consensus_mcp import _approve_consult as ac


def _init_project(repo_root):
    """Make `repo_root` a valid consuming-project root: write .consensus/config.yaml
    (what `consensus init` writes), so the now-validated explicit --repo-root
    resolver accepts it (codex finding)."""
    cfg = repo_root / ".consensus" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml.safe_dump({"schema_version": 1}), encoding="utf-8")


def _make_consult(repo_root, name="iter-test", families=("codex", "gemini"),
                  with_plan=True, allowed_files=("src/**",), with_goal_packet=True):
    """Build a synthetic post-consult iteration: sealed reviews + converged-plan +
    a goal_packet (carrying the authorized allowed_files the scope gate checks)."""
    _init_project(repo_root)
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
    """P0.1 (consult-verified #1 blocker): a successful approve must ARM the gate
    by writing the session-active marker, not just mint the design marker -
    otherwise the gate stays dormant and edits are silently allowed after
    'approval'. Hypothesis-independent: assert the gate's OWN predicate."""
    from consensus_mcp import _session_state as ss
    _make_consult(tmp_path)
    assert ss.session_active(tmp_path) is False          # dormant before approve
    res = ac.approve_consult("iter-test", scope_glob="src/**", repo_root=tmp_path)
    assert res["ok"] is True, res
    assert res.get("gate_armed") is True
    assert ss.session_active(tmp_path) is True            # gate now armed


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
