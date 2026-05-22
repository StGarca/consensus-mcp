"""Tests for the follow-up completeness gate (consensus-mcp 1.16.1).

Decisive test: a task declaring action_class=version_bump cannot mint a delivery
token while {tag, github_release, changelog_entry} are unresolved — proving
"merge a version bump but skip the release" is mechanically blocked.
"""
from pathlib import Path

import pytest

from consensus_mcp import _followup_completeness as fc
from consensus_mcp import _delivery_readiness as dr


def _sealed_iter(repo_root: Path, ref: str) -> None:
    d = repo_root / "consensus-state" / "active" / ref
    d.mkdir(parents=True, exist_ok=True)
    (d / "iteration-outcome.yaml").write_text("closing_state: quorum_close_passed\n", encoding="utf-8")


def _artifact(repo_root: Path) -> Path:
    p = repo_root / "art.txt"
    p.write_text("hello", encoding="utf-8")
    return p


def test_map_version_bump_requires_tag_release_changelog():
    req = fc.required_followups(["version_bump"])
    assert "tag" in req and "github_release" in req and "changelog_entry" in req


def test_open_followups_respects_resolved_and_deferred_with_reason():
    ledger = {"resolved": ["tag"], "deferred": [{"item": "changelog_entry", "reason": "next patch"}]}
    missing = fc.open_followups(["version_bump"], ledger)
    assert missing == ["github_release"]  # tag resolved, changelog deferred-with-reason
    # a defer WITHOUT a reason does not count as satisfied
    ledger2 = {"resolved": [], "deferred": [{"item": "tag"}]}
    assert "tag" in fc.open_followups(["version_bump"], ledger2)


def test_mint_refused_with_open_followups(tmp_path):
    _sealed_iter(tmp_path, "iteration-x")
    art = _artifact(tmp_path)
    with pytest.raises(dr.DeliveryReadinessError, match="unresolved required follow-ups"):
        dr.mint_delivery_token(art, design_consensus_ref="iteration-x",
                               vetted_by=["codex", "gemini"], action_classes=["version_bump"],
                               repo_root=tmp_path)


def test_mint_ok_when_followups_resolved(tmp_path):
    _sealed_iter(tmp_path, "iteration-x")
    art = _artifact(tmp_path)
    fc.write_ledger(str(art), tmp_path, {
        "action_classes": ["version_bump"],
        "resolved": ["tag", "github_release", "changelog_entry"], "deferred": []})
    tok = dr.mint_delivery_token(art, design_consensus_ref="iteration-x",
                                 vetted_by=["codex", "gemini"], action_classes=["version_bump"],
                                 repo_root=tmp_path)
    assert tok["followups_resolved"] is True and tok["open_followups"] == []
    assert dr.verify_delivery_token(art, repo_root=tmp_path)["ok"] is True


def test_mint_ok_when_deferred_with_reason(tmp_path):
    _sealed_iter(tmp_path, "iteration-x")
    art = _artifact(tmp_path)
    fc.write_ledger(str(art), tmp_path, {
        "action_classes": ["version_bump"],
        "resolved": ["tag", "github_release"],
        "deferred": [{"item": "changelog_entry", "reason": "folded into next release notes"}]})
    tok = dr.mint_delivery_token(art, design_consensus_ref="iteration-x",
                                 vetted_by=["codex", "gemini"], action_classes=["version_bump"],
                                 repo_root=tmp_path)
    assert tok["followups_resolved"] is True


def test_no_action_class_is_unaffected(tmp_path):
    """Tasks without a declared action_class behave exactly as 1.16.0."""
    _sealed_iter(tmp_path, "iteration-x")
    art = _artifact(tmp_path)
    tok = dr.mint_delivery_token(art, design_consensus_ref="iteration-x",
                                 vetted_by=["codex", "gemini"], repo_root=tmp_path)
    assert tok["followups_resolved"] is True and tok["open_followups"] == []
