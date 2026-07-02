"""M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q2+Q11.

Q2 - route the seal-pipeline writes through the ONE blessed atomic writer:
  - review.write_and_seal Step 8 (sealed-packet write) and Step 9 (index
    write) now go through `_atomic_io.atomic_write_text` instead of a
    hand-rolled write_text + os.replace, so they can never drift from the
    single atomic-write primitive again;
  - a monkeypatched atomic_write_text failure on the packet write yields the
    structured `packet_write_failed` refusal, and the prior index file is left
    byte-identical (nothing partially written);
  - audit.append_event's write-back already routes through the blessed writer -
    pinned here so it cannot regress;
  - the `_STATE_LOCK_TIMEOUT_S` constant and the `state_lock_timeout` refusal
    builder have ONE definition site (review_write_and_seal) that audit imports.

Q11 - the lock-acquisition budget is env-overridable via
CONSENSUS_MCP_STATE_LOCK_TIMEOUT_SECONDS (parsed once at import, positive
finite float or the 30s default), and the refusal message names the remedy:
that the operation was NOT performed, WHO holds the lock, and that the wait
can be extended via that env var.

Style/provenance mirror: consensus_mcp/tests/test_seal_index_concurrency.py
(tmp_path + monkeypatch.setenv path redirection, behavior assertions).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from consensus_mcp.tools import audit_append_event as audit_tool
from consensus_mcp.tools import review_write_and_seal as seal_tool

ITERATION_ID = "iteration-0001"
ENV_VAR = "CONSENSUS_MCP_STATE_LOCK_TIMEOUT_SECONDS"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Redirect all state under tmp_path via the lazy _paths resolvers
    (setenv, never setattr - per the modules-under-test migration notes)."""
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


def _make_fresh_lock(target: Path, pid: int = 1234, host: str = "holder-host") -> Path:
    """Fabricate a LIVE (non-stale) lock dir on `target`."""
    lock_dir = target.with_name(target.name + ".lock")
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": pid, "host": host, "claimed_at_epoch": time.time()}),
        encoding="utf-8",
    )
    return lock_dir


# ---------------------------------------------------------------------------
# Q2: both seal writes route through the ONE blessed atomic writer.
# ---------------------------------------------------------------------------


def test_seal_packet_and_index_writes_route_through_atomic_writer(repo, monkeypatch):
    """A spy over `atomic_write_text` proves BOTH the sealed-packet write and
    the index write go through the single blessed primitive (not a hand-rolled
    write_text + os.replace)."""
    _seed_index(repo)
    calls: list[Path] = []
    real = seal_tool.atomic_write_text

    def _spy(path, text, encoding="utf-8"):
        calls.append(Path(path))
        return real(path, text, encoding)

    monkeypatch.setattr(seal_tool, "atomic_write_text", _spy)

    r = seal_tool.handle(
        ITERATION_ID, "codex", "pass-a", _packet(ITERATION_ID, "codex", "pass-a")
    )
    assert "error" not in r, r
    assert r["index_updated"] is True

    # Exactly the sealed packet + the index went through the blessed writer.
    assert Path(r["sealed_path"]) in calls, calls
    assert _index_file(repo) in calls, calls
    assert len(calls) == 2, calls


def test_audit_write_back_routes_through_atomic_writer(repo, monkeypatch):
    """audit.append_event's write-back already uses the blessed writer - pin
    it so a future refactor cannot silently reintroduce a truncating
    write_text."""
    audit_path = _seed_audit(repo)
    calls: list[Path] = []
    real = audit_tool.atomic_write_text

    def _spy(path, text, encoding="utf-8"):
        calls.append(Path(path))
        return real(path, text, encoding)

    monkeypatch.setattr(audit_tool, "atomic_write_text", _spy)

    r = audit_tool.handle(
        iteration_id=ITERATION_ID,
        event_type="reviewer_invocation_pending",
        actor="alpha",
    )
    assert "error" not in r, r
    assert calls == [audit_path], calls


def test_seal_packet_write_failure_is_structured_refusal_index_byte_identical(
    repo, monkeypatch
):
    """A monkeypatched atomic_write_text failure on the packet write yields the
    structured `packet_write_failed` refusal; the prior index file is
    byte-identical (nothing partially written) and no packet file lands."""
    _seed_index(repo)
    index_before = _index_file(repo).read_bytes()

    def _boom(path, text, encoding="utf-8"):
        # Refuse only the packet write (the index write name is index.yaml);
        # the packet write is Step 8 and returns before Step 9 runs anyway.
        if Path(path).name != "index.yaml":
            raise OSError(28, "No space left on device (simulated)")
        raise AssertionError("index write must not be reached after packet failure")

    monkeypatch.setattr(seal_tool, "atomic_write_text", _boom)

    r = seal_tool.handle(
        ITERATION_ID, "codex", "pass-x", _packet(ITERATION_ID, "codex", "pass-x")
    )
    assert r["error"] == "packet_write_failed", r
    assert "OSError" in r["detail"]
    assert "No space left on device" in r["detail"]

    # Prior file byte-identical: the index never changed.
    assert _index_file(repo).read_bytes() == index_before
    # No sealed packet landed, and no atomic-writer tmp debris survived.
    review_dir = _index_file(repo).parent
    sealed = [p.name for p in review_dir.iterdir() if p.name.endswith("-pass.yaml")]
    assert sealed == [], sealed
    tmp_debris = [p.name for p in review_dir.iterdir() if ".tmp" in p.name]
    assert tmp_debris == [], tmp_debris
    # The lock was released on the refusal path.
    assert not _index_file(repo).with_name("index.yaml.lock").exists()


