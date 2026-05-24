"""Integration smoke: consensus-mcp's own tooling must pass its own gate.

Iteration-1 integration guard from the sp-consensus-optimization consult
(2026-05-23). The v1.29.4 miss was that the PreToolUse gate blocked consensus's
OWN `consensus init` / `--repair` in a governed project — a self-referential
integration failure that a full 4-AI design consult did not catch because the
panel reviewed the design, not the artifact running in its real environment.

Unit-level coverage of the exemption lives in ``test_consensus_hooks.py``. THIS
module is the higher-fidelity guard the consult asked for:

  (B2) It activates the gate through the REAL governed-project detection — a
       ``.consensus/`` directory on disk — NOT the ``CONSENSUS_MCP_FORCE_OPTED_IN``
       test shortcut. So it proves the gate is genuinely ON (a plain writer is
       blocked) yet still lets the project's own tooling through.

  (B7) The own-tooling allow-list test is DATA-DRIVEN from ``pyproject.toml``'s
       ``[project.scripts]``. A newly-added console script that someone forgets to
       exempt in the gate's ``_CONSENSUS_TOOLING`` set fails HERE — closing the
       bug *class*, not just the one binary that was fixed.

The suite is green on the v1.29.4 hook; it goes red if the exemption is reverted,
if a new own-binary is added without exemption, or if the exec-hole guard weakens.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parent.parent
    / "claude_extensions" / "hooks" / "consensus_pretooluse_gate.py"
)
_PYPROJECT = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"


def _own_console_scripts() -> list[str]:
    """The project's own console-script names, read live from pyproject so this
    test tracks reality (the source of the v1.29.4 bug class)."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return sorted(data["project"]["scripts"].keys())


def _governed_project(tmp_path: Path) -> Path:
    """A real governed project: a `.consensus/` dir on disk is what the gate keys
    off (`(repo_root / '.consensus').is_dir()`), so enforcement is REAL here, not
    forced via env."""
    consensus = tmp_path / ".consensus"
    consensus.mkdir()
    (consensus / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    return tmp_path


def _run_gate(command: str, repo_root: Path) -> subprocess.CompletedProcess:
    """Invoke the gate as a subprocess (the real Claude Code shape). Runtime is
    forced PRESENT; opt-in is deliberately NOT forced — the on-disk `.consensus/`
    is what must activate the gate, so the smoke exercises the production path."""
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(repo_root)
    env["CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"] = "1"
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT", None)
    env.pop("CONSENSUS_MCP_FORCE_OPTED_IN", None)  # REAL detection only
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo_root),
    }
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(event),
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_gate_is_really_active_in_governed_project(tmp_path):
    """The smoke is only meaningful if the gate is genuinely ON. Prove it: an
    ordinary writer is blocked (rc 2) in the governed project, and a NON-own
    command is blocked too (so the own-tooling allow is specific, not a blanket
    open)."""
    repo = _governed_project(tmp_path)
    assert _run_gate("echo hi > f", repo).returncode == 2
    assert _run_gate("make install", repo).returncode == 2


def test_gate_fails_open_without_consensus_dir(tmp_path):
    """Control: with no `.consensus/` on disk the repo is not governed, so the
    same writer is ALLOWED — confirming it's the on-disk detection (not the env)
    that activated enforcement above."""
    assert _run_gate("echo hi > f", tmp_path).returncode == 0


@pytest.mark.parametrize("script", _own_console_scripts())
def test_own_console_script_passes_gate_in_governed_project(tmp_path, script):
    """B7 (data-driven): every console script the project ships must be allowed by
    its own gate in a governed project. A new binary added to pyproject without a
    matching gate exemption fails here."""
    repo = _governed_project(tmp_path)
    result = _run_gate(f"{script} --help", repo)
    assert result.returncode == 0, (
        f"own tooling '{script}' was BLOCKED by the gate in a governed project; "
        f"add it to _CONSENSUS_TOOLING in consensus_pretooluse_gate.py. "
        f"stderr: {result.stderr[:300]}"
    )


@pytest.mark.parametrize("command", [
    "consensus-init --repair && rm -rf x",
    "consensus-init $(rm x)",
    "consensus-init --repair > /etc/passwd",
    "consensus init --check; rm -rf x",
])
def test_own_binary_does_not_open_exec_hole(tmp_path, command):
    """An allowed own-binary token must NOT smuggle a chained writer, command
    substitution, or redirection past the gate."""
    repo = _governed_project(tmp_path)
    assert _run_gate(command, repo).returncode == 2
