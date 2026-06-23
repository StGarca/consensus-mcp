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


def test_notice_carries_mit_and_upstream():
    notice = (PKG / "NOTICE").read_text(encoding="utf-8")
    assert "MIT" in notice and "ksimback/looper" in notice


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
