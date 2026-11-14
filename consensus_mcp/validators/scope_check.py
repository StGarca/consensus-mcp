"""scope_check.py - Phase 0 implementation-scope validator (P0-V5).

Per spec section 13 (implementation_scope schema), section 16 (verification.yaml
scope_check block), and section 20 (run_scope_check step). Validates that the
post-implementation git diff is contained within
consensus.yaml.implementation_scope.allowed_files and respects forbidden_files
and forbidden_actions.

Detects:
  - touched files outside allowed_files (FILE_OUTSIDE_ALLOWED_SCOPE)
  - touched files matching forbidden_files (FORBIDDEN_FILE_TOUCHED)
  - forbidden actions detected by path-pattern heuristics
    (FORBIDDEN_ACTION_DETECTED) -- Phase 0 best-effort, NOT full intent
    inference. Only the documented sentinel patterns trigger.
  - consensus.yaml lacking implementation_scope block
    (IMPLEMENTATION_SCOPE_MISSING_FROM_CONSENSUS)
  - git diff command failure (GIT_DIFF_FAILED)

Glob matching rules (Phase 0, simple):
  - Forward-slash paths only; backslashes (Windows-style from git on some
    configs) are normalized to '/' before matching.
  - 'a/**/b' matches 'a/b', 'a/x/b', 'a/x/y/b' (zero-or-more path segments
    between a and b).
  - '**' standalone matches anything (including paths with no separator).
  - Other wildcards delegate to fnmatch.fnmatchcase semantics: '*' matches
    any run of characters INCLUDING '/' (gitignore would not, but Phase 0
    accepts the looseness; allowed_files patterns are usually narrow enough
    that this does not over-match in practice).
  - Examples:
      "wiki/**/*.md"            matches "wiki/foo.md" -> True
      "wiki/**/*.md"            matches "wiki/consensus-mcp/foo.md" -> True
      "consensus_mcp/validators/*.py" matches "consensus_mcp/validators/scope_check.py" -> True
      "consensus_mcp/validators/*.py" matches "scripts/other/foo.py" -> False
      "dist/**/*"               matches "dist/wheel.whl" -> True

Forbidden-action detection (Phase 0 path-pattern heuristics):
  - "publish release artifact": any touched file matching
    "dist/**/*" or "build/**/*".
  - "merge to main": current branch == 'main' AND the diff range contains
    merge commits (git log --merges <a>..<b> non-empty).
  - Downstream consensus-mcp users can extend FORBIDDEN_ACTION_PATH_PATTERNS
    in a wrapper script to add project-specific heuristics.

Output: structured report (YAML by default, JSON via --json) suitable for
downstream tooling. The scope_check_block field matches the section 16
verification.yaml scope_check schema.

Usage:
  python consensus_mcp/validators/scope_check.py --consensus PATH \
    [--ref-a REF] [--ref-b REF] [--out PATH] [--json] [--self-test]

Exit codes:
  0 - validator ran cleanly; report written
  2 - validator could not run (consensus missing, parse error, missing input)

Findings count does NOT gate exit code (Path C / consistent with
validate_disposition_index.py and validate_review.py).
"""
from __future__ import annotations
import argparse
import fnmatch
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # executed as a script: prefer the co-located source tree
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from consensus_mcp.validators._shared import _dependency_version, _sha256_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / "consensus-state" / "state" / "scope-check-report.yaml"

# Path-pattern heuristics for Phase 0 forbidden-action detection.
# Keys: forbidden_action sentinel string (matched verbatim against
# implementation_scope.forbidden_actions list). Values: list of glob patterns
# whose match against any touched file triggers the FORBIDDEN_ACTION_DETECTED
# finding for that action.
#
# Default ships with one example mapping ("publish release artifact" ->
# any binary under dist/ or build/). Downstream consensus-mcp users override
# this dict in their own scope_check wrapper to add project-specific
# heuristics (e.g. mapping "render production media" -> "outputs/**/*.bin"
# is a downstream override, not a built-in default).
FORBIDDEN_ACTION_PATH_PATTERNS: dict[str, list[str]] = {
    "publish release artifact": ["dist/**/*", "build/**/*"],
}


