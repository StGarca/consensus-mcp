"""Behavior tests for the repo.set_section MCP tool (tools/repo_set_section.py).

v2.2.1 audit M0.1a (docs/audits/2026-07-01-v2.2.1-repo-audit.md).

Covers: get/set/get roundtrip on a realistic sectioned spec, the consensus
gate (path required, sha match, invalid yaml, per-section scope match in all
three shapes: string entries / dict entries / legacy allowed_files, plus the
spec_md alias), the round-trip safety refusal (unintended_section_change),
path-traversal containment for both the target file and consensus_yaml_path,
atomic-write behavior (no partial target on os.replace failure; no staged
.tmp leftover on success), the optional audit event, and register() wire
exposure. Refusal paths also assert the target file was NOT modified.

Style mirrors consensus_mcp/tests/test_state_read_decision_ledger.py:
tmp_path only, monkeypatch.setenv for path redirection, exact-output
behavior assertions.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
import yaml

from consensus_mcp.tools import repo_get_section as get_tool
from consensus_mcp.tools import repo_set_section as tool

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

SECTION_2_OLD = "## 2. Goals\n\nGoals body.\n\n"
SECTION_2_NEW = "## 2. Goals\n\nRewritten goals body.\n\n"


def _make_repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(repo))
    return repo


def _write_spec(repo: Path, name: str = "spec.md", text: str = SPEC_TEXT) -> Path:
    spec = repo / name
    spec.write_text(text, encoding="utf-8")
    return spec


def _canonical_sha(text: str) -> str:
    """Mirror of the tool's canonical-yaml sha: sorted-keys safe_dump re-hash."""
    return hashlib.sha256(
        yaml.safe_dump(yaml.safe_load(text), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _write_consensus(repo: Path, scope) -> tuple[str, str]:
    """Write consensus.yaml with the given implementation_scope; return
    (repo-relative path, canonical sha)."""
    text = yaml.safe_dump({"implementation_scope": scope}, sort_keys=False)
    (repo / "consensus.yaml").write_text(text, encoding="utf-8")
    return "consensus.yaml", _canonical_sha(text)


# ---------------------------------------------------------------------------
# Happy path: get/set/get roundtrip.
# ---------------------------------------------------------------------------


def test_set_then_get_roundtrip_updates_only_target_section(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/section_2"]}
    )

    before = get_tool.handle(file="spec.md", section_id="section_2")
    assert before["section_text"] == SECTION_2_OLD

    expected_file_text = SPEC_TEXT.replace(SECTION_2_OLD, SECTION_2_NEW)
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result == {
        "written": True,
        "written_sha256": hashlib.sha256(
            expected_file_text.encode("utf-8")
        ).hexdigest(),
        "sections_unchanged_verified": ["frontmatter", "section_1", "section_3"],
        "file": str(spec.resolve()),
        "audit_event_id": None,
    }
    assert spec.read_text(encoding="utf-8") == expected_file_text
    # No staged temp file left behind on success.
    assert not Path(str(spec) + ".tmp").exists()

    after = get_tool.handle(file="spec.md", section_id="section_2")
    assert after["section_text"] == SECTION_2_NEW
    # Other sections read back untouched.
    other = get_tool.handle(file="spec.md", section_id="section_1")
    assert other["section_text"] == "## 1. Overview\n\nOverview body.\n\n"
    fm = get_tool.handle(file="spec.md", section_id="frontmatter")
    assert fm["section_text"] == "version: 1\nstatus: draft\n"


def test_set_frontmatter_replaces_only_frontmatter_body(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/frontmatter"]}
    )
    result = tool.handle(
        file="spec.md",
        section_id="frontmatter",
        new_section_text="version: 2\nstatus: final\n",
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["written"] is True
    text = spec.read_text(encoding="utf-8")
    assert text.startswith("---\nversion: 2\nstatus: final\n---\n")
    assert "# Orchestration Spec" in text
    assert "## 2. Goals" in text


# ---------------------------------------------------------------------------
# Scope matching shapes.
# ---------------------------------------------------------------------------


def test_scope_dict_entry_shape_is_accepted(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    cpath, csha = _write_consensus(
        repo,
        {"allowed_sections": [{"file": "spec.md", "section_id": "section_2"}]},
    )
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["written"] is True


def test_scope_legacy_allowed_files_fallback_permits_whole_file(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    cpath, csha = _write_consensus(repo, {"allowed_files": ["spec.md"]})
    result = tool.handle(
        file="spec.md",
        section_id="section_3",
        new_section_text="## 3. Non-Goals\n\nNew non-goals.\n",
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["written"] is True


def test_scope_spec_md_alias_matches_canonical_spec_path(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec_dir = repo / "docs" / "architecture"
    spec_dir.mkdir(parents=True)
    _write_spec(spec_dir, name="orchestration-spec.md")
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec_md/section_1"]}
    )
    result = tool.handle(
        file="docs/architecture/orchestration-spec.md",
        section_id="section_1",
        new_section_text="## 1. Overview\n\nAlias-authorized rewrite.\n\n",
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["written"] is True


def test_scope_refusal_when_allowed_sections_do_not_match(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/section_1"]}
    )
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["error"] == "section_not_in_implementation_scope"
    assert result["detail"] == (
        "allowed_sections present but did not match spec.md/section_2"
    )
    assert spec.read_text(encoding="utf-8") == SPEC_TEXT  # refusal = no write


def test_scope_refusal_when_no_scope_lists_present(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    cpath, csha = _write_consensus(repo, {})
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["error"] == "section_not_in_implementation_scope"
    assert result["detail"] == (
        "neither allowed_sections nor allowed_files matched spec.md/section_2"
    )
    assert spec.read_text(encoding="utf-8") == SPEC_TEXT


def test_scope_refusal_when_implementation_scope_not_a_mapping(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    cpath, csha = _write_consensus(repo, "not-a-mapping")
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["error"] == "section_not_in_implementation_scope"
    assert result["detail"] == "implementation_scope is not a mapping"
    assert spec.read_text(encoding="utf-8") == SPEC_TEXT


# ---------------------------------------------------------------------------
# Consensus gate refusals.
# ---------------------------------------------------------------------------


def test_whitespace_consensus_path_is_required_error(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="   ",
    )
    assert result["error"] == "consensus_yaml_path_required"
    assert "v1.0 requires consensus_yaml_path" in result["detail"]
    assert spec.read_text(encoding="utf-8") == SPEC_TEXT


def test_consensus_sha_mismatch_reports_actual_canonical_sha(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/section_2"]}
    )
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256="0" * 64,
        consensus_yaml_path=cpath,
    )
    assert result["error"] == "consensus_sha_mismatch"
    assert result["consensus_yaml_sha256_actual"] == csha
    assert spec.read_text(encoding="utf-8") == SPEC_TEXT


def test_consensus_yaml_that_is_a_list_is_invalid(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    (repo / "consensus.yaml").write_text("- a\n- b\n", encoding="utf-8")
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="consensus.yaml",
    )
    assert result["error"] == "invalid_consensus_yaml"
    assert "got list" in result["detail"]


def test_unparseable_consensus_yaml_is_invalid(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    (repo / "consensus.yaml").write_text("key: [unclosed\n", encoding="utf-8")
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="consensus.yaml",
    )
    assert result["error"] == "invalid_consensus_yaml"


def test_missing_consensus_file_is_rekeyed_invalid_consensus_yaml(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="nope.yaml",
    )
    assert result["error"] == "invalid_consensus_yaml"
    assert "file_not_found" in result["detail"]


def test_consensus_path_outside_repo_is_rekeyed_invalid_consensus_yaml(
    tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    outside = tmp_path / "outside-consensus.yaml"
    outside.write_text(
        yaml.safe_dump({"implementation_scope": {"allowed_files": ["spec.md"]}}),
        encoding="utf-8",
    )
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path=str(outside),
    )
    assert result["error"] == "invalid_consensus_yaml"
    assert "path_outside_repo" in result["detail"]


# ---------------------------------------------------------------------------
# Target-file refusals (resolution happens before the consensus gate).
# ---------------------------------------------------------------------------


def test_section_not_found_lists_available_ids(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _write_spec(repo)
    result = tool.handle(
        file="spec.md",
        section_id="section_99",
        new_section_text="anything",
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="consensus.yaml",
    )
    assert result["error"] == "section_not_found"
    assert result["available_section_ids"] == [
        "frontmatter",
        "section_1",
        "section_2",
        "section_3",
    ]


def test_missing_target_file_is_file_not_found(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    result = tool.handle(
        file="missing.md",
        section_id="section_1",
        new_section_text="x",
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="consensus.yaml",
    )
    assert result["error"] == "file_not_found"
    assert result["detail"] == str((repo / "missing.md").resolve())


def test_empty_target_file_argument_is_refused_upfront(tmp_path, monkeypatch):
    _make_repo(tmp_path, monkeypatch)
    result = tool.handle(
        file="",
        section_id="section_1",
        new_section_text="x",
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="consensus.yaml",
    )
    assert result == {
        "error": "file_required",
        "detail": "path argument is empty or None",
    }


def test_non_utf8_target_is_invalid_utf8(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    binary = repo / "binary.md"
    binary.write_bytes(b"## 1. A\n\xff\xfe\x00broken")
    result = tool.handle(
        file="binary.md",
        section_id="section_1",
        new_section_text="x",
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="consensus.yaml",
    )
    assert result["error"] == "invalid_utf8"


# ---------------------------------------------------------------------------
# Path-traversal / containment refusals (repo_set_section.py resolve()+
# relative_to guards): prove escape attempts are rejected and nothing is
# written outside the project root.
# ---------------------------------------------------------------------------


def test_relative_dotdot_escape_is_refused_and_outside_file_untouched(
    tmp_path, monkeypatch
):
    _make_repo(tmp_path, monkeypatch)
    outside = tmp_path / "evil.md"
    outside_text = "## 1. Secret\nsecret body\n"
    outside.write_text(outside_text, encoding="utf-8")
    result = tool.handle(
        file="../evil.md",
        section_id="section_1",
        new_section_text="## 1. Secret\nOVERWRITTEN\n",
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="consensus.yaml",
    )
    assert result["error"] == "path_outside_repo"
    assert result["detail"] == str(outside.resolve())
    assert outside.read_text(encoding="utf-8") == outside_text


def test_absolute_outside_path_is_refused(tmp_path, monkeypatch):
    _make_repo(tmp_path, monkeypatch)
    outside = tmp_path / "evil.md"
    outside.write_text("## 1. Secret\nsecret body\n", encoding="utf-8")
    result = tool.handle(
        file=str(outside),
        section_id="section_1",
        new_section_text="x",
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="consensus.yaml",
    )
    assert result["error"] == "path_outside_repo"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_in_repo_symlink_to_outside_target_is_refused(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    outside = tmp_path / "evil.md"
    outside_text = "## 1. Secret\nsecret body\n"
    outside.write_text(outside_text, encoding="utf-8")
    link = repo / "link.md"
    link.symlink_to(outside)
    result = tool.handle(
        file="link.md",
        section_id="section_1",
        new_section_text="## 1. Secret\nOVERWRITTEN\n",
        consensus_yaml_sha256="x" * 64,
        consensus_yaml_path="consensus.yaml",
    )
    assert result["error"] == "path_outside_repo"
    assert outside.read_text(encoding="utf-8") == outside_text


# ---------------------------------------------------------------------------
# Round-trip safety refusal (unintended_section_change).
# ---------------------------------------------------------------------------


def test_heading_injection_in_new_text_is_refused_and_file_unchanged(
    tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/section_2"]}
    )
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=(
            "## 2. Goals\n\nGoals body.\n\n## 9. Injected\n\nEvil payload.\n"
        ),
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["error"] == "unintended_section_change"
    assert result["unintended_changed_section_ids"] == ["section_9"]
    assert spec.read_text(encoding="utf-8") == SPEC_TEXT


def test_dropping_the_heading_is_refused_as_unintended_change(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/section_2"]}
    )
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text="body without any heading\n",
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["error"] == "unintended_section_change"
    # section_2 vanishes and its body is absorbed into section_1.
    assert result["unintended_changed_section_ids"] == ["section_1", "section_2"]
    assert spec.read_text(encoding="utf-8") == SPEC_TEXT


# ---------------------------------------------------------------------------
# Atomic-write behavior.
# ---------------------------------------------------------------------------


def test_replace_failure_leaves_target_byte_identical(tmp_path, monkeypatch):
    """If the final os.replace fails, the target file must be untouched (the
    new content only ever exists in the staged sibling .tmp file)."""
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/section_2"]}
    )

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(tool.os, "replace", _boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        tool.handle(
            file="spec.md",
            section_id="section_2",
            new_section_text=SECTION_2_NEW,
            consensus_yaml_sha256=csha,
            consensus_yaml_path=cpath,
        )
    # Target intact - no partial write.
    assert spec.read_text(encoding="utf-8") == SPEC_TEXT
    # The full new content was staged in the sibling .tmp file.
    staged = Path(str(spec) + ".tmp")
    assert staged.exists()
    assert staged.read_text(encoding="utf-8") == SPEC_TEXT.replace(
        SECTION_2_OLD, SECTION_2_NEW
    )


# ---------------------------------------------------------------------------
# Optional audit event (iteration_id).
# ---------------------------------------------------------------------------


def test_iteration_id_emits_apply_step_landed_audit_event(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    state_root = tmp_path / "state-root"
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state_root))
    iteration_dir = state_root / "active" / "iter-0001"
    iteration_dir.mkdir(parents=True)

    _write_spec(repo)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/section_2"]}
    )
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
        iteration_id="iter-0001",
    )
    assert result["written"] is True
    assert result["audit_event_id"].endswith("_apply_step_landed_implementer")

    audit_path = iteration_dir / "independence-audit.yaml"
    data = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert len(data["audit_log"]) == 1
    event = data["audit_log"][0]
    assert event["event"] == "apply_step_landed"
    assert event["event_id"] == result["audit_event_id"]
    assert event["actor"] == "implementer"
    assert event["artifact"] == "spec.md"
    assert event["sha256"] == result["written_sha256"]
    assert event["effect"] == "repo.set_section committed spec.md#section_2"
    assert event["files_modified"] == ["spec.md"]
    assert event["section_id"] == "section_2"
    assert event["consensus_yaml_sha256"] == csha
    assert event["sections_unchanged_verified_count"] == 3


def test_missing_iteration_dir_reports_audit_write_failed_after_landing(
    tmp_path, monkeypatch
):
    """Documented reconcile semantics: the spec write lands atomically FIRST;
    an audit failure is then surfaced as audit_write_failed."""
    repo = _make_repo(tmp_path, monkeypatch)
    state_root = tmp_path / "state-root"
    state_root.mkdir()
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state_root))

    spec = _write_spec(repo)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/section_2"]}
    )
    result = tool.handle(
        file="spec.md",
        section_id="section_2",
        new_section_text=SECTION_2_NEW,
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
        iteration_id="iter-missing",
    )
    assert result["error"] == "audit_write_failed"
    assert "iteration directory not found" in result["audit_error"]
    # The write itself DID land (manual-reconcile contract).
    assert spec.read_text(encoding="utf-8") == SPEC_TEXT.replace(
        SECTION_2_OLD, SECTION_2_NEW
    )


# ---------------------------------------------------------------------------
# Module surface: PEP 562 compat + registration.
# ---------------------------------------------------------------------------


def test_pep562_repo_root_attribute_tracks_project_root_env(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    assert tool.REPO_ROOT == repo.resolve()


def test_unknown_module_attribute_raises_attributeerror():
    with pytest.raises(AttributeError):
        tool.no_such_attribute  # noqa: B018


def test_register_exposes_wire_tool_name_and_handler():
    from consensus_mcp.tool_registry import ToolRegistry

    registry = ToolRegistry()
    tool.register(registry)
    listed = registry.list_tools()
    assert [t["name"] for t in listed] == ["repo.set_section"]
    assert listed[0]["inputSchema"]["required"] == [
        "file",
        "section_id",
        "new_section_text",
        "consensus_yaml_sha256",
        "consensus_yaml_path",
    ]
    assert registry.get_handler("repo.set_section") is tool.handle


# ---------------------------------------------------------------------------
# Duplicate-heading corruption (parser roundtrip bug surfaced through T10).
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PRODUCTION BUG (v2.2.1 audit M0.1a): on a file with duplicate "
        "'## N.' heading numbers, _md_sections keys sections by number "
        "(last occurrence wins), so an authorized write to an UNRELATED "
        "section silently deletes the first duplicate block and doubles "
        "the second - while reporting it in sections_unchanged_verified. "
        "The round-trip safety gate cannot see it because pre- and "
        "post-parse maps are identical."
    ),
)
def test_duplicate_heading_file_survives_write_to_other_section(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    spec = _write_spec(repo, text=DUP_TEXT)
    cpath, csha = _write_consensus(
        repo, {"allowed_sections": ["spec.md/section_1"]}
    )
    result = tool.handle(
        file="spec.md",
        section_id="section_1",
        new_section_text="## 1. Alpha\nnew alpha body\n",
        consensus_yaml_sha256=csha,
        consensus_yaml_path=cpath,
    )
    assert result["written"] is True
    text = spec.read_text(encoding="utf-8")
    # The untouched duplicate blocks must both survive the write.
    assert "## 2. First\nfirst body\n" in text
    assert text.count("## 2. Second\nsecond body\n") == 1
