"""iter-0038 regression tests — _author_review_packet repo_root containment.

Closes codex-rev-001 (HIGH) from iteration-audit-2026-05-11-security:
operator-supplied `files` paths must be confined to repo_root. Symmetric
to v1.10.5 hardening in _dispatch_codex._normalize_relative_to_repo.

Test matrix:
  1. inside-repo path passes (smoke)
  2. outside-repo absolute path is refused
  3. relative path with ../ traversal that escapes repo_root is refused
  4. inside-repo symlink pointing at outside-repo target is refused
     (skipped if the runtime can't create symlinks)
  5. Windows mixed-case path forms still pass (case-fold fallback)
     (skipped on non-win32)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from consensus_mcp import _author_review_packet


def _scaffold_repo_markers(repo_root: Path) -> None:
    """Create the minimal directories bundle_sha + author_review_packet need."""
    (repo_root / "consensus-state").mkdir(parents=True, exist_ok=True)
    (repo_root / "consensus_mcp").mkdir(parents=True, exist_ok=True)
    (repo_root / "consensus_mcp" / "validators").mkdir(parents=True, exist_ok=True)


def test_inside_repo_path_passes(tmp_path):
    """Smoke: an inside-repo file path embeds normally and produces review-packet.yaml."""
    _scaffold_repo_markers(tmp_path)
    inside = tmp_path / "consensus_mcp" / "demo.py"
    inside.write_text("print('hello')\n", encoding="utf-8")
    iter_dir = tmp_path / "iteration-9999-demo"

    out = _author_review_packet.author_review_packet(
        iteration_dir=iter_dir,
        files=["consensus_mcp/demo.py"],
        repo_root=tmp_path,
    )

    assert out.exists(), "review-packet.yaml must be authored"
    text = out.read_text(encoding="utf-8")
    assert "demo.py" in text
    assert "hello" in text


def test_outside_repo_absolute_path_refused(tmp_path):
    """An absolute path outside repo_root must raise OutsideRepoPathError
    BEFORE any read_text. tmp_path.parent is outside the synthetic repo_root.
    """
    _scaffold_repo_markers(tmp_path)
    # Stage a file definitively outside repo_root.
    outside = tmp_path.parent / f"{tmp_path.name}_outside_secret.txt"
    outside.write_text("SECRET\n", encoding="utf-8")
    iter_dir = tmp_path / "iteration-9999-outside-abs"

    try:
        with pytest.raises(_author_review_packet.OutsideRepoPathError) as exc:
            _author_review_packet.author_review_packet(
                iteration_dir=iter_dir,
                files=[str(outside.resolve())],
                repo_root=tmp_path,
            )
        msg = str(exc.value)
        assert "outside repo_root" in msg
        assert str(outside.resolve()) in msg or "outside_secret" in msg
        # Critically: the review-packet.yaml must NOT have been written with
        # the secret embedded. The fail-closed check runs before any read.
        rp = iter_dir / "review-packet.yaml"
        if rp.exists():
            assert "SECRET" not in rp.read_text(encoding="utf-8"), (
                "containment failure must NOT leak file contents into review-packet"
            )
    finally:
        outside.unlink(missing_ok=True)


def test_relative_traversal_with_dot_dot_refused(tmp_path):
    """A relative path with ../ that escapes repo_root must be refused.
    Resolve() collapses ../ before containment check, so the resolved path
    lands outside repo_root and OutsideRepoPathError fires.
    """
    _scaffold_repo_markers(tmp_path)
    # Stage a sibling target the traversal would reach.
    sibling = tmp_path.parent / f"{tmp_path.name}_sibling.txt"
    sibling.write_text("SIBLING\n", encoding="utf-8")
    iter_dir = tmp_path / "iteration-9999-traversal"

    # The relative path "../<sibling_name>" resolves to tmp_path.parent/<sibling_name>,
    # which is outside repo_root (tmp_path).
    traversal_rel = f"../{sibling.name}"

    try:
        with pytest.raises(_author_review_packet.OutsideRepoPathError) as exc:
            _author_review_packet.author_review_packet(
                iteration_dir=iter_dir,
                files=[traversal_rel],
                repo_root=tmp_path,
            )
        msg = str(exc.value)
        assert "outside repo_root" in msg
    finally:
        sibling.unlink(missing_ok=True)


def test_inside_repo_symlink_to_outside_refused(tmp_path):
    """An in-repo symlink whose real target is outside repo_root must be refused.
    Path.resolve() follows the symlink, then containment compares the real
    target against repo_root. Skipped if the runtime can't make symlinks
    (e.g., Windows without developer-mode + non-admin).
    """
    _scaffold_repo_markers(tmp_path)
    outside_target = tmp_path.parent / f"{tmp_path.name}_symlink_target.txt"
    outside_target.write_text("REAL_SECRET\n", encoding="utf-8")
    link_path = tmp_path / "consensus_mcp" / "leak_link.txt"
    iter_dir = tmp_path / "iteration-9999-symlink"

    try:
        try:
            link_path.symlink_to(outside_target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unsupported on this runtime: {exc}")

        try:
            with pytest.raises(_author_review_packet.OutsideRepoPathError) as exc_info:
                _author_review_packet.author_review_packet(
                    iteration_dir=iter_dir,
                    files=["consensus_mcp/leak_link.txt"],
                    repo_root=tmp_path,
                )
            assert "outside repo_root" in str(exc_info.value)
            rp = iter_dir / "review-packet.yaml"
            if rp.exists():
                assert "REAL_SECRET" not in rp.read_text(encoding="utf-8"), (
                    "symlink-to-outside must not leak target contents"
                )
        finally:
            link_path.unlink(missing_ok=True)
    finally:
        outside_target.unlink(missing_ok=True)


def test_windows_case_insensitive_passes(tmp_path):
    """iter-0033 claude-rev-003 parity: on Windows, a mixed-case form of an
    inside-repo path must not trigger false-positive containment rejection.
    Skipped on non-Windows.
    """
    if sys.platform != "win32":
        pytest.skip("Windows case-insensitivity is the only platform that needs this")

    _scaffold_repo_markers(tmp_path)
    inside = tmp_path / "consensus_mcp" / "case_demo.py"
    inside.write_text("# case-fold smoke\n", encoding="utf-8")
    iter_dir = tmp_path / "iteration-9999-case"

    # Caller hands us a path string with deliberately uppercased components.
    # Without the case-fold fallback Path.relative_to does a string compare
    # against tmp_path.resolve(), which is mixed-case, and raises ValueError.
    upper_form = str(inside).upper()
    # The author_review_packet helper joins as (repo_root / rel). Passing the
    # uppercased absolute form via the file list still triggers the same path
    # through resolve(): Path(abs).resolve() returns the case-normalized real
    # path on Windows, but if the caller's repo_root carries different casing
    # the relative_to compare can still fail. We exercise containment against
    # a mixed-case repo_root form to be sure the fallback fires.
    mixed_root = Path(str(tmp_path).swapcase())

    # Sanity: repo_root marker dir exists at the real (non-swapcased) path;
    # mixed_root is the same on-disk dir on Windows due to case-insensitivity.
    out = _author_review_packet.author_review_packet(
        iteration_dir=iter_dir,
        files=[upper_form],
        repo_root=mixed_root,
    )
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "case-fold smoke" in text
