"""v1.21 packaging-gap regression.

Every asset the installer copies (`_CLAUDE_EXTENSION_FILES`) and every enforcement
hook script (`_CONSENSUS_HOOK_SPECS`) MUST be matched by a `[tool.setuptools.package-data]`
glob in pyproject.toml — otherwise a wheel/pip install ships them missing, and
`consensus-init --install-claude-code` silently skips skills and activates
settings.json hooks pointing at non-existent scripts (enforcement dead). This test
fails closed if a new vendored skill / hook is added without a covering glob.
"""
from __future__ import annotations

import glob as _glob
import tomllib
from pathlib import Path

from consensus_mcp import _init_wizard as wiz

_PKG = Path(wiz.__file__).resolve().parent              # consensus_mcp/
_PYPROJECT = _PKG.parent / "pyproject.toml"
_EXT_ROOT = _PKG / "claude_extensions"


def _package_data_globs() -> list[str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["package-data"]["consensus_mcp"]


def _packaged_paths() -> set[str]:
    """Real glob expansion (setuptools semantics: * does not cross /) of every
    package-data glob, resolved to absolute paths."""
    matched: set[str] = set()
    for g in _package_data_globs():
        for p in _glob.glob(str(_PKG / g)):
            matched.add(str(Path(p).resolve()))
    return matched


def test_every_claude_extension_file_is_packaged():
    packaged = _packaged_paths()
    uncovered = []
    for rel_src, _dst in wiz._CLAUDE_EXTENSION_FILES:
        full = (_EXT_ROOT / rel_src).resolve()
        assert full.exists(), f"shipped source asset missing on disk: {rel_src}"
        if str(full) not in packaged:
            uncovered.append(rel_src)
    assert not uncovered, (
        f"package-data does NOT cover these installer assets (wheel would ship them "
        f"missing): {uncovered}"
    )


def test_every_enforcement_hook_script_is_packaged():
    packaged = _packaged_paths()
    for _event, _matcher, script in wiz._CONSENSUS_HOOK_SPECS:
        full = (_EXT_ROOT / "hooks" / script).resolve()
        assert full.exists(), f"hook script missing on disk: hooks/{script}"
        assert str(full) in packaged, (
            f"package-data does NOT cover enforcement hook script hooks/{script} — "
            f"settings.json activation would point at a non-existent file in a wheel"
        )
