"""Tests for the consensus Claude Code hook layer (Track B, Tasks B2-B5).

Each hook script is exercised as a subprocess (the real Claude Code invocation
shape): a PreToolUse/Stop/SessionStart event JSON on stdin, assertions on exit
code + stdout/stderr. The consensus-runtime probe is controlled via the env
overrides the scripts honor (CONSENSUS_MCP_FORCE_RUNTIME_ABSENT / _PRESENT) so
the fail-open vs fail-closed branches are deterministic without touching PATH.

Verified hook semantics (cited in the deliverable):
  - PreToolUse blocks via exit code 2 + stderr reason (matches
    contrib/delivery_gate_pretooluse.py); exit 0 allows.
  - hooks.json schema matches Superpowers v5.1.0 hooks/hooks.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from consensus_mcp import _delivery_readiness as dr
from consensus_mcp import _design_approval as da

_HOOKS_DIR = (
    Path(__file__).resolve().parent.parent / "claude_extensions" / "hooks"
)
PRETOOLUSE = _HOOKS_DIR / "consensus_pretooluse_gate.py"
STOP = _HOOKS_DIR / "consensus_stop_gate.py"
SESSIONSTART = _HOOKS_DIR / "consensus_sessionstart.py"
HOOKS_JSON = _HOOKS_DIR / "hooks.json"


def _run_hook(script: Path, event: dict, *, repo_root: Path,
              runtime: str = "present") -> subprocess.CompletedProcess:
    """Invoke a hook script with `event` on stdin. `runtime` in
    {"present","absent"} forces the runtime probe."""
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(repo_root)
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT", None)
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_PRESENT", None)
    if runtime == "present":
        env["CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"] = "1"
    elif runtime == "absent":
        env["CONSENSUS_MCP_FORCE_RUNTIME_ABSENT"] = "1"
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(event),
        capture_output=True, text=True, env=env, timeout=60,
    )


def _seal_marker(repo_root: Path, scope_glob: str = "src/**",
                 cross_family_sealed: bool = True) -> None:
    (repo_root / ".consensus").mkdir(parents=True, exist_ok=True)
    da.mint_design_approval(
        repo_root=repo_root, iteration_id="it1", scope_glob=scope_glob,
        converged_plan_sha256="abc", cross_family_sealed=cross_family_sealed,
    )


# --------------------------------------------------------------------------- #
# Task B2 — PreToolUse design gate
# --------------------------------------------------------------------------- #

def test_pretooluse_edit_no_marker_denies(tmp_path):
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, cp.stderr
    assert "consensus" in cp.stderr.lower()


def test_pretooluse_edit_valid_in_scope_allows(tmp_path):
    _seal_marker(tmp_path, scope_glob="src/**")
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, cp.stderr


def test_pretooluse_edit_out_of_scope_denies(tmp_path):
    _seal_marker(tmp_path, scope_glob="src/**")
    ev = {"tool_name": "Write", "tool_input": {"file_path": "docs/y.md"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, cp.stderr


def test_pretooluse_single_claude_marker_denies(tmp_path):
    _seal_marker(tmp_path, scope_glob="src/**", cross_family_sealed=False)
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, cp.stderr
    assert "advisory" in cp.stderr.lower()


def test_pretooluse_read_only_tool_always_allows(tmp_path):
    for tool in ("Read", "Grep", "Glob"):
        ev = {"tool_name": tool, "tool_input": {"file_path": "src/x.py"},
              "cwd": str(tmp_path)}
        cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
        assert cp.returncode == 0, (tool, cp.stderr)


def test_pretooluse_runtime_absent_fails_open(tmp_path):
    # No marker, but runtime absent -> must allow (plain workflow).
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="absent")
    assert cp.returncode == 0, cp.stderr


@pytest.mark.parametrize("command", [
    "sed -i 's/a/b/' src/x.py",
    "echo hi > out.txt",
    "echo hi >> out.txt",
    "tee out.txt",
    "mv a b",
    "cp a b",
    "rm a",
    "git commit -m x",
    "git push origin main",
    "git tag v1",
    "npm publish",
    "make release",
])
def test_pretooluse_file_modifying_bash_no_marker_denies(tmp_path, command):
    ev = {"tool_name": "Bash", "tool_input": {"command": command},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, (command, cp.stderr)


@pytest.mark.parametrize("command", [
    "ls -la",
    "cat src/x.py",
    "grep -r foo src",
    "git status",
    "git diff HEAD",
])
def test_pretooluse_read_only_bash_allows(tmp_path, command):
    ev = {"tool_name": "Bash", "tool_input": {"command": command},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, (command, cp.stderr)


def test_pretooluse_file_modifying_bash_with_sealed_marker_allows(tmp_path):
    _seal_marker(tmp_path, scope_glob="**")
    ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, cp.stderr


def test_pretooluse_malformed_payload_fails_open(tmp_path):
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(tmp_path)
    env["CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"] = "1"
    cp = subprocess.run(
        [sys.executable, str(PRETOOLUSE)], input="not json{{",
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr


# --------------------------------------------------------------------------- #
# Task B3 — Stop verification soft-gate
# --------------------------------------------------------------------------- #

def _git_repo_with_modified_source(tmp_path: Path, rel: str = "src/app.py") -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    src = tmp_path / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    # Modify after HEAD so `git diff --name-only HEAD` reports it.
    src.write_text("print('v2')\n", encoding="utf-8")
    return src


def test_stop_modified_source_without_token_emits_directive(tmp_path):
    src = _git_repo_with_modified_source(tmp_path, "src/app.py")
    ev = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    cp = _run_hook(STOP, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, cp.stderr
    assert "STOP" in cp.stdout
    assert "src/app.py" in cp.stdout


def test_stop_modified_source_with_token_no_directive(tmp_path):
    src = _git_repo_with_modified_source(tmp_path, "src/app.py")
    # Mint a valid delivery token for the modified file.
    seal_dir = tmp_path / "consensus-state" / "active" / "iteration-x"
    seal_dir.mkdir(parents=True, exist_ok=True)
    (seal_dir / "iteration-outcome.yaml").write_text(
        "closing_state: quorum_close_passed\n", encoding="utf-8")
    dr.mint_delivery_token(
        src, design_consensus_ref="iteration-x",
        vetted_by=["codex", "gemini"], repo_root=tmp_path)
    ev = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    cp = _run_hook(STOP, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, cp.stderr
    assert "STOP" not in cp.stdout, cp.stdout


def test_stop_runtime_absent_noop(tmp_path):
    _git_repo_with_modified_source(tmp_path, "src/app.py")
    ev = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    cp = _run_hook(STOP, ev, repo_root=tmp_path, runtime="absent")
    assert cp.returncode == 0
    assert cp.stdout.strip() == "", cp.stdout


def test_stop_ignores_test_files(tmp_path):
    # A modified TEST file (not source) must not trigger the directive.
    _git_repo_with_modified_source(tmp_path, "tests/test_app.py")
    ev = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    cp = _run_hook(STOP, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0
    assert "STOP" not in cp.stdout, cp.stdout


# --------------------------------------------------------------------------- #
# Task B4 — SessionStart precedence injector + UserPromptSubmit nudge
# --------------------------------------------------------------------------- #

def test_sessionstart_runtime_present_injects_precedence(tmp_path):
    ev = {"hook_event_name": "SessionStart", "source": "startup",
          "cwd": str(tmp_path)}
    cp = _run_hook(SESSIONSTART, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    low = ctx.lower()
    assert "workflow a" in low
    assert "workflow b" in low
    assert "delivery" in low
    assert ".consensus/design-approved" in ctx
    assert "precedence" in low


def test_sessionstart_runtime_absent_benign_notice(tmp_path):
    ev = {"hook_event_name": "SessionStart", "source": "startup",
          "cwd": str(tmp_path)}
    cp = _run_hook(SESSIONSTART, ev, repo_root=tmp_path, runtime="absent")
    assert cp.returncode == 0
    out = json.loads(cp.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"].lower()
    assert "not detected" in ctx
    assert "plain workflow" in ctx


def test_userpromptsubmit_nudge_present(tmp_path):
    ev = {"hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path)}
    cp = _run_hook(SESSIONSTART, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0
    out = json.loads(cp.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "consensus" in out["hookSpecificOutput"]["additionalContext"].lower()


def test_userpromptsubmit_runtime_absent_noop(tmp_path):
    ev = {"hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path)}
    cp = _run_hook(SESSIONSTART, ev, repo_root=tmp_path, runtime="absent")
    assert cp.returncode == 0
    assert cp.stdout.strip() == "", cp.stdout


# --------------------------------------------------------------------------- #
# Task B5 — hooks.json manifest + degradation integration test
# --------------------------------------------------------------------------- #

def test_hooks_json_schema_matches_superpowers_shape():
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    assert set(data) == {"hooks"}
    hooks = data["hooks"]
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"):
        assert event in hooks, event
        groups = hooks[event]
        assert isinstance(groups, list) and groups
        for grp in groups:
            assert "matcher" in grp
            assert isinstance(grp["hooks"], list) and grp["hooks"]
            for h in grp["hooks"]:
                assert h["type"] == "command"
                assert "command" in h and h["command"]
    # PreToolUse matcher covers the gated tools.
    matcher = hooks["PreToolUse"][0]["matcher"]
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
        assert tool in matcher, (tool, matcher)
    # Each registered command points at an existing script.
    for event, groups in hooks.items():
        for grp in groups:
            for h in grp["hooks"]:
                ref = h["command"]
                for name in ("consensus_sessionstart.py", "consensus_pretooluse_gate.py",
                             "consensus_stop_gate.py"):
                    if name in ref:
                        assert (_HOOKS_DIR / name).exists(), name


def test_integration_runtime_absent_all_gates_noop(tmp_path):
    # No marker, modified source, etc. — with runtime absent EVERY gate is a
    # no-op (plain workflow, never worse).
    edit_ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
               "cwd": str(tmp_path)}
    assert _run_hook(PRETOOLUSE, edit_ev, repo_root=tmp_path,
                     runtime="absent").returncode == 0

    bash_ev = {"tool_name": "Bash", "tool_input": {"command": "rm -rf x"},
               "cwd": str(tmp_path)}
    assert _run_hook(PRETOOLUSE, bash_ev, repo_root=tmp_path,
                     runtime="absent").returncode == 0

    _git_repo_with_modified_source(tmp_path, "src/app.py")
    stop_ev = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    stop_cp = _run_hook(STOP, stop_ev, repo_root=tmp_path, runtime="absent")
    assert stop_cp.returncode == 0 and stop_cp.stdout.strip() == ""

    ss_ev = {"hook_event_name": "SessionStart", "source": "startup",
             "cwd": str(tmp_path)}
    ss_cp = _run_hook(SESSIONSTART, ss_ev, repo_root=tmp_path, runtime="absent")
    ss_ctx = json.loads(ss_cp.stdout)["hookSpecificOutput"]["additionalContext"].lower()
    assert "plain workflow" in ss_ctx


def test_integration_runtime_present_no_marker_pretooluse_denies(tmp_path):
    edit_ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
               "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, edit_ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, cp.stderr
