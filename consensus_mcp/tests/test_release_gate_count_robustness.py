"""H-6: robustness of test-count gating in _release_gate_check.

Before the fix the gates decided PASS by a literal substring match on a
hardcoded count (`"60/60 tests passed"`, `"21/21 tests passed"`,
`"95 passed"`). That brittle rule:
  - FAILS a good build when tests are ADDED (count grows past the literal),
  - PASSES a build where a test was deleted then re-added (count restored),
  - and was ALREADY broken for gate_pytest_dispatch_codex (suite collects 96
    now, so the hardcoded "95 passed" mismatches a green run on some legs).
  - gate_pytest_dispatch_codex additionally pointed pytest at a non-existent
    `scripts/consensus_mcp/tests/...` path (dead gate).

The fix replaces the literal match with a floor-based parse:
PASS iff returncode == 0 AND no failure indicator AND parsed passed-count
>= floor. These tests mock subprocess.run (convention from
test_dispatch_codex.py: a MagicMock with .returncode/.stdout/.stderr) and
assert directly against the real gate functions.
"""
from __future__ import annotations

import unittest.mock as _mock
from pathlib import Path

from consensus_mcp import _release_gate_check as rgc


def _fake_run_factory(returncode: int, stdout: str, stderr: str = ""):
    """Return a fake subprocess.run that ignores args and yields a MagicMock
    result carrying the supplied returncode/stdout/stderr (test_dispatch_codex
    convention)."""

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result

    return fake_run


REPO = Path("C:/does/not/matter")
PY = "python"


# ---------------------------------------------------------------------------
# (a) count grows but all green -> PASS  (RED today: literal substring miss)
# ---------------------------------------------------------------------------

def test_smoke_passes_when_count_grows_above_floor(monkeypatch):
    """gate_smoke: a green run reporting MORE tests than the old literal
    (e.g. 72/72) must PASS - adding tests must never break the gate."""
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(0, "72/72 tests passed\n"))
    ok, detail = rgc.gate_smoke(REPO, PY)
    assert ok is True, f"expected PASS for green 72/72, got FAIL: {detail}"


def test_validators_pass_when_count_grows_above_floor(monkeypatch):
    """gate_validators: green 25/25 (>21 floor) must PASS."""
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(0, "25/25 tests passed\n"))
    ok, detail = rgc.gate_validators(REPO, PY)
    assert ok is True, f"expected PASS for green 25/25, got FAIL: {detail}"


def test_install_smoke_passes_when_count_grows_above_floor(monkeypatch):
    """gate_install_smoke: green 72/72 (>60 floor) must PASS."""
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(0, "72/72 tests passed\n"))
    ok, detail = rgc.gate_install_smoke(REPO, Path("venv-python"))
    assert ok is True, f"expected PASS for green 72/72, got FAIL: {detail}"


def test_dispatch_codex_passes_when_count_grows_above_floor(monkeypatch):
    """gate_pytest_dispatch_codex: green run reporting 96 passed (>90 floor)
    must PASS. RED today: the hardcoded literal is '95 passed' and the suite
    now collects 96."""
    monkeypatch.setattr(
        rgc.subprocess, "run", _fake_run_factory(0, "96 passed, 1 skipped in 2.00s\n")
    )
    ok, detail = rgc.gate_pytest_dispatch_codex(REPO, PY)
    assert ok is True, f"expected PASS for green 96 passed, got FAIL: {detail}"


# ---------------------------------------------------------------------------
# (b) non-zero returncode -> FAIL
# ---------------------------------------------------------------------------

def test_smoke_fails_on_nonzero_returncode(monkeypatch):
    """gate_smoke: even with a passing-looking line, a non-zero rc must FAIL."""
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(1, "72/72 tests passed\n"))
    ok, detail = rgc.gate_smoke(REPO, PY)
    assert ok is False, f"expected FAIL for rc=1, got PASS: {detail}"


