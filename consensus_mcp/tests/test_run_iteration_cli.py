"""Component 2 (consult iteration-approve-two-...-f641f060): a supported console
script that runs a full iteration end-to-end, so non-Claude hosts stop hand-rolling
shims that call consensus_run_iteration.handle() directly.

Weighted synthesis (Q4): always PRINT the structured outcome to stdout (codex's
no-extra-file batch path) AND write --outcome, defaulting to {iteration_dir}/
run-outcome.json (gemini/grok/kimi). Thread --host-peer-review-yaml too (grok).
"""
from __future__ import annotations

import json

from consensus_mcp import _run_iteration_cli as cli
from consensus_mcp.tools import consensus_run_iteration


def test_cli_invokes_handle_writes_default_outcome_and_prints(monkeypatch, tmp_path, capsys):
    """A5: parse args -> handle(**kwargs) -> write default run-outcome.json -> print."""
    captured = {}

    def fake_handle(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "workflow_mode": "propose-converge", "converged": True}

    monkeypatch.setattr(consensus_run_iteration, "handle", fake_handle)

    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    goal = tmp_path / "goal.yaml"
    goal.write_text("x: 1\n", encoding="utf-8")
    target = tmp_path / "t.md"
    target.write_text("doc\n", encoding="utf-8")

    rc = cli.main([
        "--iteration-dir", str(iter_dir),
        "--goal-packet", str(goal),
        "--target", str(target),
        "--repo-root", str(tmp_path),
    ])

    assert rc == 0
    # handle invoked with the mapped kwargs
    assert captured["iteration_dir"] == str(iter_dir)
    assert captured["goal_packet_path"] == str(goal)
    assert captured["target_path"] == str(target)
    assert captured["repo_root"] == str(tmp_path)
    # default outcome file written into the iteration dir
    outcome_file = iter_dir / "run-outcome.json"
    assert outcome_file.exists()
    data = json.loads(outcome_file.read_text(encoding="utf-8"))
    assert data["result"]["ok"] is True
    assert data["result"]["workflow_mode"] == "propose-converge"
    # structured outcome also printed to stdout (no-extra-file path)
    assert "propose-converge" in capsys.readouterr().out


def test_cli_nonzero_exit_when_handle_not_ok(monkeypatch, tmp_path):
    """A failing iteration returns a non-zero exit code."""
    monkeypatch.setattr(
        consensus_run_iteration, "handle",
        lambda **kw: {"ok": False, "error": "boom", "error_type": "X"},
    )
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    goal = tmp_path / "g.yaml"
    goal.write_text("x: 1\n", encoding="utf-8")
    target = tmp_path / "t.md"
    target.write_text("d\n", encoding="utf-8")

    rc = cli.main([
        "--iteration-dir", str(iter_dir),
        "--goal-packet", str(goal),
        "--target", str(target),
    ])
    assert rc == 1


def test_cli_reads_proposal_and_host_peer_files(monkeypatch, tmp_path):
    """--claude-proposal and --host-peer-review-yaml are read and passed as YAML text."""
    captured = {}
    monkeypatch.setattr(
        consensus_run_iteration, "handle",
        lambda **kw: captured.update(kw) or {"ok": True},
    )
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    goal = tmp_path / "g.yaml"
    goal.write_text("x: 1\n", encoding="utf-8")
    target = tmp_path / "t.md"
    target.write_text("d\n", encoding="utf-8")
    prop = tmp_path / "claude.yaml"
    prop.write_text("selected_target: foo\n", encoding="utf-8")
    hp = tmp_path / "hp.yaml"
    hp.write_text("goal_satisfied: true\n", encoding="utf-8")

    cli.main([
        "--iteration-dir", str(iter_dir),
        "--goal-packet", str(goal),
        "--target", str(target),
        "--claude-proposal", str(prop),
        "--host-peer-review-yaml", str(hp),
    ])
    assert captured["claude_proposal_yaml"] == "selected_target: foo\n"
    assert captured["host_peer_review_yaml"] == "goal_satisfied: true\n"
