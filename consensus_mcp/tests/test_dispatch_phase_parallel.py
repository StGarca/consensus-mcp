"""Tests for the parallel phase-dispatch helper (consult-ratified design).

_dispatch_phase_parallel runs each contributor's dispatch CONCURRENTLY within a
phase (rounds stay sequential), collects results in the CALLING thread (so worker
threads never mutate shared engine state -- hazard H2), re-sorts into input order
(determinism -- concurrency changes only wall-clock, not outcomes), and captures
per-item exceptions instead of propagating (collect-all). max_workers caps
concurrency (Q1 refinement).
"""
from __future__ import annotations

import threading
import time

from consensus_mcp.workflow_engine import _dispatch_phase_parallel


def test_phase_parallel_runs_concurrently():
    """A barrier all N items must reach proves true concurrency: a serial
    implementation would never release the barrier and every item would time
    out (-> 'err'). Concurrent execution releases it -> all 'ok'."""
    n = 4
    barrier = threading.Barrier(n, timeout=5)

    def fn(item):
        barrier.wait()  # BrokenBarrierError if the others never arrive (serial)
        return f"ran-{item}"

    results = _dispatch_phase_parallel(list(range(n)), fn)
    assert [s for s, _ in results] == ["ok"] * n
    assert [v for _, v in results] == [f"ran-{i}" for i in range(n)]


def test_phase_parallel_returns_input_order_regardless_of_completion():
    """Item 0 finishes LAST (longest delay); results must still be in input
    order, proving the deterministic re-sort."""
    items = [0, 1, 2, 3]

    def fn(item):
        time.sleep(0.05 * (len(items) - item))  # item 0 sleeps longest
        return item * 10

    results = _dispatch_phase_parallel(items, fn)
    assert results == [("ok", 0), ("ok", 10), ("ok", 20), ("ok", 30)]


def test_phase_parallel_collect_all_captures_exception():
    """One item raises; its slot holds ('err', exc), the others still complete,
    and the call itself does NOT raise (collect-all, not fail-fast)."""
    boom = RuntimeError("dispatch blew up")

    def fn(item):
        if item == 1:
            raise boom
        return item

    results = _dispatch_phase_parallel([0, 1, 2], fn)
    assert results[0] == ("ok", 0)
    assert results[1][0] == "err" and results[1][1] is boom
    assert results[2] == ("ok", 2)


def test_phase_parallel_empty_returns_empty():
    assert _dispatch_phase_parallel([], lambda x: x) == []


def test_phase_parallel_respects_max_workers_cap():
    """With max_workers=2 over 4 items, peak concurrency must not exceed 2,
    and all 4 still complete in order."""
    lock = threading.Lock()
    state = {"cur": 0, "peak": 0}

    def fn(item):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.05)
        with lock:
            state["cur"] -= 1
        return item

    results = _dispatch_phase_parallel([0, 1, 2, 3], fn, max_workers=2)
    assert [v for _, v in results] == [0, 1, 2, 3]
    assert state["peak"] <= 2
