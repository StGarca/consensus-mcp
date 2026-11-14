"""Consult Q3 (iteration-architect-hardening-2026-06-11): `consensus
results` gains a read-only architect_goals section derived via the shared
loop_step helper - the v2.0.0 UX gap was zero architect awareness."""
from __future__ import annotations

from pathlib import Path

from consensus_mcp import _architect_paths as ap
from consensus_mcp import _results_rollup as rollup
from consensus_mcp.tools import results as results_tool


def _goal(tmp_path: Path, name: str) -> Path:
    g = ap.goal_dir(tmp_path, name)
    g.mkdir(parents=True)
    return g


def test_results_payload_lists_architect_goals(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("CONSENSUS_MCP_STATE_ROOT", raising=False)
    g1 = _goal(tmp_path, "g1")
    ap.seal_artifact(g1 / ap.OUTCOME_FILENAME,
                     {"closing_state": "delivered", "cycle": 1})
    g2 = _goal(tmp_path, "g2")
    ap.seal_artifact(ap.spec_path(g2), {"kind": "spec", "body": "x"})
    g3 = _goal(tmp_path, "g3")
    ap.seal_artifact(ap.spec_path(g3), {"kind": "spec", "body": "x"})
    ap.seal_artifact(g3 / ap.SPEC_APPROVAL_FILENAME,
                     {"spec_file": "spec.yaml", "spec_sha256": "0" * 64,
                      "base_sha": "0" * 40, "approver": "op"})
    payload = results_tool.handle()
    goals = {g["goal_id"]: g["state"] for g in payload["architect_goals"]}
    assert goals["g1"] == "closed:delivered"
    assert goals["g2"] == "awaiting_spec_approval"
    assert goals["g3"] == "blocked:spec_approval_binding_mismatch"


def test_results_architect_goals_empty_without_dir(tmp_path: Path,
                                                   monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("CONSENSUS_MCP_STATE_ROOT", raising=False)
    payload = results_tool.handle()
    assert payload["architect_goals"] == []


def test_render_table_includes_architect_section(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("CONSENSUS_MCP_STATE_ROOT", raising=False)
    g1 = _goal(tmp_path, "g1")
    ap.seal_artifact(g1 / ap.OUTCOME_FILENAME,
                     {"closing_state": "delivered", "cycle": 2})
    card = rollup.build_scorecard()
    text = rollup.render_table(card)
    assert "ARCHITECT GOALS" in text
    assert "g1" in text and "closed:delivered" in text
