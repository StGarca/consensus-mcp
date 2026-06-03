"""consensus.start_consult (P2.1): the one-call cold-start scaffold entrypoint."""
from __future__ import annotations

import yaml

from consensus_mcp import _start_consult as sc
from consensus_mcp import _session_state as ss


def test_start_consult_scaffolds_and_arms_gate(tmp_path):
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


def test_start_consult_requires_scope(tmp_path):
    res = sc.start_consult("q", scope_glob="", repo_root=tmp_path)
    assert res["ok"] is False and res["error_type"] == "missing_scope"


def test_start_consult_unique_iteration_ids(tmp_path):
    a = sc.start_consult("q1", scope_glob="x.py", reviewers=["codex"], repo_root=tmp_path)
    b = sc.start_consult("q2-different", scope_glob="x.py", reviewers=["codex"], repo_root=tmp_path)
    assert a["iteration"] != b["iteration"]
