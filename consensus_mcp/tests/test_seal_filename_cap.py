"""M1 S5 regression tests: the seal archive-filename length cap.

Consult iteration-m1-hardening-design-4d7d2469, synthesis
S5_seal_filename_length: the T6 archive filename
<date>-<iteration>-<reviewer>-<pass>-pass.yaml embeds the iteration name up
to three times (reviewer_id and pass_id conventionally contain it), so any
iteration slug over ~65 chars made EVERY seal fail on Linux (NAME_MAX 255)
with '[Errno 36] File name too long' AFTER the reviewer had done its full
work - three real dispatches (codex, gemini, kimi) died exactly this way on
2026-06-30 (dispatch_failed events in consensus-state/state/dispatch-log.jsonl).

Fixed in review_write_and_seal._bounded_seal_filename: names at or under the
cap keep the exact legacy 4-token form; longer names switch to bounded
truncated components + a 12-hex sha256 suffix over the full identity tuple.
Full identifiers stay INSIDE the sealed YAML and the index entry. A
filesystem that still refuses the write yields a structured
{'error': 'packet_write_failed'} - never a raw OSError out of handle().
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from consensus_mcp.tools import review_write_and_seal as seal_tool

# The real-world shape that killed the 2026-06-30 dispatches: a long
# iteration slug, echoed inside pass_id (as every dispatcher does).
LONG_ITER = "iteration-m1-" + "hardening-" * 11 + "design-4d7d2469"  # > 100 chars


def _packet(iteration_id: str, reviewer_id: str, pass_id: str) -> dict:
    return {
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("CONSENSUS_MCP_STATE_ROOT", raising=False)
    monkeypatch.delenv("CONSENSUS_MCP_PROJECT_ROOT", raising=False)
    return tmp_path


def _index_file(repo: Path) -> Path:
    return repo / "consensus-state" / "archive" / "review-passes" / "index.yaml"


def test_long_iteration_name_seals_on_the_real_filesystem(repo):
    """The exact 2026-06-30 defect: a 100+ char iteration name, echoed in
    pass_id, must seal successfully on the REAL filesystem (no mocks - the
    pre-fix natural name exceeds NAME_MAX 255 and this write raised
    OSError 36)."""
    assert len(LONG_ITER) > 100
    pass_id = f"codex-{LONG_ITER}-converge-1-pass1"
    r = seal_tool.handle(LONG_ITER, "codex", pass_id, _packet(LONG_ITER, "codex", pass_id))
    assert "error" not in r, r

    sealed = Path(r["sealed_path"])
    assert sealed.is_file()
    assert len(sealed.name) <= seal_tool._MAX_SEAL_FILENAME_CHARS
    assert sealed.name.endswith("-pass.yaml")

    # Full identifiers live INSIDE the sealed YAML...
    body = yaml.safe_load(sealed.read_text(encoding="utf-8"))
    assert body["iteration_id"] == LONG_ITER
    assert body["pass_id"] == pass_id
    # ...and in the index entry (raw, untruncated).
    idx = yaml.safe_load(_index_file(repo).read_text(encoding="utf-8"))
    entry = idx["passes"][-1]
    assert entry["id"] == pass_id
    assert entry["iteration_id"] == LONG_ITER
    assert entry["reviewer_id"] == "codex"


def test_short_names_keep_the_legacy_4_token_filename(repo):
    """Backward compat: at-or-under-cap names keep the exact 4-token form the
    adapter confinement checks and test_contributors.py's contract pin."""
    r = seal_tool.handle(
        "iteration-0001", "codex", "pass-a", _packet("iteration-0001", "codex", "pass-a")
    )
    assert "error" not in r, r
    name = Path(r["sealed_path"]).name
    date_str = name[:10]
    assert name == f"{date_str}-iteration-0001-codex-pass-a-pass.yaml"


def test_distinct_long_pass_ids_truncating_alike_stay_unique(repo):
    """Two pass_ids identical in every truncated component must still map to
    DISTINCT filenames (the 12-hex sha256 suffix hashes the full identity)."""
    pass_a = f"codex-{LONG_ITER}-converge-1-pass1"
    pass_b = f"codex-{LONG_ITER}-converge-1-pass2"
    r1 = seal_tool.handle(LONG_ITER, "codex", pass_a, _packet(LONG_ITER, "codex", pass_a))
    r2 = seal_tool.handle(LONG_ITER, "codex", pass_b, _packet(LONG_ITER, "codex", pass_b))
    assert "error" not in r1, r1
    assert "error" not in r2, r2
    assert Path(r1["sealed_path"]).name != Path(r2["sealed_path"]).name
    idx = yaml.safe_load(_index_file(repo).read_text(encoding="utf-8"))
    assert {e["id"] for e in idx["passes"]} == {pass_a, pass_b}


def test_long_name_reseal_is_idempotent(repo):
    """The bounded filename is a pure function of the identity tuple, so the
    idempotent re-seal path keeps working for long names."""
    pass_id = f"gemini-{LONG_ITER}-converge-1-pass1"
    packet = _packet(LONG_ITER, "gemini", pass_id)
    r1 = seal_tool.handle(LONG_ITER, "gemini", pass_id, packet)
    assert "error" not in r1, r1
    r2 = seal_tool.handle(LONG_ITER, "gemini", pass_id, dict(packet))
    assert "error" not in r2, r2
    assert r2.get("idempotent") is True
    assert r2["index_updated"] is False
    assert r2["sealed_path"] == r1["sealed_path"]


def test_filesystem_refusal_is_a_structured_error_never_raw_oserror(repo, monkeypatch):
    """Even with the cap, a filesystem can still refuse the packet write
    (exotic mounts, total-path limits): handle() must return
    {'error': 'packet_write_failed'} and release the lock - never leak the
    raw OSError.

    M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q2: the
    packet write now routes through `_atomic_io.atomic_write_text` (not a
    hand-rolled os.replace), so the failure is injected at that seam - the
    sealed-packet write is refused, the index write is let through so the lock
    release is observable."""
    real_atomic_write_text = seal_tool.atomic_write_text

    def _refuse_packet_write(path, text, encoding="utf-8"):
        # The packet write targets a *-pass.yaml file; the index write targets
        # index.yaml. Refuse only the packet write, let the index write pass.
        if Path(path).name != "index.yaml":
            raise OSError(36, "File name too long (simulated)")
        return real_atomic_write_text(path, text, encoding)

    monkeypatch.setattr(seal_tool, "atomic_write_text", _refuse_packet_write)
    r = seal_tool.handle(
        "iteration-0002", "codex", "pass-x", _packet("iteration-0002", "codex", "pass-x")
    )
    assert r.get("error") == "packet_write_failed", r
    assert "File name too long" in r["detail"]
    # Nothing registered for the failed seal.
    idx_file = _index_file(repo)
    if idx_file.exists():
        idx = yaml.safe_load(idx_file.read_text(encoding="utf-8")) or {}
        assert all(e["id"] != "pass-x" for e in idx.get("passes", []))

    # The index lock was RELEASED on the structured-refusal path: a normal
    # seal (real writer restored) succeeds immediately.
    monkeypatch.setattr(seal_tool, "atomic_write_text", real_atomic_write_text)
    r2 = seal_tool.handle(
        "iteration-0002", "codex", "pass-y", _packet("iteration-0002", "codex", "pass-y")
    )
    assert "error" not in r2, r2
