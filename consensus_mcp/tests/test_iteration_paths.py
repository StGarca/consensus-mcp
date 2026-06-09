"""Tests for consensus_mcp._iteration_paths - the single source of truth for
iteration artifact names (F7 root fix).

Covers:
  - review_family round-trips for canonical, pass-keyed, and round-keyed names.
  - pass_review_family matches ONLY pass-form names (original case kept).
  - canonical vs pass glob disjointness; all_review_files is their union.
  - iteration_outcome_path / review_packet_path / canonical_review_name shapes.
"""
from __future__ import annotations

from pathlib import Path

from consensus_mcp import _iteration_paths as ip


# ---------------------------------------------------------------------------
# review_family round-trips
# ---------------------------------------------------------------------------

def test_review_family_canonical_name():
    assert ip.review_family("codex-review.yaml") == "codex"


def test_review_family_pass_keyed_name():
    assert ip.review_family("codex-review-pass-001.yaml") == "codex"


def test_review_family_round_keyed_name():
    assert ip.review_family("gemini-review-2.yaml") == "gemini"


def test_review_family_non_greedy():
    # `<fam>` is non-greedy: kimi-review-4.yaml -> 'kimi', not 'kimi-review-4'.
    assert ip.review_family("kimi-review-4.yaml") == "kimi"


def test_review_family_rejects_non_review_artifacts():
    assert ip.review_family("iteration-outcome.yaml") is None
    assert ip.review_family("review-packet.yaml") is None
    assert ip.review_family("goal_packet.yaml") is None


def test_review_family_roundtrip_through_canonical_name():
    for fam in ("codex", "gemini", "kimi", "grok"):
        assert ip.review_family(ip.canonical_review_name(fam)) == fam


# ---------------------------------------------------------------------------
# pass_review_family (seal-canonicalization variant)
# ---------------------------------------------------------------------------

def test_pass_review_family_matches_only_pass_form():
    assert ip.pass_review_family("codex-review-pass-001.yaml") == "codex"
    assert ip.pass_review_family("gemini-review-2.yaml") == "gemini"
    assert ip.pass_review_family("codex-review.yaml") is None


def test_pass_review_family_preserves_case():
    # Unlike review_family, the seal variant does NOT lowercase: the canonical
    # filename is derived from the captured family verbatim.
    assert ip.pass_review_family("Codex-review-pass-001.yaml") == "Codex"
    assert ip.review_family("Codex-review-pass-001.yaml") == "codex"


# ---------------------------------------------------------------------------
# glob helpers: disjointness + union
# ---------------------------------------------------------------------------

def _seed_iteration(tmp_path: Path) -> Path:
    for name in (
        "codex-review.yaml",
        "gemini-review.yaml",
        "codex-review-pass-001.yaml",
        "gemini-review-2.yaml",
        "iteration-outcome.yaml",
        "review-packet.yaml",
        "goal_packet.yaml",
    ):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    return tmp_path


def test_canonical_and_pass_globs_are_disjoint(tmp_path: Path):
    iter_dir = _seed_iteration(tmp_path)
    canonical = {p.name for p in ip.canonical_review_glob(iter_dir)}
    passes = {p.name for p in ip.pass_review_glob(iter_dir)}
    assert canonical == {"codex-review.yaml", "gemini-review.yaml"}
    assert passes == {"codex-review-pass-001.yaml", "gemini-review-2.yaml"}
    assert canonical.isdisjoint(passes)


def test_all_review_files_is_union_of_canonical_and_pass(tmp_path: Path):
    iter_dir = _seed_iteration(tmp_path)
    all_names = {p.name for p in ip.all_review_files(iter_dir)}
    canonical = {p.name for p in ip.canonical_review_glob(iter_dir)}
    passes = {p.name for p in ip.pass_review_glob(iter_dir)}
    assert all_names == canonical | passes
    # Non-review artifacts never leak into the review globs.
    assert "iteration-outcome.yaml" not in all_names
    assert "review-packet.yaml" not in all_names


def test_glob_helpers_return_sorted_lists(tmp_path: Path):
    iter_dir = _seed_iteration(tmp_path)
    for fn in (ip.canonical_review_glob, ip.pass_review_glob, ip.all_review_files):
        result = fn(iter_dir)
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# fixed artifact paths
# ---------------------------------------------------------------------------

def test_fixed_artifact_paths(tmp_path: Path):
    assert ip.iteration_outcome_path(tmp_path) == tmp_path / "iteration-outcome.yaml"
    assert ip.review_packet_path(tmp_path) == tmp_path / "review-packet.yaml"
    assert ip.canonical_review_name("codex") == "codex-review.yaml"