def test_dispatch_codex_fails_on_nonzero_returncode(monkeypatch):
    """gate_pytest_dispatch_codex: non-zero rc must FAIL despite enough passes."""
    monkeypatch.setattr(
        rgc.subprocess, "run", _fake_run_factory(1, "96 passed in 2.00s\n")
    )
    ok, detail = rgc.gate_pytest_dispatch_codex(REPO, PY)
    assert ok is False, f"expected FAIL for rc=1, got PASS: {detail}"


def test_validators_fail_on_nonzero_returncode(monkeypatch):
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(2, "25/25 tests passed\n"))
    ok, detail = rgc.gate_validators(REPO, PY)
    assert ok is False, f"expected FAIL for rc=2, got PASS: {detail}"


# ---------------------------------------------------------------------------
# (c) returncode 0 but failure word in output -> FAIL
# ---------------------------------------------------------------------------

def test_dispatch_codex_fails_when_failure_word_present_despite_rc0(monkeypatch):
    """A buggy runner can exit 0 while reporting failures. The gate must read
    the failure indicator, not just rc + passed-count."""
    monkeypatch.setattr(
        rgc.subprocess,
        "run",
        _fake_run_factory(0, "94 passed, 2 failed in 2.00s\n"),
    )
    ok, detail = rgc.gate_pytest_dispatch_codex(REPO, PY)
    assert ok is False, f"expected FAIL when 'failed' present (rc0), got PASS: {detail}"


def test_dispatch_codex_fails_when_errors_present_despite_rc0(monkeypatch):
    monkeypatch.setattr(
        rgc.subprocess,
        "run",
        _fake_run_factory(0, "90 passed, 1 error in 2.00s\n"),
    )
    ok, detail = rgc.gate_pytest_dispatch_codex(REPO, PY)
    assert ok is False, f"expected FAIL when 'error' present (rc0), got PASS: {detail}"


def test_smoke_fails_when_numerator_below_denominator_rc0(monkeypatch):
    """smoke format 'X/Y tests passed': partial pass (46/60) means failures
    even if rc happened to be 0. Must FAIL (numerator != denominator)."""
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(0, "46/60 tests passed\n"))
    ok, detail = rgc.gate_smoke(REPO, PY)
    assert ok is False, f"expected FAIL for partial 46/60, got PASS: {detail}"


# ---------------------------------------------------------------------------
# (d) below-floor deletion still trips the gate (anti-deletion guard)
# ---------------------------------------------------------------------------

def test_smoke_fails_when_count_drops_below_floor(monkeypatch):
    """Floor still guards against large deletions: a green 10/10 is below the
    smoke floor (60) and must FAIL."""
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(0, "10/10 tests passed\n"))
    ok, detail = rgc.gate_smoke(REPO, PY)
    assert ok is False, f"expected FAIL for 10/10 below floor, got PASS: {detail}"


def test_dispatch_codex_fails_when_count_drops_below_floor(monkeypatch):
    """Green 50 passed is below the dispatch_codex floor (90) -> FAIL."""
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(0, "50 passed in 1.00s\n"))
    ok, detail = rgc.gate_pytest_dispatch_codex(REPO, PY)
    assert ok is False, f"expected FAIL for 50 passed below floor, got PASS: {detail}"


# ---------------------------------------------------------------------------
# (e) at-floor exactly -> PASS (boundary)
# ---------------------------------------------------------------------------

def test_smoke_passes_at_exact_floor(monkeypatch):
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(0, "60/60 tests passed\n"))
    ok, detail = rgc.gate_smoke(REPO, PY)
    assert ok is True, f"expected PASS at exact floor 60/60, got FAIL: {detail}"


def test_dispatch_codex_passes_at_exact_floor(monkeypatch):
    monkeypatch.setattr(rgc.subprocess, "run", _fake_run_factory(0, "90 passed in 2.00s\n"))
    ok, detail = rgc.gate_pytest_dispatch_codex(REPO, PY)
    assert ok is True, f"expected PASS at exact floor 90 passed, got FAIL: {detail}"
