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
# Absence of owner.json in a lock dir older than this = the holder crashed
# between mkdir and the owner write (design Q1 mechanism step 2); a live
# holder writes owner.json immediately after claiming.
_MISSING_OWNER_GRACE_S = 5.0


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


def _lock_is_stale(lock_dir: Path, owner: dict | None, stale_after_s: float) -> bool:
    """Age-only staleness: owner.json claimed_at_epoch older than
    stale_after_s, or no parseable owner record in a lock dir older than the
    missing-owner grace window (crashed pre-write holder)."""
    now = time.time()
    if owner is not None:
        epoch = owner.get("claimed_at_epoch")
        if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
            return (now - float(epoch)) > stale_after_s
    try:
        dir_mtime = os.stat(lock_dir).st_mtime
    except OSError:
        # Lock dir vanished between the failed mkdir and this stat - the
        # holder released; the next mkdir retry claims it. Not stale.
        return False
    return (now - dir_mtime) > _MISSING_OWNER_GRACE_S


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
    adjacent syscalls (a re-claimed dir has a fresh owner record / fresh
    mtime, so the overtaken waiter aborts here and rejoins the wait loop).
    """
    if not _lock_is_stale(lock_dir, _read_lock_owner(owner_path), stale_after_s):
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


@contextmanager
def locked_mutation(
    target: Path,
    *,
    timeout_s: float = 30.0,
    stale_after_s: float = 120.0,
) -> Iterator[LockStatus]:
    """Cross-process mutual exclusion for a read-modify-replace of `target`.

    Claims sibling dir `<target>.lock/` via atomic os.mkdir with jittered
    bounded retry (10ms base doubling to a 250ms cap) until timeout_s, taking
    over stale claims (age > stale_after_s) via rename-then-claim. Raises
    LockTimeout (carrying the holder's owner.json fields) on timeout. Yields
    a LockStatus; NEVER emits events - see the SINK INVARIANT block above.
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
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            pass
        status.contended = True
        last_owner = _read_lock_owner(owner_path)
        if _lock_is_stale(lock_dir, last_owner, stale_after_s):
            if _takeover_stale_lock(lock_dir, owner_path, stale_after_s):
                status.takeover = True
                status.takeover_owner = last_owner
                continue  # immediately retry the mkdir claim
            # Rename failed: another waiter won the takeover or the
            # filesystem refused - keep waiting (kimi-rev-006).
        if time.monotonic() >= deadline:
            raise LockTimeout(target, timeout_s, _read_lock_owner(owner_path) or last_owner)
        time.sleep(random.uniform(delay * 0.5, delay))
        delay = min(delay * 2.0, _LOCK_RETRY_CAP_S)
    # Claimed. Record the owner (best effort; absence is handled by the
    # missing-owner grace window on the waiter side).
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
        # Release: remove the owner record then the claim dir. Tolerate a
        # vanished dir (a stale takeover renamed it away - only possible when
        # a hold outlived stale_after_s, which the hold-window discipline
        # makes pathological).
        try:
            os.unlink(owner_path)
        except OSError:
            pass
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass
