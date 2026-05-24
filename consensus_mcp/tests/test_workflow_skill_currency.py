from pathlib import Path

import consensus_mcp._init_wizard as wiz


def _skill_text():
    p = (Path(wiz.__file__).parent / "claude_extensions" / "skills"
         / "consensus-workflow" / "SKILL.md")
    return p.read_text(encoding="utf-8")


def test_release_currency_steps_documented():
    """The release cut-sequence MUST codify the install-currency steps that
    otherwise live only in operator memory (stale-pipx failure mode)."""
    t = _skill_text().lower()
    assert "pipx install --force" in t
    assert "--install-claude-code --force" in t
    # version-asserting smoke (not just 'binary runs')
    assert "version" in t and "smoke" in t


def test_dual_path_and_host_peer_documented():
    t = _skill_text()
    assert "Path A" in t and "Path B" in t
    assert "host_peer_review_yaml" in t
    assert "run_iteration" in t


def test_consensus_gate_caveat_documented():
    """The runbook must flag that the .consensus enforcement gate is project-
    scoped (inactive where there's no .consensus/config.yaml)."""
    t = _skill_text()
    assert ".consensus" in t
