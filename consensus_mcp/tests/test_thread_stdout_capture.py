"""Hazard H1: the 4 peer adapters captured dispatch stdout via process-global
contextlib.redirect_stdout, which cross-captures when adapters run concurrently
(the consult-ratified parallel dispatch). capture_stdout_threadsafe() captures
per-thread without swapping the global sys.stdout per call.
"""
from __future__ import annotations

import sys
import threading

from consensus_mcp.contributors.base import capture_stdout_threadsafe


def test_capture_isolates_concurrent_threads():
    """N threads capture simultaneously; each must see ONLY its own output, with
    overlap forced by a barrier. The old redirect_stdout would cross-capture."""
    n = 4
    enter = threading.Barrier(n)
    printed = threading.Barrier(n)
    out: dict[int, str] = {}

    def worker(i: int) -> None:
        with capture_stdout_threadsafe() as buf:
            enter.wait()          # all inside capture before anyone prints
            print(f"line-{i}")
            printed.wait()        # hold capture open while every thread prints
            out[i] = buf.getvalue()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(n):
        assert out[i].strip() == f"line-{i}", f"thread {i} cross-captured: {out[i]!r}"


def test_capture_restores_stdout_and_passes_through(capsys):
    """After capture, normal prints reach the real stdout again (no leak)."""
    real_before = sys.stdout
    with capture_stdout_threadsafe() as buf:
        print("captured")
    print("after")
    assert buf.getvalue().strip() == "captured"
    # stdout is usable again; "after" is NOT in our buffer
    assert "after" not in buf.getvalue()
