"""M1 (consult iteration-m1-hardening-design-4d7d2469) Q2 - equivalence matrix.

Grid of (cwd inside repo / outside repo) x (CONSENSUS_MCP_REPO_ROOT set/unset)
x (CONSENSUS_MCP_PROJECT_ROOT set/unset): EVERY migrated entry point resolves
the IDENTICAL root or raises the IDENTICAL structured error (RepoRootError
family). The declared-lenient exceptions (server boot; the two tool schemas
documenting an env-or-cwd default; the author helper) return cwd in the
fail-closed cell instead of raising - exactly the leniency the design
declares at those call sites, nowhere else.

Includes the pipx/console-script invocation shapes (gemini-rev-003): cwd
outside the repo with env-anchored resolution, explicit-path calls, and a
module-__file__ independence sweep.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from consensus_mcp import _author_review_packet
from consensus_mcp import _dispatch_base
from consensus_mcp import _paths
from consensus_mcp import _release_gate_check
from consensus_mcp import _resume
from consensus_mcp import _self_drive
from consensus_mcp import _sync_section_24
from consensus_mcp import _validate_closure_invariant
from consensus_mcp import server
from consensus_mcp.tools import apply_codex_patch
from consensus_mcp.tools import consensus_get_iteration_outcome
from consensus_mcp.tools import consensus_run_iteration


ENV_KEYS = ("CONSENSUS_MCP_REPO_ROOT", "CONSENSUS_MCP_PROJECT_ROOT")

# (name, zero-arg resolver callable, mode). mode:
#   strict  - fail-closed: RepoRootError (family) when nothing resolves
#   lenient - declared exception: falls back to cwd when nothing resolves
ENTRY_POINTS = (
    ("_paths.resolve_repo_root", lambda: _paths.resolve_repo_root(), "strict"),
    ("_self_drive", lambda: _self_drive._resolve_repo_root(), "strict"),
    ("apply_codex_patch", lambda: apply_codex_patch._resolve_repo_root(), "strict"),
    ("_dispatch_base", lambda: _dispatch_base._resolve_repo_root(), "strict"),
    ("_sync_section_24", lambda: _sync_section_24._resolve_repo_root(), "strict"),
    ("_release_gate_check", lambda: _release_gate_check._resolve_repo_root(None), "strict"),
    ("_validate_closure_invariant", lambda: _validate_closure_invariant._resolve_repo_root(), "strict"),
    ("_resume", lambda: _resume._resolve_repo_root(None), "strict"),
    ("consensus_run_iteration", lambda: consensus_run_iteration._resolve_repo_root(None), "lenient"),
    ("consensus_get_iteration_outcome", lambda: consensus_get_iteration_outcome._resolve_repo_root(None), "lenient"),
    ("_author_review_packet", lambda: _author_review_packet._resolve_repo_root(None), "lenient"),
    # server BOOT leniency (declared exception): cwd + logged warning.
    ("server_boot", lambda: server._resolve_repo_root(), "lenient"),
)

STRICT_MODULES = (
    _self_drive,
    apply_codex_patch,
    _dispatch_base,
    _sync_section_24,
    _release_gate_check,
    _validate_closure_invariant,
    _resume,
)


def _governed(base: Path, name: str) -> Path:
    """Governed project satisfying BOTH marker systems: the blessed defaults
    (.consensus/ or consensus-state/) and _dispatch_base's _has_repo_markers
    (.consensus/config.yaml consuming-project form)."""
    root = base / name
    (root / ".consensus").mkdir(parents=True)
    (root / ".consensus" / "config.yaml").write_text(
        "contributors: {}\n", encoding="utf-8"
    )
    (root / "consensus-state").mkdir()
    return root


@pytest.fixture
def grid(tmp_path, monkeypatch):
    """Common fixture: two env-target repos, a walk repo, an outside dir."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    repo_a = _governed(tmp_path, "env-repo-a")
    repo_b = _governed(tmp_path, "env-repo-b")
    walk = _governed(tmp_path, "walk-repo")
    (walk / "src" / "deep").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return {"repo_a": repo_a, "repo_b": repo_b, "walk": walk, "outside": outside}


@pytest.mark.parametrize("project_env", [False, True])
@pytest.mark.parametrize("repo_env", [False, True])
@pytest.mark.parametrize("cwd_inside", [False, True])
def test_equivalence_matrix(grid, monkeypatch, cwd_inside, repo_env, project_env):
    if repo_env:
        monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(grid["repo_a"]))
    if project_env:
        monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(grid["repo_b"]))
    monkeypatch.chdir(
        grid["walk"] / "src" / "deep" if cwd_inside else grid["outside"]
    )

    if repo_env:
        expected = grid["repo_a"].resolve()
    elif project_env:
        expected = grid["repo_b"].resolve()
    elif cwd_inside:
        expected = grid["walk"].resolve()
    else:
        expected = None  # fail-closed cell

    for name, resolver, mode in ENTRY_POINTS:
        if expected is not None:
            assert resolver() == expected, (
                f"{name}: expected {expected} in cell cwd_inside={cwd_inside} "
                f"repo_env={repo_env} project_env={project_env}"
            )
        elif mode == "strict":
            with pytest.raises(_paths.RepoRootError):
                resolver()
        else:
            assert resolver() == grid["outside"].resolve(), (
                f"{name}: declared-lenient entry must fall back to cwd"
            )


# ---------------------------------------------------------------------------
# pipx / console-script invocation shapes (gemini-rev-003)
# ---------------------------------------------------------------------------


def test_pipx_shape_env_anchored_from_outside_cwd(grid, monkeypatch):
    """A pipx console script runs with cwd anywhere; the .mcp.json-style env
    (PROJECT_ROOT) must anchor every entry point to the governed project."""
    monkeypatch.chdir(grid["outside"])
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(grid["repo_b"]))
    for name, resolver, _mode in ENTRY_POINTS:
        assert resolver() == grid["repo_b"].resolve(), name


def test_pipx_shape_explicit_paths_from_outside_cwd(grid, monkeypatch):
    """Console-script shape with EXPLICIT paths: cwd outside the repo, the
    iteration path and repo_root passed explicitly (no env)."""
    monkeypatch.chdir(grid["outside"])
    iter_dir = grid["repo_b"] / "consensus-state" / "active" / "iteration-x"
    iter_dir.mkdir(parents=True)
    out = consensus_get_iteration_outcome.handle(
        iteration_dir=str(iter_dir), repo_root=str(grid["repo_b"])
    )
    assert out["ok"] is True
    assert out["iteration_id"] == "iteration-x"


def test_strict_resolvers_do_not_depend_on_module_file(grid, monkeypatch):
    """Simulate the installed (site-packages) layout for every strict module:
    with env unset and cwd inside the governed repo, each must resolve the
    repo via the walk - never a Path(__file__)-derived install-tree path."""
    fake_root = grid["outside"] / "venv" / "site-packages"
    monkeypatch.chdir(grid["walk"])
    for mod in STRICT_MODULES:
        fake = fake_root / "consensus_mcp" / (Path(mod.__file__).name)
        fake.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mod, "__file__", str(fake))
    for name, resolver, mode in ENTRY_POINTS:
        if mode != "strict":
            continue
        assert resolver() == grid["walk"].resolve(), name
