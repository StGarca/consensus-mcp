"""Unit tests for iter-0021 — embed touched-file contents in review-packet.

Per iter-0020 empirical finding: codex's read-only sandbox cannot reliably read
repo files even when given paths. iter-0021 fix: stop expecting codex to read
files. EMBED file contents directly in the review-packet so codex sees them as
part of the prompt.

This test module covers:
  1. _build_prompt substitutes {touched_files_contents_block} from
     review_packet.defect_target.touched_files_contents.
  2. _format_touched_files_contents produces the expected ``## File: <path>``
     + fenced-block format.
  3. The default template contains the {touched_files_contents_block}
     placeholder (template authoring smoke).
  4. _author_review_packet helper produces a YAML review-packet skeleton with
     defect_target.touched_files_contents populated from real files on disk.
  5. _author_review_packet helper merges into an existing review-packet
     without clobbering operator-authored fields.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _dispatch_codex  # noqa: E402


# ---------------------------------------------------------------------------
# _build_prompt: {touched_files_contents_block} substitution
# ---------------------------------------------------------------------------


def test_build_prompt_substitutes_touched_files_contents_block():
    """_build_prompt fills {touched_files_contents_block} from review-packet input."""
    template = "Files:\n{touched_files_contents_block}\n--END--"
    packet = {"goal": {"summary": "x"}, "authorization": {}}
    review_packet = {
        "defect_target": {
            "files": ["scripts/foo.py", "scripts/bar.py"],
            "touched_files_contents": {
                "scripts/foo.py": "def foo():\n    return 1\n",
                "scripts/bar.py": "BAR = 42\n",
            },
        }
    }
    prompt = _dispatch_codex._build_prompt(packet, template, review_packet=review_packet)
    assert "{touched_files_contents_block}" not in prompt
    assert "## File: scripts/foo.py" in prompt
    assert "## File: scripts/bar.py" in prompt
    assert "def foo():" in prompt
    assert "BAR = 42" in prompt


def test_build_prompt_touched_files_block_empty_when_review_packet_omits_it():
    """When review-packet has no defect_target.touched_files_contents, block is a placeholder string."""
    template = "Block:\n{touched_files_contents_block}\n--END--"
    packet = {"goal": {"summary": "x"}, "authorization": {}}
    # No review_packet kwarg supplied.
    prompt = _dispatch_codex._build_prompt(packet, template)
    assert "{touched_files_contents_block}" not in prompt
    # Empty/missing case must render a self-describing placeholder so codex
    # knows the field was intentionally absent (vs. a tooling bug).
    assert "(no touched-file contents embedded)" in prompt


def test_build_prompt_touched_files_block_with_review_packet_no_defect_target():
    """review_packet supplied but no defect_target -> placeholder."""
    template = "Block:\n{touched_files_contents_block}\n--END--"
    packet = {"goal": {"summary": "x"}, "authorization": {}}
    review_packet = {"some_other_field": "value"}
    prompt = _dispatch_codex._build_prompt(packet, template, review_packet=review_packet)
    assert "(no touched-file contents embedded)" in prompt


def test_format_touched_files_contents_renders_per_file_sections():
    """Helper produces ``## File: <path>`` then a fenced code block per file."""
    contents = {
        "scripts/a.py": "PY_A = 1\n",
        "docs/notes.md": "# Notes\n\ntext\n",
    }
    formatted = _dispatch_codex._format_touched_files_contents(contents)
    assert "## File: scripts/a.py" in formatted
    assert "## File: docs/notes.md" in formatted
    assert "```python" in formatted
    assert "```markdown" in formatted
    # Order is deterministic (sorted by path).
    a_idx = formatted.index("## File: scripts/a.py")
    n_idx = formatted.index("## File: docs/notes.md")
    # docs/ sorts before scripts/ lexicographically.
    assert n_idx < a_idx


def test_format_touched_files_contents_unknown_extension_uses_text_fence():
    contents = {"data/blob.xyz": "raw bytes\n"}
    formatted = _dispatch_codex._format_touched_files_contents(contents)
    assert "```text" in formatted
    assert "raw bytes" in formatted


