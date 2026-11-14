"""Suite-wide test guards.

v1.15.7 - neutralize real process-group signals.

Many tests drive `_dispatch_base._terminate_process_tree` with a FAKE
Popen whose `.pid` is synthetic. On POSIX that function does
`os.killpg(os.getpgid(proc.pid), SIG*)`. A synthetic pid of `0` (the
`_FakePopen` value) - or any pid that happens to resolve - makes
`os.getpgid` return the **runner's own** process group, so
`os.killpg` then SIGTERM'd the pytest / GitHub-Actions job itself.
That surfaced as ubuntu "##[error]The operation was canceled" at
~25% of the suite; Windows uses the `send_signal`/`taskkill` branch
so it was masked locally and CI was dormant v1.13.0->v1.15.3 so it
stayed hidden until v1.15.4 re-enabled CI.

Make the real process-group syscalls raise `ProcessLookupError`
suite-wide. `_terminate_process_tree` already catches that and falls
back to `proc.terminate()` (its documented path), so the abort/
watchdog logic and every `dispatch_aborted` assertion behave exactly
as before - no test asserts a real OS signal was delivered. On
Windows `os.killpg`/`os.getpgid` don't exist (`raising=False` makes
the patch a harmless no-op there; that path uses CTRL_BREAK/taskkill
which the fakes already absorb).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch, tmp_path_factory):
    for key in list(os.environ):
        if key.startswith("CONSENSUS_MCP_") or key in (
            "CLAUDECODE", "AI_AGENT", "CI", "CODEX_THREAD_ID", "CODEX_CI",
            "CURSOR_AGENT", "GEMINI_CLI", "HERMES_AGENT",
        ):
            monkeypatch.delenv(key, raising=False)
    # Tests that predate the sensitive-data gate run as "not sensitive"; the
    # gate's own tests clear or override this.
    monkeypatch.setenv("CONSENSUS_MCP_SENSITIVE_DATA", "no")
    isolated = tmp_path_factory.mktemp("system-temp")
    for key in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(key, str(isolated))
    monkeypatch.setattr(tempfile, "tempdir", str(isolated))


@pytest.fixture(autouse=True)
def _no_real_process_group_signals(monkeypatch):
    def _neutralized(*_a, **_k):
        raise ProcessLookupError("process-group signal neutralized in tests")

    monkeypatch.setattr(os, "killpg", _neutralized, raising=False)
    monkeypatch.setattr(os, "getpgid", _neutralized, raising=False)


def _state_snapshot(root):
    if not root.exists():
        return {}
    return {".": None} | {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _assert_state_unchanged(root, before):
    after = _state_snapshot(root)
    changed = sorted(
        path for path in before.keys() | after.keys()
        if path not in before or path not in after or before[path] != after[path]
    )
    assert not changed, "Tests dirtied repository consensus-state/: " + ", ".join(changed)


@pytest.fixture(scope="session", autouse=True)
def _no_repository_state_leaks():
    # Keep tracked fixtures intact and detect files, locks, or scratch dirs left by tests.
    state = Path(__file__).resolve().parents[2] / "consensus-state"
    before = _state_snapshot(state)
    yield
    _assert_state_unchanged(state, before)