def _parse_yaml_file(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit("pyyaml required (pip install pyyaml)")
    if not path.exists():
        raise SystemExit(f"consensus file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"yaml parse error in {path}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"consensus file root must be a mapping: {path}")
    return data


def _normalize_path(p: str) -> str:
    """Normalize a path string to forward-slash form for glob matching."""
    return p.replace("\\", "/").strip()


def _glob_match(pattern: str, path: str) -> bool:
    """Phase 0 glob matcher. Handles '**' as zero-or-more path segments.

    Strategy: try the pattern with each '/**/' segment expanded to BOTH '/'
    (zero segments) and '/*/' (one-or-more segments via fnmatch's cross-slash
    '*'). fnmatch's '*' already crosses '/', so '/*/' covers the
    one-or-more case. Trailing/leading '**' are normalized to '*'.
    """
    npat = _normalize_path(pattern)
    npath = _normalize_path(path)

    # Trailing '**' -> '*' (matches any tail).
    if npat.endswith("/**"):
        npat = npat[:-3] + "/*"
    # Leading '**' -> '*'.
    if npat.startswith("**/"):
        npat = "*/" + npat[3:]
    # Standalone '**'.
    if npat == "**":
        return True

    # Build candidate patterns by expanding each '/**/' to '/' (zero segments)
    # and to '/*/' (>=1 segments). For N occurrences, that's 2**N candidates,
    # which is fine for Phase 0 patterns (typically 0-1 '/**/' per pattern).
    candidates = [npat]
    while any("/**/" in c for c in candidates):
        next_candidates: list[str] = []
        for c in candidates:
            if "/**/" in c:
                next_candidates.append(c.replace("/**/", "/", 1))
                next_candidates.append(c.replace("/**/", "/*/", 1))
            else:
                next_candidates.append(c)
        candidates = next_candidates

    return any(fnmatch.fnmatchcase(npath, c) for c in candidates)


def _matches_any(patterns: list[str], path: str) -> bool:
    return any(_glob_match(p, path) for p in patterns if isinstance(p, str))


def _get_touched_files(ref_a: str, ref_b: str, cwd: Path) -> list[str]:
    """Return the file list from `git diff --name-only <a>..<b>` as forward-
    slash paths. Raises subprocess.CalledProcessError on failure (caller wraps
    into a GIT_DIFF_FAILED finding)."""
    result = subprocess.run(
        ["git", "-C", str(cwd), "diff", "--name-only", f"{ref_a}..{ref_b}"],
        capture_output=True,
        text=True,
        check=True,
    )
    out = result.stdout
    return [_normalize_path(line) for line in out.splitlines() if line.strip()]


def _git_current_branch(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:
        return None


def _git_has_merge_commits(ref_a: str, ref_b: str, cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "log", "--merges", "--oneline",
             f"{ref_a}..{ref_b}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())
    except Exception:
        return False


def _git_stdout(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception:
        return None


def _build_provenance(consensus_path: Path, ref_a: str, ref_b: str) -> dict:
    return {
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "command_line": sys.argv,
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "dependency_versions": {
            "PyYAML": _dependency_version("PyYAML"),
        },
        "git": {
            "head": _git_stdout(["rev-parse", "HEAD"]),
            "branch": _git_stdout(["branch", "--show-current"]),
            "ref_a": ref_a,
            "ref_b": ref_b,
        },
        "inputs": {
            "consensus_path": str(consensus_path.relative_to(REPO_ROOT)) if consensus_path.is_relative_to(REPO_ROOT) else str(consensus_path),
            "consensus_sha256": _sha256_file(consensus_path),
            "validator_script_path": "consensus_mcp/validators/scope_check.py",
            "validator_script_sha256": _sha256_file(Path(__file__).resolve()),
        },
    }


def scope_check(
    consensus_path: Path,
    ref_a: str = "HEAD~1",
    ref_b: str = "HEAD",
    *,
    _touched_files_override: list[str] | None = None,
    _current_branch_override: str | None = None,
    _has_merge_commits_override: bool | None = None,
) -> dict:
    """Validate the diff between ref_a and ref_b is within
    consensus.implementation_scope. Returns the structured report.

    Dependency-injection seams (underscore-prefixed kwargs) let the self-test
    bypass real git invocation. Production callers pass only the positional
    args.
    """
    findings: list[dict] = []

    consensus = _parse_yaml_file(consensus_path)

    impl_scope = consensus.get("implementation_scope")
    if not isinstance(impl_scope, dict):
        findings.append({
            "id": "IMPLEMENTATION_SCOPE_MISSING_FROM_CONSENSUS",
            "severity": "high",
            "field": "implementation_scope",
            "claim": "consensus.yaml has no implementation_scope block; "
                     "section 13 schema required for scope_check (P0-V5)",
        })
        return _wrap(
            findings,
            consensus_path,
            ref_a,
            ref_b,
            scope_block={
                "passed": False,
                "touched_files": [],
                "out_of_scope_files": [],
                "ref_a": ref_a,
                "ref_b": ref_b,
            },
        )

    allowed_files = impl_scope.get("allowed_files") or []
    forbidden_files = impl_scope.get("forbidden_files") or []
    forbidden_actions = impl_scope.get("forbidden_actions") or []

    if not isinstance(allowed_files, list):
        allowed_files = []
    if not isinstance(forbidden_files, list):
        forbidden_files = []
    if not isinstance(forbidden_actions, list):
        forbidden_actions = []

    # ---- Touched files ----
    if _touched_files_override is not None:
        touched = [_normalize_path(p) for p in _touched_files_override]
    else:
        try:
            touched = _get_touched_files(ref_a, ref_b, cwd=REPO_ROOT)
        except subprocess.CalledProcessError as e:
            findings.append({
                "id": "GIT_DIFF_FAILED",
                "severity": "high",
                "ref_a": ref_a,
                "ref_b": ref_b,
                "returncode": e.returncode,
                "stderr": (e.stderr or "")[:500],
                "claim": f"git diff --name-only {ref_a}..{ref_b} failed",
            })
            return _wrap(
                findings,
                consensus_path,
                ref_a,
                ref_b,
                scope_block={
                    "passed": False,
                    "touched_files": [],
                    "out_of_scope_files": [],
                    "ref_a": ref_a,
                    "ref_b": ref_b,
                },
            )
        except FileNotFoundError as e:
            # git not on PATH
            findings.append({
                "id": "GIT_DIFF_FAILED",
                "severity": "high",
                "ref_a": ref_a,
                "ref_b": ref_b,
                "claim": f"git executable not found: {e}",
            })
            return _wrap(
                findings,
                consensus_path,
                ref_a,
                ref_b,
                scope_block={
                    "passed": False,
                    "touched_files": [],
                    "out_of_scope_files": [],
                    "ref_a": ref_a,
                    "ref_b": ref_b,
                },
            )

    out_of_scope: list[str] = []
    for path in touched:
        if _matches_any(forbidden_files, path):
            out_of_scope.append(path)
            findings.append({
                "id": "FORBIDDEN_FILE_TOUCHED",
                "severity": "high",
                "path": path,
                "matched_forbidden_pattern": next(
                    (p for p in forbidden_files
                     if isinstance(p, str) and _glob_match(p, path)),
                    None,
                ),
                "claim": f"touched file {path!r} matches forbidden_files pattern",
            })
            continue
        # CR-4 (2026-05-22 security review): fail CLOSED. Dropping the
        # `allowed_files and` guard means an empty / missing / coerced-empty
        # allowed_files matches nothing, so every touched file is out-of-scope
        # instead of the previous silent allow-all.
        if not _matches_any(allowed_files, path):
            out_of_scope.append(path)
            findings.append({
                "id": "FILE_OUTSIDE_ALLOWED_SCOPE",
                "severity": "high",
                "path": path,
                "allowed_patterns": [p for p in allowed_files if isinstance(p, str)],
                "claim": f"touched file {path!r} not matched by any allowed_files pattern",
            })

    # ---- Forbidden actions (Phase 0 path-pattern heuristics) ----
    for action in forbidden_actions:
        if not isinstance(action, str):
            continue
        if action in FORBIDDEN_ACTION_PATH_PATTERNS:
            patterns = FORBIDDEN_ACTION_PATH_PATTERNS[action]
            triggered = [p for p in touched if _matches_any(patterns, p)]
            if triggered:
                findings.append({
                    "id": "FORBIDDEN_ACTION_DETECTED",
                    "severity": "high",
                    "action": action,
                    "detection_method": "path-pattern",
                    "matched_paths": triggered,
                    "patterns": patterns,
                    "claim": f"forbidden_action {action!r} detected via touched file paths",
                })
        elif action == "merge to main":
            if _current_branch_override is not None:
                branch = _current_branch_override
            else:
                branch = _git_current_branch(REPO_ROOT)
            if _has_merge_commits_override is not None:
                has_merges = _has_merge_commits_override
            else:
                has_merges = _git_has_merge_commits(ref_a, ref_b, REPO_ROOT)
            if branch == "main" and has_merges:
                findings.append({
                    "id": "FORBIDDEN_ACTION_DETECTED",
                    "severity": "high",
                    "action": action,
                    "detection_method": "branch+merge-commits",
                    "current_branch": branch,
                    "has_merge_commits_in_range": has_merges,
                    "claim": "forbidden_action 'merge to main' detected: "
                             "current branch is main and diff range contains merge commits",
                })
        # Other forbidden actions: Phase 0 has no detector; silently skip.
        # (Documented limitation; promotion to richer detection is post-Phase-0.)

    scope_block = {
        "passed": len(out_of_scope) == 0
                  and not any(f["id"] == "FORBIDDEN_ACTION_DETECTED" for f in findings),
        "touched_files": touched,
        "out_of_scope_files": out_of_scope,
        "ref_a": ref_a,
        "ref_b": ref_b,
    }

    return _wrap(findings, consensus_path, ref_a, ref_b, scope_block=scope_block)


def _wrap(
    findings: list[dict],
    consensus_path: Path,
    ref_a: str,
    ref_b: str,
    *,
    scope_block: dict,
) -> dict:
    severity_counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    return {
        "schema_version": 1,
        "validator": "scope_check",
        "validator_version": "0.1.0",
        "provenance": _build_provenance(consensus_path, ref_a, ref_b),
        "stats": {
            "total_findings": len(findings),
            "severity_counts": severity_counts,
        },
        "scope_check_block": scope_block,
        "findings": findings,
    }


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

FIXTURES_ROOT = REPO_ROOT / "consensus-state" / "tests" / "fixtures"


def _run_self_test() -> int:
    """Run the three fixture scenarios with deterministic touched-file
    overrides. Returns 0 if all pass, 1 otherwise."""
    failures: list[str] = []

    # ---- Scenario 1: known_good ----
    good = scope_check(
        FIXTURES_ROOT / "scope_check_known_good" / "consensus.yaml",
        _touched_files_override=[
            "wiki/consensus-mcp/foo.md",
            "consensus_mcp/validators/build_review_packet.py",
        ],
        _current_branch_override="renderer-may1-vintage",
        _has_merge_commits_override=False,
    )
    if not good["scope_check_block"]["passed"]:
        failures.append(
            f"known_good: scope_check_block.passed={good['scope_check_block']['passed']}, expected True"
        )
    if good["stats"]["total_findings"] != 0:
        failures.append(
            f"known_good: total_findings={good['stats']['total_findings']}, expected 0; "
            f"findings={[f['id'] for f in good['findings']]}"
        )

    # ---- Scenario 2: known_bad ----
    bad = scope_check(
        FIXTURES_ROOT / "scope_check_known_bad" / "consensus.yaml",
        _touched_files_override=[
            "dist/wheel.whl",
            "scripts/other/baz.py",
            "wiki/foo.md",
        ],
        _current_branch_override="renderer-may1-vintage",
        _has_merge_commits_override=False,
    )
    if bad["scope_check_block"]["passed"]:
        failures.append("known_bad: scope_check_block.passed=True, expected False")
    bad_ids = [f["id"] for f in bad["findings"]]
    if bad_ids.count("FORBIDDEN_FILE_TOUCHED") != 1:
        failures.append(
            f"known_bad: expected 1 FORBIDDEN_FILE_TOUCHED, got {bad_ids.count('FORBIDDEN_FILE_TOUCHED')}; "
            f"findings={bad_ids}"
        )
    if bad_ids.count("FILE_OUTSIDE_ALLOWED_SCOPE") != 1:
        failures.append(
            f"known_bad: expected 1 FILE_OUTSIDE_ALLOWED_SCOPE, got {bad_ids.count('FILE_OUTSIDE_ALLOWED_SCOPE')}; "
            f"findings={bad_ids}"
        )
    # Forbidden-action 'publish release artifact' should also trip from dist/wheel.whl.
    if bad_ids.count("FORBIDDEN_ACTION_DETECTED") < 1:
        failures.append(
            f"known_bad: expected at least 1 FORBIDDEN_ACTION_DETECTED, got {bad_ids.count('FORBIDDEN_ACTION_DETECTED')}; "
            f"findings={bad_ids}"
        )
    # wiki/foo.md SHOULD match wiki/**/*.md and be in-scope (sanity check that
    # the matcher is not over-flagging).
    if "wiki/foo.md" in bad["scope_check_block"]["out_of_scope_files"]:
        failures.append(
            "known_bad: wiki/foo.md flagged as out-of-scope; should match wiki/**/*.md"
        )

    # ---- Scenario 3: missing_scope ----
    missing = scope_check(
        FIXTURES_ROOT / "scope_check_missing_scope" / "consensus.yaml",
        _touched_files_override=["consensus_mcp/validators/scope_check.py"],
        _current_branch_override="renderer-may1-vintage",
        _has_merge_commits_override=False,
    )
    missing_ids = [f["id"] for f in missing["findings"]]
    if "IMPLEMENTATION_SCOPE_MISSING_FROM_CONSENSUS" not in missing_ids:
        failures.append(
            f"missing_scope: expected IMPLEMENTATION_SCOPE_MISSING_FROM_CONSENSUS, "
            f"got {missing_ids}"
        )
    if missing["scope_check_block"]["passed"]:
        failures.append("missing_scope: scope_check_block.passed=True, expected False")

    # ---- Report ----
    if failures:
        print("scope_check self-test FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("scope_check self-test passed (3 scenarios)")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--consensus", type=Path, help="path to consensus.yaml (required unless --self-test)")
    p.add_argument("--ref-a", default="HEAD~1", help="base git ref for diff (default: HEAD~1)")
    p.add_argument("--ref-b", default="HEAD", help="current git ref for diff (default: HEAD)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--json", action="store_true", help="emit JSON to stdout in addition to YAML to --out")
    p.add_argument("--self-test", action="store_true", help="run bundled fixture self-test and exit")
    args = p.parse_args(argv)

    if args.self_test:
        return _run_self_test()

    if args.consensus is None:
        print("error: --consensus is required (unless --self-test)", file=sys.stderr)
        return 2

    report = scope_check(args.consensus, args.ref_a, args.ref_b)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        args.out.write_text(yaml.safe_dump(report, sort_keys=False, default_flow_style=False), encoding="utf-8")
    except ImportError:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        sev = report["stats"]["severity_counts"]
        passed = report["scope_check_block"]["passed"]
        print(f"scope_check: passed={passed}, {report['stats']['total_findings']} finding(s) "
              f"({sev}) -> {args.out}")

    return 0


# === Workflow C autonomy_scope check (iter-workflow-abc-introduce) ===
#
# Workflow C (autonomous-execute) auto-approves emergent scope items if
# they fall within an operator-pre-declared autonomy_contract block in
# the goal_packet. Out-of-bound items are PARKED (not silently rejected)
# per converged-plan; halt-conditions short-circuit to operator review.
#
# Reused utilities: _glob_match, _matches_any, _normalize_path.

# Halt set defaults (iter-workflow-abc-introduce convergence - wide-by-
# default; operator opts OUT per-run via autonomy_contract.skip_halt_on).
DEFAULT_HALT_ON = (
    "blocking_objection",
    "test_suite_regression",
    "schema_change_proposed",
    "max_iterations_exceeded",
    "max_wall_clock_minutes_exceeded",
    "convergence_failure_after_n_rounds",
    "reviewer_dispatch_permanent_failure",
    "files_outside_allowed_patterns",
    "files_in_forbidden_patterns",
    "operator_interrupt_file_present",
    "reviewer_explicit_recommend_operator_review",
)

# Required fields in autonomy_contract block (per converged-plan deliverable).
AUTONOMY_CONTRACT_REQUIRED_FIELDS = (
    "max_iterations",
    "max_wall_clock_minutes",
    "allowed_file_patterns",
)


def validate_autonomy_contract(contract: dict) -> list[str]:
    """Validate the structure of an autonomy_contract block.

    Returns a list of error messages (empty list = valid).
    """
    errors: list[str] = []
    if not isinstance(contract, dict):
        return [f"autonomy_contract must be a mapping, got {type(contract).__name__}"]
    for field in AUTONOMY_CONTRACT_REQUIRED_FIELDS:
        if field not in contract:
            errors.append(f"autonomy_contract missing required field {field!r}")
    if "max_iterations" in contract:
        v = contract["max_iterations"]
        if not isinstance(v, int) or v <= 0:
            errors.append(f"autonomy_contract.max_iterations must be positive int, got {v!r}")
    if "max_wall_clock_minutes" in contract:
        v = contract["max_wall_clock_minutes"]
        if not isinstance(v, (int, float)) or v <= 0:
            errors.append(f"autonomy_contract.max_wall_clock_minutes must be positive number, got {v!r}")
    if "allowed_file_patterns" in contract:
        v = contract["allowed_file_patterns"]
        if not isinstance(v, list) or not all(isinstance(p, str) for p in v):
            errors.append("autonomy_contract.allowed_file_patterns must be a list of strings")
    if "forbidden_file_patterns" in contract:
        v = contract["forbidden_file_patterns"]
        if not isinstance(v, list) or not all(isinstance(p, str) for p in v):
            errors.append("autonomy_contract.forbidden_file_patterns must be a list of strings")
    if "skip_halt_on" in contract:
        v = contract["skip_halt_on"]
        if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
            errors.append("autonomy_contract.skip_halt_on must be a list of strings")
        else:
            for s in v:
                if s not in DEFAULT_HALT_ON:
                    errors.append(
                        f"autonomy_contract.skip_halt_on contains unknown halt condition {s!r}; "
                        f"valid: {sorted(DEFAULT_HALT_ON)}"
                    )
    return errors


def check_autonomy_scope(proposed_files: list[str], contract: dict) -> dict:
    """Decide whether a proposed scope item is auto-approvable under the
    autonomy_contract.

    Args:
        proposed_files: list of file paths the proposed scope item would touch
        contract: the autonomy_contract block from the goal_packet

    Returns:
        dict with:
          decision: "approved" | "parked" | "halt"
          reason: human-readable explanation
          violations: list of file paths that violated boundaries (if any)

    "approved" - every file is within allowed_file_patterns AND none are in
        forbidden_file_patterns. Auto-approve.
    "parked" - at least one file is OUTSIDE allowed_file_patterns but NONE are
        forbidden. Park for operator review when they return.
    "halt" - at least one file is in forbidden_file_patterns. Hard stop;
        operator must review before any further autonomous action.
    """
    errors = validate_autonomy_contract(contract)
    if errors:
        return {
            "decision": "halt",
            "reason": "autonomy_contract is invalid",
            "violations": errors,
        }

    allowed = contract.get("allowed_file_patterns", [])
    forbidden = contract.get("forbidden_file_patterns", [])

    forbidden_hits = [
        f for f in proposed_files
        if _matches_any(forbidden, _normalize_path(f))
    ]
    if forbidden_hits:
        return {
            "decision": "halt",
            "reason": "proposed scope touches forbidden file(s)",
            "violations": forbidden_hits,
        }

    out_of_scope = [
        f for f in proposed_files
        if not _matches_any(allowed, _normalize_path(f))
    ]
    if out_of_scope:
        return {
            "decision": "parked",
            "reason": "proposed scope touches files outside allowed patterns",
            "violations": out_of_scope,
        }

    return {
        "decision": "approved",
        "reason": "all proposed files within autonomy_contract bounds",
        "violations": [],
    }


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_run_self_test())
    sys.exit(main())
