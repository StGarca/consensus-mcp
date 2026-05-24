"""Tests for the interaction_surface declaration check (sp-optimization B1)."""
from __future__ import annotations

from consensus_mcp.validators.validate_interaction_surface import (
    validate_interaction_surface,
)


def test_missing_interaction_surface_warns():
    w = validate_interaction_surface({"goal": {}}, changed_paths=["docs/x.md"])
    assert len(w) == 1 and "missing" in w[0]


def test_reflexive_none_on_hook_change_warns():
    gp = {"interaction_surface": "none"}
    w = validate_interaction_surface(
        gp, changed_paths=["consensus_mcp/claude_extensions/hooks/consensus_pretooluse_gate.py"])
    assert len(w) == 1 and "reflexive" in w[0].lower()


def test_reflexive_none_on_dispatcher_change_warns():
    w = validate_interaction_surface(
        {"interaction_surface": []},
        changed_paths=["consensus_mcp/_dispatch_codex.py"])
    assert len(w) == 1


def test_declared_surface_on_hook_change_is_clean():
    gp = {"interaction_surface": ["PreToolUse gate", "consensus-init"]}
    w = validate_interaction_surface(
        gp, changed_paths=["consensus_mcp/claude_extensions/hooks/consensus_pretooluse_gate.py"])
    assert w == []


def test_reflexive_none_on_consensus_config_change_warns():
    """codex-rev-001: .consensus config IS governance machinery."""
    w = validate_interaction_surface(
        {"interaction_surface": "none"},
        changed_paths=["myproject/.consensus/config.yaml"])
    assert len(w) == 1 and "reflexive" in w[0].lower()


def test_genuine_none_on_doc_change_is_clean():
    w = validate_interaction_surface(
        {"interaction_surface": "none"}, changed_paths=["docs/readme.md", "CHANGELOG.md"])
    assert w == []


def test_none_with_no_changed_paths_is_clean():
    assert validate_interaction_surface({"interaction_surface": "none"}) == []
