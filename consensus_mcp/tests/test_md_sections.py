"""Behavior tests for the shared spec-md section parser (tools/_md_sections.py).

v2.2.1 audit M0.1a (docs/audits/2026-07-01-v2.2.1-repo-audit.md).

The parser underpins repo.get_section (T9) and repo.set_section (T10); its
documented invariants are:
  - parse(text) -> SectionMap; reconstruct(parse(text)) == text (byte-identical)
  - code-fence aware (fenced '## N.' lines are never headings)
  - frontmatter detected only when '---' is the very first line
  - subsections (### N.M) stay inside their parent section

Style mirrors consensus_mcp/tests/test_state_read_decision_ledger.py:
pure-tmp_path hermetic tests, exact-output behavior assertions.
"""
from __future__ import annotations

import pytest

from consensus_mcp.tools._md_sections import (
    DuplicateSectionError,
    SectionMap,
    parse,
    reconstruct,
)


# Realistic sectioned spec: frontmatter + H1 preamble + 3 numbered sections,
# with a fenced fake heading inside section 1 and a subsection inside section 2.
SPEC_TEXT = (
    "---\n"
    "version: 1\n"
    "status: draft\n"
    "---\n"
    "# Orchestration Spec\n"
    "\n"
    "Preamble paragraph.\n"
    "\n"
    "## 1. Overview\n"
    "\n"
    "Overview body.\n"
    "\n"
    "```\n"
    "## 9. Fenced fake heading\n"
    "```\n"
    "\n"
    "## 2. Goals\n"
    "\n"
    "Goals body.\n"
    "\n"
    "### 2.1 Subgoal\n"
    "\n"
    "Subgoal body.\n"
    "\n"
    "## 3. Non-Goals\n"
    "\n"
    "Non-goals body.\n"
)


def test_parse_section_ids_in_source_order_frontmatter_first():
    smap = parse(SPEC_TEXT)
    assert smap.section_ids() == [
        "frontmatter",
        "section_1",
        "section_2",
        "section_3",
    ]


def test_parse_frontmatter_is_raw_body_between_markers():
    smap = parse(SPEC_TEXT)
    assert smap.get("frontmatter") == "version: 1\nstatus: draft\n"
    # Markers themselves are hidden fields, not part of the section text.
    assert smap._frontmatter_open == "---\n"
    assert smap._frontmatter_close == "---\n"


def test_parse_preamble_preserved_verbatim_and_not_a_section():
    smap = parse(SPEC_TEXT)
    assert smap._preamble == "# Orchestration Spec\n\nPreamble paragraph.\n\n"
    assert "_preamble" not in smap.sections


def test_parse_section_text_spans_heading_to_next_heading():
    smap = parse(SPEC_TEXT)
    assert smap.get("section_1") == (
        "## 1. Overview\n"
        "\n"
        "Overview body.\n"
        "\n"
        "```\n"
        "## 9. Fenced fake heading\n"
        "```\n"
        "\n"
    )
    assert smap.get("section_3") == "## 3. Non-Goals\n\nNon-goals body.\n"


def test_fenced_heading_does_not_open_a_section():
    smap = parse(SPEC_TEXT)
    assert "section_9" not in smap.sections


def test_subsection_stays_inside_parent_section():
    smap = parse(SPEC_TEXT)
    assert "### 2.1 Subgoal" in smap.get("section_2")
    assert smap.get("section_2") == (
        "## 2. Goals\n"
        "\n"
        "Goals body.\n"
        "\n"
        "### 2.1 Subgoal\n"
        "\n"
        "Subgoal body.\n"
        "\n"
    )


def test_roundtrip_is_byte_identical():
    assert reconstruct(parse(SPEC_TEXT)) == SPEC_TEXT


def test_roundtrip_without_frontmatter():
    text = "## 1. Alpha\n\nalpha body\n\n## 2. Beta\n\nbeta body\n"
    smap = parse(text)
    assert smap.section_ids() == ["section_1", "section_2"]
    assert "frontmatter" not in smap.sections
    assert reconstruct(smap) == text


def test_unclosed_frontmatter_treated_as_no_frontmatter():
    text = "---\nversion: 1\n## 1. Alpha\nalpha body\n"
    smap = parse(text)
    assert "frontmatter" not in smap.sections
    # The stray '---' and yaml line become preamble; roundtrip still exact.
    assert smap._preamble == "---\nversion: 1\n"
    assert smap.section_ids() == ["section_1"]
    assert reconstruct(smap) == text


def test_blank_line_before_delimiter_disqualifies_frontmatter():
    text = "\n---\nversion: 1\n---\n## 1. Alpha\nalpha body\n"
    smap = parse(text)
    assert "frontmatter" not in smap.sections
    assert reconstruct(smap) == text


def test_fenced_heading_in_preamble_is_not_a_section():
    text = (
        "intro\n"
        "```\n"
        "## 1. Fenced fake\n"
        "```\n"
        "## 2. Real\n"
        "real body\n"
    )
    smap = parse(text)
    assert smap.section_ids() == ["section_2"]
    assert "## 1. Fenced fake" in smap._preamble
    assert reconstruct(smap) == text


