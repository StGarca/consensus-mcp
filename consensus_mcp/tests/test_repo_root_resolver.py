"""M1 (consult iteration-m1-hardening-design-4d7d2469) Q2 - repo-root resolver.

Unit tests for the ONE blessed resolver (_paths.resolve_repo_root), the two
PINNED regressions the design ordered fixed first (tools/apply_codex_patch
pipx-bootstrap break; _self_drive site-packages fallback), the grep/ast
census gate (exactly ONE non-shim implementation), and the hook vendored-
block byte-identity drift guard (kimi-rev-005: runs in the standard pytest
suite on both CI OSes).
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from consensus_mcp import _paths
from consensus_mcp import _self_drive
from consensus_mcp.tools import apply_codex_patch as acp


PACKAGE_ROOT = Path(_paths.__file__).resolve().parent

BEGIN_MARKER = (
    "# === BEGIN CONSENSUS REPO-ROOT RESOLVER "
    "(vendored block; source of truth: consensus_mcp/_paths.py) ==="
)
END_MARKER = "# === END CONSENSUS REPO-ROOT RESOLVER ==="

HOOK_FILES = (
    PACKAGE_ROOT / "claude_extensions" / "hooks" / "consensus_sessionstart.py",
    PACKAGE_ROOT / "claude_extensions" / "hooks" / "consensus_pretooluse_gate.py",
    PACKAGE_ROOT / "claude_extensions" / "hooks" / "consensus_stop_gate.py",
)

ENV_KEYS = ("CONSENSUS_MCP_REPO_ROOT", "CONSENSUS_MCP_PROJECT_ROOT")


@pytest.fixture
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _governed(base: Path, name: str = "proj") -> Path:
    """A minimal governed project: `.consensus/config.yaml` + `consensus-state/`
    (satisfies BOTH the blessed default markers and _dispatch_base's stricter
    _has_repo_markers consuming-project check)."""
    root = base / name
    (root / ".consensus").mkdir(parents=True)
    (root / ".consensus" / "config.yaml").write_text(
        "contributors: {}\n", encoding="utf-8"
    )
    (root / "consensus-state").mkdir()
    return root


# ---------------------------------------------------------------------------
# resolve_repo_root unit semantics (the blessed precedence table)
# ---------------------------------------------------------------------------


def test_repo_root_env_wins_over_project_root_and_cwd(tmp_path, monkeypatch, clean_env):
    repo_a = _governed(tmp_path, "a")
    repo_b = _governed(tmp_path, "b")
    walk = _governed(tmp_path, "walk")
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(repo_a))
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(repo_b))
    monkeypatch.chdir(walk)
    assert _paths.resolve_repo_root() == repo_a.resolve()


def test_project_root_env_honored_when_repo_root_unset(tmp_path, monkeypatch, clean_env):
    repo_b = _governed(tmp_path, "b")
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(repo_b))
    monkeypatch.chdir(tmp_path)
    assert _paths.resolve_repo_root() == repo_b.resolve()


def test_empty_string_env_treated_as_unset(tmp_path, monkeypatch, clean_env):
    walk = _governed(tmp_path, "walk")
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", "")
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", "")
    monkeypatch.chdir(walk)
    assert _paths.resolve_repo_root() == walk.resolve()


def test_walk_prefers_nearest_ancestor(tmp_path, monkeypatch, clean_env):
    outer = _governed(tmp_path, "outer")
    inner = _governed(outer, "inner")
    deep = inner / "src" / "deep"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert _paths.resolve_repo_root() == inner.resolve()


def test_git_is_not_a_default_marker(tmp_path, monkeypatch, clean_env):
    """kimi-rev-002 (binding): a plain git repo must NOT anchor resolution -
    that would silently widen authority to any subdirectory of any git repo."""
    gitrepo = tmp_path / "gitrepo"
    (gitrepo / ".git").mkdir(parents=True)
    monkeypatch.chdir(gitrepo)
    with pytest.raises(_paths.RepoRootError):
        _paths.resolve_repo_root()


def test_git_marker_available_only_via_explicit_opt_in(tmp_path, monkeypatch, clean_env):
    gitrepo = tmp_path / "gitrepo"
    (gitrepo / ".git").mkdir(parents=True)
    sub = gitrepo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    # Explicit opt-in (this test IS the justifying call site: it exercises
    # the allow_git_marker escape hatch itself).
    assert _paths.resolve_repo_root(allow_git_marker=True) == gitrepo.resolve()


def test_git_marker_accepts_worktree_gitlink_file(tmp_path, monkeypatch, clean_env):
    linked = tmp_path / "worktree"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    monkeypatch.chdir(linked)
    assert _paths.resolve_repo_root(allow_git_marker=True) == linked.resolve()


def test_require_markers_false_falls_back_to_cwd(tmp_path, monkeypatch, clean_env):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert _paths.resolve_repo_root(require_markers=False) == outside.resolve()


def test_failure_is_actionable_names_env_keys_and_markers(tmp_path, monkeypatch, clean_env):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    with pytest.raises(_paths.RepoRootError) as exc:
        _paths.resolve_repo_root()
    msg = str(exc.value)
    assert "CONSENSUS_MCP_REPO_ROOT" in msg
    assert "CONSENSUS_MCP_PROJECT_ROOT" in msg
    assert ".consensus/" in msg
    assert "consensus-state/" in msg


def test_no_cwd_walk_and_no_env_raises_even_inside_repo(tmp_path, monkeypatch, clean_env):
    walk = _governed(tmp_path, "walk")
    monkeypatch.chdir(walk)
    with pytest.raises(_paths.RepoRootError):
        _paths.resolve_repo_root(allow_cwd_walk=False)


def test_unsupported_on_failure_is_a_valueerror(clean_env):
    with pytest.raises(ValueError):
        _paths.resolve_repo_root(on_failure="return_none")


def test_repo_root_error_is_a_runtime_error():
    """Callers with a documented 'raises RuntimeError' contract (_resume) stay
    honest when the shared error propagates."""
    assert issubclass(_paths.RepoRootError, RuntimeError)


# ---------------------------------------------------------------------------
# PINNED regression 1: tools/apply_codex_patch pipx-bootstrap break
# ---------------------------------------------------------------------------


def test_apply_codex_patch_resolver_does_not_depend_on_file(tmp_path, monkeypatch, clean_env):
    """Reproduces the pipx break: simulate the installed layout by pointing
    the module's __file__ at a fake site-packages tree. The OLD resolver
    returned Path(__file__).parent.parent.parent (the INSTALL tree) whenever
    the env was unset; the fixed resolver must anchor to the governed
    project found by the cwd walk instead."""
    fake = tmp_path / "venv" / "site-packages" / "consensus_mcp" / "tools" / "apply_codex_patch.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("# fake installed module", encoding="utf-8")
    install_tree_root = fake.parent.parent.parent  # what the OLD code returned
    monkeypatch.setattr(acp, "__file__", str(fake))

    project = _governed(tmp_path, "governed-project")
    monkeypatch.chdir(project)

    resolved = acp._resolve_repo_root()
    assert resolved == project.resolve()
    assert resolved != install_tree_root.resolve(), (
        "resolver must never anchor to the install tree (site-packages)"
    )


def test_apply_codex_patch_env_project_root_honored(tmp_path, monkeypatch, clean_env):
    """The .mcp.json shape: `consensus init` launches the server with
    CONSENSUS_MCP_PROJECT_ROOT; the tool must honor it (the old resolver
    ignored PROJECT_ROOT entirely)."""
    project = _governed(tmp_path, "governed-project")
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(project))
    monkeypatch.chdir(tmp_path)
    assert acp._resolve_repo_root() == project.resolve()


def test_apply_codex_patch_handle_refuses_structured_when_unresolvable(
    tmp_path, monkeypatch, clean_env
):
    """envs unset + cwd outside any repo -> the TOOL returns its structured
    refusal shape (never an escaping exception, never an install-tree path)."""
    outside = tmp_path / "nowhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    result = acp.handle(
        iteration_dir="iteration-x",
        patch_id="codex-rev-001-patch",
        actor={
            "id": "codex-x-1",
            "model_family": "codex",
            "role": "fix_author",
            "pass_id": "codex-x-1-pass1",
        },
    )
    assert result["ok"] is False
    assert result["applied"] is False
    assert result["error"].startswith("repo_root_unresolvable:")


# ---------------------------------------------------------------------------
# PINNED regression 2: _self_drive site-packages fallback GONE
# ---------------------------------------------------------------------------


def test_self_drive_site_packages_fallback_gone(tmp_path, monkeypatch, clean_env):
    """envs unset + cwd outside any repo -> RepoRootError. The OLD code fell
    back to Path(__file__).parent.parent, which under pipx resolves to
    site-packages (the exact failure class _dispatch_base's comments call
    unsafe)."""
    outside = tmp_path / "nowhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    with pytest.raises(_paths.RepoRootError):
        _self_drive._resolve_repo_root()


def test_self_drive_resolver_does_not_depend_on_file(tmp_path, monkeypatch, clean_env):
    fake = tmp_path / "venv" / "site-packages" / "consensus_mcp" / "_self_drive.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("# fake installed module", encoding="utf-8")
    monkeypatch.setattr(_self_drive, "__file__", str(fake))

    project = _governed(tmp_path, "governed-project")
    monkeypatch.chdir(project)
    resolved = _self_drive._resolve_repo_root()
    assert resolved == project.resolve()
    assert resolved != fake.parent.parent.resolve(), (
        "the Path(__file__).parent.parent fallback must be gone"
    )


# ---------------------------------------------------------------------------
# Census: exactly ONE non-shim implementation (acceptance gate Q2)
# ---------------------------------------------------------------------------


def _delegates_to_shared_resolver(fn_node: ast.FunctionDef) -> bool:
    """True iff the function body calls the blessed resolver (directly, via an
    import alias `_shared_resolve_repo_root`, or as `_paths.resolve_repo_root`)."""
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in (
            "resolve_repo_root",
            "_shared_resolve_repo_root",
        ):
            return True
        if isinstance(func, ast.Attribute) and func.attr == "resolve_repo_root":
            return True
    return False


def _uses_dunder_file(fn_node: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id == "__file__"
        for node in ast.walk(fn_node)
    )


def _derives_root_from_file(fn_node: ast.FunctionDef) -> bool:
    """M1-remediation (consult iteration-path-to-a-remediation-260caad1): True
    iff the function derives a repo/project root by walking up TWO or more
    levels from ``__file__`` (a ``Path(__file__)...parent.parent`` chain) whose
    result is used as a bare root - i.e. NOT immediately extended by a ``/``
    join. This is the site-packages-anchoring class the census must flag
    regardless of the function's NAME (Q5 widening).

    Deliberately NOT flagged (legitimate, not root-derivation):
      - a single ``Path(__file__).resolve().parent`` (the package dir itself,
        e.g. locating a bundled ``spec_template.md`` / ``dispatch_templates/``);
      - a marker-validated ``__file__`` walk that fails loud rather than
        returning a bare parent (``_visibility_watchdog._default_repo_root``
        starts its walk from a single ``.parent``, then loops - never a bare
        ``.parent.parent``);
      - a walk-up immediately joined to a subpath
        (``... .parent.parent / "schemas"``), which is a packaged-resource
        lookup, not a root the caller anchors state on.
    """
    # Inner links: nodes consumed as the ``.value`` of a ``.parent`` access.
    # The TIP of a ``.parent`` chain is a ``.parent`` Attribute that is NOT
    # itself some other ``.parent``'s ``.value``.
    inner = {
        id(node.value)
        for node in ast.walk(fn_node)
        if isinstance(node, ast.Attribute) and node.attr == "parent"
    }
    # Left operands of every ``/`` (Div) BinOp: ``<walk-up> / "sub"`` is a
    # packaged-resource lookup, so a tip consumed as a Div-left is allowed.
    div_left = {
        id(node.left)
        for node in ast.walk(fn_node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    }
    for node in ast.walk(fn_node):
        if not (isinstance(node, ast.Attribute) and node.attr == "parent"):
            continue
        if id(node) in inner:
            continue  # not the tip of the chain
        if not (isinstance(node.value, ast.Attribute) and node.value.attr == "parent"):
            continue  # single `.parent` (package dir) is not a repo-root walk-up
        if not _uses_dunder_file(node):
            continue  # the walk-up must root at __file__
        if id(node) in div_left:
            continue  # `<walk-up> / "sub"` -> packaged resource, allowed
        return True
    return False


def test_census_exactly_one_non_shim_implementation():
    """Every `_resolve_repo_root`/`resolve_repo_root` definition in the package
    (tests and the vendored hook copies excluded - the drift test below owns
    the hooks) must be a delegating shim of _paths.resolve_repo_root and must
    never touch __file__. Exactly ONE real implementation exists: _paths.py.

    M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q5: the
    census also closes the NAME-scoped hole. It no longer inspects only the
    `resolve_repo_root` name family - it now flags ANY non-test package
    function that derives a repo/project root from a `Path(__file__)...
    parent.parent` walk-up (`_derives_root_from_file`), whatever its name.
    That is the site-packages-anchoring class: a function named
    `_default_repo_root` (or anything else) that returns a bare `__file__`
    walk-up silently anchors to site-packages under a pipx/wheel install.
    Packaged-resource lookups (`... .parent / "spec_template.md"`,
    `... .parent.parent / "schemas"`) and marker-validated fail-loud walks
    (`_visibility_watchdog`) are deliberately NOT flagged."""
    implementations: list[str] = []
    offenders: list[str] = []
    for py in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel = py.relative_to(PACKAGE_ROOT).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        if rel.startswith("claude_extensions/hooks/"):
            continue  # byte-identical vendored copies; guarded below
        text = py.read_text(encoding="utf-8")
        # Widened pre-filter: scan every file that either references the
        # resolver name family OR touches __file__ (the walk-up class is a
        # __file__ derivation - the old `resolve_repo_root`-only filter skipped
        # e.g. _visibility_tui.py entirely, which is the hole Q5 closes).
        if "resolve_repo_root" not in text and "__file__" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            # M1-remediation Q5: universal guard - NO package function may
            # derive a repo root by walking up from __file__, regardless of
            # name. Use _paths.resolve_repo_root (or fail loud) instead.
            if _derives_root_from_file(node):
                offenders.append(
                    f"{rel}:{node.name} derives a repo root from a "
                    f"Path(__file__)...parent.parent walk-up "
                    f"(use _paths.resolve_repo_root instead)"
                )
            if node.name == "resolve_repo_root":
                implementations.append(rel)
                continue
            if node.name != "_resolve_repo_root":
                continue
            if not _delegates_to_shared_resolver(node):
                offenders.append(f"{rel}:{node.name} does not delegate")
            if _uses_dunder_file(node):
                offenders.append(f"{rel}:{node.name} uses __file__")
    assert implementations == ["_paths.py"], (
        f"expected the ONE implementation in _paths.py; found {implementations}"
    )
    assert offenders == [], offenders


def _only_func(src: str) -> ast.FunctionDef:
    """Parse a one-function snippet and return its FunctionDef node."""
    fn = ast.parse(src).body[0]
    assert isinstance(fn, ast.FunctionDef)
    return fn


def test_walk_up_detector_precision():
    """M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q5:
    lock in the census detector's precision so a future edit cannot silently
    re-narrow it. A bare `Path(__file__)...parent.parent` walk-up (the
    site-packages-anchoring class) is flagged regardless of function name; a
    single `.parent` (package dir), a walk-up joined to a subpath (packaged
    resource), a non-__file__ walk-up, and a marker-validated single-parent
    fail-loud walk are NOT flagged."""
    flagged = {
        "bare_double_parent": "def f():\n return Path(__file__).resolve().parent.parent\n",
        "bare_triple_parent": "def f():\n return Path(__file__).resolve().parent.parent.parent\n",
        "assigned_then_used": (
            "def f():\n walked = Path(__file__).resolve().parent.parent\n"
            " return walked / 'x' if walked else walked\n"
        ),
        "misleading_name_ok": "def project_dir():\n return Path(__file__).parent.parent\n",
    }
    allowed = {
        "single_parent_pkg_dir": "def f():\n return Path(__file__).resolve().parent / 'spec_template.md'\n",
        "walk_up_joined_resource": "def f():\n return Path(__file__).resolve().parent.parent / 'schemas'\n",
        "triple_parent_joined": "def f():\n return Path(__file__).resolve().parent.parent.parent / 'x'\n",
        "non_file_walk_up": "def f():\n base = Path.cwd()\n return base.parent.parent\n",
        "fail_loud_single_then_walk": (
            "def f():\n start = Path(__file__).resolve().parent\n"
            " node = start\n"
            " while node.parent != node:\n"
            "  node = node.parent\n"
            " return node\n"
        ),
    }
    for label, src in flagged.items():
        assert _derives_root_from_file(_only_func(src)) is True, f"should flag: {label}"
    for label, src in allowed.items():
        assert _derives_root_from_file(_only_func(src)) is False, f"should allow: {label}"


# ---------------------------------------------------------------------------
# Hook vendored-block drift guard (kimi-rev-005: standard pytest suite)
# ---------------------------------------------------------------------------


def _extract_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert BEGIN_MARKER in text, f"{path} is missing the BEGIN marker"
    assert END_MARKER in text, f"{path} is missing the END marker"
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER) + len(END_MARKER)
    return text[start:end]


def test_hook_vendored_resolver_blocks_are_byte_identical():
    canonical = _extract_block(PACKAGE_ROOT / "_paths.py")
    for hook in HOOK_FILES:
        assert _extract_block(hook) == canonical, (
            f"{hook.name} vendored resolver block diverges from _paths.py - "
            f"re-stamp per the procedure documented in _init_wizard.py "
            f"(_CLAUDE_EXTENSION_FILES hook entries)"
        )


def test_vendored_block_is_self_contained(tmp_path, monkeypatch, clean_env):
    """The detached ~/.claude hook copy runs the block WITHOUT the package on
    sys.path: exec it in a bare namespace (os + Path only) and resolve."""
    namespace: dict = {"os": os, "Path": Path}
    exec(compile(_extract_block(PACKAGE_ROOT / "_paths.py"), "<vendored>", "exec"), namespace)
    project = _governed(tmp_path, "governed-project")
    monkeypatch.chdir(project)
    assert namespace["resolve_repo_root"]() == project.resolve()
    with monkeypatch.context() as m:
        m.chdir(tmp_path)
        with pytest.raises(namespace["RepoRootError"]):
            namespace["resolve_repo_root"]()
