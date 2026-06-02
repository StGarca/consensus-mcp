"""Regression tests for the Windows-portability init-wizard crash (v1.33.4).

Reported from a fresh install on a Windows machine:

  1. ENCODING — the Windows console defaulted to cp1252. The first status line
     the wizard prints contains a Unicode glyph ('✓ installed' / '✗ missing',
     and the Next-steps '->' arrows). On a cp1252 stream `print("✓ ...")` raises
     UnicodeEncodeError and the whole wizard aborts. The operator worked around
     it by exporting PYTHONUTF8=1 — a bug we should fix at the source so
     `consensus-init` works out of the box on any console code page.

  2. INTERACTIVITY — the run "went interactive asking for reviewer selection"
     even though it was launched from a Claude Code skill (an agent shell, no
     human to answer). `_stdin_is_interactive()` gated only on `isatty()`, but a
     Windows ConPTY-backed subprocess can report isatty()=True while still being
     unanswerable. The harness/CI marker env vars (CLAUDECODE / AI_AGENT / CI)
     are the reliable signal and must force the non-interactive path regardless
     of what isatty() claims.

Fix:
  - `_force_utf8_streams()` reconfigures stdout/stderr to UTF-8 (errors=replace),
    best-effort, called at the very top of main() before any print().
  - `_stdin_is_interactive()` returns False when any agent/CI marker env var is
    set, before consulting isatty().
"""
from __future__ import annotations

import io

import pytest

from consensus_mcp import _init_wizard as wiz


# ============================================================
# 1. ENCODING — _force_utf8_streams()
# ============================================================

def test_force_utf8_makes_cp1252_stream_accept_glyphs(monkeypatch):
    """The exact crash: a cp1252-backed text stream rejects '✓' until the wizard
    reconfigures it to UTF-8. After _force_utf8_streams() the write succeeds."""
    cp1252_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")
    # Precondition: cp1252 genuinely cannot encode the glyph.
    with pytest.raises(UnicodeEncodeError):
        cp1252_stdout.write("✓ installed")

    monkeypatch.setattr(wiz.sys, "stdout", cp1252_stdout)
    wiz._force_utf8_streams()

    # Now the same write must succeed (stream is UTF-8) — no crash.
    wiz.sys.stdout.write("✓ installed")
    wiz.sys.stdout.flush()
    assert wiz.sys.stdout.encoding.lower() == "utf-8"


def test_force_utf8_tolerates_stream_without_reconfigure(monkeypatch):
    """A plain stream lacking reconfigure() (e.g. io.StringIO) must not crash."""
    monkeypatch.setattr(wiz.sys, "stdout", io.StringIO())
    monkeypatch.setattr(wiz.sys, "stderr", io.StringIO())
    wiz._force_utf8_streams()  # must be a no-op, not raise


def test_force_utf8_tolerates_none_stream(monkeypatch):
    """A detached stdout (None) must not crash _force_utf8_streams()."""
    monkeypatch.setattr(wiz.sys, "stdout", None)
    monkeypatch.setattr(wiz.sys, "stderr", None)
    wiz._force_utf8_streams()  # must be a no-op, not raise


def test_force_utf8_swallows_reconfigure_errors(monkeypatch):
    """If reconfigure() raises (already-detached buffer), swallow it."""
    class _Hostile:
        encoding = "cp1252"

        def reconfigure(self, **kwargs):
            raise ValueError("underlying buffer has been detached")

    monkeypatch.setattr(wiz.sys, "stdout", _Hostile())
    wiz._force_utf8_streams()  # must not propagate the ValueError


# ============================================================
# 2. INTERACTIVITY — env markers force non-interactive
# ============================================================

class _FakeStdin:
    def isatty(self) -> bool:
        return True  # claims to be a TTY (Windows ConPTY behavior)


@pytest.mark.parametrize("marker", ["CLAUDECODE", "AI_AGENT", "CI"])
def test_agent_or_ci_marker_forces_non_interactive_even_on_tty(monkeypatch, marker):
    """Under an agent/CI marker, isatty()=True must NOT be treated as
    interactive — no human can answer the reviewer-selection prompt."""
    for var in ("CLAUDECODE", "AI_AGENT", "CI"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(marker, "1")
    monkeypatch.setattr(wiz.sys, "stdin", _FakeStdin())
    assert wiz._stdin_is_interactive() is False


def test_tty_without_markers_is_interactive(monkeypatch):
    """A real TTY with no agent/CI markers stays interactive (unchanged)."""
    for var in ("CLAUDECODE", "AI_AGENT", "CI"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(wiz.sys, "stdin", _FakeStdin())
    assert wiz._stdin_is_interactive() is True
