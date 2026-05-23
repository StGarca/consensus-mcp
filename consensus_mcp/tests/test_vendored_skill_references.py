"""Reference-integrity for the vendored skills.

The 2026-05-23 review (and its re-review) found adapted skills + companion files
referencing files/skills that were never vendored — dangling links the
install/packaging tests didn't catch. This test makes that class mechanically
impossible to reintroduce. It scans EVERY *.md in each skill dir (SKILL.md AND
companions) for three reference shapes:

  A. same-dir companion refs   `./x.md` / `@x.md`     -> must exist in that dir
  B. subdirectory refs         `dir/x.md` (bare, backticked, or markdown-link)
                               -> must resolve (skill-relative, incl. an upstream
                                  skill name that needs the consensus- prefix)
  C. skill cross-refs          `<ns>:<skill-name>`    -> must use the consensus:
                                  namespace AND name an existing skill dir

(B) is what the original test missed: `requesting-code-review/code-reviewer.md`
slipped through because it has no `./`/`@` prefix.
"""
import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "claude_extensions" / "skills"
# Path prefixes that are OUTPUT/doc/url/install targets, not vendored skill files.
# "claude/" catches install-location refs like `~/.claude/skills/...` (the leading
# `~/.` is stripped by the path regex, leaving `claude/...`).
_IGNORE_PREFIX = ("docs/", "/", "~", "http", ".github/", "claude/", ".claude/")


def _skill_dirs():
    return [d for d in sorted(SKILLS.iterdir())
            if d.is_dir() and (d / "SKILL.md").exists()]


def _all_md(d):
    return sorted(d.glob("*.md"))


# --- A: same-dir companion refs (./x.md, @x.md) ----------------------------- #

def test_same_dir_companion_references_resolve():
    missing = []
    for d in _skill_dirs():
        for md in _all_md(d):
            for name in re.findall(r"(?:\./|@)([A-Za-z0-9_-]+\.md)",
                                   md.read_text(encoding="utf-8")):
                if not (d / name).exists():
                    missing.append(f"{md.relative_to(SKILLS)} -> ./{name}")
    assert not missing, "dangling same-dir companion refs:\n" + "\n".join(missing)


# --- B: subdirectory-style refs (dir/file.md), any wrapper ------------------ #

def _is_skill_path_ref(ref: str) -> bool:
    if not ref.endswith(".md") or "/" not in ref:
        return False
    if ref.startswith(_IGNORE_PREFIX):
        return False
    if any(c in ref for c in "<>") or "YYYY" in ref:
        return False
    # Reject domain-like refs (a dot before the .md extension), e.g. a URL path
    # component such as example.com/foo.md that the path regex can capture.
    if "." in ref[:-len(".md")]:
        return False
    return True


def _resolves(ref: str, skill_dir: Path) -> bool:
    parts = ref.split("/")
    candidates = [
        skill_dir / ref,                                        # relative to this skill
        SKILLS / ref,                                           # relative to skills root
        SKILLS / ("consensus-" + parts[0]) / "/".join(parts[1:]),  # upstream name + prefix
    ]
    if parts[0] == "skills" and len(parts) > 1:                 # `skills/consensus-x/y.md`
        candidates.append(SKILLS / "/".join(parts[1:]))
    return any(c.exists() for c in candidates)


def test_subdirectory_md_references_resolve():
    ref_re = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.md")
    missing = []
    for d in _skill_dirs():
        for md in _all_md(d):
            for ref in ref_re.findall(md.read_text(encoding="utf-8")):
                if ref.startswith("@"):
                    ref = ref[1:]
                if ref.startswith("./"):
                    ref = ref[2:]
                if _is_skill_path_ref(ref) and not _resolves(ref, d):
                    missing.append(f"{md.relative_to(SKILLS)} -> {ref}")
    assert not missing, "dangling subdirectory refs:\n" + "\n".join(missing)


# --- C: skill cross-refs use consensus: namespace + exist (ALL *.md) -------- #

def test_skill_cross_references_are_consensus_namespace_and_exist():
    # `<ns>:<skill-name>` where the NAME is hyphenated (writing-plans,
    # test-driven-development). The hyphen requirement avoids CSS/time/graphviz
    # tokens (shape:box, 12:00, align-items:center -> name has no hyphen).
    pat = re.compile(r"\b([a-z][a-z0-9-]*):([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b")
    bad = []
    for d in _skill_dirs():
        for md in _all_md(d):
            for ns, name in pat.findall(md.read_text(encoding="utf-8")):
                if ns != "consensus":
                    bad.append(f"{md.relative_to(SKILLS)}: foreign skill ref '{ns}:{name}'")
                elif not (SKILLS / f"consensus-{name}").is_dir():
                    bad.append(f"{md.relative_to(SKILLS)}: 'consensus:{name}' -> no consensus-{name}")
    assert not bad, "bad skill cross-references:\n" + "\n".join(bad)


# --- resolver/heuristic unit proofs (round-3 re-review findings) ------------- #

def test_resolver_handles_skills_prefixed_refs():
    # A real `skills/consensus-*/x.md` ref must RESOLVE, not false-positive
    # (codex/kimi round-3: the extra `skills/` segment regressed resolution).
    d = _skill_dirs()[0]
    assert _resolves("skills/consensus-brainstorming/SKILL.md", d)


def test_heuristic_ignores_urlish_md_refs_but_keeps_real_ones():
    # kimi round-3: a URL component like example.com/foo.md must NOT be treated
    # as a skill ref; a real sibling ref still must be.
    assert not _is_skill_path_ref("example.com/page.md")
    assert _is_skill_path_ref("requesting-code-review/code-reviewer.md")