# ---------------------------------------------------------------------------
# Q2: single definition site shared between the two writers.
# ---------------------------------------------------------------------------


def test_timeout_constant_and_refusal_builder_are_shared_one_definition(repo):
    """audit imports the timeout budget + the refusal builder from
    review_write_and_seal, so there is exactly one definition site."""
    assert audit_tool._STATE_LOCK_TIMEOUT_S == seal_tool._STATE_LOCK_TIMEOUT_S
    assert audit_tool._state_lock_timeout_refusal is seal_tool._state_lock_timeout_refusal


# ---------------------------------------------------------------------------
# Q11: env override of the lock-acquisition budget.
# ---------------------------------------------------------------------------


def test_state_lock_timeout_env_resolver(monkeypatch):
    """The resolver honors a positive finite float and falls back to 30.0 for
    every other input (unset, non-numeric, <= 0, NaN, +/-inf)."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert seal_tool._resolve_state_lock_timeout() == 30.0

    for good, expected in (("5.5", 5.5), ("90", 90.0), ("0.25", 0.25)):
        monkeypatch.setenv(ENV_VAR, good)
        assert seal_tool._resolve_state_lock_timeout() == expected, good

    for bad in ("0", "-3", "-0.1", "abc", "", "nan", "inf", "-inf", "  "):
        monkeypatch.setenv(ENV_VAR, bad)
        assert seal_tool._resolve_state_lock_timeout() == 30.0, bad


_ENV_WIRING_SCRIPT = """
from consensus_mcp.tools import review_write_and_seal as s
from consensus_mcp.tools import audit_append_event as a
print(repr(s._STATE_LOCK_TIMEOUT_S), repr(a._STATE_LOCK_TIMEOUT_S))
"""


def test_env_override_wires_into_the_module_constant_at_import(monkeypatch):
    """Parsed ONCE at import: a fresh process with the env set sees the
    override in BOTH modules' `_STATE_LOCK_TIMEOUT_S`; an invalid value falls
    back to 30.0 in both."""
    import os as _os

    def _run(value: str | None) -> tuple[float, float]:
        env = _os.environ.copy()
        if value is None:
            env.pop(ENV_VAR, None)
        else:
            env[ENV_VAR] = value
        out = subprocess.run(
            [sys.executable, "-c", _ENV_WIRING_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        s_val, a_val = out.stdout.strip().split()
        return float(s_val), float(a_val)

    assert _run("7.5") == (7.5, 7.5)
    assert _run("nonsense") == (30.0, 30.0)
    assert _run(None) == (30.0, 30.0)


# ---------------------------------------------------------------------------
# Q11: the state_lock_timeout refusal names the remedy.
# ---------------------------------------------------------------------------


def test_state_lock_timeout_refusal_builder_names_remedy():
    """Direct unit check of the shared builder (no lock primitive): the refusal
    carries the M1 owner fields AND the Q11 remedy detail. Deterministic and
    independent of _atomic_io.locked_mutation internals."""
    from consensus_mcp._atomic_io import LockTimeout

    target = Path("/state/index.yaml")
    exc = LockTimeout(
        target, 0.3, {"pid": 1234, "host": "holder-host", "claimed_at_epoch": 123.0}
    )
    r = seal_tool._state_lock_timeout_refusal(exc, target)
    assert r["error"] == "state_lock_timeout"
    assert r["lock_target"] == str(target)
    assert r["owner_pid"] == 1234
    assert r["owner_host"] == "holder-host"
    assert r["owner_claimed_at_epoch"] == 123.0
    _assert_names_remedy(r["detail"])
    # The specific timeout used is named, so the operator knows the current budget.
    assert "0.3s" in r["detail"]


def _assert_names_remedy(detail: str) -> None:
    lower = detail.lower()
    # (1) the operation was NOT performed
    assert "not performed" in lower, detail
    # (2) the holder identity (pid + host from the fresh lock)
    assert "1234" in detail, detail
    assert "holder-host" in detail, detail
    # (3) the remedy env var to extend the wait
    assert ENV_VAR in detail, detail
    assert "extend" in lower, detail


def test_seal_lock_timeout_message_names_remedy(repo, monkeypatch):
    _seed_index(repo)
    _make_fresh_lock(_index_file(repo))
    monkeypatch.setattr(seal_tool, "_STATE_LOCK_TIMEOUT_S", 0.3)

    r = seal_tool.handle(
        ITERATION_ID, "codex", "pass-lt", _packet(ITERATION_ID, "codex", "pass-lt")
    )
    assert r["error"] == "state_lock_timeout", r
    # M1 owner fields still surfaced (gemini-rev-001).
    assert r["owner_pid"] == 1234
    assert r["owner_host"] == "holder-host"
    assert r["lock_target"] == str(_index_file(repo))
    _assert_names_remedy(r["detail"])


def test_audit_lock_timeout_message_names_remedy(repo, monkeypatch):
    audit_path = _seed_audit(repo)
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
    _assert_names_remedy(r["detail"])
