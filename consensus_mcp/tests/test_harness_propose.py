from __future__ import annotations

import json
from pathlib import Path

import yaml

from consensus_mcp.tool_registry import ToolRegistry
from consensus_mcp.tools import harness_propose


def test_harness_propose_refuses_missing_or_empty_traces(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path / "consensus-state"))
    result = harness_propose.handle()
    assert result["ok"] is False
    assert "no trace rows" in result["error"]


def test_harness_propose_generates_proposal_from_results_trace(tmp_path, monkeypatch):
    state_root = tmp_path / "consensus-state"
    trace_dir = state_root / "state"
    trace_dir.mkdir(parents=True)
    row = {
        "iteration_id": "iteration-x",
        "findings": [
            {"id": "codex-rev-001", "severity": "high", "summary": "missed gate"},
            {"id": "kimi-rev-001", "severity": "blocking", "summary": "unsafe scope"},
        ],
    }
    (trace_dir / "results-v1.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state_root))

    out = tmp_path / "proposal.yaml"
    result = harness_propose.handle(output_path=str(out), max_records=10)

    assert result["ok"] is True
    assert result["evidence_count"] >= 1
    assert result["recommendation_count"] >= 1
    proposal = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert proposal["safety_policy"]["proposal_only"] is True
    assert proposal["safety_policy"]["no_source_mutation"] is True
    assert proposal["evidence"]
    assert proposal["recommendations"]
    assert all(
        item.startswith(("consensus_mcp/dispatch_templates/", "consensus_mcp/looper_plan/rubrics/", "consensus_mcp/validators/", "consensus_mcp/tests/", "docs/workflows/", "docs/superpowers/specs/"))
        for item in proposal["allowed_files"]
    )


def test_harness_propose_detects_dispatch_failed_events(tmp_path, monkeypatch):
    """dispatch_failed events without ok=False should still be counted."""
    state_root = tmp_path / "consensus-state"
    trace_dir = state_root / "state"
    trace_dir.mkdir(parents=True)
    row = {
        "timestamp_utc": "2026-06-25T03:23:44Z",
        "event": "dispatch_failed",
        "reviewer_id": "kimi",
        "iteration_id": "iteration-test",
        "error_type": "_SnapshotIndexError",
        "error": "integrity snapshot exceeded its budget",
    }
    (trace_dir / "dispatch-log.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state_root))

    out = tmp_path / "proposal.yaml"
    result = harness_propose.handle(output_path=str(out), max_records=10)

    assert result["ok"] is True
    proposal = yaml.safe_load(out.read_text(encoding="utf-8"))
    recs = proposal["recommendations"]
    # Should have the dispatch failure recommendation (rec-002)
    dispatch_rec = [r for r in recs if r["id"] == "harness-rec-002"]
    assert dispatch_rec, "expected harness-rec-002 for dispatch_failed event"
    assert "1 failed dispatch" in dispatch_rec[0]["rationale"]
    assert "_SnapshotIndexError" in dispatch_rec[0]["rationale"]
    assert "kimi" in dispatch_rec[0]["rationale"]


def test_harness_propose_registers_mcp_tool():
    reg = ToolRegistry()
    harness_propose.register(reg)
    [entry] = reg.list_tools()
    assert entry["name"] == "harness.propose"
    assert entry["inputSchema"]["type"] == "object"
