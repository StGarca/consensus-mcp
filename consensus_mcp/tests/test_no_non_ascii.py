"""Guard: the repository must contain NO non-ASCII bytes in tracked text files.

Operator directive (2026-06-02): no emoji / non-ASCII typography anywhere in the
project. Non-ASCII glyphs are an "AI-slop" tell AND a portability liability (a
cp1252 Windows console or a locale-naive subprocess crashes on them). This test
is the permanent enforcement: it scans every tracked file and fails on the first
byte > 0x7F, naming the file and offending codepoints.

If you legitimately need a non-ASCII *runtime* value in a test, construct it with
chr()/ordinals so the SOURCE stays ASCII (see test_init_wizard_encoding.py and
test_snapshot_state.py for the pattern) -- do not put the raw glyph in the file.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Binary file types are exempt (they are not text and may legitimately contain
# bytes > 0x7F). Everything else tracked by git is in scope.
_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tgz",
    ".whl", ".pyc", ".so", ".woff", ".woff2", ".ttf", ".otf", ".bin", ".jar",
}
# The build/ tree is a generated copy; not authored source.
_SKIP_PREFIXES = ("build/",)


def _tracked_text_files() -> list[str]:
    out = subprocess.check_output(
        ["git", "ls-files"], cwd=_REPO_ROOT, text=True
    )
    files = []
    for rel in out.splitlines():
        if rel.startswith(_SKIP_PREFIXES):
            continue
        if os.path.splitext(rel)[1].lower() in _BINARY_EXT:
            continue
        files.append(rel)
    return files


def _non_ascii_codepoints(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        # Undecodable as UTF-8 -> treat as binary, not our concern here.
        return []
    return sorted({f"U+{ord(c):04X} {c!r}" for c in text if ord(c) > 0x7F})


def test_no_non_ascii_in_tracked_files():
    offenders: dict[str, list[str]] = {}
    for rel in _tracked_text_files():
        bad = _non_ascii_codepoints(_REPO_ROOT / rel)
        if bad:
            offenders[rel] = bad
    if offenders:
        lines = [f"  {rel}: {', '.join(cps)}" for rel, cps in sorted(offenders.items())]
        pytest.fail(
            "Non-ASCII bytes found in tracked text files (project policy: ASCII "
            "only -- use chr()/ordinals for non-ASCII runtime values in tests):\n"
            + "\n".join(lines)
        )
