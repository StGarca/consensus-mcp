"""Tests for the write-enabled builder dispatch canon (consult Q6)."""
from __future__ import annotations

from pathlib import Path

from consensus_mcp.validators.validate_builder_dispatch import (
    validate_builder_argv,
)


def _lane(tmp_path: Path) -> Path:
    lane = tmp_path / ".consensus" / "architect" / "g1" / "lane"
    lane.mkdir(parents=True)
    return lane


def _good_argv(lane: Path) -> list[str]:
    return [
        "codex", "exec", "--skip-git-repo-check",
        "--cd", str(lane),
        "--sandbox", "workspace-write",
        "--output-schema", "schema.json",
        "-o", "out.json", "-",
    ]


def test_canonical_shape_passes(tmp_path: Path):
    lane = _lane(tmp_path)
    assert validate_builder_argv(_good_argv(lane), tmp_path) == []


def test_rejects_git_token(tmp_path: Path):
    lane = _lane(tmp_path)
    argv = _good_argv(lane) + ["git"]
    violations = validate_builder_argv(argv, tmp_path)
    assert any("git" in v for v in violations)


def test_rejects_read_only_sandbox(tmp_path: Path):
    lane = _lane(tmp_path)
    argv = _good_argv(lane)
    argv[argv.index("workspace-write")] = "read-only"
    violations = validate_builder_argv(argv, tmp_path)
    assert any("workspace-write" in v for v in violations)


def test_rejects_danger_sandbox(tmp_path: Path):
    lane = _lane(tmp_path)
    argv = _good_argv(lane)
    argv[argv.index("workspace-write")] = "danger-full-access"
    violations = validate_builder_argv(argv, tmp_path)
    assert any("workspace-write" in v for v in violations)


def test_rejects_cd_outside_lane(tmp_path: Path):
    lane = _lane(tmp_path)
    argv = _good_argv(lane)
    argv[argv.index("--cd") + 1] = str(tmp_path)  # repo root, not a lane
    violations = validate_builder_argv(argv, tmp_path)
    assert any("--cd" in v for v in violations)


def test_rejects_cd_symlink_escape(tmp_path: Path):
    lane = _lane(tmp_path)
    outside = tmp_path.parent / "outside-lane"
    outside.mkdir(exist_ok=True)
    link = lane.parent / "lane-link"
    link.symlink_to(outside, target_is_directory=True)
    argv = _good_argv(lane)
    argv[argv.index("--cd") + 1] = str(link)
    violations = validate_builder_argv(argv, tmp_path)
    assert violations  # resolved path escapes .consensus/architect/*/lane


def test_rejects_wrong_binary(tmp_path: Path):
    lane = _lane(tmp_path)
    argv = _good_argv(lane)
    argv[0] = "bash"
    violations = validate_builder_argv(argv, tmp_path)
    assert any("binary" in v for v in violations)
