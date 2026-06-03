"""The repo-root resolver must work in a CONSUMING project (the cold-start
blocker): a project that ran `consensus init` has .consensus/config.yaml but
NONE of consensus-mcp's own source markers. Before this fix it either failed
marker validation (with CONSENSUS_MCP_REPO_ROOT set) or silently resolved to
consensus-mcp's own repo (no env) - scaffolding/arming the gate in the wrong tree.
"""
from __future__ import annotations

from consensus_mcp._dispatch_base import _resolve_repo_root, _has_repo_markers


def _make_consuming(tmp_path):
    (tmp_path / ".consensus").mkdir()
    (tmp_path / ".consensus" / "config.yaml").write_text("contributors: {}\n", encoding="utf-8")
    return tmp_path


def test_has_markers_accepts_consuming_project(tmp_path):
    assert _has_repo_markers(tmp_path) is False
    _make_consuming(tmp_path)
    assert _has_repo_markers(tmp_path) is True


def test_resolve_via_cwd_in_consuming_project(tmp_path, monkeypatch):
    _make_consuming(tmp_path)
    for v in ("CONSENSUS_MCP_REPO_ROOT", "CONSENSUS_MCP_PROJECT_ROOT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.chdir(tmp_path)
    assert _resolve_repo_root() == tmp_path.resolve()


def test_resolve_via_project_root_env(tmp_path, monkeypatch):
    _make_consuming(tmp_path)
    monkeypatch.delenv("CONSENSUS_MCP_REPO_ROOT", raising=False)
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(tmp_path))
    # cwd is elsewhere; PROJECT_ROOT (what .mcp.json sets) must win
    monkeypatch.chdir(tmp_path.parent)
    assert _resolve_repo_root() == tmp_path.resolve()


def test_resolve_via_cwd_ancestor(tmp_path, monkeypatch):
    _make_consuming(tmp_path)
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    for v in ("CONSENSUS_MCP_REPO_ROOT", "CONSENSUS_MCP_PROJECT_ROOT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.chdir(sub)
    assert _resolve_repo_root() == tmp_path.resolve()
