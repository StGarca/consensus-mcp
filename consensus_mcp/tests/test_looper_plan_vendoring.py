"""Reference-integrity + attribution for the vendored Looper slice (Task 1)."""
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "looper_plan"


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
