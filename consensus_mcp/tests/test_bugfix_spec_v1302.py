"""v1.30.2 — guards for the 3 bugs the ebook2audiobook consult exposed.

Spec: ebook2audiobook/consensus-state/CONSENSUS-MCP-BUGFIX-SPEC-2026-05-24.md
A: convergence reviewer_id not round-keyed -> T6 index_collision on round >=2.
B: review-target CONTENT not embedded -> sandboxed reviewers can't read the packet.
C: kimi copytree fills tmpfs in a no-git / large repo.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from consensus_mcp._dispatch_base import _build_prompt
from consensus_mcp import _dispatch_kimi as dk


# ---- Bug A: convergence reviewer_id is round-keyed (per adapter_options.round_number) ----

from consensus_mcp.contributors.base import DispatchPacket
from consensus_mcp.contributors._phase_mode import PHASE_CONVERGE
from consensus_mcp.contributors.codex import CodexAdapter
from consensus_mcp import _dispatch_codex


def _packet(tmp_path, round_number):
    gp = tmp_path / "goal_packet.yaml"
    gp.write_text("pilot: x\n", encoding="utf-8")
    return DispatchPacket(
        phase=PHASE_CONVERGE, contributor="codex",
        iteration_dir=tmp_path, goal_packet_path=gp, review_target_path=None,
        reviewer_id=None, pass_id=None, timeout_seconds=600,
        adapter_options={"round_number": round_number},
    )


def _captured_reviewer_id(monkeypatch, packet):
    captured = {}

    def fake_main(argv):
        captured["argv"] = list(argv)
        print("{}")  # adapter then fails to build a SealedArtifact; we only need the argv
        return 0

    monkeypatch.setattr(_dispatch_codex, "main", fake_main)
    try:
        CodexAdapter().dispatch(packet)
    except Exception:
        pass
    argv = captured["argv"]
    return argv[argv.index("--reviewer-id") + 1]


def test_bug_a_converge_reviewer_id_is_round_keyed(tmp_path, monkeypatch):
    r1 = _captured_reviewer_id(monkeypatch, _packet(tmp_path, 1))
    r2 = _captured_reviewer_id(monkeypatch, _packet(tmp_path, 2))
    assert r1 != r2                                  # round 2 != round 1 -> no T6 collision
    assert r1.endswith(f"{PHASE_CONVERGE}-1")
    assert r2.endswith(f"{PHASE_CONVERGE}-2")


# ---- Bug B: _build_prompt embeds the review-target CONTENT, not just its path ----

def test_bug_b_embeds_review_target_content():
    out = _build_prompt(
        {"goal": {"summary": "g"}},
        "GOAL: {goal_summary}\nTARGET: {review_target_path}\n",
        review_target_path="consensus-state/active/iter/convergence-packet-round-2.yaml",
        review_target_content="THE CONVERGENCE PACKET BODY",
    )
    assert "THE CONVERGENCE PACKET BODY" in out
    assert "REVIEW TARGET CONTENT" in out


def test_bug_b_no_double_embed_when_touched_files_contents_present():
    rp = {"defect_target": {"touched_files_contents": {"a.py": "CODE BODY"}}}
    out = _build_prompt(
        {"goal": {}},
        "{touched_files_contents_block}",
        review_packet=rp,
        review_target_content="WHOLE REVIEW PACKET YAML",
    )
    assert "CODE BODY" in out                  # code-review path already embeds bodies
    assert "WHOLE REVIEW PACKET YAML" not in out  # so no redundant second block


# ---- Bug C: kimi workdir copy is bounded (env-extendable + .gitignore-aware) ----

def test_bug_c_extra_ignore_dirs_from_env(monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", "Applio, models ,python_env")
    assert dk._extra_ignore_dirs() == {"Applio", "models", "python_env"}


def test_bug_c_gitignored_top_level_dirs(tmp_path):
    (tmp_path / ".gitignore").write_text(
        "# comment\nmodels/\n.worktrees\n*.log\nsub/dir\n!keep\n", encoding="utf-8")
    assert dk._gitignored_top_level_dirs(tmp_path) == {"models", ".worktrees"}


def test_bug_c_copytree_excludes_heavy_dir_in_no_git_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "models").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "models" / "big.bin").write_text("x" * 2000, encoding="utf-8")
    (repo / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.setenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", "models")
    tmproot = tmp_path / "tmp"; tmproot.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmproot))

    wd = dk._make_disposable_workdir(repo)  # no .git -> copytree fallback
    try:
        assert (wd / "src" / "a.py").exists()
        assert not (wd / "models").exists()  # heavy dir excluded -> doesn't fill tmpfs
    finally:
        dk._cleanup_disposable_workdir(wd)
