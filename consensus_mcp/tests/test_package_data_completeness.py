"""v1.21 packaging-gap regression.

Every asset the installer copies (`_CLAUDE_EXTENSION_FILES`) and every enforcement
hook script (`_CONSENSUS_HOOK_SPECS`) MUST be matched by a `[tool.setuptools.package-data]`
glob in pyproject.toml - otherwise a wheel/pip install ships them missing, and
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


def test_every_skill_on_disk_is_in_the_install_manifest():
    """Reverse guard (v2.1.0): the install manifest (_CLAUDE_EXTENSION_FILES) is
    hardcoded, so a NEW skill dir present on disk + covered by package-data still
    ships in the wheel but `--install-claude-code` never copies it. The forward
    test (manifest -> packaged) cannot catch that. Assert every skills/*/SKILL.md
    on disk is named in the install manifest."""
    manifest_srcs = {rel_src for rel_src, _ in wiz._CLAUDE_EXTENSION_FILES}
    missing = [
        skill_md.relative_to(_EXT_ROOT).as_posix()
        for skill_md in sorted((_EXT_ROOT / "skills").glob("*/SKILL.md"))
        if skill_md.relative_to(_EXT_ROOT).as_posix() not in manifest_srcs
    ]
    assert not missing, (
        f"skill SKILL.md on disk but NOT in _CLAUDE_EXTENSION_FILES install manifest "
        f"(--install-claude-code would skip it): {missing}"
    )


def _installed_skill_dirs() -> list[Path]:
    """The skill directories `--install-claude-code` actually installs - derived
    STRUCTURALLY as the parent of every skills/*/SKILL.md named in the install
    manifest (`_CLAUDE_EXTENSION_FILES`). Scoping to installed skills matches what
    the installer copies (an un-installed skill dir's companions are irrelevant)."""
    return sorted(
        {
            (_EXT_ROOT / rel_src).parent
            for rel_src, _ in wiz._CLAUDE_EXTENSION_FILES
            if rel_src.startswith("skills/") and rel_src.endswith("/SKILL.md")
        }
    )


def test_every_installed_skill_companion_md_is_in_the_install_manifest():
    """M1-remediation (consult iteration-path-to-a-remediation-260caad1) - Q7
    companion-file reverse guard.

    The wheel ships every skills/*/*.md via the package-data glob, but
    `--install-claude-code` copies ONLY the files named in _CLAUDE_EXTENSION_FILES.
    The existing reverse guard (test_every_skill_on_disk_is_in_the_install_manifest)
    checks the SKILL.md itself, NOT its companions. So a NEW companion .md added
    into an installed skill dir (e.g. a prompt the SKILL.md hands off to) ships in
    the wheel yet is silently never copied by the installer, leaving a dangling
    reference in the installed location - the twice-shipped v1.22 / v2.1.1
    dangling-companion class.

    Derive the required install set STRUCTURALLY from what is physically shipped:
    for every installed skill dir, every companion .md present in it on disk MUST
    also be named in the manifest. (We key off physical presence, NOT SKILL.md
    prose references - prose cites many non-companion .md files: project files like
    CLAUDE.md/README.md, docs paths, and rubric files vendored elsewhere - which
    would make a reference-scraping guard fragile and false-positive.)
    """
    manifest_srcs = {rel_src for rel_src, _ in wiz._CLAUDE_EXTENSION_FILES}
    skill_dirs = _installed_skill_dirs()
    assert skill_dirs, (
        "no installed skill dirs derived from _CLAUDE_EXTENSION_FILES - the guard "
        "would be vacuous; check the manifest / skills layout"
    )

    checked = 0
    omitted: list[str] = []
    for skill_dir in skill_dirs:
        for companion in sorted(skill_dir.glob("*.md")):
            rel = companion.relative_to(_EXT_ROOT).as_posix()
            if rel.endswith("/SKILL.md"):
                continue  # the SKILL.md itself is guarded by the sibling test
            checked += 1
            if rel not in manifest_srcs:
                omitted.append(rel)

    # Non-vacuous tripwire: this class only exists because installed skills DO
    # carry companion .md files (4 today). If that ever drops to zero the guard has
    # silently stopped protecting anything - fail loud so it is re-examined.
    assert checked, (
        "no companion .md files found in any installed skill dir - the reverse "
        "guard is vacuous; verify _EXT_ROOT and the skills/*/ layout"
    )
    assert not omitted, (
        f"companion .md ships in the wheel (skills/*/*.md glob) but is NOT in "
        f"_CLAUDE_EXTENSION_FILES, so `--install-claude-code` would skip it and "
        f"leave a dangling reference: {omitted}"
    )


def test_every_enforcement_hook_script_is_packaged():
    packaged = _packaged_paths()
    for _event, _matcher, script in wiz._CONSENSUS_HOOK_SPECS:
        full = (_EXT_ROOT / "hooks" / script).resolve()
        assert full.exists(), f"hook script missing on disk: hooks/{script}"
        assert str(full) in packaged, (
            f"package-data does NOT cover enforcement hook script hooks/{script} - "
            f"settings.json activation would point at a non-existent file in a wheel"
        )
