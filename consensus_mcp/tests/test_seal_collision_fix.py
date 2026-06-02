"""iteration-seal-archive-collision-fix: tests for the T6 seal
archive-filename collision fix.

Defect: `review_write_and_seal.handle` built the archive filename from
`reviewer_id` only, so re-using a reviewer_id across passes produced a
hard `packet_path_collision` BEFORE the pass_id-aware index logic ran.
Converged fix (workflow A, weighted-synthesis; codex+gemini majority):
4-token filename including pass_id; index pass_id+sha check reordered
ahead of the path-exists guard; exact re-seal is an idempotent success
with an on-disk integrity guard.
"""
from __future__ import annotations

import yaml
import pytest

from consensus_mcp.tools import review_write_and_seal as t6
from consensus_mcp.tools.review_write_and_seal import SCHEMA, _sanitize_for_filename


# ---------- output_schema contract (codex-sealfix-audit-2 finding) ----------
# Workflow B post-review caught that the new idempotent integrity error
# codes + the `idempotent` success field were returned by handle() but
# missing from the registered output_schema. This test pins the contract
# so the drift cannot silently regress.

def _failure_enum():
    for branch in SCHEMA["output_schema"]["oneOf"]:
        if branch.get("title") == "failure":
            return set(branch["properties"]["error"]["enum"])
    raise AssertionError("no failure branch in output_schema")


def _success_props():
    for branch in SCHEMA["output_schema"]["oneOf"]:
        if branch.get("title") == "success":
            return branch["properties"]
    raise AssertionError("no success branch in output_schema")


def test_schema_failure_enum_includes_idempotent_error_codes():
    enum = _failure_enum()
    for code in (
        "idempotent_target_missing",
        "idempotent_target_unreadable",
        "idempotent_target_integrity_mismatch",
    ):
        assert code in enum, f"{code!r} missing from output_schema failure enum"


def test_schema_success_branch_documents_idempotent_field():
    assert "idempotent" in _success_props(), (
        "output_schema success branch must document the `idempotent` field"
    )


def test_every_handle_error_code_is_in_schema_enum():
    """Every error code the implementation can return must be in the
    schema enum (prevents the exact drift codex flagged)."""
    import inspect
    src = inspect.getsource(t6.handle)
    import re
    returned = set(re.findall(r'"error":\s*"([a-z_]+)"', src))
    enum = _failure_enum()
    missing = returned - enum
    assert not missing, f"handle() returns error codes absent from schema enum: {missing}"


