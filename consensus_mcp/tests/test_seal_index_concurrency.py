"""Concurrency tests for the seal-index / audit-log lost-update class.

v2.2.1 audit M0.3 (docs/audits/2026-07-01-v2.2.1-repo-audit.md) originally
pinned audit finding H1 here as two strict-xfail reproducers: both
review.write_and_seal and audit.append_event used an unserialized
read-modify-write on shared state files, so two concurrent writers holding
the same snapshot lost one entry/event (plus the shared index.yaml.tmp path
raised an uncaught FileNotFoundError for the tmp-race loser, and the audit
write-back was a plain truncating write_text).

M1 (consult iteration-m1-hardening-design-4d7d2469) Q1 FIXED the class: both
tools now hold `_atomic_io.locked_mutation(<state file>)` across the whole
read -> guard -> write -> os.replace window, the index tmp name is unique per
writer (index.yaml.tmp.<pid>.<rand8>), and the audit write-back goes through
the blessed unique-tmp atomic writer. The former xfail reproducers are
flipped below to PASSING assertions of the fixed behavior, plus:

  - hammer tests (2 threads x 50 concurrent seals AND 2 subprocesses x 25)
    asserting ZERO lost index entries and zero uncaught exceptions - the
    permanent, root-cause-independent lost-state detectors from the
    converged plan's independent_safeguard;
  - structured `state_lock_timeout` refusal tests for both tools (the
    refusal carries the holder's owner.json fields per gemini-rev-001);
  - stale-takeover caller tests: the takeover event lands in
    dispatch-log.jsonl ONLY - never in the file being locked - and the
    emission path takes NO additional locked_mutation (the no-recursive-
    append regression pinning the gemini-rev-002 + kimi-rev-001 sink
    invariant, regardless of which file is locked).

Style/provenance mirror: consensus_mcp/tests/test_state_read_decision_ledger.py
(tmp_path + monkeypatch.setenv path redirection, behavior assertions).
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from consensus_mcp._atomic_io import LockStatus, locked_mutation
from consensus_mcp.tools import audit_append_event as audit_tool
from consensus_mcp.tools import review_write_and_seal as seal_tool

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


def _dispatch_log(repo: Path) -> Path:
    return repo / "consensus-state" / "state" / "dispatch-log.jsonl"


def _seed_index(repo: Path) -> None:
    """Pre-seed the archive index with one already-sealed pass so losing an
    entry is demonstrable against a real non-empty index (the seed entry
    must survive alongside every new entry)."""
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
    """Create the active iteration dir + an audit log with one seed event."""
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


def _make_stale_lock(target: Path, pid: int = 4242, host: str = "ghost") -> Path:
    """Fabricate an aged (crashed-holder) lock dir on `target`."""
    lock_dir = target.with_name(target.name + ".lock")
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": pid, "host": host, "claimed_at_epoch": time.time() - 100000}),
        encoding="utf-8",
    )
    return lock_dir


def _make_fresh_lock(target: Path, pid: int = 1234, host: str = "holder-host") -> Path:
    """Fabricate a LIVE (non-stale) lock dir on `target`."""
    lock_dir = target.with_name(target.name + ".lock")
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": pid, "host": host, "claimed_at_epoch": time.time()}),
        encoding="utf-8",
    )
    return lock_dir


def _run_in_threads(fns, join_timeout: float = 10.0):
    """Run callables in parallel threads; capture results and exceptions.

    Threads are daemonized and joined with a bounded timeout so a broken
    lock can never hang the suite."""
    results = [None] * len(fns)
    errors = [None] * len(fns)

    def _runner(i, fn):
        try:
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - must capture everything
            errors[i] = exc

    threads = [
        threading.Thread(target=_runner, args=(i, fn), daemon=True)
        for i, fn in enumerate(fns)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=join_timeout)
    assert not any(t.is_alive() for t in threads), "worker thread hung"
    return results, errors


# ---------------------------------------------------------------------------
# Sequential controls (green pre- and post-fix): prove the invariants hold
# under the single-writer discipline, so any concurrency-test failure can
# only be attributed to concurrency, not to test setup.
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
    """Negative path: the index-based guard that now runs INSIDE the lock.
    Re-using a pass_id with substantively different content must refuse
    with the exact index_collision code."""
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
# Flipped H1 reproducers (formerly strict-xfail): M1 Q1 landed, so concurrent
# writers must lose NOTHING. A start barrier launches both writers
# simultaneously; locked_mutation is what serializes the mutation windows.
# ---------------------------------------------------------------------------


def test_concurrent_seals_register_both_index_entries(repo):
    """FLIPPED former xfail reproducer (test_concurrent_seals_lose_an_index_entry).

    Two concurrent write_and_seal calls with DISTINCT pass_ids must both
    land in index.yaml: the seed entry survives, both new entries are
    registered, and both calls return structured success (no uncaught
    FileNotFoundError from a shared tmp path, no {'error': ...})."""
    _seed_index(repo)
    start = threading.Barrier(2)

    def _seal(reviewer, pid):
        def _run():
            start.wait(timeout=5)
            return seal_tool.handle(
                ITERATION_ID, reviewer, pid, _packet(ITERATION_ID, reviewer, pid)
            )

        return _run

    results, errors = _run_in_threads(
        [_seal("codex", "pass-a"), _seal("gemini", "pass-b")]
    )

    assert errors == [None, None], f"handle() raised: {errors}"
    for r in results:
        assert "error" not in r, r
        assert r["index_updated"] is True

    index_data = yaml.safe_load(_index_file(repo).read_text(encoding="utf-8"))
    ids = {e["id"] for e in index_data.get("passes", [])}
    assert ids == {"pass-seed", "pass-a", "pass-b"}, (
        f"lost update: surviving index ids = {sorted(ids)}"
    )
    # No leftover lock dir or tmp debris.
    assert not _index_file(repo).with_name("index.yaml.lock").exists()
    leftovers = [
        p.name
        for p in _index_file(repo).parent.iterdir()
        if ".tmp" in p.name
    ]
    assert leftovers == [], f"tmp debris: {leftovers}"


def test_concurrent_audit_appends_keep_both_events(repo):
    """FLIPPED former xfail reproducer (test_concurrent_audit_appends_lose_an_event).

    Two concurrent audit.append_event calls for the same iteration must
    both persist alongside the seed event, and both calls must return
    structured success."""
    audit_path = _seed_audit(repo)
    start = threading.Barrier(2)

    def _append(actor):
        def _run():
            start.wait(timeout=5)
            return audit_tool.handle(
                iteration_id=ITERATION_ID,
                event_type="reviewer_invocation_pending",
                actor=actor,
            )

        return _run

    results, errors = _run_in_threads([_append("alpha"), _append("beta")])

    assert errors == [None, None], f"handle() raised: {errors}"
    for r in results:
        assert "error" not in r, r
        assert "event_id" in r

    log = yaml.safe_load(audit_path.read_text(encoding="utf-8"))["audit_log"]
    assert [e.get("event") for e in log][0] == "review_packet_built"
    pending_actors = {
        e.get("actor")
        for e in log
        if isinstance(e, dict) and e.get("event") == "reviewer_invocation_pending"
    }
    assert pending_actors == {"alpha", "beta"}, (
        f"lost update: surviving pending actors = {sorted(pending_actors)}"
    )
    assert not audit_path.with_name(audit_path.name + ".lock").exists()


# ---------------------------------------------------------------------------
# Hammer tests (M1 Q1 acceptance gates + the permanent independent safeguard:
# they detect LOST STATE ITSELF, independent of the lock hypothesis).
# ---------------------------------------------------------------------------


def test_hammer_threaded_seals_lose_nothing(repo):
    """2 threads x 50 concurrent seals: zero lost entries, zero exceptions."""
    per_thread = 50

    def _worker(tag):
        def _run():
            out = []
            for i in range(per_thread):
                pid = f"pass-{tag}-{i:03d}"
                out.append(
                    seal_tool.handle(
                        ITERATION_ID, tag, pid, _packet(ITERATION_ID, tag, pid)
                    )
                )
            return out

        return _run

    results, errors = _run_in_threads(
        [_worker("codex"), _worker("gemini")], join_timeout=25.0
    )
    assert errors == [None, None], f"handle() raised: {errors}"
    flat = [r for batch in results for r in batch]
    bad = [r for r in flat if "error" in r]
    assert bad == [], f"structured failures under hammer: {bad[:3]}"

    index_data = yaml.safe_load(_index_file(repo).read_text(encoding="utf-8"))
    ids = {e["id"] for e in index_data.get("passes", [])}
    expected = {f"pass-{tag}-{i:03d}" for tag in ("codex", "gemini") for i in range(per_thread)}
    assert ids == expected, (
        f"lost {len(expected - ids)} entries: {sorted(expected - ids)[:5]}"
    )


_SUBPROC_SEAL_SCRIPT = """
import json, sys
from consensus_mcp.tools import review_write_and_seal as seal_tool
tag = sys.argv[1]
n = int(sys.argv[2])
bad = []
for i in range(n):
    pid = "pass-%s-%03d" % (tag, i)
    packet = {
        "iteration_id": "iteration-0001",
        "reviewer_id": tag,
        "pass_id": pid,
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
    }
    r = seal_tool.handle("iteration-0001", tag, pid, packet)
    if "error" in r:
        bad.append(r)
