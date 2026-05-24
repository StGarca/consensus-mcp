"""v1.30.1 hot-patch guards: kimi work-dir leak sweep + gate fail-open-on-crash."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import time
from pathlib import Path

from consensus_mcp import _dispatch_kimi as dk


# ---- Fix #1: stale kimi work-dir sweep (defends against SIGKILL-leaked dirs) ----

def test_sweep_removes_stale_keeps_fresh_and_non_kimi(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / "kimi-workdir-OLD"; old.mkdir()
    fresh = tmp_path / "kimi-workdir-NEW"; fresh.mkdir()
    other = tmp_path / "not-a-kimi-dir"; other.mkdir()
    past = time.time() - 7200  # 2h old, past the 1h threshold
    os.utime(old, (past, past))

    removed = dk._sweep_stale_workdirs(max_age_seconds=3600)

    assert not old.exists()      # stale leaked dir swept
    assert fresh.exists()        # recent dir (possible in-flight) kept
    assert other.exists()        # only kimi-workdir-* are touched
    assert removed == 1


def test_sweep_never_raises_on_missing_tmp(monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/no/such/dir/xyz")
    assert dk._sweep_stale_workdirs() == 0  # best-effort, never raises


def test_consensus_state_excluded_from_workdir_copy():
    # the leaked copy of consensus-state is what filled /tmp; it must be ignored
    assert "consensus-state" in dk._TEMP_WORKDIR_IGNORE_DIRS


# ---- Fix #3: PreToolUse gate fails OPEN on any unexpected exception ----

_HOOK = (Path(__file__).resolve().parent.parent
         / "claude_extensions" / "hooks" / "consensus_pretooluse_gate.py")


def _load_gate():
    spec = importlib.util.spec_from_file_location("gate_under_test", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gate_fails_open_when_main_crashes(monkeypatch):
    g = _load_gate()

    def _boom(*a, **k):
        raise RuntimeError("simulated hook crash")

    monkeypatch.setattr(g, "main", _boom)
    assert g._main_fail_open() == 0  # crash -> ALLOW (never brick the tool/shell)


def test_gate_preserves_allow_and_deny_codes(monkeypatch):
    g = _load_gate()
    monkeypatch.setattr(g, "main", lambda *a, **k: 0)
    assert g._main_fail_open() == 0   # allow preserved
    monkeypatch.setattr(g, "main", lambda *a, **k: 2)
    assert g._main_fail_open() == 2   # deny preserved (not swallowed by fail-open)
