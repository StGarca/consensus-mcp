from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
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


def test_harness_propose_rejects_unsafe_recommendation_scope_with_rec_id(tmp_path, monkeypatch):
    """Loop-4 proposals must validate candidate scopes at generation time.

    If a recommendation tries to scope direct source mutation, the proposal must
    fail before writing YAML and name the offending recommendation so an
    operator can fix the generator rather than rubber-stamping unsafe scope.
    """
    state_root = tmp_path / "consensus-state"
    trace_dir = state_root / "state"
    trace_dir.mkdir(parents=True)
    (trace_dir / "dispatch-log.jsonl").write_text(
        json.dumps({"event": "dispatch_start", "reviewer_id": "codex"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state_root))
    monkeypatch.setattr(
        harness_propose,
        "_recommendations",
        lambda _rows: [{
            "id": "harness-rec-unsafe",
            "summary": "unsafe",
            "rationale": "tries to mutate source directly",
            "candidate_files": ["consensus_mcp/tools/harness_propose.py"],
        }],
    )

    out = tmp_path / "proposal.yaml"
    result = harness_propose.handle(output_path=str(out), max_records=10)

    assert result["ok"] is False
    assert "harness-rec-unsafe" in result["error"]
    assert "consensus_mcp/tools/harness_propose.py" in result["error"]
    assert not out.exists()


def test_harness_propose_registers_mcp_tool():
    reg = ToolRegistry()
    harness_propose.register(reg)
    [entry] = reg.list_tools()
    assert entry["name"] == "harness.propose"
    assert entry["inputSchema"]["type"] == "object"


# ---------------------------------------------------------------------------
# Q4 (M1-remediation, consult iteration-path-to-a-remediation-260caad1):
# output_path containment - a proposal-only tool must never be usable to
# overwrite the consensus enforcement surface and disable the design gate.
# ---------------------------------------------------------------------------


def _fake_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "hooks").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows Path.home()
    return fake_home


def test_harness_propose_refuses_output_path_onto_settings_json(tmp_path, monkeypatch):
    """An output_path resolving onto ~/.claude/settings.json is refused BEFORE
    any trace read or write, so the enforcement surface is never touched."""
    fake_home = _fake_home(tmp_path, monkeypatch)
    target = fake_home / ".claude" / "settings.json"
    result = harness_propose.handle(output_path=str(target))
    assert result["ok"] is False
    assert "enforcement surface" in result["error"]
    assert not target.exists()


def test_harness_propose_refuses_output_path_onto_consensus_hook(tmp_path, monkeypatch):
    fake_home = _fake_home(tmp_path, monkeypatch)
    target = fake_home / ".claude" / "hooks" / "consensus_pretooluse_gate.py"
    result = harness_propose.handle(output_path=str(target))
    assert result["ok"] is False
    assert "enforcement surface" in result["error"]
    assert not target.exists()


def test_harness_propose_refuses_tilde_output_path_onto_settings(tmp_path, monkeypatch):
    """The tilde form is expanded before the containment check, so
    '~/.claude/settings.json' is refused just like the absolute path."""
    fake_home = _fake_home(tmp_path, monkeypatch)
    result = harness_propose.handle(output_path="~/.claude/settings.json")
    assert result["ok"] is False
    assert "enforcement surface" in result["error"]
    assert not (fake_home / ".claude" / "settings.json").exists()


def test_harness_propose_refuses_hardlink_alias_to_settings(tmp_path, monkeypatch):
    """A hardlink alias to an existing settings.json resolves to its own path,
    so the pathname guard misses it; the inode-identity check catches it."""
    fake_home = _fake_home(tmp_path, monkeypatch)
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    alias = tmp_path / "alias.yaml"
    try:
        os.link(settings, alias)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hardlinks unsupported on this platform")
    result = harness_propose.handle(output_path=str(alias))
    assert result["ok"] is False
    assert "enforcement surface" in result["error"]
    # The alias content is untouched (still the original settings bytes).
    assert settings.read_text(encoding="utf-8") == "{}"


def test_harness_propose_safe_output_path_still_writes(tmp_path, monkeypatch):
    """NO REGRESSION: a normal output_path (not the enforcement surface) still
    produces a proposal even with a fake HOME set."""
    _fake_home(tmp_path, monkeypatch)
    state_root = tmp_path / "consensus-state"
    trace_dir = state_root / "state"
    trace_dir.mkdir(parents=True)
    (trace_dir / "results-v1.jsonl").write_text(
        json.dumps({"iteration_id": "iter-x",
                    "findings": [{"id": "codex-rev-001", "severity": "high"}]}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state_root))
    out = tmp_path / "safe-proposal.yaml"
    result = harness_propose.handle(output_path=str(out), max_records=10)
    assert result["ok"] is True, result
    assert out.exists()