print(json.dumps(bad))
"""


def test_hammer_subprocess_seals_lose_nothing(repo, monkeypatch):
    """2 SEPARATE PROCESSES x 25 concurrent seals: the exact production shape
    (parallel CLI dispatchers) an in-process threading.Lock cannot serialize.
    Zero lost entries, zero structured failures, zero crashes."""
    import os as _os

    env = _os.environ.copy()
    env["CONSENSUS_MCP_REPO_ROOT"] = str(repo)
    env.pop("CONSENSUS_MCP_STATE_ROOT", None)
    env.pop("CONSENSUS_MCP_PROJECT_ROOT", None)

    per_proc = 25
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _SUBPROC_SEAL_SCRIPT, tag, str(per_proc)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in ("codex", "gemini")
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, f"subprocess crashed: rc={p.returncode} stderr={err[-2000:]}"
        outs.append(out)
    for out in outs:
        assert json.loads(out.strip().splitlines()[-1]) == [], out

    index_data = yaml.safe_load(_index_file(repo).read_text(encoding="utf-8"))
    ids = {e["id"] for e in index_data.get("passes", [])}
    expected = {f"pass-{tag}-{i:03d}" for tag in ("codex", "gemini") for i in range(per_proc)}
    assert ids == expected, (
        f"lost {len(expected - ids)} entries: {sorted(expected - ids)[:5]}"
    )


def test_hammer_threaded_audit_appends_lose_nothing(repo):
    """2 threads x 25 concurrent audit appends: every event persists."""
    audit_path = _seed_audit(repo)
    per_thread = 25

    def _worker(tag):
        def _run():
            out = []
            for i in range(per_thread):
                out.append(
                    audit_tool.handle(
                        iteration_id=ITERATION_ID,
                        event_type="reviewer_invocation_pending",
                        actor=f"{tag}-{i:03d}",
                    )
                )
            return out

        return _run

    results, errors = _run_in_threads(
        [_worker("alpha"), _worker("beta")], join_timeout=25.0
    )
    assert errors == [None, None], f"handle() raised: {errors}"
    flat = [r for batch in results for r in batch]
    bad = [r for r in flat if "error" in r]
    assert bad == [], f"structured failures under hammer: {bad[:3]}"

    log = yaml.safe_load(audit_path.read_text(encoding="utf-8"))["audit_log"]
    actors = {
        e.get("actor")
        for e in log
        if isinstance(e, dict) and e.get("event") == "reviewer_invocation_pending"
    }
    expected = {f"{tag}-{i:03d}" for tag in ("alpha", "beta") for i in range(per_thread)}
    assert actors == expected, (
        f"lost {len(expected - actors)} events: {sorted(expected - actors)[:5]}"
    )


# ---------------------------------------------------------------------------
# LockTimeout -> structured refusal (gemini-rev-001: owner fields surfaced).
# ---------------------------------------------------------------------------


def test_seal_lock_timeout_returns_structured_refusal(repo, monkeypatch):
    _seed_index(repo)
    _make_fresh_lock(_index_file(repo))
    monkeypatch.setattr(seal_tool, "_STATE_LOCK_TIMEOUT_S", 0.3)

    r = seal_tool.handle(
        ITERATION_ID, "codex", "pass-lt", _packet(ITERATION_ID, "codex", "pass-lt")
    )
    assert r["error"] == "state_lock_timeout", r
    assert r["owner_pid"] == 1234
    assert r["owner_host"] == "holder-host"
    assert isinstance(r["owner_claimed_at_epoch"], float)
    assert r["lock_target"] == str(_index_file(repo))
    # Fail loud, never proceed unlocked: NOTHING was written - the packet
    # write sits inside the hold.
    sealed = [
        p.name
        for p in _index_file(repo).parent.iterdir()
        if p.name.endswith("-pass.yaml")
    ]
    assert sealed == []
    index_data = yaml.safe_load(_index_file(repo).read_text(encoding="utf-8"))
    assert [e["id"] for e in index_data["passes"]] == ["pass-seed"]


def test_audit_lock_timeout_returns_structured_refusal(repo, monkeypatch):
    audit_path = _seed_audit(repo)
    before = audit_path.read_text(encoding="utf-8")
    _make_fresh_lock(audit_path)
    monkeypatch.setattr(audit_tool, "_STATE_LOCK_TIMEOUT_S", 0.3)

    r = audit_tool.handle(
        iteration_id=ITERATION_ID,
        event_type="reviewer_invocation_pending",
        actor="late",
    )
    assert r["error"] == "state_lock_timeout", r
    assert r["owner_pid"] == 1234
    assert r["owner_host"] == "holder-host"
    assert r["lock_target"] == str(audit_path)
    # Refusal wrote nothing.
    assert audit_path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Stale takeover at the callers + the no-recursive-append sink invariant
# (gemini-rev-002 + kimi-rev-001).
# ---------------------------------------------------------------------------


def test_audit_stale_takeover_emits_to_dispatch_log_only(repo, monkeypatch):
    """A crashed holder's lock on the AUDIT FILE is taken over; the takeover
    event goes to dispatch-log.jsonl ONLY (never the audit file - that
    would re-enter the very lock being held), and the emission path takes
    NO additional locked_mutation (no recursive append)."""
    audit_path = _seed_audit(repo)
    _make_stale_lock(audit_path, pid=4242, host="ghost")

    lock_calls: list[Path] = []
    real_locked_mutation = audit_tool.locked_mutation

    @contextmanager
    def counting_locked_mutation(target, **kwargs):
        lock_calls.append(Path(target))
        with real_locked_mutation(target, **kwargs) as st:
            yield st

    monkeypatch.setattr(audit_tool, "locked_mutation", counting_locked_mutation)

    r = audit_tool.handle(
        iteration_id=ITERATION_ID,
        event_type="reviewer_invocation_pending",
        actor="gamma",
    )
    assert "error" not in r, r

    # Takeover event in the dispatch log, with the stale owner identified.
    lines = [
        json.loads(line)
        for line in _dispatch_log(repo).read_text(encoding="utf-8").splitlines()
    ]
    takeovers = [l for l in lines if l.get("event") == "state_lock_stale_takeover"]
    assert len(takeovers) == 1, lines
    assert takeovers[0]["target"] == str(audit_path)
    assert takeovers[0]["stale_owner_pid"] == 4242
    assert takeovers[0]["stale_owner_host"] == "ghost"

    # NEVER in the audit file itself.
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "state_lock_stale_takeover" not in audit_text
    # The append itself landed.
    log = yaml.safe_load(audit_text)["audit_log"]
    assert log[-1]["actor"] == "gamma"

    # No recursive append: EXACTLY ONE lock acquisition (the audit file's
    # own) - the dispatch-log sink took none.
    assert lock_calls == [audit_path]


def test_seal_stale_takeover_emits_to_dispatch_log_and_seal_succeeds(repo):
    _seed_index(repo)
    _make_stale_lock(_index_file(repo), pid=777, host="crashed-ci")

    r = seal_tool.handle(
        ITERATION_ID, "codex", "pass-a", _packet(ITERATION_ID, "codex", "pass-a")
    )
    assert "error" not in r, r
    assert r["index_updated"] is True

    lines = [
        json.loads(line)
        for line in _dispatch_log(repo).read_text(encoding="utf-8").splitlines()
    ]
    takeovers = [l for l in lines if l.get("event") == "state_lock_stale_takeover"]
    assert len(takeovers) == 1, lines
    assert takeovers[0]["target"] == str(_index_file(repo))
    assert takeovers[0]["stale_owner_pid"] == 777


def test_no_recursive_append_even_when_dispatch_log_itself_is_locked(repo):
    """Sink-invariant regression, 'regardless of which file is locked': the
    dispatch-log append path takes NO locked_mutation, so emitting a
    takeover report while dispatch-log.jsonl ITSELF is held by
    locked_mutation completes immediately - no recursion, no deadlock. If
    someone ever wraps the dispatch-log append in locked_mutation, this
    test hangs out its bounded timeout and fails."""
    dlog = _dispatch_log(repo)
    dlog.parent.mkdir(parents=True, exist_ok=True)
    status = LockStatus(target=Path("some-state-file.yaml"))
    status.takeover = True
    status.takeover_owner = {"pid": 99, "host": "h", "claimed_at_epoch": 0.0}

    t0 = time.monotonic()
    with locked_mutation(dlog, timeout_s=5.0):
        audit_tool._emit_state_lock_takeover(Path("some-state-file.yaml"), status)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"emission blocked for {elapsed:.2f}s - sink is not lock-free"

    lines = [
        json.loads(line) for line in dlog.read_text(encoding="utf-8").splitlines()
    ]
    assert any(l.get("event") == "state_lock_stale_takeover" for l in lines)
