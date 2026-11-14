"""Reference-integrity + attribution for the vendored Looper slice (Task 1)
and packaging-completeness guard (Task 10)."""
import fnmatch
import glob
import tomllib
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "looper_plan"
_REPO = Path(__file__).resolve().parents[2]
_CONSENSUS_MCP = _REPO / "consensus_mcp"


def test_rubrics_and_schemas_present():
    for name in ("goal", "verification", "council", "control"):
        assert (PKG / "rubrics" / f"{name}-rubric.md").is_file()
    for name in ("loop.v1.schema.json", "loop.resolved.v1.schema.json"):
        assert (PKG / "schemas" / name).is_file()


def test_vendored_md_lists_every_shipped_rubric_and_schema():
    vend = (PKG / "VENDORED.md").read_text(encoding="utf-8")
    for name in ("goal-rubric.md", "verification-rubric.md",
                 "council-rubric.md", "control-rubric.md",
                 "loop.v1.schema.json", "loop.resolved.v1.schema.json"):
        assert name in vend, f"{name} not recorded in VENDORED.md"


def test_vendored_md_pins_an_upstream_commit():
    vend = (PKG / "VENDORED.md").read_text(encoding="utf-8")
    assert "Pinned commit:" in vend and "ksimback/looper" in vend


def test_notice_carries_full_mit_attestation():
    """MIT compliance: the copyright line AND the full permission notice must be
    retained in the shipped copy, plus the source for provenance."""
    notice = (PKG / "NOTICE").read_text(encoding="utf-8")
    assert "MIT License" in notice
    assert "Copyright (c) 2026 Kevin Simback" in notice
    assert "Permission is hereby granted" in notice
    assert "The above copyright notice and this permission notice" in notice
    assert "https://github.com/ksimback/looper" in notice


def test_external_project_identifiers_confined_to_attestation():
    """Operator policy: the external project's handle/author/URL may appear ONLY
    in the NOTICE / VENDORED.md attestation files - never in the package code,
    rubrics, schemas, the skill, or the looper-plan docs. Guards against a
    promotional external credit/link regressing back into code or marketing."""
    import re
    attest = {PKG / "NOTICE", PKG / "VENDORED.md"}
    scan_roots = [
        PKG,
        _REPO / "consensus_mcp" / "claude_extensions" / "skills" / "consensus-looper-plan",
        _REPO / "docs" / "superpowers" / "specs" / "2026-06-23-consensus-build-looper-plan-design.md",
        _REPO / "docs" / "consensus" / "plans" / "2026-06-23-consensus-build-looper-plan.md",
    ]
    needle = re.compile(r"ksimback|Kevin Simback", re.I)
    offenders = []
    for root in scan_roots:
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for f in files:
            if f in attest or "__pycache__" in f.parts:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if needle.search(text):
                offenders.append(str(f.relative_to(_REPO)))
    assert not offenders, f"external project identifier outside NOTICE/VENDORED: {offenders}"


def test_looper_plan_is_a_declared_package():
    """The .py modules ship only if looper_plan is a declared package."""
    data = tomllib.load(open(_REPO / "pyproject.toml", "rb"))
    assert "consensus_mcp.looper_plan" in data["tool"]["setuptools"]["packages"]


def test_every_looper_plan_data_file_is_packaged():
    """Non-vacuous packaging guard: every shipped data file under looper_plan/
    (rubrics, schemas, NOTICE, VENDORED.md) MUST be matched by a package-data
    glob, or a wheel install ships it missing (mirrors the claude_extensions
    completeness test)."""
    data = tomllib.load(open(_REPO / "pyproject.toml", "rb"))
    globs = data["tool"]["setuptools"]["package-data"]["consensus_mcp"]
    abs_matches = set()
    for g in globs:
        abs_matches.update(glob.glob(str(_CONSENSUS_MCP / g)))
    data_files = [
        p for p in PKG.rglob("*")
        if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts
    ]
    uncovered = [str(p.relative_to(_CONSENSUS_MCP)) for p in data_files
                 if str(p) not in abs_matches
                 and not any(fnmatch.fnmatch(str(p), str(_CONSENSUS_MCP / g)) for g in globs)]
    assert not uncovered, f"looper_plan data files not in package-data: {uncovered}"
