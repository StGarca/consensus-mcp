"""Tests for the write-enabled builder dispatch canon (consult Q6)."""
from __future__ import annotations

from pathlib import Path

import pytest

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
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
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


def test_windows_binary_variants_pass(tmp_path: Path):
    # R1: _dispatch_codex._resolve_codex_bin (v1.10.3 Windows hardening)
    # resolves a bare 'codex' to codex.cmd / codex.exe / codex.bat /
    # codex.ps1 on PATH (npm ships the .cmd shim, preferred over .ps1).
    # The canon must accept every shape the canonical dispatch path emits
    # or no legitimate Windows builder dispatch can pass.
    lane = _lane(tmp_path)
    for name in ("codex", "codex.exe", "codex.cmd", "codex.bat", "codex.ps1"):
        argv = _good_argv(lane)
        argv[0] = name
        assert validate_builder_argv(argv, tmp_path) == [], name


def test_rejects_unknown_binary_extension(tmp_path: Path):
    # R1 stays an allowlist of extensions, not a bare-stem match.
    lane = _lane(tmp_path)
    argv = _good_argv(lane)
    argv[0] = "codex.sh"
    violations = validate_builder_argv(argv, tmp_path)
    assert any("binary" in v for v in violations)


def test_rejects_empty_argv(tmp_path: Path):
    assert validate_builder_argv([], tmp_path) == ["empty argv"]


def test_rejects_nonexistent_cd(tmp_path: Path):
    # R4 resolve(strict=True): a --cd that does not exist on disk must
    # fail closed, even if its textual shape looks like a lane path.
    lane = _lane(tmp_path)
    argv = _good_argv(lane)
    missing = tmp_path / ".consensus" / "architect" / "g2" / "lane"
    argv[argv.index("--cd") + 1] = str(missing)
    violations = validate_builder_argv(argv, tmp_path)
    assert any("--cd" in v for v in violations)


def test_rejects_duplicate_sandbox(tmp_path: Path):
    # R3 'exactly one': two --sandbox flags must be rejected even when
    # both values are 'workspace-write'.
    lane = _lane(tmp_path)
    argv = _good_argv(lane) + ["--sandbox", "workspace-write"]
    violations = validate_builder_argv(argv, tmp_path)
    assert any("--sandbox" in v for v in violations)


def test_rejects_equals_form_sandbox(tmp_path: Path):
    # Pin the fail-closed behavior: --sandbox=workspace-write is NOT canon
    # (only the space-separated form is). A future equals-form-tolerant
    # _flag_values refactor must not silently weaken R3.
    lane = _lane(tmp_path)
    argv = _good_argv(lane)
    i = argv.index("--sandbox")
    argv[i:i + 2] = ["--sandbox=workspace-write"]
    violations = validate_builder_argv(argv, tmp_path)
    assert any("--sandbox" in v for v in violations)


def test_rejects_equals_form_cd(tmp_path: Path):
    # Same pin for R4: --cd=<lane> is NOT canon.
    lane = _lane(tmp_path)
    argv = _good_argv(lane)
    i = argv.index("--cd")
    argv[i:i + 2] = [f"--cd={lane}"]
    violations = validate_builder_argv(argv, tmp_path)
    assert any("--cd" in v for v in violations)


def test_rejects_shell_metacharacters(tmp_path: Path):
    # R6 defense-in-depth: every metacharacter class in _SHELL_META_RE.
    lane = _lane(tmp_path)
    for bad in (
        "out.json;rm",
        "$(whoami)",
        "a|b",
        "a&b",
        "`x`",
        "a>b",
        "a<b",
        "a\nb",
    ):
        argv = _good_argv(lane)
        argv[argv.index("out.json")] = bad
        violations = validate_builder_argv(argv, tmp_path)
        assert any("shell metacharacter" in v for v in violations), bad
