"""Concurrency reproducers for the seal-index / audit-log lost-update race.

v2.2.1 audit M0.3 (docs/audits/2026-07-01-v2.2.1-repo-audit.md)

Audit finding H1: review.write_and_seal and audit.append_event both use an
unserialized read-modify-write on shared state files. The race windows at
HEAD are:

  consensus_mcp/tools/review_write_and_seal.py
    - Step 6 index READ:            lines 384-388
      (_index.exists() / _index.read_bytes() / yaml.safe_load)
    - in-memory append:             lines 514-516
    - Step 9 index WRITE-BACK:      lines 518-521
      (yaml.safe_dump -> tmp_index.write_text -> os.replace)
    Nothing serializes the read against the replace across concurrent
    callers, so two sealers that both read version N each write N+their-own
    -entry and the last os.replace wins: one index entry is silently lost.
    SECONDARY defect in the same window: line 519 derives ONE shared tmp
    path (index.yaml.tmp) for every caller, so under concurrency the loser
    of the tmp-file race gets an UNCAUGHT FileNotFoundError out of
    os.replace (handle() raises instead of returning {"error": ...}).

  consensus_mcp/tools/audit_append_event.py
    - audit READ:                   lines 326-331
      (audit_path.exists() / read_bytes() / yaml.safe_load)
    - in-memory append:             lines 433-434
    - WRITE-BACK:                   line 437 (audit_path.write_text)
    Same lost-update shape; additionally line 437 is a plain truncating
    write_text - NOT tmp+os.replace atomic, despite the module docstring
    (lines 24-26) claiming the write step itself is os.replace-atomic.

Both modules document this as a known v1.0 single-writer limitation; the
FIX (per-file locking) is scheduled as milestone M1.1. These tests make the
defect a deterministic, executable artifact: a threading.Barrier placed on
a seam BETWEEN the read and the write-back proves both writers held the
same snapshot before either replaced the file, and the race tests are
xfail(strict=True) so they flip loudly when M1.1 lands.

Seams used (test-only, confined to each module's namespace binding):
  - review_write_and_seal: monkeypatch the module's `os` binding; barrier
    inside the Step-8 PACKET os.replace (line 501), which runs after the
    Step-6 index read and before the Step-9 index replace.
  - audit_append_event: monkeypatch the module's `yaml` binding; barrier
    inside the first per-thread yaml.safe_load, i.e. the line-328 read.

Style/provenance mirror: consensus_mcp/tests/test_state_read_decision_ledger.py
(tmp_path + monkeypatch.setenv path redirection, behavior assertions).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
import yaml

from consensus_mcp.tools import audit_append_event as audit_tool
from consensus_mcp.tools import review_write_and_seal as seal_tool

_H1_REASON = (
    "audit H1: lost-update race on shared read-modify-os.replace window "
    "(review_write_and_seal.py read 384-388 vs replace 518-521; "
    "audit_append_event.py read 326-331 vs write_text 437); "
    "fix scheduled M1.1"
)

ITERATION_ID = "iteration-0001"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Redirect all state under tmp_path via the lazy _paths resolvers.

    setenv (never setattr) per the iter-0036/0037 migration notes in the
    modules under test. STATE_ROOT/PROJECT_ROOT are cleared because they
    take precedence over REPO_ROOT and would leak ambient state in.
    """
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("CONSENSUS_MCP_STATE_ROOT", raising=False)
    monkeypatch.delenv("CONSENSUS_MCP_PROJECT_ROOT", raising=False)
    return tmp_path


def _packet(iteration_id: str, reviewer_id: str, pass_id: str) -> dict:
    return {
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
    }


def _index_file(repo: Path) -> Path:
    return repo / "consensus-state" / "archive" / "review-passes" / "index.yaml"


