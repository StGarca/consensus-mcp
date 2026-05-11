"""_release_gate_check.py - run all 10 release gates for v1.11.0.

Per codex-iter0007-L2 deliverable in iteration-0007 consensus + the
9-gate release checklist (canonical-iter0007-001 + claude-iter0007-1).

Gates:
  G_smoke           : in-tree _smoke_test.py exits 0 with "60/60 tests passed"
  G_validators      : run_validator_tests.py exits 0 with "21/21 tests passed"
  G_frontmatter     : YAML frontmatter parses OK in every shippable .md
  G_unstaged        : git diff --quiet on shippable boundary paths
  G_untracked_pkg   : git ls-files --others --exclude-standard on boundary -> 0
  G_install         : python -m build wheel + pip install in clean tempvenv
  G_install_smoke   : installed package smoke run -> 60/60
  G_server_starts   : consensus-mcp --boot-and-exit returns 0 in <2s
  G_real_iter       : iteration-outcome.yaml.closing_state contains
                      "implementation_ready_apply_landed"
  G_pytest_dispatch_codex : pytest test_dispatch_codex.py exits 0 with N passed

Exit 0 iff all 10 pass. Exit 1 otherwise. Writes per-gate result dict to
stdout in human-readable form, then a final SUMMARY block.

Usage:
  python -m consensus_mcp._release_gate_check [--repo-root PATH]

Path-resolution: same env-var override as server.py / _smoke_test.py
(CONSENSUS_MCP_REPO_ROOT) plus a --repo-root CLI flag for explicit
override. Default: in-tree parent walk.
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml


def _resolve_repo_root(cli_override: str | None) -> Path:
    if cli_override:
        return Path(cli_override).resolve()
    env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


# Paths (relative to REPO_ROOT) that the wheel ships.
SHIPPABLE_BOUNDARY = [
    "consensus_mcp",
    "consensus_mcp/validators",
]

# Paths to gate G_unstaged scoping. Same as boundary plus the new artifacts.
UNSTAGED_SCOPE = [
    "pyproject.toml",
    "consensus_mcp/_release_gate_check.py",
    "consensus_mcp/docs",
    "consensus_mcp",
    "consensus_mcp/validators",
]


def _print_gate(name: str, passed: bool, detail: str = "") -> None:
    mark = "PASS" if passed else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)


def gate_smoke(repo_root: Path, python: str) -> tuple[bool, str]:
    """G_smoke: in-tree smoke 60/60 (was 59/59 pre-iter-0021)."""
    try:
        result = subprocess.run(
            [python, str(repo_root / "consensus_mcp" / "_smoke_test.py")],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
        return False, f"exception: {exc}"
    tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
    last = tail[0]
    ok = result.returncode == 0 and "60/60 tests passed" in last
    return ok, f"exit={result.returncode} last={last!r}"


def gate_validators(repo_root: Path, python: str) -> tuple[bool, str]:
    """G_validators: run_validator_tests 21/21."""
    try:
        result = subprocess.run(
            [python, str(repo_root / "consensus_mcp" / "validators" / "run_validator_tests.py")],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
        return False, f"exception: {exc}"
    tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
    last = tail[0]
    ok = result.returncode == 0 and "21/21 tests passed" in last
    return ok, f"exit={result.returncode} last={last!r}"


def gate_frontmatter(repo_root: Path) -> tuple[bool, str]:
    """G_frontmatter: every shippable .md has parseable frontmatter (or no frontmatter is OK)."""
    md_files: list[Path] = []
    for rel in SHIPPABLE_BOUNDARY:
        base = repo_root / rel
        if not base.exists():
            continue
        md_files.extend(base.rglob("*.md"))
    md_files.append(repo_root / "consensus_mcp" / "docs" / "README.md")
    md_files.append(repo_root / "consensus_mcp" / "docs" / "tool-reference.md")
    md_files.append(repo_root / "consensus_mcp" / "docs" / "state-schema.md")
    bad: list[str] = []
    for path in sorted(set(md_files)):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            bad.append(f"{path.name}: read failed ({exc})")
            continue
        if not text.startswith("---"):
            # No frontmatter is acceptable for ship docs.
            continue
        end = text.find("\n---", 3)
        if end == -1:
            bad.append(f"{path.name}: frontmatter unterminated")
            continue
        block = text[3:end]
        try:
            yaml.safe_load(block)
        except yaml.YAMLError as exc:
            bad.append(f"{path.name}: yaml parse error ({exc})")
    return (len(bad) == 0), (f"checked={len(md_files)} bad={bad}" if bad else f"checked={len(md_files)} all_ok")


def _git(repo_root: Path, *args: str) -> tuple[int, str, str]:
    """Run git; return (exitcode, stdout, stderr) separately so callers can
    avoid polluting structured output (e.g. --name-only) with stderr warnings
    like the Windows CRLF/LF normalization message.
    """
    try:
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=30,
        )
        return result.returncode, (result.stdout or ""), (result.stderr or "")
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
        return -1, "", f"git exception: {exc}"


def gate_unstaged(repo_root: Path) -> tuple[bool, str]:
    """G_unstaged: no unstaged changes inside the boundary."""
    code, _out, _err = _git(repo_root, "diff", "--quiet", "--", *UNSTAGED_SCOPE)
    if code == 0:
        return True, "no unstaged diff in scope"
    if code == 1:
        c2, out2, _ = _git(repo_root, "diff", "--name-only", "--", *UNSTAGED_SCOPE)
        names = [n for n in (out2 or "").splitlines() if n.strip()]
        return False, f"{len(names)} changed: {names[:5]}"
    return False, f"git error code={code}"


def gate_untracked_pkg(repo_root: Path) -> tuple[bool, str]:
    """G_untracked_pkg: no untracked files inside the boundary."""
    code, out, _ = _git(
        repo_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        *UNSTAGED_SCOPE,
    )
    if code != 0:
        return False, f"git error code={code}"
    # Filter out build artifacts (dist/, build/, *.egg-info/) intentionally:
    # those are generated by G_install during the run itself.
    lines = [
        ln for ln in (out or "").splitlines()
        if ln.strip() and "/dist/" not in ln and "/build/" not in ln
        and ".egg-info" not in ln and "__pycache__" not in ln
    ]
    if lines:
        return False, f"{len(lines)} untracked: {lines[:5]}"
    return True, "no untracked in scope"


def gate_install(repo_root: Path, python: str, work: Path) -> tuple[bool, str, Path | None, Path | None]:
    """G_install: build wheel + pip install in clean tempvenv.

    Returns (passed, detail, venv_python_path, installed_console_script_path).

    Per v1.10.2 F5 hardening: pre-clean dist/ before build so the gate always
    selects the just-built wheel. Naive `sorted(dist.glob(...))[-1]` lex-sort
    would otherwise mis-pick a stale wheel if multiple versions coexist
    (e.g., 1.10.1 sorts BEFORE 1.9.3rc0 lexicographically).
    """
    pkg_dir = repo_root / "scripts" / "consensus_mcp"
    dist_dir = pkg_dir / "dist"
    # Pre-clean dist/ so old wheels can't shadow the new build (F5 fix).
    if dist_dir.exists():
        shutil.rmtree(dist_dir, ignore_errors=True)

    # Build wheel with --no-isolation so package_dir for the validators sub-package resolves.
    # With --no-isolation, build-system requirements must already exist in the
    # invoking interpreter. Bootstrap the minimal build tools the gate itself
    # needs before asking `python -m build` to enforce pyproject requirements.
    missing_build_tools = []
    for module_name, package_name in (("build", "build"), ("wheel", "wheel")):
        try:
            subprocess.run(
                [python, "-c", f"import {module_name}"],
                check=True,
                capture_output=True,
                timeout=10,
            )
        except subprocess.CalledProcessError:
            missing_build_tools.append(package_name)
    if missing_build_tools:
        pip_install = subprocess.run(
            [python, "-m", "pip", "install", *missing_build_tools],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if pip_install.returncode != 0:
            return False, f"could not install build tools {missing_build_tools}: {pip_install.stderr[:300]}", None, None

    build_proc = subprocess.run(
        [python, "-m", "build", str(pkg_dir), "--wheel", "--no-isolation"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if build_proc.returncode != 0:
        return False, f"build failed: {(build_proc.stderr or build_proc.stdout)[-400:]}", None, None

    dist = pkg_dir / "dist"
    wheels = sorted(dist.glob("consensus_mcp-*.whl"))
    if not wheels:
        return False, "no wheel produced", None, None
    wheel = wheels[-1]

    venv_dir = work / "venv"
    venv_proc = subprocess.run(
        [python, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if venv_proc.returncode != 0:
        return False, f"venv failed: {venv_proc.stderr[:300]}", None, None

    if os.name == "nt":
        venv_python = venv_dir / "Scripts" / "python.exe"
        venv_console = venv_dir / "Scripts" / "consensus-mcp.exe"
    else:
        venv_python = venv_dir / "bin" / "python"
        venv_console = venv_dir / "bin" / "consensus-mcp"

    pip_proc = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", str(wheel)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if pip_proc.returncode != 0:
        return False, f"pip install failed: {(pip_proc.stderr or pip_proc.stdout)[-400:]}", None, None
    return True, f"wheel={wheel.name}", venv_python, venv_console


def gate_install_smoke(repo_root: Path, venv_python: Path) -> tuple[bool, str]:
    """G_install_smoke: smoke from installed package -> 60/60 (was 59/59 pre-iter-0021).

    Smoke needs CONSENSUS_MCP_REPO_ROOT env var so it can find fixtures
    + spec md back in the source repo.
    """
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(repo_root)
    try:
        result = subprocess.run(
            [str(venv_python), "-m", "consensus_mcp._smoke_test"],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
        return False, f"exception: {exc}"
    last = (result.stdout or "").strip().splitlines()[-1:] or [""]
    ok = result.returncode == 0 and "60/60 tests passed" in last[0]
    return ok, f"exit={result.returncode} last={last[0]!r}"


def gate_server_starts(repo_root: Path, venv_console: Path) -> tuple[bool, str]:
    """G_server_starts: consensus-mcp --boot-and-exit -> exit 0 in <2s."""
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(repo_root)
    audit_sink = Path(tempfile.gettempdir()) / f"gate-check-mcp-audit-{os.getpid()}.jsonl"
    if audit_sink.exists():
        audit_sink.unlink()
    env["CONSENSUS_MCP_AUDIT_LOG"] = str(audit_sink)
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            [str(venv_console), "--boot-and-exit"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
        return False, f"exception: {exc}"
    elapsed = time.monotonic() - t0
    if audit_sink.exists():
        try:
            audit_sink.unlink()
        except OSError:
            pass
    ok = result.returncode == 0 and elapsed < 2.0
    return ok, f"exit={result.returncode} elapsed={elapsed:.2f}s"


def gate_real_iter(repo_root: Path) -> tuple[bool, str]:
    """G_real_iter: at least one closed real iteration exists with a valid closing_state.

    iter-0012 F4: prior implementation hardcoded iter-0007. Now scans every
    consensus-state/active/iteration-*/iteration-outcome.yaml, accepts closing_state
    in {quorum_close_passed, implementation_ready_apply_landed}, and proves
    against the LATEST such iteration (lex-sorted by directory name).
    """
    accepted_states = {"quorum_close_passed", "implementation_ready_apply_landed"}
    iteration_dirs = sorted(
        (repo_root / "consensus-state" / "active").glob("iteration-*"),
        key=lambda p: p.name,
    )
    matches: list[tuple[str, str]] = []
    parse_errors: list[str] = []
    for d in iteration_dirs:
        outcome = d / "iteration-outcome.yaml"
        if not outcome.exists():
            continue
        try:
            data = yaml.safe_load(outcome.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            parse_errors.append(f"{d.name}:{exc}")
            continue
        # Per codex iter-0012 codex-rev-001: extract the closing_state token strictly.
        # Many iteration-outcome.yaml files put the token on the first line of a
        # multi-line block, so isolate the first non-empty stripped line and require
        # exact membership in accepted_states (no substring containment).
        raw = (data or {}).get("closing_state")
        first_line = ""
        if isinstance(raw, str):
            for ln in raw.splitlines():
                if ln.strip():
                    first_line = ln.strip()
                    break
        state = first_line
        if state in accepted_states:
            matches.append((d.name, state))
    if not matches:
        detail = (
            f"no closed iteration found in {len(iteration_dirs)} iteration-* dirs; "
            f"accepted_states={sorted(accepted_states)}"
        )
        if parse_errors:
            detail += f"; parse_errors={parse_errors[:3]}"
        return False, detail
    latest_name, latest_state = matches[-1]
    return True, f"latest_closed={latest_name} closing_state={latest_state!r} (n={len(matches)})"


def gate_archive_section_24_synced(repo_root: Path, python: str) -> tuple[bool, str]:
    """G_archive_section_24_synced: spec md section 24 mirrors archive index.

    Per codex 2026-05-10 v2 guardrail #2: section-24 drift after each closed
    iteration is a recurring failure class (4 manual fixes this session).
    This gate blocks close until the mirror is current. Fix: run
    `python_env\\python.exe -m consensus_mcp._sync_section_24 --apply`.
    """
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(repo_root)
    try:
        result = subprocess.run(
            [python, "-m", "consensus_mcp._sync_section_24"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=30,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
        return False, f"exception: {exc}"
    ok = result.returncode == 0
    if ok:
        return True, "spec md section 24 in sync with archive index"
    return False, (
        f"DRIFT: archive-index has passes not in spec md section 24. "
        f"Run: python_env\\python.exe -m consensus_mcp._sync_section_24 --apply"
    )


def gate_pytest_dispatch_codex(repo_root: Path, python: str) -> tuple[bool, str]:
    """G_pytest_dispatch_codex: pytest test_dispatch_codex.py exits 0 with current expected count.

    Per F7 (codex review 2026-05-09): smoke covers import/help/template-loading;
    this gate runs the full pytest behavior suite so parser/subprocess/sealing
    regressions can't pass the release gate undetected.
    """
    try:
        result = subprocess.run(
            [
                python, "-m", "pytest",
                str(repo_root / "scripts" / "consensus_mcp" / "tests" / "test_dispatch_codex.py"),
                "-q",
            ],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
        return False, f"exception: {exc}"
    tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
    last = tail[0]
    # iter-0028 bumped: +14 new tests (F4 diff-body scope, F1/F2 template+validate,
    # F5 env-fail-closed). Prior: 67 (iter-0026 baseline).
    # 2026-05-10 schema/validator alignment: +1 regression test
    # (test_patch_proposal_missing_expected_tests_rejected). Then: 82.
    # v1.10.5 hardening: +5 regression tests (4 containment + 1 dispatch_failed
    # with review-target). Then: 87.
    # iter-0033: +3 regression tests (outside_repo_review_target_via_main,
    # mkdir_oserror_during_preflight, containment_case_insensitive_on_windows
    # which skips on non-win32 — but counts in "passed" via skip). Local
    # baseline 90; Windows includes the win32 test too so still 90 on Win.
    ok = result.returncode == 0 and "95 passed" in last
    return ok, f"exit={result.returncode} last={last!r}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run all 10 release gates for consensus-mcp v1.11.0."
    )
    parser.add_argument("--repo-root", default=None, help="Override REPO_ROOT.")
    args = parser.parse_args(argv)

    repo_root = _resolve_repo_root(args.repo_root)
    python = sys.executable
    print(f"REPO_ROOT: {repo_root}")
    print(f"PYTHON   : {python}\n")

    results: dict[str, tuple[bool, str]] = {}

    print("Gates 1-3 (in-tree):")
    results["G_smoke"] = gate_smoke(repo_root, python)
    _print_gate("G_smoke", *results["G_smoke"])
    results["G_validators"] = gate_validators(repo_root, python)
    _print_gate("G_validators", *results["G_validators"])
    results["G_frontmatter"] = gate_frontmatter(repo_root)
    _print_gate("G_frontmatter", *results["G_frontmatter"])

    print("\nGates 4-5 (git scoped):")
    results["G_unstaged"] = gate_unstaged(repo_root)
    _print_gate("G_unstaged", *results["G_unstaged"])
    results["G_untracked_pkg"] = gate_untracked_pkg(repo_root)
    _print_gate("G_untracked_pkg", *results["G_untracked_pkg"])

    print("\nGates 6-8 (clean-env install):")
    work = Path(tempfile.mkdtemp(prefix="consensus-mcp-rgc-"))
    venv_python: Path | None = None
    venv_console: Path | None = None
    try:
        ok_install, detail_install, venv_python, venv_console = gate_install(repo_root, python, work)
        results["G_install"] = (ok_install, detail_install)
        _print_gate("G_install", *results["G_install"])

        if ok_install and venv_python is not None and venv_console is not None:
            results["G_install_smoke"] = gate_install_smoke(repo_root, venv_python)
            _print_gate("G_install_smoke", *results["G_install_smoke"])

            results["G_server_starts"] = gate_server_starts(repo_root, venv_console)
            _print_gate("G_server_starts", *results["G_server_starts"])
        else:
            results["G_install_smoke"] = (False, "skipped: G_install failed")
            _print_gate("G_install_smoke", *results["G_install_smoke"])
            results["G_server_starts"] = (False, "skipped: G_install failed")
            _print_gate("G_server_starts", *results["G_server_starts"])
    finally:
        # Best-effort cleanup of temp venv. Permission errors on Windows are
        # tolerable; the OS reaps temp dirs eventually.
        shutil.rmtree(work, ignore_errors=True)

    print("\nGate 9 (iteration record):")
    results["G_real_iter"] = gate_real_iter(repo_root)
    _print_gate("G_real_iter", *results["G_real_iter"])

    print("\nGate 10 (dispatch pytest behavior suite):")
    results["G_pytest_dispatch_codex"] = gate_pytest_dispatch_codex(repo_root, python)
    _print_gate("G_pytest_dispatch_codex", *results["G_pytest_dispatch_codex"])

    print("\nGate 11 (archive index <-> spec md section-24 sync):")
    results["G_archive_section_24_synced"] = gate_archive_section_24_synced(repo_root, python)
    _print_gate("G_archive_section_24_synced", *results["G_archive_section_24_synced"])

    print("\nSUMMARY:")
    passed = sum(1 for ok, _ in results.values() if ok)
    total = len(results)
    for name, (ok, detail) in results.items():
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name}  -- {detail}")
    print(f"\n{passed}/{total} gates passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