def test_tilde_fence_also_hides_headings():
    text = "## 1. A\n~~~\n## 7. Fake\n~~~\n## 2. B\nb body\n"
    smap = parse(text)
    assert smap.section_ids() == ["section_1", "section_2"]
    assert "## 7. Fake" in smap.get("section_1")
    assert reconstruct(smap) == text


def test_multi_digit_heading_number():
    text = "## 10. Ten\nten body\n## 11. Eleven\neleven body\n"
    smap = parse(text)
    assert smap.section_ids() == ["section_10", "section_11"]
    assert smap.get("section_10") == "## 10. Ten\nten body\n"


def test_heading_requires_space_after_dot():
    text = "## 1. A\nx\n## 5.NoSpace\ny\n"
    smap = parse(text)
    assert smap.section_ids() == ["section_1"]
    # The non-matching line is owned by the preceding section.
    assert "## 5.NoSpace" in smap.get("section_1")
    assert reconstruct(smap) == text


def test_empty_text_parses_to_empty_map_and_roundtrips():
    smap = parse("")
    assert smap.sections == {}
    assert smap.section_ids() == []
    assert reconstruct(smap) == ""


def test_preamble_only_text_has_no_sections_and_roundtrips():
    text = "# Title only\n\nNo numbered headings here.\n"
    smap = parse(text)
    assert smap.sections == {}
    assert smap._preamble == text
    assert reconstruct(smap) == text


def test_replace_returns_new_map_and_leaves_original_untouched():
    smap = parse(SPEC_TEXT)
    new_body = "## 2. Goals\n\nRewritten goals.\n\n"
    replaced = smap.replace("section_2", new_body)
    assert replaced is not smap
    assert replaced.get("section_2") == new_body
    assert smap.get("section_2") != new_body
    # Reconstruction of the replaced map equals a plain string substitution.
    expected = SPEC_TEXT.replace(
        "## 2. Goals\n\nGoals body.\n\n### 2.1 Subgoal\n\nSubgoal body.\n\n",
        new_body,
    )
    assert reconstruct(replaced) == expected


def test_replace_unknown_section_id_raises_keyerror():
    smap = parse(SPEC_TEXT)
    with pytest.raises(KeyError):
        smap.replace("section_99", "anything")


def test_reconstruct_empty_sectionmap_is_empty_string():
    assert reconstruct(SectionMap()) == ""


# ---------------------------------------------------------------------------
# Duplicate '## N.' heading numbers (malformed input) - structured refusal.
# M1 (consult iteration-m1-hardening-design-4d7d2469) S3: parse() now raises
# DuplicateSectionError instead of keyed-by-number last-wins, which broke the
# byte-identical roundtrip invariant and let set_section corrupt such files.
# ---------------------------------------------------------------------------

DUP_TEXT = (
    "## 1. Alpha\n"
    "alpha body\n"
    "## 2. First\n"
    "first body\n"
    "## 2. Second\n"
    "second body\n"
    "## 3. Omega\n"
    "omega body\n"
)


def test_duplicate_heading_numbers_raise_structured_error():
    """M1 S3 flip of the M0 documents-HEAD-behavior test: last-wins keying is
    gone; parse() refuses with the duplicated ids carried on the exception."""
    with pytest.raises(DuplicateSectionError) as excinfo:
        parse(DUP_TEXT)
    assert excinfo.value.duplicate_section_ids == ["section_2"]


def test_duplicate_heading_numbers_roundtrip_byte_identical():
    """Flipped M0 strict-xfail (v2.2.1 audit M0.1a): the corrupting roundtrip
    (reconstruct dropped the first '## 2.' block and doubled the second) is
    now UNREACHABLE because parse() refuses duplicate ids outright. The
    documented byte-identical invariant therefore holds for every text
    parse() accepts."""
    with pytest.raises(DuplicateSectionError):
        parse(DUP_TEXT)
    # The invariant still holds on the nearest well-formed variant.
    fixed = DUP_TEXT.replace("## 2. Second\n", "## 4. Second\n")
    assert reconstruct(parse(fixed)) == fixed


def test_duplicate_error_reports_all_duplicated_ids_sorted():
    text = (
        "## 2. B1\nb1\n"
        "## 1. A1\na1\n"
        "## 2. B2\nb2\n"
        "## 1. A2\na2\n"
        "## 2. B3\nb3\n"
    )
    with pytest.raises(DuplicateSectionError) as excinfo:
        parse(text)
    assert excinfo.value.duplicate_section_ids == ["section_1", "section_2"]
    # str() names the ids so raw tracebacks are actionable.
    assert "section_1" in str(excinfo.value)
    assert "section_2" in str(excinfo.value)


def test_duplicate_error_is_a_valueerror():
    assert issubclass(DuplicateSectionError, ValueError)


def test_fenced_duplicate_heading_does_not_trigger_refusal():
    """A '## N.' line inside a code fence is not a heading, so it cannot
    collide with a real section of the same number."""
    text = (
        "## 1. Real\n"
        "```\n"
        "## 1. Fenced fake duplicate\n"
        "```\n"
        "## 2. Next\nnext body\n"
    )
    smap = parse(text)
    assert smap.section_ids() == ["section_1", "section_2"]
    assert reconstruct(smap) == text
