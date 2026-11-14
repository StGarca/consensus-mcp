"""Tests for _dispatch_builder (workflow D write-enabled dispatch)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from consensus_mcp import _architect_paths as ap
from consensus_mcp import _dispatch_builder as db


def _lane(tmp_path: Path) -> Path:
    lane = tmp_path / ".consensus" / "architect" / "g1" / "lane"
    lane.mkdir(parents=True)
    return lane


def _patch_tempdir(tmp_path: Path, monkeypatch) -> Path:
    """Point the dispatcher's temp-file generation at an observable dir so
    tests can assert the out-file lifecycle (created post-canon, unlinked on
    every exit path)."""
    tempdir = tmp_path / "observed-tmp"
    tempdir.mkdir()
    monkeypatch.setattr(db.tempfile, "gettempdir", lambda: str(tempdir))
    return tempdir


def _identity_resolver(monkeypatch):
    """Pin argv[0] to the caller-supplied name regardless of what is on the
    host PATH (on a codex-installed machine _resolve_codex_bin returns a full
    path, which would make exact-argv assertions machine-dependent)."""
    monkeypatch.setattr(db, "_resolve_codex_bin", lambda b: b)


def _fake_popen_factory(payload: dict, returncode: int = 0):
    calls = {}

    class FakeProc:
        def __init__(self, argv, **kwargs):
            calls["argv"] = list(argv)
            calls["kwargs"] = kwargs
            self._argv = list(argv)
            self.returncode = None

        def communicate(self, input=None, timeout=None):
            calls["input"] = input
            calls["timeout"] = timeout
            out_idx = self._argv.index("-o") + 1
            Path(self._argv[out_idx]).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            self.returncode = returncode
            return b"", b"" if returncode == 0 else b"boom"

        def poll(self):
            return self.returncode

    return FakeProc, calls


def test_dispatch_builder_happy_path(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    tempdir = _patch_tempdir(tmp_path, monkeypatch)
    _identity_resolver(monkeypatch)
    fake_popen, calls = _fake_popen_factory(
        {"summary": "implemented slice 1", "pushback": None, "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    result = db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="BUILD per spec", codex_bin="codex", timeout_seconds=60,
    )
    assert result["summary"] == "implemented slice 1"
    assert result["pushback"] is None
    argv = calls["argv"]
    assert argv[:2] == ["codex", "exec"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--cd") + 1] == str(lane)
    # out-file is unlinked after a successful dispatch too
    assert list(tempdir.iterdir()) == []


def test_binary_resolved_through_resolver(tmp_path: Path, monkeypatch):
    """Finding 1: argv[0] must route through _resolve_codex_bin so
    npm-installed codex (codex.cmd) resolves on Windows."""
    lane = _lane(tmp_path)
    seen = {}

    def fake_resolver(codex_bin):
        seen["input"] = codex_bin
        return "/resolved/path/codex.cmd"

    monkeypatch.setattr(db, "_resolve_codex_bin", fake_resolver)
    fake_popen, calls = _fake_popen_factory(
        {"summary": "ok", "pushback": None, "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="x", codex_bin="codex", timeout_seconds=60,
    )
    assert seen["input"] == "codex"
    # The RESOLVED path is what reaches argv[0] (and it passes the canon,
    # whose suffix set was written to admit _resolve_codex_bin's output).
    assert calls["argv"][0] == "/resolved/path/codex.cmd"


def test_resolver_error_maps_to_dispatch_error(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)

    def exploding_resolver(codex_bin):
        raise db.CodexInvocationError("App Execution Alias stub")

    monkeypatch.setattr(db, "_resolve_codex_bin", exploding_resolver)

    def must_not_spawn(*a, **k):
        raise AssertionError("Popen reached despite resolver failure")

    monkeypatch.setattr(db.subprocess, "Popen", must_not_spawn)
    with pytest.raises(db.BuilderDispatchError, match="resolution failed"):
        db.dispatch_builder(
            repo_root=tmp_path, lane=lane,
            prompt="x", codex_bin="codex", timeout_seconds=60,
        )


def test_dispatch_builder_rejects_noncanon_argv(tmp_path: Path, monkeypatch):
    # lane outside .consensus/architect/*/lane -> canon violation pre-Popen
    bad_lane = tmp_path / "elsewhere"
    bad_lane.mkdir()
    tempdir = _patch_tempdir(tmp_path, monkeypatch)
    _identity_resolver(monkeypatch)
    called = {"n": 0}

    def must_not_run(*a, **k):
        called["n"] += 1
        raise AssertionError("Popen reached despite canon violation")

    monkeypatch.setattr(db.subprocess, "Popen", must_not_run)
    with pytest.raises(db.BuilderDispatchError, match="canon"):
        db.dispatch_builder(
            repo_root=tmp_path, lane=bad_lane,
            prompt="x", codex_bin="codex", timeout_seconds=60,
        )
    assert called["n"] == 0
    # Finding 3: argv is validated BEFORE the temp out-file is created -
    # a canon-violating dispatch leaves zero litter.
    assert list(tempdir.iterdir()) == []


def test_dispatch_builder_pushback_passthrough(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    _identity_resolver(monkeypatch)
    fake_popen, _ = _fake_popen_factory(
        {"summary": "", "pushback": "spec contradicts itself", "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    result = db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="x", codex_bin="codex", timeout_seconds=60,
    )
    assert result["pushback"] == "spec contradicts itself"


def test_dispatch_builder_nonzero_exit_raises(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    tempdir = _patch_tempdir(tmp_path, monkeypatch)
    _identity_resolver(monkeypatch)
    fake_popen, _ = _fake_popen_factory(
        {"summary": "x", "pushback": None, "notes": ""}, returncode=2
    )
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    with pytest.raises(db.BuilderDispatchError, match="exited 2"):
        db.dispatch_builder(
            repo_root=tmp_path, lane=lane,
            prompt="x", codex_bin="codex", timeout_seconds=60,
        )
    # Finding 3: the nonzero-exit path unlinks the temp out-file too.
    assert list(tempdir.iterdir()) == []


def test_dispatch_builder_invalid_output_raises(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    tempdir = _patch_tempdir(tmp_path, monkeypatch)
    _identity_resolver(monkeypatch)
    fake_popen, _ = _fake_popen_factory({"unexpected": True})
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    with pytest.raises(db.BuilderDispatchError, match="summary"):
        db.dispatch_builder(
            repo_root=tmp_path, lane=lane,
            prompt="x", codex_bin="codex", timeout_seconds=60,
        )
    assert list(tempdir.iterdir()) == []


def test_dispatch_builder_nonstring_notes_raises(tmp_path: Path, monkeypatch):
    """Quality finding: 'notes' is required+string per builder_build_schema
    and is the reviewer pointer consumed by the handoff renderer; a missing
    or non-string notes must fail closed like summary/pushback, not be
    silently coerced to ''."""
    lane = _lane(tmp_path)
    tempdir = _patch_tempdir(tmp_path, monkeypatch)
    _identity_resolver(monkeypatch)
    for payload in (
        {"summary": "ok", "pushback": None, "notes": ["not", "a", "string"]},
        {"summary": "ok", "pushback": None},  # notes missing entirely
    ):
        fake_popen, _ = _fake_popen_factory(payload)
        monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
        with pytest.raises(db.BuilderDispatchError, match="notes"):
            db.dispatch_builder(
                repo_root=tmp_path, lane=lane,
                prompt="x", codex_bin="codex", timeout_seconds=60,
            )
        assert list(tempdir.iterdir()) == []


def test_timeout_terminates_process_tree_and_cleans_temp(
    tmp_path: Path, monkeypatch
):
    """Finding 2: on timeout the WHOLE process group is killed (a builder
    descendant must not keep writing in the lane after the supervisor moves
    on), and the temp out-file is unlinked (finding 3)."""
    lane = _lane(tmp_path)
    tempdir = _patch_tempdir(tmp_path, monkeypatch)
    _identity_resolver(monkeypatch)
    spawned = {}

    class TimingOutProc:
        def __init__(self, argv, **kwargs):
            spawned["proc"] = self
            self.returncode = None

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)

        def poll(self):
            return self.returncode

    monkeypatch.setattr(db.subprocess, "Popen", TimingOutProc)
    killed = []
    monkeypatch.setattr(db, "_terminate_process_tree", killed.append)
    with pytest.raises(db.BuilderDispatchError, match="timed out"):
        db.dispatch_builder(
            repo_root=tmp_path, lane=lane,
            prompt="x", codex_bin="codex", timeout_seconds=60,
        )
    assert killed == [spawned["proc"]]
    assert list(tempdir.iterdir()) == []


def test_spawn_uses_new_process_group(tmp_path: Path, monkeypatch):
    """Finding 2: the builder spawns in its own process group so
    _terminate_process_tree can signal the whole tree."""
    lane = _lane(tmp_path)
    _identity_resolver(monkeypatch)
    fake_popen, calls = _fake_popen_factory(
        {"summary": "ok", "pushback": None, "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="x", codex_bin="codex", timeout_seconds=60,
    )
    kwargs = calls["kwargs"]
    if sys.platform == "win32":
        assert kwargs.get("creationflags") == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs.get("start_new_session") is True


def test_env_is_scrubbed(tmp_path: Path, monkeypatch):
    lane = _lane(tmp_path)
    _identity_resolver(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    fake_popen, calls = _fake_popen_factory(
        {"summary": "ok", "pushback": None, "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="x", codex_bin="codex", timeout_seconds=60,
    )
    assert "OPENAI_API_KEY" not in calls["kwargs"]["env"]


def test_scrub_uses_shared_all_provider_keys(tmp_path: Path, monkeypatch):
    """v2 hardening: the builder runs effectively unsandboxed with network
    access (decisive experiment), so it must scrub EVERY provider's keys -
    the shared ALL_PROVIDER_SCRUBBED_ENV_KEYS from _dispatch_base, the same
    union the verification gate uses (no local copy to drift)."""
    from consensus_mcp._dispatch_base import ALL_PROVIDER_SCRUBBED_ENV_KEYS

    assert db.ALL_PROVIDER_SCRUBBED_ENV_KEYS is ALL_PROVIDER_SCRUBBED_ENV_KEYS
    # the union must cover all four providers, not just codex's OPENAI key
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "XAI_API_KEY", "GROK_API_KEY", "KIMI_API_KEY"):
        assert key in ALL_PROVIDER_SCRUBBED_ENV_KEYS
    lane = _lane(tmp_path)
    _identity_resolver(monkeypatch)
    for key in ALL_PROVIDER_SCRUBBED_ENV_KEYS:
        monkeypatch.setenv(key, "leak-" + key)
    fake_popen, calls = _fake_popen_factory(
        {"summary": "ok", "pushback": None, "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="x", codex_bin="codex", timeout_seconds=60,
    )
    for key in ALL_PROVIDER_SCRUBBED_ENV_KEYS:
        assert key not in calls["kwargs"]["env"]


# ---- Q2 hardening (consult iteration-architect-hardening-2026-06-11) ----


def test_builder_env_default_deny(tmp_path: Path, monkeypatch):
    """Default-deny: unknown and credential names are absent without any
    pattern knowledge; baseline operational vars survive."""
    lane = _lane(tmp_path)
    _identity_resolver(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_leak")
    monkeypatch.setenv("CUSTOM_CORP_SECRET", "leak")
    monkeypatch.setenv("NEW_VENDOR_TOKEN", "leak")
    fake_popen, calls = _fake_popen_factory(
        {"summary": "ok", "pushback": None, "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="x", codex_bin="codex", timeout_seconds=60,
    )
    env = calls["kwargs"]["env"]
    assert "GITHUB_TOKEN" not in env
    assert "CUSTOM_CORP_SECRET" not in env
    assert "NEW_VENDOR_TOKEN" not in env
    assert "PATH" in env


def test_builder_env_allow_extension_and_hard_floor(tmp_path: Path,
                                                    monkeypatch):
    """CONSENSUS_MCP_BUILDER_ENV_ALLOW admits project vars; the hard-floor
    credential set can never be allowed through it."""
    lane = _lane(tmp_path)
    _identity_resolver(monkeypatch)
    monkeypatch.setenv("FOO_PROJECT_VAR", "v")
    monkeypatch.setenv("GH_TOKEN", "ghp_leak")
    monkeypatch.setenv("CONSENSUS_MCP_BUILDER_ENV_ALLOW",
                       "FOO_PROJECT_VAR,GH_TOKEN")
    fake_popen, calls = _fake_popen_factory(
        {"summary": "ok", "pushback": None, "notes": ""}
    )
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    db.dispatch_builder(
        repo_root=tmp_path, lane=lane,
        prompt="x", codex_bin="codex", timeout_seconds=60,
    )
    env = calls["kwargs"]["env"]
    assert env.get("FOO_PROJECT_VAR") == "v"
    assert "GH_TOKEN" not in env
