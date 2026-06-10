"""Tests for _architect_paths (single source of truth for workflow-D names)."""
from __future__ import annotations

from pathlib import Path

import yaml

from consensus_mcp import _architect_paths as ap


def test_goal_dir_layout(tmp_path: Path):
    g = ap.goal_dir(tmp_path, "my-goal")
    assert g == tmp_path / ".consensus" / "architect" / "my-goal"
    assert ap.lane_dir(g) == g / "lane"
    assert ap.cycle_dir(g, 3) == g / "cycle-3"


def test_goal_id_rejects_path_tricks(tmp_path: Path):
    import pytest
    for bad in ("", "a/b", "..", "a\\b", ".hidden"):
        with pytest.raises(ap.ArchitectPathError):
            ap.goal_dir(tmp_path, bad)


def test_current_cycle_empty_is_one(tmp_path: Path):
    g = ap.goal_dir(tmp_path, "g1")
    g.mkdir(parents=True)
    assert ap.current_cycle(g) == 1


def test_current_cycle_advances_with_rulings(tmp_path: Path):
    g = ap.goal_dir(tmp_path, "g1")
    c1 = ap.cycle_dir(g, 1)
    c1.mkdir(parents=True)
    # cycle 1 closed by a revise ruling -> current is 2
    (c1 / ap.RULING_FILENAME).write_text(
        "disposition: revise\n", encoding="utf-8"
    )
    assert ap.current_cycle(g) == 2
    # cycle 2 has a build-result but no ruling -> still cycle 2
    c2 = ap.cycle_dir(g, 2)
    c2.mkdir(parents=True)
    (c2 / ap.BUILD_RESULT_FILENAME).write_text("summary: x\n", encoding="utf-8")
    assert ap.current_cycle(g) == 2


def test_seal_artifact_roundtrip(tmp_path: Path):
    out = tmp_path / "spec.yaml"
    sealed = ap.seal_artifact(out, {"kind": "spec", "body": "hello"})
    on_disk = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert on_disk["kind"] == "spec"
    assert on_disk["sealed_at_utc"].endswith("Z")
    assert on_disk["payload_sha256"] == sealed["payload_sha256"]
    assert len(sealed["payload_sha256"]) == 64


def test_spec_paths_and_latest_rev(tmp_path: Path):
    g = ap.goal_dir(tmp_path, "g1")
    g.mkdir(parents=True)
    assert ap.spec_path(g) == g / "spec.yaml"
    (g / "spec.yaml").write_text("v: 0\n", encoding="utf-8")
    assert ap.latest_spec_path(g) == g / "spec.yaml"
    (g / "spec-rev-1.yaml").write_text("v: 1\n", encoding="utf-8")
    (g / "spec-rev-2.yaml").write_text("v: 2\n", encoding="utf-8")
    assert ap.latest_spec_path(g) == g / "spec-rev-2.yaml"
