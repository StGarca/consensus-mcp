"""Drift lint for dispatch review templates (F6).

The per-CLI review templates in consensus_mcp/dispatch_templates/ diverge
legitimately in their CLI-specific sections (codex patch_proposal rules,
gemini/grok/kimi no-patch rules), but they MUST stay aligned on:

  1. the shared 'PACKET FIELDS YOU MUST USE' block (F5) - byte-identical
     in every review template, so an edit to the block in one template and
     not the others fails here; and
  2. a canonical set of core section markers every reviewer shares.

Templates are discovered by glob so a newly added *_review_template.md is
covered automatically.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = REPO_ROOT / "consensus_mcp" / "dispatch_templates"

SHARED_BLOCK_HEADING = "## PACKET FIELDS YOU MUST USE"

# Canonical text of the shared block (F5). Any change to the block must be
# made in EVERY review template AND here, or this lint fails.
SHARED_PACKET_FIELDS_BLOCK = """\
## PACKET FIELDS YOU MUST USE

The review packet accompanying this dispatch carries structured fields.
Wherever they appear in your input, you MUST use them as follows:

- `objective`: the goal your review evaluates the change against.
- `open_blockers`: address EVERY listed blocker explicitly in your findings.
- `check_results_if_any`: ground-truth test/check evidence; weigh it above
  your own speculation about whether checks pass.
- `gate_state`: the current gate state of the iteration; respect it.
- `changed_sections`: scope your attention to these sections first.
- `requested_output_schema`: your output MUST follow this schema.
"""

# Observable invariants present in every review template today.
CORE_MARKERS = [
    "# Review target",
    "# Acceptance gates",
    "# Allowed files (in-scope; everything else is OUT of scope)",
]

EXPECTED_TEMPLATES = {
    "codex_review_template.md",
    "gemini_review_template.md",
    "grok_review_template.md",
    "kimi_review_template.md",
    "host_peer_review_template.md",
}


def _review_templates() -> list[Path]:
    return sorted(TEMPLATES_DIR.glob("*_review_template.md"))


def _extract_shared_block(text: str, name: str) -> str:
    """Return the shared block: from its heading up to the next h1 heading."""
    start = text.index(SHARED_BLOCK_HEADING)
    rest = text[start:]
    end = rest.find("\n# ")
    assert end != -1, f"no h1 heading follows the shared block in {name}"
    return rest[:end].rstrip("\n")


def test_expected_review_templates_discovered():
    names = {p.name for p in _review_templates()}
    missing = EXPECTED_TEMPLATES - names
    assert not missing, f"review templates missing from dispatch_templates: {missing}"


def test_shared_packet_fields_block_byte_identical_in_every_template():
    canonical = SHARED_PACKET_FIELDS_BLOCK.rstrip("\n")
    templates = _review_templates()
    assert templates, "no *_review_template.md discovered"
    for path in templates:
        text = path.read_text(encoding="utf-8")
        count = text.count(SHARED_BLOCK_HEADING)
        assert count == 1, (
            f"{path.name}: expected exactly one shared packet-fields block, found {count}"
        )
        block = _extract_shared_block(text, path.name)
        assert block == canonical, (
            f"{path.name}: shared 'PACKET FIELDS YOU MUST USE' block drifted from "
            f"the canonical text (it must be byte-identical in every review template)"
        )


def test_core_markers_present_in_every_template():
    templates = _review_templates()
    assert templates, "no *_review_template.md discovered"
    for path in templates:
        text = path.read_text(encoding="utf-8")
        for marker in CORE_MARKERS:
            assert marker in text, f"{path.name} missing core marker {marker!r}"
