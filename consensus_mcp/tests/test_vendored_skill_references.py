"""Reference-integrity for the vendored skills.

The 2026-05-23 3-family review found that several adapted SKILL.md files referenced
companion files (prompt templates, testing-anti-patterns.md) and a foreign skill
(elements-of-style:) that were never vendored — dangling links the install/packaging
tests didn't catch. This test makes that class mechanically impossible to reintroduce:

  1. Every `./x.md` / `@x.md` companion reference resolves to a file in that skill dir.
  2. Every `skills/consensus-*/x.md` reference resolves.
  3. Every hyphenated skill cross-reference (`<ns>:<skill-name>`) uses the `consensus:`
     namespace AND points at a skill directory that exists.
"""
import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "claude_extensions" / "skills"


def _skill_dirs():
    return [d for d in sorted(SKILLS.iterdir())
            if d.is_dir() and (d / "SKILL.md").exists()]


def test_companion_file_references_resolve():
    missing = []
    for d in _skill_dirs():
        for md in sorted(d.glob("*.md")):
            text = md.read_text(encoding="utf-8")
            for name in re.findall(r"(?:\./|@)([A-Za-z0-9_-]+\.md)", text):
                if not (d / name).exists():
                    missing.append(f"{md.relative_to(SKILLS)} -> ./{name} (absent in {d.name}/)")
            for rel in re.findall(r"skills/(consensus-[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.md)", text):
                if not (SKILLS / rel).exists():
                    missing.append(f"{md.relative_to(SKILLS)} -> skills/{rel} (absent)")
    assert not missing, "dangling companion-file references:\n" + "\n".join(missing)


def test_skill_cross_references_are_consensus_namespace_and_exist():
    # A skill ref is `<namespace>:<skill-name>` where the NAME has at least one
    # hyphen (skill names are hyphenated: writing-plans, test-driven-development).
    # This shape avoids matching CSS/time/graphviz tokens (no hyphen in the name).
    pat = re.compile(r"\b([a-z][a-z0-9-]*):([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b")
    bad = []
    for d in _skill_dirs():
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        for ns, name in pat.findall(text):
            if ns != "consensus":
                bad.append(f"{d.name}: foreign skill ref '{ns}:{name}' (not vendored)")
            elif not (SKILLS / f"consensus-{name}").is_dir():
                bad.append(f"{d.name}: 'consensus:{name}' -> no skill dir consensus-{name}")
    assert not bad, "bad skill cross-references:\n" + "\n".join(bad)
