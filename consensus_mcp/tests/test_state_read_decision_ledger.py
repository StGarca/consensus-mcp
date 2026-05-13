"""Tests for state.read_decision_ledger MCP tool.

iter-0026 (Phase B step 1 of iter-0024 plan): added when migrating this
tool to use `_paths.state_root()` instead of a module-level REPO_ROOT
capture. Demonstrates the migration win: `monkeypatch.setenv()` after
import now redirects the ledger lookup without needing the iter-0019
`_isolate_archive_root` helper.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from consensus_mcp.tools import state_read_decision_ledger as tool


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level cache between tests so prior runs don't leak."""
    tool._CACHE["sha256"] = None
    tool._CACHE["yaml_text"] = None
    tool._CACHE["mtime_ns"] = None
    yield
    tool._CACHE["sha256"] = None
    tool._CACHE["yaml_text"] = None
    tool._CACHE["mtime_ns"] = None


def _write_ledger(state_root: Path, content: dict) -> Path:
    ledger_dir = state_root / "state"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / "disposition-ledger.yaml"
    ledger_path.write_text(yaml.safe_dump(content), encoding="utf-8")
    return ledger_path


def test_read_missing_ledger_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    result = tool.handle()
    assert "error" in result
    assert "ledger not found" in result["error"]


def test_read_returns_yaml_and_sha(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    _write_ledger(tmp_path, {"entries": [{"id": "iter-0001"}]})
    result = tool.handle()
    assert "error" not in result
    assert "entries" in result["ledger_yaml"]
    assert len(result["ledger_sha256"]) == 64


def test_lazy_state_root_resolution(tmp_path, monkeypatch):
    """The iter-0026 win: changing CONSENSUS_MCP_STATE_ROOT between calls
    redirects the lookup without needing _isolate_archive_root.

    Before iter-0026, module-level REPO_ROOT capture would have frozen the
    ledger path at import time. This test would have failed."""
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()

    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(first))
    _write_ledger(first, {"loc": "first"})
    r1 = tool.handle()
    assert "first" in r1["ledger_yaml"]

    # Reset cache + flip env (the iter-0019 gotcha that the migration fixes).
    tool._CACHE["sha256"] = None
    tool._CACHE["yaml_text"] = None
    tool._CACHE["mtime_ns"] = None

    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(second))
    _write_ledger(second, {"loc": "second"})
    r2 = tool.handle()
    assert "second" in r2["ledger_yaml"]
    assert r1["ledger_sha256"] != r2["ledger_sha256"]


def test_cache_returns_same_payload_on_unchanged_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    _write_ledger(tmp_path, {"key": "value"})
    r1 = tool.handle()
    r2 = tool.handle()
    assert r1["ledger_sha256"] == r2["ledger_sha256"]
    assert r1["ledger_yaml"] == r2["ledger_yaml"]


def test_register_attaches_tool():
    from consensus_mcp.tool_registry import ToolRegistry
    registry = ToolRegistry()
    tool.register(registry)
    listed = registry.list_tools()
    names = {t["name"] for t in listed}
    assert "state.read_decision_ledger" in names
