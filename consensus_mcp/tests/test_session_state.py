"""Tests for consensus_mcp._session_state.

Covers the v1.32.1 per-invocation activation contract (consult
iteration-v133-gate-scope-shift-2026-05-26): the session-active
marker is the dormant<->active toggle for the gate.

Trust-root regression gates: the marker is NOT a trust artifact;
forging it must NOT bypass `verify_design_approval`'s seal checks.
That's tested in test_seal_iteration.py + test_consensus_hooks.py
under the new model; here we just verify the marker's own contract.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _session_state as ss  # noqa: E402


def _repo(tmp_path):
    r = tmp_path / "repo"
    (r / "consensus-state" / "active" / "iter-test").mkdir(parents=True)
    return r


# ----- write_session_marker ----------------------------------------

def test_write_session_marker_creates_yaml_with_required_fields(tmp_path):
    repo = _repo(tmp_path)
    path = ss.write_session_marker(
        repo,
        iteration_id="iter-test",
        scope_glob="docs/consensus/**",
        activated_by="test-fixture",
        activation_source="test_fixture",
    )
    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert data["schema_version"] == 1
    assert data["iteration_id"] == "iter-test"
    assert data["scope_glob"] == "docs/consensus/**"
    assert data["activated_by"] == "test-fixture"
    assert data["activation_source"] == "test_fixture"
    assert "activated_at_utc" in data


def test_write_session_marker_accepts_glob_list(tmp_path):
    # G3: a list scope_glob writes scope_globs (and no scalar scope_glob); a
    # single-element list stays byte-compatible (scalar scope_glob).
    repo = _repo(tmp_path)
    path = ss.write_session_marker(
        repo, iteration_id="iter-test",
        scope_glob=["consensus_mcp/**", "docs/**"],
        activated_by="test-fixture", activation_source="test_fixture")
    data = yaml.safe_load(path.read_text())
    assert data["scope_globs"] == ["consensus_mcp/**", "docs/**"]
    assert "scope_glob" not in data
    # single-element list -> scalar (byte-compatible with the legacy shape)
    path2 = ss.write_session_marker(
        repo, iteration_id="iter-test", scope_glob=["src/**"],
        activated_by="t", activation_source="test_fixture")
    data2 = yaml.safe_load(path2.read_text())
    assert data2["scope_glob"] == "src/**" and "scope_globs" not in data2


def test_write_session_marker_leaves_no_predictable_tmp(tmp_path):
    """kimi-rev-003: the atomic write must not leave a predictable
    `session-active.tmp` behind, and the marker round-trips. mkstemp temp files
    are removed by the rename; nothing guessable lingers in .consensus/."""
    repo = _repo(tmp_path)
    ss.write_session_marker(repo, iteration_id="iter-test", scope_glob="x/**",
                            activated_by="t", activation_source="test_fixture")
    consensus_dir = repo / ".consensus"
    # no predictable '<name>.tmp' sibling, and no leftover mkstemp temp files.
    assert not (consensus_dir / "session-active.tmp").exists()
    leftovers = [p.name for p in consensus_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], leftovers
    assert ss.read_session_marker(repo)["iteration_id"] == "iter-test"


def test_write_session_marker_refuses_unsafe_iteration_id(tmp_path):
    repo = _repo(tmp_path)
    for bad in ("", "../escape", "iter/with/slashes", "iter\\with\\backslashes", "iter..traversal"):
        with pytest.raises(ValueError):
            ss.write_session_marker(repo, iteration_id=bad, scope_glob="*",
                                    activated_by="t", activation_source="test_fixture")


def test_write_session_marker_refuses_invalid_source(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError):
        ss.write_session_marker(repo, iteration_id="iter-test", scope_glob="*",
                                activated_by="t", activation_source="forged_source")


# ----- read_session_marker ----------------------------------------

def test_read_returns_none_when_marker_absent(tmp_path):
    repo = _repo(tmp_path)
    assert ss.read_session_marker(repo) is None


def test_read_returns_none_on_unparseable_yaml(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".consensus").mkdir(parents=True, exist_ok=True)
    (repo / ".consensus" / "session-active").write_text("key: value: bad colon", encoding="utf-8")
    # Not an error - dormant mode (gate stays off; operator notices).
    assert ss.read_session_marker(repo) is None


def test_read_returns_none_when_iteration_id_missing(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".consensus").mkdir(parents=True, exist_ok=True)
    (repo / ".consensus" / "session-active").write_text(
        "schema_version: 1\nscope_glob: '*'\n", encoding="utf-8",
    )
    assert ss.read_session_marker(repo) is None


# ----- session_active ----------------------------------------------

def test_session_active_false_when_no_marker(tmp_path):
    repo = _repo(tmp_path)
    assert ss.session_active(repo) is False


def test_session_active_true_when_marker_points_at_real_iteration(tmp_path):
    repo = _repo(tmp_path)
    ss.write_session_marker(repo, iteration_id="iter-test", scope_glob="*",
                            activated_by="t", activation_source="test_fixture")
    assert ss.session_active(repo) is True


def test_session_active_false_when_iteration_does_not_exist(tmp_path):
    """A stale session marker pointing at a non-existent iteration
    is treated as DORMANT (R4 mitigation - the operator's
    crash-recovery story)."""
    repo = _repo(tmp_path)
    ss.write_session_marker(repo, iteration_id="iter-test", scope_glob="*",
                            activated_by="t", activation_source="test_fixture")
    # Remove the iteration dir -> marker now points at nothing.
    import shutil
    shutil.rmtree(repo / "consensus-state" / "active" / "iter-test")
    assert ss.session_active(repo) is False


def test_session_active_true_when_legacy_env_var_set(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    # No session marker.
    assert ss.session_active(repo) is False
    monkeypatch.setenv("CONSENSUS_MCP_LEGACY_ALWAYS_ON", "1")
    assert ss.session_active(repo) is True


def test_session_active_true_when_legacy_marker_file_present(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".consensus").mkdir(parents=True, exist_ok=True)
    (repo / ".consensus" / "legacy-always-on").write_text("", encoding="utf-8")
    assert ss.session_active(repo) is True


# ----- clear_session_marker ----------------------------------------

def test_clear_session_marker_returns_true_when_present(tmp_path):
    repo = _repo(tmp_path)
    ss.write_session_marker(repo, iteration_id="iter-test", scope_glob="*",
                            activated_by="t", activation_source="test_fixture")
    assert ss.clear_session_marker(repo) is True
    assert ss.session_active(repo) is False


def test_clear_session_marker_returns_false_when_absent(tmp_path):
    repo = _repo(tmp_path)
    assert ss.clear_session_marker(repo) is False


# ----- migration warning -------------------------------------------

def test_migration_warning_fires_once_then_suppresses(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / ".consensus").mkdir(parents=True, exist_ok=True)
    # No session-active marker, no legacy-always-on -> migration applies.
    assert ss.emit_migration_warning_once(repo) is True
    captured = capsys.readouterr()
    assert "PER-INVOCATION" in captured.err
    assert "legacy-always-on" in captured.err

    # Second call: suppressed.
    assert ss.emit_migration_warning_once(repo) is False


def test_migration_warning_skipped_when_legacy_opt_in(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / ".consensus").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CONSENSUS_MCP_LEGACY_ALWAYS_ON", "1")
    assert ss.emit_migration_warning_once(repo) is False


def test_migration_warning_skipped_when_no_consensus_dir(tmp_path):
    repo = _repo(tmp_path)
    # No .consensus dir at all -> no migration to perform.
    assert ss.emit_migration_warning_once(repo) is False


def test_migration_warning_skipped_when_session_active(tmp_path):
    repo = _repo(tmp_path)
    ss.write_session_marker(repo, iteration_id="iter-test", scope_glob="*",
                            activated_by="t", activation_source="test_fixture")
    # Session already active under new model -> no migration warning needed.
    assert ss.emit_migration_warning_once(repo) is False


# ----- gate_should_enforce (shared hook activation predicate) -------
# v1.33 gate-consistency fix: the ONE predicate the PreToolUse gate, Stop gate,
# and SessionStart injector all consult, so they cannot drift back to firing in
# non-consensus everyday work.

def _clean_gate_env(monkeypatch):
    for var in ("CONSENSUS_MCP_GATE_DISABLE", "CONSENSUS_MCP_FORCE_OPTED_IN",
                ss.LEGACY_ENV_VAR):
        monkeypatch.delenv(var, raising=False)


def test_gate_should_enforce_dormant_by_default(tmp_path, monkeypatch):
    _clean_gate_env(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    # No marker, no legacy opt-in, no env override -> DORMANT (the everyday case).
    assert ss.gate_should_enforce(repo) is False


def test_gate_ignores_live_session_marker_in_on_demand_project(tmp_path, monkeypatch):
    _clean_gate_env(monkeypatch)
    repo = _repo(tmp_path)  # creates consensus-state/active/iter-test
    ss.write_session_marker(repo, iteration_id="iter-test", scope_glob="*",
                            activated_by="t", activation_source="test_fixture")
    assert ss.session_active(repo) is True
    assert ss.gate_should_enforce(repo) is False


def test_gate_should_enforce_explicit_continuous_project(tmp_path, monkeypatch):
    _clean_gate_env(monkeypatch)
    repo = tmp_path / "repo"
    config = repo / ".consensus" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "schema_version: 1\ngovernance:\n  mode: continuous\n",
        encoding="utf-8",
    )
    assert ss.governance_mode(repo) == "continuous"
    assert ss.gate_should_enforce(repo) is True


def test_gate_should_enforce_force_opted_in(tmp_path, monkeypatch):
    _clean_gate_env(monkeypatch)
    monkeypatch.setenv("CONSENSUS_MCP_FORCE_OPTED_IN", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    assert ss.gate_should_enforce(repo) is True


def test_gate_should_enforce_gate_disable_overrides_everything(tmp_path, monkeypatch):
    _clean_gate_env(monkeypatch)
    # GATE_DISABLE is the operator escape hatch - it wins over BOTH a live marker
    # and FORCE_OPTED_IN (the human trust-root can never be deadlocked).
    monkeypatch.setenv("CONSENSUS_MCP_GATE_DISABLE", "1")
    monkeypatch.setenv("CONSENSUS_MCP_FORCE_OPTED_IN", "1")
    repo = _repo(tmp_path)
    ss.write_session_marker(repo, iteration_id="iter-test", scope_glob="*",
                            activated_by="t", activation_source="test_fixture")
    assert ss.gate_should_enforce(repo) is False


def test_gate_should_enforce_legacy_marker_activates(tmp_path, monkeypatch):
    _clean_gate_env(monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".consensus").mkdir(parents=True)
    (repo / ".consensus" / "legacy-always-on").write_text("", encoding="utf-8")
    assert ss.gate_should_enforce(repo) is True


# ----- session TTL (operator-lockout fix, 2026-07-05) ---------------
# A marker left behind by a crashed/abandoned consult must NEVER keep the
# gate armed indefinitely: markers older than the TTL are treated as
# abandoned, best-effort self-cleaned, and the probe stays dormant.

def _write_marker_with_age(repo, age_hours):
    import datetime
    ts = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(hours=age_hours))
    marker = repo / ".consensus" / "session-active"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(yaml.safe_dump({
        "schema_version": 1, "iteration_id": "iter-test", "scope_glob": "*",
        "activated_by": "t", "activation_source": "test_fixture",
        "activated_at_utc": ts.isoformat(),
    }), encoding="utf-8")
    return marker


def test_session_active_expired_marker_is_dormant_and_self_cleans(tmp_path, monkeypatch):
    monkeypatch.delenv(ss.SESSION_TTL_ENV_VAR, raising=False)
    repo = _repo(tmp_path)
    marker = _write_marker_with_age(repo, age_hours=120)  # 5 days >> 24h default
    assert ss.session_active(repo) is False
    assert not marker.exists()  # self-cleaned: no residue for the next session


def test_session_active_fresh_marker_survives_ttl(tmp_path, monkeypatch):
    monkeypatch.delenv(ss.SESSION_TTL_ENV_VAR, raising=False)
    repo = _repo(tmp_path)
    marker = _write_marker_with_age(repo, age_hours=1)
    assert ss.session_active(repo) is True
    assert marker.exists()  # a live consult is untouched


def test_session_active_ttl_env_override(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _write_marker_with_age(repo, age_hours=2)
    monkeypatch.setenv(ss.SESSION_TTL_ENV_VAR, "1")
    assert ss.session_active(repo) is False  # 2h old > 1h TTL
    _write_marker_with_age(repo, age_hours=2)
    monkeypatch.setenv(ss.SESSION_TTL_ENV_VAR, "48")
    assert ss.session_active(repo) is True   # 2h old < 48h TTL


def test_session_active_missing_timestamp_expires(tmp_path, monkeypatch):
    # Fail-STALE: an unprovable activation time must not hold a lock.
    monkeypatch.delenv(ss.SESSION_TTL_ENV_VAR, raising=False)
    repo = _repo(tmp_path)
    marker = repo / ".consensus" / "session-active"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(yaml.safe_dump({
        "schema_version": 1, "iteration_id": "iter-test", "scope_glob": "*",
        "activated_by": "t", "activation_source": "test_fixture",
    }), encoding="utf-8")
    assert ss.session_active(repo) is False
    assert not marker.exists()
