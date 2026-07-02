"""Behavior tests for the repo.get_section MCP tool (tools/repo_get_section.py).

v2.2.1 audit M0.1a (docs/audits/2026-07-01-v2.2.1-repo-audit.md).

Covers: exact section read (zero leakage), section_not_found with the
available-ids diagnostic, file_not_found / file_required / invalid_utf8
refusals, the path-traversal containment guard (relative '..' escape,
absolute outside path, in-repo symlink to outside), lazy project_root()
env redirection, and register() wire-name exposure.

Style mirrors consensus_mcp/tests/test_state_read_decision_ledger.py:
tmp_path only, monkeypatch.setenv for path redirection, exact-output
behavior assertions.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from consensus_mcp.tools import repo_get_section as tool

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
    "## 2. Goals\n"
    "\n"
    "Goals body.\n"
    "\n"
    "## 3. Non-Goals\n"
    "\n"
    "Non-goals body.\n"
)

SECTION_2_TEXT = "## 2. Goals\n\nGoals body.\n\n"


def _make_repo(tmp_path: Path, monkeypatch) -> Path:
    """Create an isolated project root and point the tool at it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(repo))
    return repo


def _write_spec(repo: Path, name: str = "spec.md", text: str = SPEC_TEXT) -> Path:
    spec = repo / name
    spec.write_text(text, encoding="utf-8")
    return spec