def _packet(iteration_id, reviewer_id, pass_id, findings=None):
    return {
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "findings": findings if findings is not None else [],
        "goal_satisfied": True,
        "blocking_objections": [],
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # _paths re-reads env on every call (lazy resolvers) - see memory
    # feedback_monkeypatch_getattr_pollution: use setenv, not setattr.
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    return tmp_path


# ---------- _sanitize_for_filename ----------

def test_sanitize_passthrough_for_safe_id():
    assert _sanitize_for_filename("codex-iter0044-2-pass1") == "codex-iter0044-2-pass1"


def test_sanitize_replaces_hostile_chars():
    assert _sanitize_for_filename("gemini/iter:0012 pass2") == "gemini-iter-0012-pass2"


def test_sanitize_collapses_runs_and_strips():
    assert _sanitize_for_filename("--a///b--") == "a-b"


def test_sanitize_all_hostile_falls_back():
    assert _sanitize_for_filename("///") == "pass"
    assert _sanitize_for_filename("") == "pass"


# ---------- pass_id body/param consistency (codex-sealfix-audit-3) ----------

def test_packet_pass_id_must_match_parameter(repo):
    """Symmetric with the iteration_id/reviewer_id guards: an embedded
    packet.pass_id that disagrees with the pass_id parameter is rejected
    so the sealed body cannot contradict the filename + index identity."""
    p = _packet("iter-id", "rev", "rev-pass1")
    p["pass_id"] = "rev-pass-DIFFERENT"
    r = t6.handle("iter-id", "rev", "rev-pass1", p)
    assert r.get("error") == "missing_required_field", r
    assert r.get("field") == "pass_id", r


def test_packet_without_pass_id_but_with_pass_label_is_allowed(repo):
    """The packet may legitimately carry only pass_label; the guard must
    NOT fire when packet.pass_id is absent."""
    p = {
        "iteration_id": "iter-id",
        "reviewer_id": "rev",
        "pass_label": "round-2",  # no pass_id key in the body
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
    }
    r = t6.handle("iter-id", "rev", "rev-pass1", p)
    assert "error" not in r, r
    # codex-sealfix-audit-4 medium finding: the sealed body must now
    # self-record the canonical pass_id even for pass-label-only input.
    import yaml as _yaml
    from pathlib import Path
    sealed = _yaml.safe_load(Path(r["sealed_path"]).read_text(encoding="utf-8"))
    assert sealed.get("pass_id") == "rev-pass1", sealed


def test_matching_packet_pass_id_passes(repo):
    p = _packet("iter-id", "rev", "rev-pass1")  # _packet sets pass_id == param
    r = t6.handle("iter-id", "rev", "rev-pass1", p)
    assert "error" not in r, r


# ---------- filename scheme (Q1-b: 4 tokens incl pass_id) ----------

def test_filename_contains_all_four_tokens(repo):
    r = t6.handle("iter-x", "codex-rev", "codex-rev-pass1", _packet("iter-x", "codex-rev", "codex-rev-pass1"))
    assert "error" not in r, r
    name = r["sealed_path"].replace("\\", "/").split("/")[-1]
    assert "iter-x" in name
    assert "codex-rev" in name
    assert "codex-rev-pass1" in name
    assert name.endswith("-pass.yaml")


# ---------- the actual defect: same reviewer_id, distinct pass_ids ----------

def test_same_reviewer_distinct_pass_ids_no_collision(repo):
    """The triggering scenario: same reviewer_id re-used across passes
    must NOT collide (this is exactly gemini-iteration-0012-2 pass1 vs
    pass2 from the report)."""
    rid = "gemini-iteration-0012-2"
    r1 = t6.handle("iter-0012", rid, f"{rid}-pass1", _packet("iter-0012", rid, f"{rid}-pass1", findings=[{"a": 1}]))
    r2 = t6.handle("iter-0012", rid, f"{rid}-pass2", _packet("iter-0012", rid, f"{rid}-pass2", findings=[{"b": 2}]))
    assert "error" not in r1, r1
    assert "error" not in r2, r2
    assert r1["sealed_path"] != r2["sealed_path"]


# ---------- idempotency (Q2-a) ----------

def test_exact_reseal_is_idempotent_success(repo):
    p = _packet("iter-id", "rev", "rev-pass1")
    r1 = t6.handle("iter-id", "rev", "rev-pass1", p)
    assert "error" not in r1, r1
    r2 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1"))
    assert "error" not in r2, r2
    assert r2.get("idempotent") is True
    assert r2["index_updated"] is False
    assert r2["sealed_path"] == r1["sealed_path"]
    assert r2["packet_sha256"] == r1["packet_sha256"]


def test_idempotent_reseal_is_timestamp_independent(repo):
    """codex-sealfix-audit-4 HIGH finding: a re-dispatch builds a fresh
    packet (no sealed_at_utc) -> Step 3 stamps a NEW timestamp -> its
    packet_sha256 differs from the original. Idempotency must STILL be
    detected (content identity), not misclassified as index_collision.
    Simulate the timestamp/hash divergence by rewriting the on-disk
    archive's volatile seal-provenance to clearly different values; the
    content-identity comparison must ignore them."""
    import yaml as _yaml
    from pathlib import Path
    r1 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1"))
    assert "error" not in r1, r1
    archived = _yaml.safe_load(Path(r1["sealed_path"]).read_text(encoding="utf-8"))
    archived["sealed_at_utc"] = "1999-01-01T00:00:00Z"        # clearly different seal time
    archived["packet_sha256"] = "deadbeef" * 8                # clearly different self-hash
    Path(r1["sealed_path"]).write_text(_yaml.safe_dump(archived), encoding="utf-8")
    r2 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1"))
    assert "error" not in r2, r2
    assert r2.get("idempotent") is True, r2
    assert r2["index_updated"] is False


def test_idempotent_integrity_guard_detects_non_mapping_archive(repo):
    """codex-sealfix-audit-4 medium finding: a tampered archive that
    still parses as valid non-mapping YAML (a list/scalar) must NOT
    crash the integrity path and must report a distinct integrity
    error rather than a false success."""
    from pathlib import Path
    r1 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1"))
    assert "error" not in r1, r1
    Path(r1["sealed_path"]).write_text("- just\n- a\n- list\n", encoding="utf-8")
    r2 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1"))
    assert r2.get("error") == "idempotent_target_integrity_mismatch", r2


def test_idempotent_returns_authoritative_sha_when_index_entry_sha_blank(repo):
    """codex-sealfix-audit-5 medium finding: even if the index entry's
    packet_sha256 is missing/stale, the idempotent path must return a
    correct, non-empty packet_sha256 derived from the actual on-disk
    artifact (consistent with sealed_path)."""
    import yaml as _yaml
    from pathlib import Path
    r1 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1"))
    assert "error" not in r1, r1
    # Corrupt ONLY the index entry's recorded sha (blank it), leaving
    # the on-disk archive intact. `repo` is the tmp repo root.
    matches = list(repo.rglob("index.yaml"))
    assert matches, "index.yaml not written under repo root"
    idx_path = matches[0]
    idx = _yaml.safe_load(idx_path.read_text(encoding="utf-8"))
    for e in idx.get("passes", []):
        if e.get("id") == "rev-pass1":
            e["packet_sha256"] = ""
    idx_path.write_text(_yaml.safe_dump(idx), encoding="utf-8")
    r2 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1"))
    assert "error" not in r2, r2
    assert r2.get("idempotent") is True, r2
    assert r2["packet_sha256"], "idempotent path returned an empty packet_sha256"
    # It must equal the on-disk artifact's own recorded self-hash.
    on_disk = _yaml.safe_load(Path(r2["sealed_path"]).read_text(encoding="utf-8"))
    assert r2["packet_sha256"] == on_disk["packet_sha256"], (r2, on_disk.get("packet_sha256"))


def test_idempotent_target_missing_when_archive_deleted(repo):
    from pathlib import Path
    r1 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1"))
    assert "error" not in r1, r1
    Path(r1["sealed_path"]).unlink()
    r2 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1"))
    assert r2.get("error") == "idempotent_target_missing", r2


# ---------- index_collision unchanged (Q2: same pass_id, diff content) ----------

def test_same_pass_id_different_content_is_index_collision(repo):
    t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1", findings=[{"x": 1}]))
    r2 = t6.handle("iter-id", "rev", "rev-pass1", _packet("iter-id", "rev", "rev-pass1", findings=[{"y": 2}]))
    assert r2.get("error") == "index_collision", r2


# ---------- hostile pass_id sanitization ----------

def test_hostile_pass_id_sanitized_in_filename_raw_in_index(repo, tmp_path):
    hostile = "rev/iter:99 pass1"
    r = t6.handle("iter-h", "rev", hostile, _packet("iter-h", "rev", hostile))
    assert "error" not in r, r
    name = r["sealed_path"].replace("\\", "/").split("/")[-1]
    assert "/" not in name and ":" not in name and " " not in name
    # Raw pass_id preserved verbatim in the index entry.
    index_yaml = (tmp_path / "consensus-state" / "archive" / "review-passes" / "index.yaml")
    if not index_yaml.exists():
        # _paths may locate the index elsewhere under repo root; find it.
        matches = list(tmp_path.rglob("index.yaml"))
        assert matches, "index.yaml not written"
        index_yaml = matches[0]
    idx = yaml.safe_load(index_yaml.read_text(encoding="utf-8"))
    ids = [e.get("id") for e in idx.get("passes", [])]
    assert hostile in ids, f"raw pass_id not preserved in index: {ids}"