def _seed_index(repo: Path) -> None:
    """Pre-seed the archive index with one already-sealed pass so the race
    is demonstrated against a real non-empty index (the seed entry must
    survive; one of the two new entries is what gets lost)."""
    index_file = _index_file(repo)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    seed = {
        "id": "pass-seed",
        "path": "consensus-state/archive/review-passes/seed-pass.yaml",
        "sealed_at": "2026-07-01T00:00:00Z",
        "packet_sha256": "0" * 64,
        "iteration_id": ITERATION_ID,
        "reviewer_id": "seed",
    }
    index_file.write_text(
        yaml.safe_dump({"passes": [seed]}, sort_keys=False), encoding="utf-8"
    )


def _seed_audit(repo: Path) -> Path:
    """Create the active iteration dir + an audit log with one seed event
    (the file must pre-exist so the line-328 read-path safe_load runs)."""
    iteration_dir = repo / "consensus-state" / "active" / ITERATION_ID
    iteration_dir.mkdir(parents=True, exist_ok=True)
    audit_path = iteration_dir / "independence-audit.yaml"
    seed_event = {
        "event": "review_packet_built",
        "timestamp_utc": "2026-07-01T00:00:00Z",
        "event_id": "2026-07-01T00:00:00Z_review_packet_built_anon",
        "artifact": "seed-artifact.yaml",
        "sha256": "0" * 64,
    }
    audit_path.write_text(
        yaml.safe_dump({"audit_log": [seed_event]}, sort_keys=False),
        encoding="utf-8",
    )
    return audit_path


