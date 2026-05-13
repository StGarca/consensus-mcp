"""Unit tests for consensus_mcp._import_parent_history.

Uses tmp_path fixtures rather than the real parent project; verifies the
mirror layout, sha256_tree integrity, manifest shape, and idempotency.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from consensus_mcp import _import_parent_history as iph


def _make_fake_parent(root: Path) -> Path:
    """Build a minimal fake parent agent-loop/ tree under root."""
    al = root / "agent-loop"
    (al / "active" / "iteration-0000").mkdir(parents=True)
    (al / "active" / "iteration-0000" / "goal_packet.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (al / "active" / "iteration-0001-foo").mkdir(parents=True)
    (al / "active" / "iteration-0001-foo" / "outcome.yaml").write_text("done: true\n", encoding="utf-8")
    (al / "archive" / "review-passes").mkdir(parents=True)
    (al / "archive" / "review-passes" / "2026-01-01-iter-0000-pass.yaml").write_text("findings: []\n", encoding="utf-8")
    return al


def test_import_creates_layout(tmp_path):
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_al = _make_fake_parent(parent_root)
    target = tmp_path / "import-target"
    manifest = iph.import_history(parent_al, target)
    assert (target / "README.md").exists()
    assert (target / "source-manifest.yaml").exists()
    assert (target / "active-iterations" / "iteration-0000" / "goal_packet.yaml").exists()
    assert (target / "active-iterations" / "iteration-0001-foo" / "outcome.yaml").exists()
    assert (target / "archive-review-passes" / "2026-01-01-iter-0000-pass.yaml").exists()


def test_manifest_shape(tmp_path):
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_al = _make_fake_parent(parent_root)
    target = tmp_path / "import-target"
    manifest = iph.import_history(parent_al, target)
    assert manifest["schema_version"] == 1
    assert manifest["source"]["repo"] == "ebook2audiobook-26.4.16"
    # 2 iteration_dir entries + 1 archive_pass entry
    iter_dirs = [e for e in manifest["entries"] if e["kind"] == "iteration_dir"]
    archive_passes = [e for e in manifest["entries"] if e["kind"] == "archive_pass"]
    assert len(iter_dirs) == 2
    assert len(archive_passes) == 1
    # sha256_tree is non-empty and stable.
    for e in iter_dirs:
        assert "sha256_tree" in e and len(e["sha256_tree"]) == 64
    for e in archive_passes:
        assert "sha256_content" in e and len(e["sha256_content"]) == 64


def test_dry_run_does_not_create_target(tmp_path):
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_al = _make_fake_parent(parent_root)
    target = tmp_path / "import-target"
    manifest = iph.import_history(parent_al, target, dry_run=True)
    assert not target.exists() or not any(target.iterdir())
    # But manifest is still computed.
    assert len(manifest["entries"]) == 3  # 2 iter dirs + 1 archive pass


def test_refuses_existing_target_without_force(tmp_path):
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_al = _make_fake_parent(parent_root)
    target = tmp_path / "import-target"
    target.mkdir()
    (target / "existing.txt").write_text("preexisting", encoding="utf-8")
    with pytest.raises(RuntimeError, match="non-empty"):
        iph.import_history(parent_al, target, force=False)


def test_force_overwrites_existing_target(tmp_path):
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_al = _make_fake_parent(parent_root)
    target = tmp_path / "import-target"
    target.mkdir()
    (target / "existing.txt").write_text("preexisting", encoding="utf-8")
    iph.import_history(parent_al, target, force=True)
    # Old content gone, new content present.
    assert not (target / "existing.txt").exists()
    assert (target / "README.md").exists()


def test_refuses_missing_parent(tmp_path):
    nonexistent = tmp_path / "does-not-exist"
    target = tmp_path / "out"
    with pytest.raises(RuntimeError, match="does not exist"):
        iph.import_history(nonexistent, target)


def test_idempotent_sha256_tree(tmp_path):
    """Re-importing the same parent into a new target produces identical sha256_tree."""
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_al = _make_fake_parent(parent_root)
    t1 = tmp_path / "out1"
    t2 = tmp_path / "out2"
    m1 = iph.import_history(parent_al, t1)
    m2 = iph.import_history(parent_al, t2)
    # Use Path().name so the comparison is platform-agnostic (Windows sep is \).
    s1 = sorted([(Path(e["target"]).name, e.get("sha256_tree") or e.get("sha256_content")) for e in m1["entries"]])
    s2 = sorted([(Path(e["target"]).name, e.get("sha256_tree") or e.get("sha256_content")) for e in m2["entries"]])
    assert s1 == s2


def test_manifest_yaml_loadable(tmp_path):
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_al = _make_fake_parent(parent_root)
    target = tmp_path / "out"
    iph.import_history(parent_al, target)
    loaded = yaml.safe_load((target / "source-manifest.yaml").read_text(encoding="utf-8"))
    assert loaded["schema_version"] == 1
    assert isinstance(loaded["entries"], list)