# ---------------------------------------------------------------------------
# Default template authoring smoke — placeholder is present
# ---------------------------------------------------------------------------


def test_default_template_has_touched_files_contents_block_placeholder():
    """Default codex_review_template.md contains {touched_files_contents_block}."""
    template_path = (
        REPO_ROOT
        / "consensus_mcp"
        / "dispatch_templates"
        / "codex_review_template.md"
    )
    text = template_path.read_text(encoding="utf-8")
    assert "{touched_files_contents_block}" in text
    # The instructional preamble must tell codex to use embedded contents and
    # not attempt filesystem reads.
    assert "Do NOT" in text or "do NOT" in text
    assert "embed" in text.lower()


# ---------------------------------------------------------------------------
# _author_review_packet helper (CLI behavior + merge semantics)
# ---------------------------------------------------------------------------


def test_author_review_packet_creates_skeleton_with_embedded_contents(tmp_path):
    """Helper writes review-packet.yaml with defect_target.touched_files_contents populated."""
    from consensus_mcp import _author_review_packet

    iter_dir = tmp_path / "iteration-0021-fake"
    iter_dir.mkdir()
    foo = tmp_path / "scripts" / "foo.py"
    foo.parent.mkdir(parents=True, exist_ok=True)
    foo.write_text("FOO = 1\n", encoding="utf-8")
    bar = tmp_path / "scripts" / "bar.py"
    bar.write_text("BAR = 2\n", encoding="utf-8")

    rc = _author_review_packet.main([
        "--iteration-dir", str(iter_dir),
        "--files", "scripts/foo.py,scripts/bar.py",
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0
    out = iter_dir / "review-packet.yaml"
    assert out.exists()
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    dt = data["defect_target"]
    assert dt["files"] == ["scripts/foo.py", "scripts/bar.py"]
    assert dt["touched_files_contents"]["scripts/foo.py"] == "FOO = 1\n"
    assert dt["touched_files_contents"]["scripts/bar.py"] == "BAR = 2\n"
    # base_sha must be present (computed via bundle_sha).
    assert "base_sha" in dt
    assert isinstance(dt["base_sha"], str)
    assert len(dt["base_sha"]) == 64


def test_author_review_packet_merges_into_existing_file(tmp_path):
    """Existing review-packet.yaml fields outside defect_target are preserved."""
    from consensus_mcp import _author_review_packet

    iter_dir = tmp_path / "iteration-0021-fake"
    iter_dir.mkdir()
    existing = {
        "schema_version": 1,
        "iteration_id": "iteration-0021-fake",
        "review_target": "operator-authored review_target text",
        "scope": {"goal_summary": "operator goal"},
    }
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump(existing, sort_keys=False), encoding="utf-8"
    )
    foo = tmp_path / "scripts" / "foo.py"
    foo.parent.mkdir(parents=True, exist_ok=True)
    foo.write_text("FOO = 1\n", encoding="utf-8")

    rc = _author_review_packet.main([
        "--iteration-dir", str(iter_dir),
        "--files", "scripts/foo.py",
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0
    data = yaml.safe_load((iter_dir / "review-packet.yaml").read_text(encoding="utf-8"))
    # Operator fields preserved.
    assert data["review_target"] == "operator-authored review_target text"
    assert data["scope"]["goal_summary"] == "operator goal"
    # defect_target injected.
    assert data["defect_target"]["files"] == ["scripts/foo.py"]
    assert data["defect_target"]["touched_files_contents"]["scripts/foo.py"] == "FOO = 1\n"


def test_author_review_packet_refuses_missing_file(tmp_path):
    """If a listed file does not exist, helper exits non-zero (fail-closed)."""
    from consensus_mcp import _author_review_packet

    iter_dir = tmp_path / "iteration-0021-fake"
    iter_dir.mkdir()

    rc = _author_review_packet.main([
        "--iteration-dir", str(iter_dir),
        "--files", "scripts/does_not_exist.py",
        "--repo-root", str(tmp_path),
    ])
    assert rc != 0
