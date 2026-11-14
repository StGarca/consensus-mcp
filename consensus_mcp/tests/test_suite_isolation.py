"""Regression coverage for the suite's repository-state pollution guard."""
from __future__ import annotations

import pytest

from consensus_mcp.tests.conftest import _assert_state_unchanged, _state_snapshot


@pytest.mark.parametrize("change", ["file", "directory", "modify", "delete"])
def test_repository_state_guard_rejects_changes(tmp_path, change):
    state = tmp_path / "consensus-state"
    state.mkdir()
    existing = state / "fixture.yaml"
    existing.write_text("original", encoding="utf-8")
    before = _state_snapshot(state)
    if change == "file":
        leaked = state / "archive" / "review-passes" / "index.lock"
        leaked.parent.mkdir(parents=True)
        leaked.write_bytes(b"\0")
    elif change == "directory":
        (state / "scratch-kimi-test").mkdir()
    elif change == "modify":
        existing.write_text("changed", encoding="utf-8")
    else:
        existing.unlink()

    with pytest.raises(AssertionError, match="Tests dirtied repository consensus-state/"):
        _assert_state_unchanged(state, before)


def test_repository_state_guard_preserves_existing_fixtures(tmp_path):
    (tmp_path / "fixture.yaml").write_text("original", encoding="utf-8")
    _assert_state_unchanged(tmp_path, _state_snapshot(tmp_path))


def test_repository_state_guard_rejects_new_empty_state_root(tmp_path):
    state = tmp_path / "consensus-state"
    before = _state_snapshot(state)
    state.mkdir()
    with pytest.raises(AssertionError, match="Tests dirtied repository consensus-state/"):
        _assert_state_unchanged(state, before)
