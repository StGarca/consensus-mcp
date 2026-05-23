"""v1.21: the vendored Superpowers skills + attribution install via the existing
_install_claude_extensions copy-to-CLAUDE_HOME path (no new mechanism, no
Superpowers prerequisite)."""
from pathlib import Path

from consensus_mcp import _init_wizard as wiz


VENDORED_SKILLS = (
    "consensus-brainstorming",
    "consensus-writing-plans",
    "consensus-executing-plans",
    "consensus-subagent-driven-development",
    "consensus-test-driven-development",
    "consensus-requesting-code-review",
    "consensus-receiving-code-review",
    "consensus-verification-before-completion",
    "consensus-finishing-a-development-branch",
    "consensus-using-git-worktrees",
)


def _dst_set():
    return {dst for _src, dst in wiz._CLAUDE_EXTENSION_FILES}


def test_all_10_vendored_skills_are_wired():
    dsts = _dst_set()
    for name in VENDORED_SKILLS:
        assert f"skills/{name}/SKILL.md" in dsts, f"{name} not wired into _CLAUDE_EXTENSION_FILES"


def test_attribution_assets_wired():
    dsts = _dst_set()
    assert "NOTICE" in dsts
    assert "VENDORED.md" in dsts


def test_source_assets_exist_for_every_wired_entry():
    root = wiz._claude_extensions_source_root()
    for rel_src, _dst in wiz._CLAUDE_EXTENSION_FILES:
        assert (root / rel_src).exists(), f"shipped source asset missing: {rel_src}"


def test_install_copies_vendored_skills(tmp_path: Path):
    statuses = wiz._install_claude_extensions(tmp_path, force=False)
    for name in VENDORED_SKILLS:
        dst = tmp_path / "skills" / name / "SKILL.md"
        assert dst.exists(), f"{name} not copied to CLAUDE_HOME"
        assert dst.read_text(encoding="utf-8").strip(), f"{name} copied empty"
    assert (tmp_path / "NOTICE").exists()
    assert not any(s.startswith("WARN") for s in statuses), statuses


def test_no_residual_superpowers_skill_refs_in_vendored(tmp_path: Path):
    """Vendored bodies must reference consensus:* not superpowers:* (outside the
    MIT attribution line)."""
    root = wiz._claude_extensions_source_root()
    for name in VENDORED_SKILLS:
        text = (root / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        offending = [
            ln for ln in text.splitlines()
            if "superpowers:" in ln and "obra/superpowers" not in ln and "Vendored from" not in ln
        ]
        assert not offending, f"{name} has residual superpowers: refs: {offending[:3]}"