def _run_in_threads(fns):
    """Run callables in parallel threads; capture results and exceptions.

    Threads are daemonized and joined with a timeout larger than the
    barrier timeout so a broken seam can never hang the suite."""
    results = [None] * len(fns)
    errors = [None] * len(fns)

    def _runner(i, fn):
        try:
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - reproducer must capture all
            errors[i] = exc

    threads = [
        threading.Thread(target=_runner, args=(i, fn), daemon=True)
        for i, fn in enumerate(fns)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "worker thread hung"
    return results, errors


class _BarrierAtPacketReplaceOs:
    """Proxy for review_write_and_seal's module-level `os` binding.

    Holds each sealing thread at the Step-8 PACKET os.replace (line 501) -
    which per program order is AFTER that thread's Step-6 index read
    (lines 384-388) and BEFORE its Step-9 index replace (line 521) - until
    both threads arrive. Both sealers therefore provably hold in-memory
    index snapshots read from the SAME index version before either
    replaces index.yaml. Index replaces (dst name == 'index.yaml') pass
    straight through."""

    def __init__(self, barrier: threading.Barrier):
        self._barrier = barrier

    def __getattr__(self, name):
        return getattr(os, name)

    def replace(self, src, dst):
        if Path(dst).name != "index.yaml":
            try:
                self._barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass  # never hang; the behavior assertions still decide
        return os.replace(src, dst)


class _BarrierAtFirstLoadYaml:
    """Proxy for audit_append_event's module-level `yaml` binding.

    Barriers inside the FIRST per-thread yaml.safe_load - the line-328
    parse of the just-read audit bytes - so both appenders provably parse
    the SAME audit-log version before either writes back (line 437).
    Subsequent per-thread loads (_canonical_sha256, line 210) pass
    through."""

    def __init__(self, barrier: threading.Barrier):
        self._barrier = barrier
        self._local = threading.local()

    def __getattr__(self, name):
        return getattr(yaml, name)

    def safe_load(self, stream):
        if not getattr(self._local, "synced", False):
            self._local.synced = True
            try:
                self._barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass  # never hang; the behavior assertions still decide
        return yaml.safe_load(stream)


# ---------------------------------------------------------------------------
# Sequential controls (green): prove the invariants asserted by the race
# tests DO hold under the documented single-writer discipline, so the xfail
# below can only be attributed to concurrency, not to test setup.
# ---------------------------------------------------------------------------


def test_sequential_seals_both_land_in_index(repo):
    _seed_index(repo)
    r1 = seal_tool.handle(
        ITERATION_ID, "codex", "pass-a", _packet(ITERATION_ID, "codex", "pass-a")
    )
    r2 = seal_tool.handle(
        ITERATION_ID, "gemini", "pass-b", _packet(ITERATION_ID, "gemini", "pass-b")
    )
    assert "error" not in r1, r1
    assert "error" not in r2, r2
    assert r1["index_updated"] is True
    assert r2["index_updated"] is True
    # No active iteration dir was created -> audit step is skipped, by contract.
    assert r1["audit_event_id"] == "skipped_no_iteration_dir"
    assert r2["audit_event_id"] == "skipped_no_iteration_dir"
    assert Path(r1["sealed_path"]).is_file()
    assert Path(r2["sealed_path"]).is_file()

    index_data = yaml.safe_load(_index_file(repo).read_text(encoding="utf-8"))
    ids = [e["id"] for e in index_data["passes"]]
    assert ids == ["pass-seed", "pass-a", "pass-b"]


def test_sequential_pass_id_reuse_with_different_content_refuses(repo):
    """Negative path: the index-based guard the race bypasses. Re-using a
    pass_id with substantively different content must refuse with the
    exact index_collision code."""
    _seed_index(repo)
    r1 = seal_tool.handle(
        ITERATION_ID, "codex", "pass-a", _packet(ITERATION_ID, "codex", "pass-a")
    )
    assert "error" not in r1, r1
    conflicting = _packet(ITERATION_ID, "codex", "pass-a")
    conflicting["findings"] = [{"id": "F1", "severity": "high", "summary": "x"}]
    r2 = seal_tool.handle(ITERATION_ID, "codex", "pass-a", conflicting)
    assert r2.get("error") == "index_collision", r2
    assert "pass-a" in r2["detail"]
    assert "substantively different content" in r2["detail"]


def test_sequential_audit_appends_keep_all_events(repo):
    audit_path = _seed_audit(repo)
    r1 = audit_tool.handle(
        iteration_id=ITERATION_ID,
        event_type="reviewer_invocation_pending",
        actor="alpha",
    )
    r2 = audit_tool.handle(
        iteration_id=ITERATION_ID,
        event_type="reviewer_invocation_pending",
        actor="beta",
    )
    assert "error" not in r1, r1
    assert "error" not in r2, r2
    assert r1["event_id"].endswith("_reviewer_invocation_pending_alpha")
    assert r2["event_id"].endswith("_reviewer_invocation_pending_beta")
    assert len(r1["audit_yaml_post_sha256"]) == 64
    # The second append changed the file, so the canonical hash moved.
    assert r1["audit_yaml_post_sha256"] != r2["audit_yaml_post_sha256"]

    log = yaml.safe_load(audit_path.read_text(encoding="utf-8"))["audit_log"]
    assert [e["event"] for e in log] == [
        "review_packet_built",
        "reviewer_invocation_pending",
        "reviewer_invocation_pending",
    ]
    assert [e.get("actor") for e in log] == [None, "alpha", "beta"]


def test_audit_append_missing_iteration_dir_refuses(repo):
    """Negative path: appending against a non-existent iteration refuses
    with the exact iteration-directory error (nothing is written)."""
    result = audit_tool.handle(
        iteration_id="iteration-9999",
        event_type="reviewer_invocation_pending",
        actor="alpha",
    )
    assert "iteration directory not found" in result["error"]
    assert "iteration-9999" in result["error"]


# ---------------------------------------------------------------------------
# H1 reproducers (xfail strict): deterministic lost-update via barrier seam.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=_H1_REASON)
def test_concurrent_seals_lose_an_index_entry(repo, monkeypatch):
    """Two concurrent write_and_seal calls with DISTINCT pass_ids must both
    land in index.yaml (desired post-M1.1 behavior).

    Deterministic reproduction: the barrier at the Step-8 packet replace
    guarantees both threads completed the Step-6 index read (both saw
    passes == [pass-seed]) before either executed the Step-9 index
    replace. Each thread therefore writes an index containing the seed
    plus ONLY its own entry, and os.replace atomicity means the surviving
    index.yaml is exactly one of those two 2-entry documents - never the
    3-entry union - regardless of scheduling. The 'both ids present'
    assertion below can never pass at HEAD, which is what strict=True
    pins. (In some interleavings the loser of the shared index.yaml.tmp
    path additionally raises FileNotFoundError out of handle(); that is
    the secondary defect noted in the module docstring above.)"""
    _seed_index(repo)
    barrier = threading.Barrier(2)
    monkeypatch.setattr(seal_tool, "os", _BarrierAtPacketReplaceOs(barrier))

    results, errors = _run_in_threads(
        [
            lambda: seal_tool.handle(
                ITERATION_ID, "codex", "pass-a",
                _packet(ITERATION_ID, "codex", "pass-a"),
            ),
            lambda: seal_tool.handle(
                ITERATION_ID, "gemini", "pass-b",
                _packet(ITERATION_ID, "gemini", "pass-b"),
            ),
        ]
    )

    index_data = yaml.safe_load(_index_file(repo).read_text(encoding="utf-8"))
    ids = {e["id"] for e in index_data.get("passes", [])}
    # Desired behavior: nothing lost - the seed survives and BOTH new seals
    # are registered. At HEAD exactly one of pass-a/pass-b is missing.
    assert ids == {"pass-seed", "pass-a", "pass-b"}, (
        f"lost update: surviving index ids = {sorted(ids)}"
    )
    # Desired behavior: both calls return structured success (no uncaught
    # FileNotFoundError from the shared index.yaml.tmp, no {'error': ...}).
    assert errors == [None, None], f"handle() raised: {errors}"
    for r in results:
        assert "error" not in r, r
        assert r["index_updated"] is True


@pytest.mark.xfail(strict=True, reason=_H1_REASON)
def test_concurrent_audit_appends_lose_an_event(repo, monkeypatch):
    """Two concurrent audit.append_event calls for the same iteration must
    both persist (desired post-M1.1 behavior).

    Deterministic reproduction: the barrier inside the first per-thread
    yaml.safe_load (the line-328 parse of the just-read audit bytes)
    guarantees both threads parsed the SAME 1-event audit log before
    either wrote back (line 437). Each write_text therefore persists the
    seed plus ONLY that thread's event; whichever write lands last, the
    surviving audit_log can never contain both alpha and beta, which is
    what strict=True pins. (line 437 is a plain truncating write_text -
    not tmp+os.replace - so an unlucky interleaving may even leave the
    file transiently corrupt; that also fails this test, as it should.)"""
    audit_path = _seed_audit(repo)
    barrier = threading.Barrier(2)
    monkeypatch.setattr(audit_tool, "yaml", _BarrierAtFirstLoadYaml(barrier))

    results, errors = _run_in_threads(
        [
            lambda: audit_tool.handle(
                iteration_id=ITERATION_ID,
                event_type="reviewer_invocation_pending",
                actor="alpha",
            ),
            lambda: audit_tool.handle(
                iteration_id=ITERATION_ID,
                event_type="reviewer_invocation_pending",
                actor="beta",
            ),
        ]
    )

    log = yaml.safe_load(audit_path.read_text(encoding="utf-8"))["audit_log"]
    pending_actors = {
        e.get("actor")
        for e in log
        if isinstance(e, dict) and e.get("event") == "reviewer_invocation_pending"
    }
    # Desired behavior: the seed event survives and BOTH appends persist.
    assert [e.get("event") for e in log][0] == "review_packet_built"
    assert pending_actors == {"alpha", "beta"}, (
        f"lost update: surviving pending actors = {sorted(pending_actors)}"
    )
    # Desired behavior: both calls return structured success.
    assert errors == [None, None], f"handle() raised: {errors}"
    for r in results:
        assert "error" not in r, r
        assert "event_id" in r
