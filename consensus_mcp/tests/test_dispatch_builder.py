"""Tests for _dispatch_builder (workflow D write-enabled dispatch)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from consensus_mcp import _architect_paths as ap
from consensus_mcp import _dispatch_builder as db


def _lane(tmp_path: Path) -> Path:
    lane = tmp_path / ".consensus" / "architect" / "g1" / "lane"
    lane.mkdir(parents=True)
    return lane


def _fake_run_factory(payload: dict, returncode: int = 0):
    calls = {}
    def fake_run(argv, **kwargs):
        calls["argv"] = list(argv)
        calls["kwargs"] = kwargs
        out_idx = argv.index("-o") + 1
        Path(argv[out_idx]).write_text(json.dumps(payload), encoding="utf-8")
        class R:
            pass
        r = R()
        r.returncode = returncode
        r.stdout = ""
        r.stderr = "" if returncode == 0 else "boom"
        return r
    return fake_run, calls


def test_dispatch_builder_happy_path(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    fake_run, calls = _fake_run_factory(
        {"summary": "implemented slice 1", "pushback": None, "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "run", fake_run)
    result = db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="BUILD per spec", codex_bin="codex", timeout_seconds=60,
    )
    assert result["summary"] == "implemented slice 1"
    assert result["pushback"] is None
    argv = calls["argv"]
    assert argv[:2] == ["codex", "exec"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--cd") + 1] == str(lane)


def test_dispatch_builder_rejects_noncanon_argv(tmp_path: Path, monkeypatch):
    # lane outside .consensus/architect/*/lane -> canon violation pre-Popen
    bad_lane = tmp_path / "elsewhere"
    bad_lane.mkdir()
    called = {"n": 0}
    def must_not_run(*a, **k):
        called["n"] += 1
        raise AssertionError("Popen reached despite canon violation")
    monkeypatch.setattr(db.subprocess, "run", must_not_run)
    with pytest.raises(db.BuilderDispatchError, match="canon"):
        db.dispatch_builder(
            repo_root=tmp_path, lane=bad_lane,
            prompt="x", codex_bin="codex", timeout_seconds=60,
        )
    assert called["n"] == 0


def test_dispatch_builder_pushback_passthrough(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    fake_run, _ = _fake_run_factory(
        {"summary": "", "pushback": "spec contradicts itself", "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "run", fake_run)
    result = db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="x", codex_bin="codex", timeout_seconds=60,
    )
    assert result["pushback"] == "spec contradicts itself"


def test_dispatch_builder_nonzero_exit_raises(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    fake_run, _ = _fake_run_factory({"summary": "x", "pushback": None, "notes": ""}, returncode=2)
    monkeypatch.setattr(db.subprocess, "run", fake_run)
    with pytest.raises(db.BuilderDispatchError, match="exited 2"):
        db.dispatch_builder(
            repo_root=tmp_path, lane=lane,
            prompt="x", codex_bin="codex", timeout_seconds=60,
        )


def test_dispatch_builder_invalid_output_raises(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    fake_run, _ = _fake_run_factory({"unexpected": True})
    monkeypatch.setattr(db.subprocess, "run", fake_run)
    with pytest.raises(db.BuilderDispatchError, match="summary"):
        db.dispatch_builder(
            repo_root=tmp_path, lane=lane,
            prompt="x", codex_bin="codex", timeout_seconds=60,
        )


def test_env_is_scrubbed(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    fake_run, calls = _fake_run_factory(
        {"summary": "ok", "pushback": None, "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "run", fake_run)
    db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="x", codex_bin="codex", timeout_seconds=60,
    )
    assert "OPENAI_API_KEY" not in calls["kwargs"]["env"]
