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
    # "abc\n": re.match with a $ anchor accepts a trailing newline, which
    # embeds a newline in the directory name (mkdir-fatal on Windows);
    # fullmatch must reject it.
    for bad in ("", "a/b", "..", "a\\b", ".hidden", "abc\n", "a\nb"):
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


def test_seal_artifact_strip_and_rehash_invariant(tmp_path: Path):
    """Stripping the two stamp fields and re-hashing with the established
    spec-section-7 formula (tools/review_write_and_seal._canonical_yaml_sha256)
    must reproduce payload_sha256 -- including for the normal
    load-sealed-file -> revise -> re-seal flow for spec-rev-N.yaml, where the
    input payload already carries stale sealed_at_utc/payload_sha256 stamps.
    """
    from consensus_mcp.tools.review_write_and_seal import _canonical_yaml_sha256

    first = ap.seal_artifact(tmp_path / "spec.yaml", {"kind": "spec", "v": 1})
    body = {
        k: v
        for k, v in first.items()
        if k not in ("sealed_at_utc", "payload_sha256")
    }
    assert _canonical_yaml_sha256(body) == first["payload_sha256"]

    # re-seal flow: the already-sealed dict (stamps and all) is revised and
    # sealed again; stale stamps must NOT be hashed into the new seal.
    revised = dict(first, v=2)
    resealed = ap.seal_artifact(tmp_path / "spec-rev-1.yaml", revised)
    assert "sealed_at_utc" in resealed and "payload_sha256" in resealed
    body = {
        k: v
        for k, v in resealed.items()
        if k not in ("sealed_at_utc", "payload_sha256")
    }
    assert _canonical_yaml_sha256(body) == resealed["payload_sha256"]

    # content identity: re-sealing the SAME substantive payload (carrying the
    # old stamps) reproduces the same payload_sha256 as the first seal.
    resealed_same = ap.seal_artifact(tmp_path / "spec-rev-2.yaml", dict(first))
    assert resealed_same["payload_sha256"] == first["payload_sha256"]


def test_spec_paths_and_latest_rev(tmp_path: Path):
    g = ap.goal_dir(tmp_path, "g1")
    g.mkdir(parents=True)
    assert ap.spec_path(g) == g / "spec.yaml"
    (g / "spec.yaml").write_text("v: 0\n", encoding="utf-8")
    assert ap.latest_spec_path(g) == g / "spec.yaml"
    (g / "spec-rev-1.yaml").write_text("v: 1\n", encoding="utf-8")
    (g / "spec-rev-2.yaml").write_text("v: 2\n", encoding="utf-8")
    assert ap.latest_spec_path(g) == g / "spec-rev-2.yaml"


def test_goal_id_rejects_windows_reserved_and_trailing_dot(tmp_path: Path):
    import pytest
    for bad in ("CON", "con", "nul.txt", "com1", "goal."):
        with pytest.raises(ap.ArchitectPathError):
            ap.goal_dir(tmp_path, bad)


def test_cycle_dir_rejects_non_int(tmp_path: Path):
    import pytest
    g = ap.goal_dir(tmp_path, "g1")
    for bad in (0, -1, 2.9, True, "3"):
        with pytest.raises(ap.ArchitectPathError):
            ap.cycle_dir(g, bad)


def test_current_cycle_accept_ruling_stays(tmp_path: Path):
    g = ap.goal_dir(tmp_path, "g1")
    c1 = ap.cycle_dir(g, 1)
    c1.mkdir(parents=True)
    (c1 / ap.RULING_FILENAME).write_text(
        "disposition: accept\n", encoding="utf-8"
    )
    assert ap.current_cycle(g) == 1


def test_current_cycle_double_digit_ordering(tmp_path: Path):
    g = ap.goal_dir(tmp_path, "g1")
    for n in (2, 10):
        ap.cycle_dir(g, n).mkdir(parents=True)
    (ap.cycle_dir(g, 2) / ap.RULING_FILENAME).write_text(
        "disposition: revise\n", encoding="utf-8"
    )
    # cycle-10 is highest (int compare, not lexicographic) and open
    assert ap.current_cycle(g) == 10


def test_current_cycle_ignores_stray_file(tmp_path: Path):
    g = ap.goal_dir(tmp_path, "g1")
    g.mkdir(parents=True)
    (g / "cycle-5").write_text("not a dir\n", encoding="utf-8")
    assert ap.current_cycle(g) == 1


def test_latest_spec_double_digit_ordering(tmp_path: Path):
    g = ap.goal_dir(tmp_path, "g1")
    g.mkdir(parents=True)
    for n in (2, 10):
        (g / f"spec-rev-{n}.yaml").write_text(f"v: {n}\n", encoding="utf-8")
    assert ap.latest_spec_path(g) == g / "spec-rev-10.yaml"
