"""The ONE low-level atomic writer, shared across every module that persists a
marker or config (gemini-rev-001 / kimi-rev-001).

History: the symlink-safe atomic writer was introduced in _init_wizard (v1.26,
"the single ROOT fix for the tmp-symlink class across ALL writers"), but later
writers - the session-active marker (_session_state) and the design-approved trust
pointer (_design_approval) - grew their OWN bespoke temp-file logic. A blessed
primitive duplicated three ways is a primitive that will drift. This module hosts
the single implementation; every writer imports it so the symlink/atomicity/durability
guarantees can never diverge again.

Guarantees:
  - The temp file is created O_CREAT|O_EXCL|O_WRONLY with an UNPREDICTABLE name, so
    a pre-planted symlink or file at the temp path cannot redirect the write
    (O_EXCL fails on any existing path; the random name defeats prediction).
  - Contents are flushed + fsync'd before the rename (durability).
  - os.replace atomically swaps the temp into place, replacing a destination
    symlink (the link itself, never its target).
  - The temp is unlinked on any error.

M1 (consult iteration-m1-hardening-design-4d7d2469): this module also hosts
`locked_mutation`, the ONE cross-process mutual-exclusion primitive for
shared-state read-modify-replace windows (mkdir lock dir + owner record +
jittered bounded retry + age-based stale takeover) - one root primitive per
the project's fix-the-class doctrine. See the block comment above it for the
mechanism, the hold-window discipline, and the event-sink invariant.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import socket
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write `data` to `path` via a secure, unpredictable temp file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        tmp = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Atomically write `text` (encoded) to `path` via `atomic_write_bytes`."""
    atomic_write_bytes(path, text.encode(encoding))


