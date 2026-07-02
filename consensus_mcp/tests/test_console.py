"""Tests for the shared console bootstrap helper (consensus_mcp._console).

M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q10.

Covers force_utf8_streams():
  - idempotent (calling twice reconfigures both streams both times, no raise),
  - safe when a stream lacks reconfigure() (captured StringIO under pytest),
  - safe when a stream is None,
  - swallows a reconfigure() that raises (detached buffer),
and that every console entry-point main() (server + the four dispatchers)
calls it before doing anything else. The dispatcher cases double as a smoke
import of each dispatcher module.
"""
from __future__ import annotations

import importlib
import io
import sys

import pytest

from consensus_mcp._console import force_utf8_streams


class _RecordingStream:
    """Stand-in text stream that records reconfigure() kwargs."""

    def __init__(self):
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


class _RaisingStream:
    def reconfigure(self, **kwargs):
        raise OSError("underlying buffer detached")


# ---------------------------------------------------------------------------
# force_utf8_streams
# ---------------------------------------------------------------------------

def test_force_utf8_streams_reconfigures_both_streams(monkeypatch):
    out = _RecordingStream()
    err = _RecordingStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    force_utf8_streams()

    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_force_utf8_streams_is_idempotent(monkeypatch):
    """Calling it twice reconfigures each stream twice and never raises."""
    out = _RecordingStream()
    err = _RecordingStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    force_utf8_streams()
    force_utf8_streams()

    expected = [{"encoding": "utf-8", "errors": "replace"}] * 2
    assert out.calls == expected
    assert err.calls == expected


def test_force_utf8_streams_safe_when_stream_lacks_reconfigure(monkeypatch):
    """A plain StringIO has no reconfigure() -> skipped, no exception."""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    force_utf8_streams()  # must not raise


def test_force_utf8_streams_safe_when_stream_is_none(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    force_utf8_streams()  # must not raise


def test_force_utf8_streams_swallows_reconfigure_errors(monkeypatch):
    """A reconfigure() that raises (e.g. detached buffer) is best-effort and
    swallowed, so the entry point still runs."""
    monkeypatch.setattr(sys, "stdout", _RaisingStream())
    monkeypatch.setattr(sys, "stderr", _RaisingStream())
    force_utf8_streams()  # must not raise


# ---------------------------------------------------------------------------
# Entry-point wiring: each dispatcher main() calls it before argparse.
# (Doubles as a smoke import of each dispatcher module.)
# ---------------------------------------------------------------------------

_DISPATCHERS = [
    "_dispatch_codex",
    "_dispatch_gemini",
    "_dispatch_grok",
    "_dispatch_kimi",
]


@pytest.mark.parametrize("modname", _DISPATCHERS)
def test_dispatcher_main_hardens_utf8_before_argparse(modname, monkeypatch):
    mod = importlib.import_module(f"consensus_mcp.{modname}")
    assert hasattr(mod, "force_utf8_streams"), f"{modname} did not import the helper"

    calls: list[bool] = []
    monkeypatch.setattr(mod, "force_utf8_streams", lambda: calls.append(True))

    # Empty argv -> argparse errors on the required --goal-packet/--iteration-dir
    # and raises SystemExit(2). force_utf8_streams() runs first (top of main()).
    with pytest.raises(SystemExit):
        mod.main([])

    assert calls == [True], f"{modname}.main did not call force_utf8_streams first"
