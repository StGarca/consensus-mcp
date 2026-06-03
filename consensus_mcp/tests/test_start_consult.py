"""consensus.start_consult (P2.1): the one-call cold-start scaffold entrypoint."""
from __future__ import annotations

import yaml

from consensus_mcp import _start_consult as sc
from consensus_mcp import _session_state as ss


def _init_project(repo_root):
    """Make repo_root a valid consuming-project root (.consensus/config.yaml) so
    the now-validated explicit --repo-root resolver accepts it (codex finding)."""
    cfg = repo_root / ".consensus" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml.safe_dump({"schema_version": 1}), encoding="utf-8")


def test_start_consult_scaffolds_and_arms_gate(tmp_path):
    _init_project(tmp_path)
    res = sc.start_consult("Should we parallelize dispatch?",
                           scope_glob="consensus_mcp/x.py",
                           reviewers=["codex", "gemini"], repo_root=tmp_path)
    assert res["ok"] is True, res
    iter_dir = tmp_path / "consensus-state" / "active" / res["iteration"]
    assert (iter_dir / "goal_packet.yaml").exists()
    assert (iter_dir / "review-packet.yaml").exists()
    gp = yaml.safe_load((iter_dir / "goal_packet.yaml").read_text())
    assert gp["pilot_id"] == res["iteration"]
    assert gp["allowed_files"] == ["consensus_mcp/x.py"]
    assert res["gate_armed"] is True
    assert ss.session_active(tmp_path) is True            # gate armed at start
    assert "consensus-mcp-approve" in res["next_steps"]["3_approve_to_unblock_edits"]
    # gemini finding: the terminal DISARM step is surfaced too.
    assert "consensus-mcp-seal-iteration close" in res["next_steps"]["4_disarm_when_done"]


def test_start_consult_requires_scope(tmp_path):
    _init_project(tmp_path)
    res = sc.start_consult("q", scope_glob="", repo_root=tmp_path)
    assert res["ok"] is False and res["error_type"] == "missing_scope"


def test_start_consult_unique_iteration_ids(tmp_path):
    _init_project(tmp_path)
    a = sc.start_consult("q1", scope_glob="x.py", reviewers=["codex"], repo_root=tmp_path)
    b = sc.start_consult("q2-different", scope_glob="x.py", reviewers=["codex"], repo_root=tmp_path)
    assert a["iteration"] != b["iteration"]


def test_start_consult_uses_configured_panel(tmp_path):
    """codex-rev-002 / kimi-rev-006: when reviewers are not passed explicitly,
    start_consult dispatches the project's CONFIGURED panel (contributors.enabled
    minus host), not a hardcoded set."""
    cfg = tmp_path / ".consensus" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml.safe_dump({
        "contributors": {
            "enabled": ["claude", "codex", "kimi"],
            "profiles": {"claude": {"kind": "host"}},
        }
    }), encoding="utf-8")
    res = sc.start_consult("q", scope_glob="x.py", repo_root=tmp_path)
    assert res["ok"] is True, res
    cmds = res["next_steps"]["1_dispatch_each_reviewer_in_its_OWN_shell"]
    joined = "\n".join(cmds)
    assert "dispatch-codex" in joined and "dispatch-kimi" in joined
    assert "dispatch-claude" not in joined        # host excluded
    assert "dispatch-gemini" not in joined         # not in configured panel
    assert "dispatch-grok" not in joined


def test_start_consult_clears_stale_design_approved(tmp_path):
    """kimi-rev-004: a fresh (unapproved) consult must clear any prior
    design-approved marker so it cannot authorize an OLD scope for the new
    iteration (marker poisoning)."""
    _init_project(tmp_path)
    stale = tmp_path / ".consensus" / "design-approved"
    stale.write_text("stale prior approval\n", encoding="utf-8")
    res = sc.start_consult("q", scope_glob="x.py", reviewers=["codex"], repo_root=tmp_path)
    assert res["ok"] is True, res
    assert not stale.exists()                      # cleared on arm


def test_start_consult_cli_defaults_to_configured_panel(tmp_path, capsys):
    """codex-rev-001: the CLI's --reviewers default must be None (not a hardcoded
    list) so an omitted flag falls through to the configured panel, not a shadow
    default."""
    import json
    cfg = tmp_path / ".consensus" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml.safe_dump({
        "contributors": {"enabled": ["claude", "codex", "kimi"],
                         "profiles": {"claude": {"kind": "host"}}}
    }), encoding="utf-8")
    rc = sc.main(["--question", "q", "--scope-glob", "x.py",
                  "--repo-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    cmds = "\n".join(out["next_steps"]["1_dispatch_each_reviewer_in_its_OWN_shell"])
    assert "dispatch-codex" in cmds and "dispatch-kimi" in cmds
    assert "dispatch-gemini" not in cmds and "dispatch-grok" not in cmds


def test_start_consult_fails_closed_when_stale_marker_unclearable(tmp_path, monkeypatch):
    """grok-rev-002: if a stale design-approved marker cannot be removed, start
    must FAIL CLOSED (not silently pass) so a poisoned prior-scope marker can't
    authorize edits under the new, unapproved consult."""
    _init_project(tmp_path)
    stale = tmp_path / ".consensus" / "design-approved"
    stale.write_text("stale\n", encoding="utf-8")

    real_unlink = stale.unlink

    class _StubPath:
        def exists(self):
            return True
        def unlink(self):
            raise OSError("permission denied")
        def __fspath__(self):
            return str(stale)
        def __str__(self):
            return str(stale)
    monkeypatch.setattr(sc, "_design_marker_path", lambda rr: _StubPath())
    res = sc.start_consult("q", scope_glob="x.py", reviewers=["codex"], repo_root=tmp_path)
    assert res["ok"] is False
    assert res["error_type"] == "stale_marker_unclearable"
    # codex-rev-001: the check runs BEFORE any iteration state is created, so no
    # half-created iteration dir is left behind.
    active = tmp_path / "consensus-state" / "active"
    assert not active.exists() or list(active.iterdir()) == []


def test_start_consult_rejects_uninitialized_repo_root(tmp_path):
    """codex finding: an explicit --repo-root with no .consensus/config.yaml (not a
    consensus project) must be rejected, not scaffolded into verbatim."""
    bogus = tmp_path / "not-a-project"
    bogus.mkdir()
    res = sc.start_consult("q", scope_glob="x.py", reviewers=["codex"], repo_root=bogus)
    assert res["ok"] is False
    assert res["error_type"] == "repo_root_unresolved"
