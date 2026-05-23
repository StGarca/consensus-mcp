"""v1.21 — tests for per-project Claude Code subagent install (AGENTS-INIT).

Per converged-plan iteration-consensus-agents-init-design (decisions C + E):
`consensus init` writes two subagent definitions into the PROJECT's
.claude/agents/ during per-project bootstrap:

  - consensus-orchestrator.md       (Agent + consensus MCP tools + Read/Bash/Grep/Glob)
  - consensus-host-peer-reviewer.md (read-only: Read/Grep/Glob/Bash — NO Agent)

Mechanics mirror the .mcp.json bootstrap: honored by --dry-run ("would write"),
non-destructive skip-if-exists unless --force, and a --no-agents opt-out.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from consensus_mcp import _init_wizard as wiz


_AGENT_FILES = ("consensus-orchestrator.md", "consensus-host-peer-reviewer.md")


def _parse_frontmatter(text: str) -> dict:
    """Parse the leading YAML frontmatter block of an agent markdown file."""
    assert text.startswith("---"), "agent file must start with YAML frontmatter"
    parts = text.split("---", 2)
    assert len(parts) >= 3, "agent file must have a closed frontmatter block"
    return yaml.safe_load(parts[1])


def _init_args(**overrides):
    extra = []
    for k, v in overrides.items():
        if v is True:
            extra.append(f"--{k.replace('_', '-')}")
    return [
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        *extra,
    ]


# ---------- package-data presence ----------

def test_agent_files_ship_in_package():
    pkg_root = Path(wiz.__file__).resolve().parent
    agents = pkg_root / "claude_extensions" / "agents"
    for fname in _AGENT_FILES:
        path = agents / fname
        assert path.exists(), f"missing packaged agent file {path}"
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["name"]
        assert fm["description"]
        assert fm["tools"]


# ---------- init writes the agent files ----------

def test_init_writes_project_agents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main(_init_args())
    assert rc == 0
    agents_dir = tmp_path / ".claude" / "agents"
    for fname in _AGENT_FILES:
        assert (agents_dir / fname).exists(), f"missing {fname}"


def test_agent_frontmatter_parses_and_agent_tool_only_orchestrator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main(_init_args())
    assert rc == 0
    agents_dir = tmp_path / ".claude" / "agents"

    orch = _parse_frontmatter((agents_dir / "consensus-orchestrator.md").read_text(encoding="utf-8"))
    reviewer = _parse_frontmatter((agents_dir / "consensus-host-peer-reviewer.md").read_text(encoding="utf-8"))

    assert orch["name"] == "consensus-orchestrator"
    assert reviewer["name"] == "consensus-host-peer-reviewer"

    def _tool_set(fm):
        tools = fm["tools"]
        if isinstance(tools, str):
            return {t.strip() for t in tools.split(",") if t.strip()}
        return set(tools)

    orch_tools = _tool_set(orch)
    reviewer_tools = _tool_set(reviewer)

    # Agent tool granted ONLY to the orchestrator.
    assert "Agent" in orch_tools
    assert "Agent" not in reviewer_tools

    # Reviewer is read-only.
    assert reviewer_tools == {"Read", "Grep", "Glob", "Bash"}

    # Orchestrator carries consensus MCP tools + read/bash/grep/glob.
    assert {"Read", "Bash", "Grep", "Glob"}.issubset(orch_tools)
    assert any(t.startswith("mcp__consensus-mcp__") for t in orch_tools)


# ---------- non-destructive skip-if-exists + --force ----------

def test_rerun_skips_existing_divergent_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert wiz.main(_init_args()) == 0
    agents_dir = tmp_path / ".claude" / "agents"
    orch = agents_dir / "consensus-orchestrator.md"
    # User edits the file; a plain rerun must NOT clobber it.
    orch.write_text("USER EDITED CONTENT\n", encoding="utf-8")
    assert wiz.main(["--reconfigure", *_init_args()]) == 0
    assert orch.read_text(encoding="utf-8") == "USER EDITED CONTENT\n"


def test_force_overwrites_divergent_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert wiz.main(_init_args()) == 0
    agents_dir = tmp_path / ".claude" / "agents"
    orch = agents_dir / "consensus-orchestrator.md"
    orch.write_text("USER EDITED CONTENT\n", encoding="utf-8")
    assert wiz.main(["--reconfigure", "--force", *_init_args()]) == 0
    text = orch.read_text(encoding="utf-8")
    assert text != "USER EDITED CONTENT\n"
    assert "consensus-orchestrator" in text


# ---------- --no-agents opt-out ----------

def test_no_agents_suppresses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([*_init_args(), "--no-agents"])
    assert rc == 0
    assert not (tmp_path / ".claude" / "agents").exists()


# ---------- --dry-run reports without writing ----------

def test_dry_run_reports_agents_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--dry-run", "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "agents" in out.lower()
    assert not (tmp_path / ".claude" / "agents").exists()


def test_dry_run_no_agents_reports_skip(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--dry-run", "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-agents",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--no-agents" in out or "agents" in out.lower()
    assert not (tmp_path / ".claude" / "agents").exists()