def test_get_section_returns_exact_text_sha_and_resolved_file(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    result = tool.handle(file="spec.md", section_id="section_2")
    assert result == {
        "section_text": SECTION_2_TEXT,
        "section_sha256": hashlib.sha256(SECTION_2_TEXT.encode("utf-8")).hexdigest(),
        "file": str(spec.resolve()),
    }


def test_get_section_zero_leakage_of_other_sections(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    result = tool.handle(file="spec.md", section_id="section_2")
    assert "Overview body." not in result["section_text"]
    assert "Non-goals body." not in result["section_text"]
    assert "version: 1" not in result["section_text"]


def test_get_frontmatter_returns_raw_body_without_markers(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    result = tool.handle(file="spec.md", section_id="frontmatter")
    assert result["section_text"] == "version: 1\nstatus: draft\n"
    assert "---" not in result["section_text"]


def test_get_section_accepts_absolute_in_repo_path(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    result = tool.handle(file=str(spec), section_id="section_1")
    assert result["section_text"] == "## 1. Overview\n\nOverview body.\n\n"


def test_section_not_found_lists_available_ids(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    result = tool.handle(file="spec.md", section_id="section_99")
    assert result["error"] == "section_not_found"
    assert result["detail"] == "section_id 'section_99' not in file"
    assert result["available_section_ids"] == [
        "frontmatter",
        "section_1",
        "section_2",
        "section_3",
    ]


def test_file_with_no_sections_reports_empty_available_ids(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo, text="# Just a title\n\nNo numbered sections.\n")
    result = tool.handle(file="spec.md", section_id="section_1")
    assert result["error"] == "section_not_found"
    assert result["available_section_ids"] == []


def test_missing_file_is_file_not_found_with_resolved_detail(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    result = tool.handle(file="missing.md", section_id="section_1")
    assert result["error"] == "file_not_found"
    assert result["detail"] == str((repo / "missing.md").resolve())


def test_empty_file_argument_is_refused_upfront(tmp_path, monkeypatch):
    _make_repo(tmp_path, monkeypatch)
    result = tool.handle(file="", section_id="section_1")
    assert result == {
        "error": "file_required",
        "detail": "file argument is empty or None",
    }


def test_directory_path_is_refused_as_file_required(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    (repo / "docs").mkdir()
    result = tool.handle(file="docs", section_id="section_1")
    assert result["error"] == "file_required"
    assert "directory" in result["detail"]


def test_non_utf8_file_is_invalid_utf8(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    binary = repo / "binary.md"
    binary.write_bytes(b"## 1. A\n\xff\xfe\x00broken")
    result = tool.handle(file="binary.md", section_id="section_1")
    assert result["error"] == "invalid_utf8"
    assert result["detail"]


# ---------------------------------------------------------------------------
# Duplicate '## N.' headings (M1 S3, consult iteration-m1-hardening-design-
# 4d7d2469): parse() refuses duplicate section ids, so a read of ANY section
# of such a file surfaces the structured error instead of a last-wins text
# that misrepresents the file.
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


def test_duplicate_heading_file_is_refused_with_duplicate_ids(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo, text=DUP_TEXT)
    # Reading the duplicated section itself is refused...
    result = tool.handle(file="spec.md", section_id="section_2")
    assert result["error"] == "duplicate_section_id"
    assert result["duplicate_section_ids"] == ["section_2"]
    assert "section_2" in result["detail"]
    # ...and so is reading an unrelated section of the same file (the parse
    # refusal is file-level; there is no trustworthy SectionMap to serve).
    other = tool.handle(file="spec.md", section_id="section_1")
    assert other["error"] == "duplicate_section_id"
    assert other["duplicate_section_ids"] == ["section_2"]


# ---------------------------------------------------------------------------
# Output-schema failure contract (M1 S4): the M0 audit found 'file_required'
# returned by the handler but missing from the schema enum; S3 adds
# 'duplicate_section_id'. Pin the corrected enum exactly.
# ---------------------------------------------------------------------------


def test_output_schema_failure_enum_is_the_corrected_contract():
    enum = tool.SCHEMA["output_schema"]["oneOf"][1]["properties"]["error"]["enum"]
    assert enum == [
        "file_not_found",
        "file_required",
        "path_outside_repo",
        "invalid_utf8",
        "duplicate_section_id",
        "section_not_found",
    ]
    failure_props = tool.SCHEMA["output_schema"]["oneOf"][1]["properties"]
    assert failure_props["duplicate_section_ids"] == {
        "type": ["array", "null"],
        "items": {"type": "string"},
    }


# ---------------------------------------------------------------------------
# Path-traversal / containment refusals.
# ---------------------------------------------------------------------------


def test_relative_dotdot_escape_is_refused(tmp_path, monkeypatch):
    _make_repo(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("## 1. Secret\nsecret body\n", encoding="utf-8")
    result = tool.handle(file="../outside.md", section_id="section_1")
    assert result["error"] == "path_outside_repo"
    assert result["detail"] == str(outside.resolve())


def test_absolute_outside_path_is_refused(tmp_path, monkeypatch):
    _make_repo(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("## 1. Secret\nsecret body\n", encoding="utf-8")
    result = tool.handle(file=str(outside), section_id="section_1")
    assert result["error"] == "path_outside_repo"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_in_repo_symlink_to_outside_target_is_refused(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("## 1. Secret\nsecret body\n", encoding="utf-8")
    link = repo / "link.md"
    link.symlink_to(outside)
    result = tool.handle(file="link.md", section_id="section_1")
    assert result["error"] == "path_outside_repo"
    assert result["detail"] == str(outside.resolve())


# ---------------------------------------------------------------------------
# Lazy project_root() resolution (iter-0034 migration win).
# ---------------------------------------------------------------------------


def test_lazy_project_root_env_flip_redirects_between_calls(tmp_path, monkeypatch):
    """Changing CONSENSUS_MCP_PROJECT_ROOT between calls redirects resolution
    without re-import (module-level REPO_ROOT capture would have frozen it)."""
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    _write_spec(first, text="## 1. A\nfirst root\n")
    _write_spec(second, text="## 1. A\nsecond root\n")

    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(first))
    r1 = tool.handle(file="spec.md", section_id="section_1")
    assert r1["section_text"] == "## 1. A\nfirst root\n"

    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(second))
    r2 = tool.handle(file="spec.md", section_id="section_1")
    assert r2["section_text"] == "## 1. A\nsecond root\n"
    assert r1["section_sha256"] != r2["section_sha256"]


def test_legacy_repo_root_env_fallback(tmp_path, monkeypatch):
    repo = tmp_path / "legacyroot"
    repo.mkdir()
    _write_spec(repo)
    monkeypatch.delenv("CONSENSUS_MCP_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(repo))
    result = tool.handle(file="spec.md", section_id="section_3")
    assert result["section_text"] == "## 3. Non-Goals\n\nNon-goals body.\n"


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_register_exposes_wire_tool_name_and_handler():
    from consensus_mcp.tool_registry import ToolRegistry

    registry = ToolRegistry()
    tool.register(registry)
    listed = registry.list_tools()
    assert [t["name"] for t in listed] == ["repo.get_section"]
    assert listed[0]["inputSchema"]["required"] == ["file", "section_id"]
    assert registry.get_handler("repo.get_section") is tool.handle
