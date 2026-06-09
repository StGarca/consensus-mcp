"""_shared.py - helpers shared by the Phase 0 validators.

_sha256_file and _dependency_version were byte-for-byte identical copies in
seven validator modules; they live here so the copies cannot silently drift.

_build_provenance is intentionally NOT shared: each validator builds a
different provenance shape (different signatures, inputs blocks, and git
blocks), so the per-validator definitions stay local - same rationale as the
per-validator _wrap helpers.
"""
from __future__ import annotations
import hashlib
import importlib.metadata
from pathlib import Path


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dependency_version(dist_name: str) -> str | None:
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None