def exclusive_create_text(path: Path, text: str, encoding: str = "utf-8") -> bool:
    """O_EXCL TEST-AND-SET create of `path` with `text`; False iff the path
    already exists.

    The lock-file primitive: atomic_write_bytes' os.replace would silently
    clobber a concurrent winner's lock, so mutual-exclusion markers must
    never go through it - creation itself is the atomic claim (no temp +
    rename involved). Contents are flushed + fsync'd like atomic_write_bytes;
    a failure mid-write unlinks the partially-created file so no corrupt
    lock survives. Any OSError other than FileExistsError propagates.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode(encoding))
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return True


# ---------------------------------------------------------------------------
# M1 (consult iteration-m1-hardening-design-4d7d2469) Q1: cross-process mutual
# exclusion for shared-state read-modify-replace windows.
#
# Lock identity is a sibling DIRECTORY `<target>.lock/` claimed via os.mkdir -
# an atomic test-and-set on POSIX, Windows, and WSL/NTFS with no fcntl/msvcrt
# platform split, and crash-inspectable on disk. The holder records
# {pid, host, claimed_at_epoch} in `<target>.lock/owner.json` (best effort).
# Staleness is judged by AGE ALONE (no pid-alive probing - PIDs recycle and
# cross-platform liveness checks are unreliable); real holds are
# ~milliseconds, so the 120s default is orders of magnitude of headroom.
#
# HOLD-WINDOW DISCIPLINE (callers): the lock wraps ONLY
# read -> modify -> unique-tmp write -> os.replace. No dispatch work, no
# hashing of large trees, no subprocess calls inside the lock.
#
# SINK INVARIANT (gemini-rev-002 + kimi-rev-001): locked_mutation NEVER emits
# events itself. It reports takeover/contention on the yielded LockStatus and
# the CALLER emits to the dispatch log (dispatch-log.jsonl), whose append path
# is lock-free - it takes no locked_mutation - so a caller may emit from
# inside a hold without recursion or deadlock REGARDLESS of which file is
# locked (including the dispatch log itself). Future maintainers: do NOT wrap
# dispatch-log.jsonl writes in locked_mutation; the no-recursive-append
# regression test in tests/test_seal_index_concurrency.py pins this invariant.
#
# NOT re-entrant: a thread that already holds `<target>.lock/` and acquires
# it again will wait out timeout_s and raise LockTimeout.
# ---------------------------------------------------------------------------

# Jittered bounded backoff per the anchored design: 10ms base, x2 per retry,
# capped at 250ms.
_LOCK_RETRY_BASE_S = 0.010
_LOCK_RETRY_CAP_S = 0.250

# M1-remediation (consult iteration-path-to-a-remediation-260caad1) D2
# refinement: a lock dir observed CONTINUOUSLY owner-less by THIS waiter for
# longer than this generous window is a crashed pre-write holder (mkdir landed,
# the owner.json write never did) and may be reaped. This is a PER-WAITER
# observation duration tracked in the acquire loop's local state - NEVER the
# directory's mtime. Windows directory mtime is coarse/lagged, and keying the
# missing-owner case on it (the removed _MISSING_OWNER_GRACE_S branch) let a
# waiter steal a takeover winner's just-re-mkdir'd, still-owner-less claim,
# producing the double takeover. A live holder writes owner.json within
# microseconds of mkdir, so this window is orders of magnitude of headroom over
# the normal owner-record latency.
_ORPHAN_REAP_OBSERVED_S = 5.0

# M1-remediation (consult iteration-path-to-a-remediation-260caad1) D1: release
# removal budget. On Windows, deletion is DEFERRED while any transient handle
# (AV, indexer, a sibling thread mid-read) lingers, so os.unlink/os.rmdir raise
# OSError transiently; the pre-remediation swallow-and-orphan release then left
# a still-fresh-owner lock dir behind (the fresh-holder LockTimeout + the
# audit-append hammer hang). A short jittered bounded retry makes release
# reliable without changing the primitive; POSIX unlinks synchronously so the
# loop returns on its first pass on Linux/WSL.
_RELEASE_RETRY_BASE_S = 0.005
_RELEASE_RETRY_CAP_S = 0.050
_RELEASE_BUDGET_S = 1.5


class LockTimeout(TimeoutError):
    """locked_mutation could not claim `<target>.lock/` within timeout_s.

    M1 (consult iteration-m1-hardening-design-4d7d2469, gemini-rev-001):
    carries the holder's parsed owner.json fields (owner_pid, owner_host,
    owner_claimed_at_epoch; None when unreadable) so callers can surface WHO
    holds the lock in their structured `state_lock_timeout` refusal - fail
    loud, never proceed unlocked.
    """

    def __init__(self, target: Path, timeout_s: float, owner: dict | None = None):
        self.target = Path(target)
        self.timeout_s = timeout_s
        owner = owner if isinstance(owner, dict) else {}
        self.owner_pid = owner.get("pid")
        self.owner_host = owner.get("host")
        self.owner_claimed_at_epoch = owner.get("claimed_at_epoch")
        super().__init__(
            f"could not acquire {self.target.name}.lock within {timeout_s}s; "
            f"current holder: pid={self.owner_pid} host={self.owner_host} "
            f"claimed_at_epoch={self.owner_claimed_at_epoch}"
        )


@dataclass
class LockStatus:
    """Acquisition report yielded by locked_mutation (never emitted here).

    takeover/takeover_owner describe a stale-lock takeover performed DURING
    this acquisition; the caller is responsible for reporting it to the
    dispatch log (see SINK INVARIANT above).
    """

    target: Path
    contended: bool = False
    waited_s: float = 0.0
    takeover: bool = False
    takeover_owner: dict | None = None


def _read_lock_owner(owner_path: Path) -> dict | None:
    """Parse `<target>.lock/owner.json`; None when absent/corrupt/non-dict."""
    try:
        parsed = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _lock_is_stale(owner: dict | None, stale_after_s: float) -> bool:
    """Age-only staleness of a PARSEABLE owner record: True iff owner.json's
    claimed_at_epoch is older than stale_after_s.

    M1-remediation (consult iteration-path-to-a-remediation-260caad1) D2: an
    owner-LESS lock dir is NEVER stale by this predicate (returns False). The
    pre-remediation missing-owner directory-mtime grace branch is REMOVED -
    Windows directory mtime is coarse/lagged, so keying the missing-owner case
    on it let a waiter steal a takeover winner's just-re-mkdir'd (still
    owner-less) claim, producing the double takeover. Owner-less dirs are now
    handled entirely by (a) the robust D1 release and (b) the caller's
    per-waiter observation-time orphan reap - never directory mtime."""
    if owner is None:
        return False
    epoch = owner.get("claimed_at_epoch")
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
        return (time.time() - float(epoch)) > stale_after_s
    return False


def _takeover_stale_lock(
    lock_dir: Path, owner_path: Path, stale_after_s: float
) -> bool:
    """Rename-then-claim takeover of a stale lock dir.

    M1 (consult iteration-m1-hardening-design-4d7d2469, kimi-rev-006): the
    rename IS the atomic 'delete' step - two waiters cannot both succeed at
    renaming the same dir, so exactly one performs the takeover (prevents two
    waiters both 'deleting and claiming'). The rename target carries a
    uuid4-derived suffix so it never collides with pre-existing
    `<target>.lock.stale.*` debris (which is tolerated/ignored). Rename
    failure (lost the race, filesystem refusal) returns False and the caller
    falls back to continued bounded waiting - never an uncaught OSError.

    The staleness verdict is re-derived IMMEDIATELY before the rename: a
    waiter whose earlier observation was overtaken by a sibling's
    takeover-and-reclaim would otherwise rename the sibling's LIVE claim
    away. Re-verifying shrinks that observe->rename TOCTOU window to two
    adjacent syscalls (a re-claimed dir has a fresh owner record, so the
    overtaken waiter aborts here and rejoins the wait loop).

    M1-remediation (consult iteration-path-to-a-remediation-260caad1) D2: the
    re-verification now requires a PARSEABLE owner record older than
    stale_after_s. A lock dir with NO owner record is never taken over by this
    path (`_lock_is_stale(None, ...)` is False), which removes the empty-window
    steal where a slower waiter renamed a takeover winner's just-re-mkdir'd,
    not-yet-owner-written claim away. Owner-less dirs are reclaimed only by the
    caller's per-waiter observation-time orphan reap (`_reap_ownerless_orphan`).
    """
    if not _lock_is_stale(_read_lock_owner(owner_path), stale_after_s):
        return False
    stale_name = lock_dir.with_name(lock_dir.name + ".stale." + uuid.uuid4().hex)
    try:
        os.rename(lock_dir, stale_name)
    except OSError:
        return False
    # Best-effort GC of the parked stale dir; leftover debris is harmless
    # (renamed-aside dirs never block future claims).
    shutil.rmtree(stale_name, ignore_errors=True)
    return True


def _reap_ownerless_orphan(lock_dir: Path, owner_path: Path) -> bool:
    """Rename-then-claim reap of a dir THIS waiter has observed continuously
    owner-less past the orphan-reap threshold.

    M1-remediation (consult iteration-path-to-a-remediation-260caad1) D2
    refinement: this is the ONLY path that reclaims an owner-less lock dir (a
    holder that crashed between mkdir and its owner.json write), and it is
    gated by the CALLER's per-waiter observed-ownerless duration - NEVER
    directory mtime. Re-verify the dir is STILL owner-less immediately before
    the rename (a holder that wrote owner.json in the interim must not be
    stolen), then rename-aside - the same atomic 'delete' as the stale-owner
    takeover: exactly one waiter can rename a given dir, and the loser gets an
    OSError -> False and resets its observation so it re-observes the
    replacement dir from scratch (closing the empty-window double-reap)."""
    if _read_lock_owner(owner_path) is not None:
        return False
    stale_name = lock_dir.with_name(lock_dir.name + ".stale." + uuid.uuid4().hex)
    try:
        os.rename(lock_dir, stale_name)
    except OSError:
        return False
    shutil.rmtree(stale_name, ignore_errors=True)
    return True


def _emit_release_failure(lock_dir: Path) -> None:
    """M1-remediation (consult iteration-path-to-a-remediation-260caad1) D1: a
    release that ultimately cannot remove the lock dir is a real fault - never
    silently orphan it. Emit a diagnostic to the LOCK-FREE dispatch-log sink
    (never to a locked file, and via no locked_mutation), preserving the SINK
    INVARIANT. Best effort: a logging failure must never propagate out of
    release (the context manager's finally must not raise)."""
    try:
        from consensus_mcp._dispatch_base import _log_dispatch
        from consensus_mcp._paths import dispatch_log_path

        _log_dispatch(
            dispatch_log_path(),
            {
                "event": "state_lock_release_failed",
                "lock_dir": str(lock_dir),
                "pid": os.getpid(),
            },
        )
    except Exception:
        pass


def _release_lock(lock_dir: Path, owner_path: Path) -> None:
    """Robustly remove the owner record then the claim dir.

    M1-remediation (consult iteration-path-to-a-remediation-260caad1) D1: the
    pre-remediation release swallowed os.unlink/os.rmdir OSErrors and orphaned
    the dir. On Windows, deletion is deferred while a transient handle lingers,
    so a swallowed rmdir left a still-fresh-owner lock dir behind - every
    subsequent waiter then read a fresh (non-stale) owner and blocked to its
    deadline (the fresh-holder LockTimeout and the audit-append hammer hang).
    Retry the unlink+rmdir with jittered bounded backoff to a small total
    budget (the transient handle clears within milliseconds), then fall back to
    shutil.rmtree; if the dir STILL cannot be removed, log to the lock-free
    dispatch-log sink rather than silently orphan. POSIX unlinks synchronously,
    so this returns on the first pass on Linux/WSL."""
    deadline = time.monotonic() + _RELEASE_BUDGET_S
    delay = _RELEASE_RETRY_BASE_S
    while True:
        try:
            os.unlink(owner_path)
        except OSError:
            # Transient (deferred deletion) or already gone; the rmdir attempt
            # below decides success and drives the retry.
            pass
        try:
            os.rmdir(lock_dir)
            return
        except FileNotFoundError:
            # Already gone (e.g. a stale takeover renamed it away).
            return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            break
        time.sleep(random.uniform(delay * 0.5, delay))
        delay = min(delay * 2.0, _RELEASE_RETRY_CAP_S)
    shutil.rmtree(lock_dir, ignore_errors=True)
    try:
        still_there = lock_dir.exists()
    except OSError:
        still_there = True
    if still_there:
        _emit_release_failure(lock_dir)


@contextmanager
def locked_mutation(
    target: Path,
    *,
    timeout_s: float = 30.0,
    stale_after_s: float = 120.0,
    orphan_reap_after_s: float = _ORPHAN_REAP_OBSERVED_S,
) -> Iterator[LockStatus]:
    """Cross-process mutual exclusion for a read-modify-replace of `target`.

    Claims sibling dir `<target>.lock/` via atomic os.mkdir with jittered
    bounded retry (10ms base doubling to a 250ms cap) until timeout_s, taking
    over stale claims (age > stale_after_s) via rename-then-claim. Raises
    LockTimeout (carrying the holder's owner.json fields) on timeout. Yields
    a LockStatus; NEVER emits events - see the SINK INVARIANT block above.

    M1-remediation (consult iteration-path-to-a-remediation-260caad1) D2: only
    a lock dir with a PARSEABLE, aged owner record is taken over by the rename
    path. A lock dir this waiter has observed continuously owner-less for longer
    than orphan_reap_after_s (a crashed pre-write holder) is reaped separately
    (`_reap_ownerless_orphan`), keyed on that per-waiter observation - never on
    directory mtime.
    """
    target = Path(target)
    lock_dir = target.with_name(target.name + ".lock")
    owner_path = lock_dir / "owner.json"
    status = LockStatus(target=target)
    # The target's parent must exist for mkdir of the sibling lock dir.
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    deadline = start + timeout_s
    delay = _LOCK_RETRY_BASE_S
    last_owner: dict | None = None
    # M1-remediation (consult iteration-path-to-a-remediation-260caad1) D2
    # refinement: the monotonic time at which THIS waiter first saw the lock dir
    # owner-less in its current continuous owner-less streak. Reset to None
    # whenever an owner record reappears or a reap-rename is lost, so a coarse
    # directory mtime is never consulted and the streak can never span a
    # takeover winner's re-mkdir empty window.
    observed_ownerless_since: float | None = None
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            pass
        status.contended = True
        last_owner = _read_lock_owner(owner_path)
        if last_owner is not None:
            # A parseable owner record is present: any owner-less streak is
            # broken, and only a STALE owner record is taken over (rename-based).
            observed_ownerless_since = None
            if _lock_is_stale(last_owner, stale_after_s):
                if _takeover_stale_lock(lock_dir, owner_path, stale_after_s):
                    status.takeover = True
                    status.takeover_owner = last_owner
                    continue  # immediately retry the mkdir claim
                # Rename failed: another waiter won the takeover or the
                # filesystem refused - keep waiting (kimi-rev-006).
        else:
            # Owner-LESS dir: NEVER stolen by the stale-owner rename path (D2).
            # Reap ONLY after this waiter has itself observed the dir
            # continuously owner-less past the generous threshold (a crashed
            # pre-write holder) - never on directory mtime.
            now = time.monotonic()
            if observed_ownerless_since is None:
                observed_ownerless_since = now
            elif (now - observed_ownerless_since) > orphan_reap_after_s:
                if _reap_ownerless_orphan(lock_dir, owner_path):
                    status.takeover = True
                    status.takeover_owner = None
                    observed_ownerless_since = None
                    continue  # immediately retry the mkdir claim
                # Lost the reap race (another waiter reaped, the holder just
                # wrote owner.json, or the dir was recreated): restart this
                # waiter's observation so it must re-observe the replacement dir
                # owner-less from scratch, closing the empty-window double-reap.
                observed_ownerless_since = None
        if time.monotonic() >= deadline:
            raise LockTimeout(target, timeout_s, _read_lock_owner(owner_path) or last_owner)
        time.sleep(random.uniform(delay * 0.5, delay))
        delay = min(delay * 2.0, _LOCK_RETRY_CAP_S)
    # Claimed. Record the owner (best effort; a crash before this write leaves
    # an owner-less dir a waiter reclaims only via the observation-time orphan
    # reap above - never a directory-mtime grace).
    try:
        owner_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "claimed_at_epoch": time.time(),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    status.waited_s = time.monotonic() - start
    try:
        yield status
    finally:
        # M1-remediation (consult iteration-path-to-a-remediation-260caad1) D1:
        # robust bounded-retry removal of the owner record then the claim dir -
        # never the pre-remediation swallow-and-orphan. A dir a stale takeover
        # renamed away is already gone and _release_lock returns immediately on
        # the FileNotFoundError. A dir that ultimately cannot be removed is
        # logged to the lock-free dispatch-log sink, never silently orphaned.
        _release_lock(lock_dir, owner_path)
