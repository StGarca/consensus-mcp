"""Process-tree cleanup must kill the worker even when `ps` fails."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from consensus_mcp import _jobs


@pytest.mark.skipif(os.name == "nt", reason="POSIX ps/process-group path")
def test_kill_tree_kills_worker_when_ps_times_out(monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        def slow_ps(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], 5)
        monkeypatch.setattr(_jobs.subprocess, "check_output", slow_ps)
        _jobs._kill_tree(proc)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
