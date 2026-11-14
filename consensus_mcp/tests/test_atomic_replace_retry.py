"""Windows transiently refuses os.replace onto a file another handle has open
(WinError 5); found as an intermittent strict-suite failure in plan passes."""
from __future__ import annotations

import os

import pytest

from consensus_mcp import _atomic_io


def test_transient_windows_replace_denial_is_retried(tmp_path, monkeypatch):
    real, calls, sleeps = os.replace, [], []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(_atomic_io.os, "replace", flaky)
    monkeypatch.setattr(_atomic_io.sys, "platform", "win32")
    monkeypatch.setattr(_atomic_io.time, "sleep", sleeps.append)
    target = tmp_path / "plan-objections.yaml"
    target.write_text("old", encoding="utf-8")
    _atomic_io.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert len(calls) == 3 and len(sleeps) == 2
    assert not list(tmp_path.glob(".*.tmp"))


def test_persistent_denial_still_raises_and_cleans_up(tmp_path, monkeypatch):
    def denied(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(_atomic_io.os, "replace", denied)
    monkeypatch.setattr(_atomic_io.sys, "platform", "win32")
    monkeypatch.setattr(_atomic_io.time, "sleep", lambda _s: None)
    target = tmp_path / "x.yaml"
    with pytest.raises(PermissionError):
        _atomic_io.atomic_write_text(target, "new")
    assert not target.exists() and not list(tmp_path.glob(".*.tmp"))


def test_non_windows_does_not_retry(tmp_path, monkeypatch):
    calls = []

    def denied(src, dst):
        calls.append(1)
        raise PermissionError(13, "denied")

    monkeypatch.setattr(_atomic_io.os, "replace", denied)
    monkeypatch.setattr(_atomic_io.sys, "platform", "linux")
    with pytest.raises(PermissionError):
        _atomic_io.atomic_write_text(tmp_path / "y", "z")
    assert calls == [1]
