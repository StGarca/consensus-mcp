"""The single shared atomic writer (gemini-rev-001 / kimi-rev-001)."""
from __future__ import annotations

import os
from pathlib import Path

from consensus_mcp import _atomic_io as aio


def test_atomic_write_text_roundtrips_and_creates_parents(tmp_path):
    p = tmp_path / "sub" / "marker.yaml"
    aio.atomic_write_text(p, "key: value\n")
    assert p.read_text(encoding="utf-8") == "key: value\n"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    p = tmp_path / "marker"
    aio.atomic_write_text(p, "x")
    leftovers = [q.name for q in tmp_path.iterdir() if q.name != "marker"]
    assert leftovers == [], leftovers


def test_atomic_write_replaces_destination_symlink_not_target(tmp_path):
    """os.replace swaps the temp into place, replacing a destination SYMLINK (the
    link itself) - the symlink's target is never written through."""
    target = tmp_path / "outside.txt"
    target.write_text("original", encoding="utf-8")
    link = tmp_path / "marker"
    os.symlink(target, link)
    aio.atomic_write_text(link, "new-marker-content")
    # the symlink was replaced by a real file; the target is untouched.
    assert link.read_text(encoding="utf-8") == "new-marker-content"
    assert not link.is_symlink()
    assert target.read_text(encoding="utf-8") == "original"


def test_atomic_write_overwrites_existing(tmp_path):
    p = tmp_path / "m"
    aio.atomic_write_text(p, "v1")
    aio.atomic_write_text(p, "v2")
    assert p.read_text(encoding="utf-8") == "v2"
