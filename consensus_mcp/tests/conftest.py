"""Suite-wide test guards.

v1.15.7 — neutralize real process-group signals.

Many tests drive `_dispatch_base._terminate_process_tree` with a FAKE
Popen whose `.pid` is synthetic. On POSIX that function does
`os.killpg(os.getpgid(proc.pid), SIG*)`. A synthetic pid of `0` (the
`_FakePopen` value) — or any pid that happens to resolve — makes
`os.getpgid` return the **runner's own** process group, so
`os.killpg` then SIGTERM'd the pytest / GitHub-Actions job itself.
That surfaced as ubuntu "##[error]The operation was canceled" at
~25% of the suite; Windows uses the `send_signal`/`taskkill` branch
so it was masked locally and CI was dormant v1.13.0→v1.15.3 so it
stayed hidden until v1.15.4 re-enabled CI.

Make the real process-group syscalls raise `ProcessLookupError`
suite-wide. `_terminate_process_tree` already catches that and falls
back to `proc.terminate()` (its documented path), so the abort/
watchdog logic and every `dispatch_aborted` assertion behave exactly
as before — no test asserts a real OS signal was delivered. On
Windows `os.killpg`/`os.getpgid` don't exist (`raising=False` makes
the patch a harmless no-op there; that path uses CTRL_BREAK/taskkill
which the fakes already absorb).
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_process_group_signals(monkeypatch):
    def _neutralized(*_a, **_k):
        raise ProcessLookupError("process-group signal neutralized in tests")

    monkeypatch.setattr(os, "killpg", _neutralized, raising=False)
    monkeypatch.setattr(os, "getpgid", _neutralized, raising=False)
