"""Tests for state.update_decision_ledger MCP tool (the canonical ledger WRITER).

v2.2.1 audit M0.1c (docs/audits/2026-07-01-v2.2.1-repo-audit.md)

First coverage for consensus_mcp/tools/state_update_decision_ledger.py.
Mirrors test_state_read_decision_ledger.py: hermetic tmp_path fixtures,
`monkeypatch.setenv` path redirection through `_paths` resolvers, behavior
assertions on exact result shapes and on-disk bytes.

The validate-then-write gate runs validate_disposition_index against a
minimal spec authored per test. The validator module still carries an
import-time REPO_ROOT constant (it predates the iter-0035 lazy `_paths`
migration), so the fixture monkeypatches that constant into tmp_path too;
this keeps every git subprocess and path resolution inside the sandbox.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from consensus_mcp.tools import state_read_decision_ledger as reader
from consensus_mcp.tools import state_update_decision_ledger as tool
from consensus_mcp.validators import validate_disposition_index as vdi_mod


# A minimal spec that validate_disposition_index scores at ZERO findings:
# frontmatter present, review_archive_index explicitly null (skips validator
# 6 per the validator's test-fixture contract), empty disposition lists with
# matching status_counts, no section 23 scripts, no known_blockers.
CLEAN_SPEC = """---
status: test-spec
review_archive_index: null
disposition_ledger: "consensus-state/state/disposition-ledger.yaml"
known_blockers: {}
---

## 24. Disposition index

```yaml
status_counts:
  resolved: 0
  archived: 0
  deferred: 0
resolved: []
archived: []
deferred: []
```
"""

# Same spec but with one archived entry whose archived_at file does not
# exist on disk -> exactly one blocking ARCHIVED_FILE_MISSING finding.
DIRTY_SPEC = """---
status: test-spec
review_archive_index: null
disposition_ledger: "consensus-state/state/disposition-ledger.yaml"
known_blockers: {}
---

## 24. Disposition index

