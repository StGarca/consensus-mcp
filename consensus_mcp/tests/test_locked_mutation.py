"""Unit tests for _atomic_io.locked_mutation - the M1 Q1 cross-process
mutual-exclusion primitive (consult iteration-m1-hardening-design-4d7d2469).

Covers the designed mechanism directly, at the primitive:
  - mkdir-dir claim + owner.json record + rmdir release;
  - contention serializes read-modify-write (threaded counter);
  - LockTimeout carries the holder's parsed owner.json fields
    (gemini-rev-001);
  - age-based stale takeover of an aged owner.json record, reported on the
    yielded LockStatus (never emitted - the sink invariant is pinned at the
    callers in test_seal_index_concurrency.py);
  - M1-remediation (consult iteration-path-to-a-remediation-260caad1) D2
    refinement: an owner-less orphan dir (crashed pre-write holder) is reaped
    ONLY after THIS waiter observes it continuously owner-less past the
    orphan-reap threshold, keyed on that per-waiter observation - NEVER on
    directory mtime (a coarse/stale mtime must not trigger a premature reap);
  - kimi-rev-006: pre-existing <target>.lock.stale.* debris is tolerated,
    and a takeover-rename failure falls back to continued bounded waiting
    (LockTimeout), never an uncaught OSError.

Caller-level behavior (structured state_lock_timeout refusals, dispatch-log
takeover events, no-recursive-append) lives in test_seal_index_concurrency.py.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

import consensus_mcp._atomic_io as atomic_io
from consensus_mcp._atomic_io import LockTimeout, locked_mutation


def _lock_dir(target: Path) -> Path:
    return target.with_name(target.name + ".lock")


def _write_owner(lock_dir: Path, pid: int, host: str, claimed_at_epoch: float) -> None:
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": pid, "host": host, "claimed_at_epoch": claimed_at_epoch}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Uncontended acquire / release.
# ---------------------------------------------------------------------------


def test_uncontended_acquire_claims_and_releases(tmp_path):
    target = tmp_path / "state.yaml"
    with locked_mutation(target) as status:
        assert _lock_dir(target).is_dir()
        owner = json.loads((_lock_dir(target) / "owner.json").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert isinstance(owner["claimed_at_epoch"], float)
        assert status.contended is False
        assert status.takeover is False
        assert status.takeover_owner is None
    # Fully released: owner.json gone, claim dir gone.
    assert not _lock_dir(target).exists()


def test_release_happens_even_when_body_raises(tmp_path):
    target = tmp_path / "state.yaml"
    with pytest.raises(RuntimeError):
        with locked_mutation(target):
            raise RuntimeError("body failure")
    assert not _lock_dir(target).exists()


def test_creates_missing_parent_directory(tmp_path):
    target = tmp_path / "deep" / "nested" / "state.yaml"
    with locked_mutation(target):
        assert _lock_dir(target).is_dir()
    assert not _lock_dir(target).exists()


# ---------------------------------------------------------------------------
# Contention: the lock actually serializes a read-modify-write.
# ---------------------------------------------------------------------------


def test_contended_threads_serialize_read_modify_write(tmp_path):
    """2 threads x 20 locked read-increment-write cycles on one counter file:
    the final value proves every mutation window was serialized (an
    unserialized interleaving loses increments)."""
    target = tmp_path / "counter.txt"
    target.write_text("0", encoding="utf-8")
    per_thread = 20
    saw_contention = []
    errors = []

    def _worker():
        try:
            for _ in range(per_thread):
                with locked_mutation(target, timeout_s=15.0) as status:
                    if status.contended:
                        saw_contention.append(True)
                    value = int(target.read_text(encoding="utf-8"))
                    target.write_text(str(value + 1), encoding="utf-8")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "worker hung"
    assert errors == [], errors
    assert int(target.read_text(encoding="utf-8")) == 2 * per_thread
    assert not _lock_dir(target).exists()


# ---------------------------------------------------------------------------
# LockTimeout: bounded wait, owner fields carried (gemini-rev-001).
# ---------------------------------------------------------------------------


def test_lock_timeout_carries_owner_fields(tmp_path):
    target = tmp_path / "state.yaml"
    lock_dir = _lock_dir(target)
    lock_dir.mkdir()
    claimed = time.time()
    _write_owner(lock_dir, pid=1234, host="holder-host", claimed_at_epoch=claimed)

    t0 = time.monotonic()
    with pytest.raises(LockTimeout) as excinfo:
        with locked_mutation(target, timeout_s=0.3):
            pass
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, "timeout not bounded"
    exc = excinfo.value
    assert exc.owner_pid == 1234
    assert exc.owner_host == "holder-host"
    assert exc.owner_claimed_at_epoch == pytest.approx(claimed)
    assert exc.target == target
    # The foreign lock is left in place (it was live, not stale).
    assert lock_dir.is_dir()


def test_lock_timeout_with_unreadable_owner_has_none_fields(tmp_path):
    """A lock dir with no owner.json (owner-less, observed below the
    orphan-reap threshold) times out with None owner fields rather than
    crashing on the absent record."""
    target = tmp_path / "state.yaml"
    _lock_dir(target).mkdir()

    with pytest.raises(LockTimeout) as excinfo:
        with locked_mutation(target, timeout_s=0.3):
            pass
    exc = excinfo.value
    assert exc.owner_pid is None
    assert exc.owner_host is None
    assert exc.owner_claimed_at_epoch is None


# ---------------------------------------------------------------------------
# Stale takeover (age is the sole criterion; rename-then-claim).
# ---------------------------------------------------------------------------


def test_stale_owner_takeover_claims_and_reports(tmp_path):
    target = tmp_path / "state.yaml"
    lock_dir = _lock_dir(target)
    lock_dir.mkdir()
    stale_epoch = time.time() - 100000
    _write_owner(lock_dir, pid=4242, host="ghost", claimed_at_epoch=stale_epoch)

    with locked_mutation(target, timeout_s=5.0, stale_after_s=120.0) as status:
        assert status.takeover is True
        assert status.contended is True
        assert status.takeover_owner["pid"] == 4242
        assert status.takeover_owner["host"] == "ghost"
        assert status.takeover_owner["claimed_at_epoch"] == pytest.approx(stale_epoch)
        # We hold a FRESH claim now, recorded as ours.
        owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
    assert not lock_dir.exists()


def test_ownerless_orphan_reaped_after_observed_ownerless_threshold(tmp_path):
    """M1-remediation (consult iteration-path-to-a-remediation-260caad1) D2
    refinement: a lock dir a waiter observes CONTINUOUSLY owner-less past the
    per-waiter orphan-reap threshold (a holder that crashed between mkdir and
    its owner.json write) is reaped via rename-then-claim - reported on the
    yielded LockStatus, takeover_owner None (nothing to parse for a pre-write
    crash). This replaces the old missing-owner directory-mtime grace test."""
    target = tmp_path / "state.yaml"
    lock_dir = _lock_dir(target)
    lock_dir.mkdir()  # owner-less: crashed before the owner.json write

    with locked_mutation(target, timeout_s=5.0, orphan_reap_after_s=0.2) as status:
        assert status.takeover is True
        assert status.takeover_owner is None  # nothing to parse for a pre-write crash
    assert not lock_dir.exists()


def test_coarse_dir_mtime_does_not_trigger_premature_reap(tmp_path):
    """M1-remediation (consult iteration-path-to-a-remediation-260caad1) D2
    refinement: the orphan reap is keyed on THIS waiter's observed-ownerless
    duration, NEVER directory mtime. An owner-less dir whose mtime is hours old
    (as coarse/lagged Windows dir mtime can report) is NOT reaped when the reap
    threshold exceeds the acquisition timeout: the waiter cannot accumulate the
    observation window in time and raises a bounded LockTimeout instead of
    stealing the dir. Under the removed mtime-grace model this 1h-old dir would
    have been taken over - so a LockTimeout here proves mtime is not consulted."""
    target = tmp_path / "state.yaml"
    lock_dir = _lock_dir(target)
    lock_dir.mkdir()  # owner-less
    aged = time.time() - 3600
    os.utime(lock_dir, (aged, aged))  # coarse/stale mtime, as Windows can report

    t0 = time.monotonic()
    with pytest.raises(LockTimeout) as excinfo:
        with locked_mutation(target, timeout_s=0.3, orphan_reap_after_s=10.0):
            pass
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, "timeout not bounded"
    # Untouched: still present and still owner-less - mtime did NOT reap it.
    assert lock_dir.is_dir()
    assert not (lock_dir / "owner.json").exists()
    exc = excinfo.value
    assert exc.owner_pid is None  # owner-less: no fields to carry


def test_fresh_lock_is_never_taken_over(tmp_path):
    """Age below stale_after_s -> no takeover, bounded LockTimeout instead
    (never steal a live holder's claim)."""
    target = tmp_path / "state.yaml"
    lock_dir = _lock_dir(target)
    lock_dir.mkdir()
    _write_owner(lock_dir, pid=1, host="live", claimed_at_epoch=time.time())

    with pytest.raises(LockTimeout):
        with locked_mutation(target, timeout_s=0.3, stale_after_s=120.0):
            pass
    assert lock_dir.is_dir()
    owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
    assert owner["pid"] == 1  # untouched


def test_preexisting_stale_debris_is_tolerated(tmp_path):
    """kimi-rev-006: pre-existing <target>.lock.stale.* entries (debris from
    prior takeovers/crashes) are ignored - they block neither a takeover
    nor a plain claim."""
    target = tmp_path / "state.yaml"
    debris_a = tmp_path / "state.yaml.lock.stale.deadbeefdeadbeefdeadbeefdeadbeef"
    debris_a.mkdir()
    (debris_a / "owner.json").write_text("{}", encoding="utf-8")
    debris_b = tmp_path / "state.yaml.lock.stale.cafe"
    debris_b.mkdir()

    lock_dir = _lock_dir(target)
    lock_dir.mkdir()
    _write_owner(lock_dir, pid=4242, host="ghost", claimed_at_epoch=time.time() - 100000)

    with locked_mutation(target, timeout_s=5.0) as status:
        assert status.takeover is True
    assert not lock_dir.exists()


def test_takeover_rename_failure_falls_back_to_waiting(tmp_path, monkeypatch):
    """kimi-rev-006: when the stale-dir rename fails (lost the takeover race
    or filesystem refusal), locked_mutation keeps waiting within its bound
    and surfaces LockTimeout - NEVER the raw OSError."""

    class _RenameRefusingOs:
        """Proxy for _atomic_io's module-level `os` binding: rename always
        fails; everything else passes through."""

        def __getattr__(self, name):
            return getattr(os, name)

        def rename(self, src, dst):
            raise PermissionError(13, "rename refused by test")

    target = tmp_path / "state.yaml"
    lock_dir = _lock_dir(target)
    lock_dir.mkdir()
    _write_owner(lock_dir, pid=4242, host="ghost", claimed_at_epoch=time.time() - 100000)

    monkeypatch.setattr(atomic_io, "os", _RenameRefusingOs())
    with pytest.raises(LockTimeout):
        with locked_mutation(target, timeout_s=0.4):
            pass
    # The stale dir is still there, unrenamed and unclaimed.
    assert lock_dir.is_dir()


def test_two_waiters_stale_takeover_exactly_one_wins_rename(tmp_path):
    """Rename-then-claim prevents two waiters both 'deleting and claiming':
    both waiters may acquire (serially), but the takeover itself is
    performed by exactly one of them."""
    target = tmp_path / "state.yaml"
    lock_dir = _lock_dir(target)
    lock_dir.mkdir()
    _write_owner(lock_dir, pid=4242, host="ghost", claimed_at_epoch=time.time() - 100000)

    statuses = [None, None]
    errors = []
    start = threading.Barrier(2)

    def _worker(i):
        try:
            start.wait(timeout=5)
            with locked_mutation(target, timeout_s=10.0) as status:
                statuses[i] = (status.takeover, status.takeover_owner)
                time.sleep(0.01)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in threads), "worker hung"
    assert errors == [], errors
    takeovers = [s for s in statuses if s is not None and s[0]]
    assert len(takeovers) == 1, statuses
    assert takeovers[0][1]["pid"] == 4242
    assert not lock_dir.exists()
