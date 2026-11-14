"""Regressions found by the first live six-seat consult (2026-11-13).

1. `--profile deepseek` was parsed as an abbreviation of --profile-json.
2. A sibling seat's seal lock under consensus-state/ failed other seats'
   post-dispatch integrity checks when seats ran in parallel.
3. agy -p prints nothing until done, so the 180 s silence watchdog killed
   every healthy Gemini run.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from consensus_mcp import _dispatch_gemini, _dispatch_runner
from consensus_mcp._dispatch_openai_compat import profile_from_argv, profile_name_from_argv


def test_profile_name_is_not_taken_as_profile_json():
    argv = ["--profile", "deepseek", "--iteration", "x"]
    assert profile_from_argv(argv) is None
    assert profile_name_from_argv(argv, "fallback") == "deepseek"


def test_profile_json_still_parsed_when_given_in_full():
    assert profile_from_argv(["--profile-json", '{"name": "deepseek"}']) == {"name": "deepseek"}


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_integrity_snapshot_ignores_panel_write_area(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    before = _dispatch_runner.repo_status_snapshot(tmp_path)
    lock = tmp_path / "consensus-state" / "archive" / "review-passes" / "index.lock"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"")
    (tmp_path / "consensus-state" / "active").mkdir()
    (tmp_path / "consensus-state" / "active" / "codex-review.yaml").write_text("x", encoding="utf-8")
    assert _dispatch_runner.repo_status_snapshot(tmp_path) == before
    # A real reviewer mutation outside the panel area is still caught.
    (tmp_path / "src.py").write_text("mutated", encoding="utf-8")
    assert "src.py" in _dispatch_runner.repo_status_snapshot(tmp_path)


@pytest.mark.parametrize("binary,expect_raised", [("C:/tools/agy.exe", True), ("C:/tools/gemini.exe", False)])
def test_agy_silence_watchdog_waits_for_full_timeout(monkeypatch, tmp_path, binary, expect_raised):
    seen = {}
    monkeypatch.setattr(_dispatch_gemini, "_resolve_gemini_bin", lambda _b: binary)

    def fake_run_process(argv, **kwargs):
        seen.update(kwargs)
        if "--input-format" in argv:  # agy speaks stream-json
            return '{"event":"result","result":{"status":"SUCCESS","response":"{}"}}\n'
        return "{}"

    monkeypatch.setattr(_dispatch_gemini, "run_process", fake_run_process)
    _dispatch_gemini._invoke_gemini(prompt="x", gemini_bin="agy", model="gemini-4-argon-high",
                                    timeout_seconds=1500, repo_root=tmp_path)
    assert seen["stall_silence_seconds"] == (1500.0 if expect_raised else 180.0)


def test_claude_silence_watchdog_waits_for_full_timeout(monkeypatch, tmp_path):
    """`claude -p --output-format json` prints only at the end; the live run
    killed a healthy Fable review at 180 s."""
    from types import SimpleNamespace
    from consensus_mcp import _dispatch_claude, _dispatch_runner as runner
    seen = {}

    def fake_run_process(argv, **kwargs):
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(runner, "run_process", fake_run_process)
    ns = SimpleNamespace(claude_bin="claude", timeout_seconds=1500, stall_silence_seconds=180)
    _dispatch_claude._invoke_claude(prompt="x", model="claude-fable-5-2", repo_root=tmp_path,
                                    schema_path=None, ns=ns, log_path=None, anchors=None)
    assert seen["stall_silence_seconds"] == 1500.0