```yaml
status_counts:
  resolved: 0
  archived: 1
  deferred: 0
resolved: []
archived:
  - id: pass-9999
    archived_at: consensus-state/archive/review-passes/pass-9999.md
deferred: []
```
"""

# Deliberately NOT in canonical key order (schema_version before entries)
# so the write-then-read test can prove raw bytes vs canonical re-dump.
PROPOSED_V1 = (
    "schema_version: 1\n"
    "entries:\n"
    "- id: iter-0001\n"
    "  decision: adopt-cache\n"
    "  status: accepted\n"
)
PROPOSED_V2 = (
    "schema_version: 1\n"
    "entries:\n"
    "- id: iter-0001\n"
    "  decision: adopt-cache\n"
    "  status: superseded\n"
    "- id: iter-0002\n"
    "  decision: adopt-writer\n"
    "  status: accepted\n"
)
CONSENSUS_SHA = "a" * 64


def _canonical_sha(yaml_text: str) -> str:
    """Spec section 7 canonical_yaml_sha256 formula, computed independently."""
    return hashlib.sha256(
        yaml.safe_dump(yaml.safe_load(yaml_text), sort_keys=True).encode("utf-8")
    ).hexdigest()


@pytest.fixture(autouse=True)
def _reset_reader_cache():
    """Keep the reader's module-level cache from leaking across tests."""
    reader._CACHE.update({"sha256": None, "yaml_text": None, "mtime_ns": None})
    yield
    reader._CACHE.update({"sha256": None, "yaml_text": None, "mtime_ns": None})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Hermetic project sandbox: spec + env redirection + validator REPO_ROOT.

    Does NOT pre-create consensus-state/state: the writer is expected to
    create it (asserted explicitly in the first success test).
    """
    proj = (tmp_path / "proj").resolve()
    proj.mkdir()
    spec = proj / "orchestration-spec.md"
    spec.write_text(CLEAN_SPEC, encoding="utf-8")
    state = proj / "consensus-state"

    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(proj))
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(proj))
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(state))
    monkeypatch.setenv("CONSENSUS_MCP_SPEC_PATH", str(spec))
    # The validator resolves archived_at paths and runs git against its
    # import-time REPO_ROOT constant; point it into the sandbox so nothing
    # touches the real repo.
    monkeypatch.setattr(vdi_mod, "REPO_ROOT", proj)

    return SimpleNamespace(
        proj=proj,
        spec=spec,
        state=state,
        ledger=state / "state" / "disposition-ledger.yaml",
    )


def _assert_no_staging_litter(env) -> None:
    """Neither the staged ledger/spec siblings nor the .tmp file survive."""
    state_dir = env.state / "state"
    if state_dir.is_dir():
        assert list(state_dir.glob(".staged-ledger-*")) == []
        assert not (state_dir / "disposition-ledger.yaml.tmp").exists()
    assert list(env.spec.parent.glob(".staged-spec-*")) == []


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


def test_create_new_ledger_returns_exact_success_shape(env):
    """End-to-end create: exact result dict, exact on-disk bytes, dir created."""
    assert not (env.state / "state").exists()  # writer must create it

    result = tool.handle(PROPOSED_V1, CONSENSUS_SHA)

    assert result == {
        "written": True,
        "validate_disposition_index_findings_pre": 0,
        "validate_disposition_index_findings_post": 0,
        "ledger_path": "consensus-state/state/disposition-ledger.yaml",
        "ledger_canonical_sha256_post_write": _canonical_sha(PROPOSED_V1),
        "audit_event_id": None,
    }
    # M1-remediation (consult iteration-path-to-a-remediation-260caad1) W2:
    # the ledger_path result contract is posix-normalized (forward slashes) even
    # on Windows, where relative_to() would otherwise emit backslashes.
    assert "\\" not in result["ledger_path"]
    # The proposed text lands verbatim -- no re-serialization on disk.
    assert env.ledger.read_text(encoding="utf-8") == PROPOSED_V1
    _assert_no_staging_litter(env)


def test_update_existing_entry_replaces_file_bytes(env):
    r1 = tool.handle(PROPOSED_V1, CONSENSUS_SHA)
    assert r1["written"] is True

    r2 = tool.handle(PROPOSED_V2, CONSENSUS_SHA)
    assert r2["written"] is True
    assert env.ledger.read_text(encoding="utf-8") == PROPOSED_V2
    assert r2["ledger_canonical_sha256_post_write"] == _canonical_sha(PROPOSED_V2)
    assert (
        r2["ledger_canonical_sha256_post_write"]
        != r1["ledger_canonical_sha256_post_write"]
    )
    _assert_no_staging_litter(env)


def test_success_with_iteration_emits_apply_step_landed_audit_event(env):
    iteration_dir = env.state / "active" / "iter-0007"
    iteration_dir.mkdir(parents=True)

    result = tool.handle(PROPOSED_V1, CONSENSUS_SHA, iteration_id="iter-0007")

    assert result["written"] is True
    assert result["audit_event_id"] is not None
    assert result["audit_event_id"].endswith("_apply_step_landed_orchestrator")

    audit = yaml.safe_load(
        (iteration_dir / "independence-audit.yaml").read_text(encoding="utf-8")
    )
    assert len(audit["audit_log"]) == 1
    event = audit["audit_log"][0]
    assert event["event"] == "apply_step_landed"
    assert event["event_id"] == result["audit_event_id"]
    assert event["actor"] == "orchestrator"
    assert event["artifact"] == "consensus-state/state/disposition-ledger.yaml"
    assert event["sha256"] == _canonical_sha(PROPOSED_V1)
    assert event["effect"] == "state.update_decision_ledger committed"
    assert event["files_modified"] == [
        "consensus-state/state/disposition-ledger.yaml"
    ]
    # extra_fields are flattened into the record by audit.append_event.
    assert event["consensus_yaml_sha256"] == CONSENSUS_SHA
    assert event["validate_disposition_index_findings_pre"] == 0
    assert event["validate_disposition_index_findings_post"] == 0


def test_write_then_read_back_via_reader_tool(env):
    """Writer and reader agree: same canonical sha, canonical re-dump text."""
    written = tool.handle(PROPOSED_V1, CONSENSUS_SHA)
    assert written["written"] is True

    read = reader.handle()
    assert "error" not in read
    assert read["ledger_sha256"] == written["ledger_canonical_sha256_post_write"]
    # The reader returns the canonical (sorted-keys) re-dump, not raw bytes.
    expected_canonical = yaml.safe_dump(yaml.safe_load(PROPOSED_V1), sort_keys=True)
    assert read["ledger_yaml"] == expected_canonical
    assert read["ledger_yaml"] != PROPOSED_V1  # PROPOSED_V1 is non-canonical order
    assert yaml.safe_load(read["ledger_yaml"]) == yaml.safe_load(PROPOSED_V1)


# ---------------------------------------------------------------------------
# Refusal paths -- input validation (step 0)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_sha", ["", "   "])
def test_refuses_missing_consensus_sha(env, bad_sha):
    result = tool.handle(PROPOSED_V1, bad_sha)
    assert result == {
        "error": "no_consensus_sha_provided",
        "detail": "consensus_yaml_sha256 is required and must be non-empty.",
    }
    assert not env.ledger.exists()


def test_sha_refusal_precedes_validator(env):
    """Step-0 refusal returns before the validator ever reads the spec."""
    env.spec.unlink()
    result = tool.handle(PROPOSED_V1, "")
    assert result["error"] == "no_consensus_sha_provided"
    assert not env.ledger.exists()


def test_refuses_unparseable_yaml(env):
    result = tool.handle("entries: [unclosed", CONSENSUS_SHA)
    assert result["error"] == "invalid_yaml"
    assert result["detail"].startswith("proposed_ledger_yaml failed to parse:")
    assert not env.ledger.exists()


@pytest.mark.parametrize(
    ("payload", "type_name"),
    [
        ("- a\n- b\n", "list"),
        ("just-a-scalar\n", "str"),
        ("", "NoneType"),
    ],
)
def test_refuses_non_mapping_yaml(env, payload, type_name):
    result = tool.handle(payload, CONSENSUS_SHA)
    assert result == {
        "error": "invalid_yaml",
        "detail": (
            f"proposed_ledger_yaml must parse to a YAML mapping; got {type_name}"
        ),
    }
    assert not env.ledger.exists()


def test_invalid_yaml_leaves_prior_ledger_byte_identical(env):
    assert tool.handle(PROPOSED_V1, CONSENSUS_SHA)["written"] is True
    before = env.ledger.read_bytes()

    result = tool.handle("entries: [unclosed", CONSENSUS_SHA)
    assert result["error"] == "invalid_yaml"
    assert env.ledger.read_bytes() == before


def test_missing_required_kwargs_raise_typeerror(env):
    """Documented module contract: missing required args are TypeError,
    not a structured {"error": ...} return."""
    with pytest.raises(TypeError):
        tool.handle(proposed_ledger_yaml=PROPOSED_V1)
    with pytest.raises(TypeError):
        tool.handle()


# ---------------------------------------------------------------------------
# Refusal paths -- validate-then-write gate (step 3)
# ---------------------------------------------------------------------------


def test_refuses_on_nonzero_post_findings_and_keeps_prior_bytes(env):
    # Establish a prior ledger under the clean spec.
    assert tool.handle(PROPOSED_V1, CONSENSUS_SHA)["written"] is True
    before = env.ledger.read_bytes()

    # Break the spec: one archived entry whose file is missing on disk.
    env.spec.write_text(DIRTY_SPEC, encoding="utf-8")
    iteration_dir = env.state / "active" / "iter-0008"
    iteration_dir.mkdir(parents=True)

    result = tool.handle(PROPOSED_V2, CONSENSUS_SHA, iteration_id="iter-0008")

    assert result["error"] == "validate_post_findings_nonzero"
    assert result["validate_disposition_index_findings_post"] == 1
    assert result["findings"] == [
        {
            "id": "ARCHIVED_FILE_MISSING",
            "severity": "blocking",
            "entry_id": "pass-9999",
            "path": "consensus-state/archive/review-passes/pass-9999.md",
            "claim": "archived_at file does not exist on disk",
        }
    ]
    assert result["detail"] == (
        "validate_disposition_index reports 1 finding(s) against "
        "the hypothetical post-write state; ledger was NOT written."
    )
    # Atomicity: the refusal leaves the prior file byte-identical.
    assert env.ledger.read_bytes() == before
    # No audit event lands on refusal, even with iteration_id supplied.
    assert not (iteration_dir / "independence-audit.yaml").exists()
    _assert_no_staging_litter(env)


# ---------------------------------------------------------------------------
# Failure path -- audit_write_failed (ledger written, NOT rolled back)
# ---------------------------------------------------------------------------


def test_audit_write_failed_when_iteration_dir_missing(env):
    result = tool.handle(PROPOSED_V1, CONSENSUS_SHA, iteration_id="iter-nope")

    assert result["error"] == "audit_write_failed"
    assert result["ledger_canonical_sha256_post_write"] == _canonical_sha(PROPOSED_V1)
    assert result["audit_error"].startswith("iteration directory not found:")
    assert "Ledger was atomically written" in result["detail"]
    # Documented semantics: the ledger update stays on disk (no rollback).
    assert env.ledger.read_text(encoding="utf-8") == PROPOSED_V1


# ---------------------------------------------------------------------------
# M1 hardened refusal paths (S1/S2)
# ---------------------------------------------------------------------------


def test_missing_spec_returns_structured_refusal(env):
    """M1 S1 (consult iteration-m1-hardening-design-4d7d2469): the validator's
    SystemExit('spec not found: ...') is converted to the tool's structured
    refusal shape at source. Old contract (M0): the SystemExit escaped
    handle() -- and through the live MCP server it escaped tools/call too,
    whose except clause only caught Exception, killing the whole server."""
    env.spec.unlink()

    result = tool.handle(PROPOSED_V1, CONSENSUS_SHA)

    assert result["error"] == "spec_validation_failed"
    assert result["detail"].startswith("validate_disposition_index could not run:")
    assert "spec not found" in result["detail"]
    assert not env.ledger.exists()


def test_missing_spec_refusal_leaves_prior_ledger_byte_identical(env):
    """The S1 refusal fires before staging/write: prior bytes survive."""
    assert tool.handle(PROPOSED_V1, CONSENSUS_SHA)["written"] is True
    before = env.ledger.read_bytes()

    env.spec.unlink()
    result = tool.handle(PROPOSED_V2, CONSENSUS_SHA)

    assert result["error"] == "spec_validation_failed"
    assert env.ledger.read_bytes() == before
    _assert_no_staging_litter(env)


def test_state_root_outside_project_root_returns_structured_result(
    env, tmp_path, monkeypatch
):
    """M1 S2 (consult iteration-m1-hardening-design-4d7d2469): a state_root
    outside project_root is a structured refusal -- the staged-ledger
    validation redirect requires a project-root-relative path, so the write
    is refused. Old contract (M0 xfail): an uncaught ValueError escaped from
    sibling_ledger.relative_to(project_root())."""
    outside = (tmp_path / "outside-state").resolve()
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(outside))

    result = tool.handle(PROPOSED_V1, CONSENSUS_SHA)

    assert result["error"] == "state_root_outside_project_root"
    assert str(outside) in result["detail"]
    assert "ledger was NOT written" in result["detail"]
    # Fail-closed with zero side effects: the refusal fires before the old
    # code path's mkdir, so nothing is created at the outside root and no
    # ledger is written anywhere.
    assert not outside.exists()
    assert not env.ledger.exists()
    _assert_no_staging_litter(env)


# ---------------------------------------------------------------------------
# Registration + legacy module aliases
# ---------------------------------------------------------------------------


def test_register_wires_name_and_handler():
    from consensus_mcp.tool_registry import ToolRegistry

    registry = ToolRegistry()
    tool.register(registry)
    names = {t["name"] for t in registry.list_tools()}
    assert "state.update_decision_ledger" in names
    assert registry.get_handler("state.update_decision_ledger") is tool.handle


def test_legacy_module_aliases_resolve_lazily(env):
    """PEP 562 __getattr__ aliases track env redirection at access time."""
    assert tool.LEDGER_PATH == env.ledger
    assert tool.SPEC_PATH == env.spec
    assert tool.REPO_ROOT == env.proj
    with pytest.raises(AttributeError, match="NOT_A_REAL_ATTR"):
        getattr(tool, "NOT_A_REAL_ATTR")
