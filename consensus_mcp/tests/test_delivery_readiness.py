"""Tests for the delivery-readiness gate (anti-self-judge enforcement, 1.16.0).

Decisive test: an artifact whose design_consensus_ref is UNSEALED cannot be
minted/verified - proving self-judged delivery is mechanically impossible.
"""
from pathlib import Path

import pytest

from consensus_mcp import _delivery_readiness as dr


def _sealed_iter(repo_root: Path, ref: str, state: str = "quorum_close_passed") -> None:
    d = repo_root / "consensus-state" / "active" / ref
    d.mkdir(parents=True, exist_ok=True)
    (d / "iteration-outcome.yaml").write_text(f"closing_state: {state}\n", encoding="utf-8")


def _artifact(repo_root: Path, name: str = "art.txt", body: str = "hello") -> Path:
    p = repo_root / name
    p.write_text(body, encoding="utf-8")
    return p


def test_mint_refused_without_sealed_ref(tmp_path):
    art = _artifact(tmp_path)
    with pytest.raises(dr.DeliveryReadinessError, match="not sealed"):
        dr.mint_delivery_token(art, design_consensus_ref="iteration-does-not-exist",
                               vetted_by=["codex", "gemini"], repo_root=tmp_path)


def test_mint_refused_with_unsealed_state(tmp_path):
    _sealed_iter(tmp_path, "iteration-x", state="in_progress")
    art = _artifact(tmp_path)
    with pytest.raises(dr.DeliveryReadinessError, match="not sealed"):
        dr.mint_delivery_token(art, design_consensus_ref="iteration-x",
                               vetted_by=["codex", "gemini"], repo_root=tmp_path)


def test_mint_refused_self_vet_only(tmp_path):
    _sealed_iter(tmp_path, "iteration-x")
    art = _artifact(tmp_path)
    with pytest.raises(dr.DeliveryReadinessError, match="non-claude"):
        dr.mint_delivery_token(art, design_consensus_ref="iteration-x",
                               vetted_by=["claude", "claude-2"], repo_root=tmp_path)


def test_mint_refused_known_flaws_without_ack(tmp_path):
    _sealed_iter(tmp_path, "iteration-x")
    art = _artifact(tmp_path)
    with pytest.raises(dr.DeliveryReadinessError, match="caveat-and-ship"):
        dr.mint_delivery_token(art, design_consensus_ref="iteration-x",
                               vetted_by=["codex", "gemini"], known_flaws=["saturated metric"],
                               repo_root=tmp_path)


def test_mint_and_verify_ok(tmp_path):
    _sealed_iter(tmp_path, "iteration-x")
    art = _artifact(tmp_path)
    tok = dr.mint_delivery_token(art, design_consensus_ref="iteration-x",
                                 vetted_by=["codex", "gemini", "kimi"], repo_root=tmp_path)
    assert tok["design_closing_state"] == "quorum_close_passed"
    res = dr.verify_delivery_token(art, repo_root=tmp_path)
    assert res["ok"] is True, res


def test_verify_fails_after_artifact_edited(tmp_path):
    _sealed_iter(tmp_path, "iteration-x")
    art = _artifact(tmp_path, body="hello")
    dr.mint_delivery_token(art, design_consensus_ref="iteration-x",
                           vetted_by=["codex", "gemini"], repo_root=tmp_path)
    art.write_text("hello EDITED", encoding="utf-8")
    res = dr.verify_delivery_token(art, repo_root=tmp_path)
    assert res["ok"] is False and "hash mismatch" in res["reason"], res


def test_verify_fails_without_token(tmp_path):
    art = _artifact(tmp_path)
    res = dr.verify_delivery_token(art, repo_root=tmp_path)
    assert res["ok"] is False and "no delivery-readiness token" in res["reason"], res


# --- path-normalization regression (field bug 2026-05-28) ----------------
# `_token_path` keyed on the raw, un-normalized path STRING, so the same file
# referenced two ways (mint/verify got a repo-RELATIVE path; close enumerates
# ABSOLUTE rglob paths) hashed to two different token filenames -> false
# `missing_delivery_tokens` at close even though a valid token existed. The fix
# canonicalizes to the repo-relative form at one root primitive.

def test_token_path_canonical_across_path_forms(tmp_path):
    """The token filename for a file must be identical whether the artifact is
    addressed by its repo-relative path or its absolute path."""
    _artifact(tmp_path, "art.txt")
    rel = Path("art.txt")
    abs_ = tmp_path / "art.txt"
    assert dr._token_path(rel, tmp_path) == dr._token_path(abs_, tmp_path)


def test_token_found_via_absolute_after_mint_relative(tmp_path, monkeypatch):
    """Mirrors the close path: mint with the repo-relative form, then look the
    token up by the absolute form (as `repo_root.rglob('*')` yields)."""
    _sealed_iter(tmp_path, "iteration-x")
    _artifact(tmp_path, "art.txt")
    monkeypatch.chdir(tmp_path)
    dr.mint_delivery_token(Path("art.txt"), design_consensus_ref="iteration-x",
                           vetted_by=["codex", "gemini"], repo_root=tmp_path)
    abs_art = tmp_path / "art.txt"
    assert dr._token_path(abs_art, tmp_path).exists()


def test_verify_ok_when_minted_relative_verified_absolute(tmp_path, monkeypatch):
    """The cousin bug: `verify` also compared the stored `artifact_path` against
    the raw string form. Mint-relative then verify-absolute must still pass."""
    _sealed_iter(tmp_path, "iteration-x")
    _artifact(tmp_path, "art.txt")
    monkeypatch.chdir(tmp_path)
    dr.mint_delivery_token(Path("art.txt"), design_consensus_ref="iteration-x",
                           vetted_by=["codex", "gemini"], repo_root=tmp_path)
    res = dr.verify_delivery_token(tmp_path / "art.txt", repo_root=tmp_path)
    assert res["ok"] is True, res
