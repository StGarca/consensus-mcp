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
"""
from __future__ import annotations

import os
from pathlib import Path


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
