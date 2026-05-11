"""Phase 4 bounded self-drive PARTIAL helper. NOT an autonomous API executor.

Per autonomy contract (docs/architecture/2026-05-09-autonomy-contract.md
Section 8): Phase 4 v1.0 implements Level 2 via the supervised-quorum-orchestrator
pattern. The orchestrator-LLM (caller) follows the autonomy contract on a sealed
goal_packet; this script provides PARTIAL enforcement via:

  - goal_packet schema validation (load + check required fields)
  - scope-signature computation + verification over the safety-critical field set
    (goal, allowed_files, allowed_sections, forbidden_files, max_iterations,
    max_patch_size, validators_required, acceptance_gates, stop_conditions,
    operator_escalation_triggers, authorization.authorized_by)
  - state-machine transitions recorded as canonical event types
  - acceptance-gate evaluation (running each `check` command + recording result)
  - changed-files-in-scope verification using exact-file, dir-prefix (trailing /),
    and fnmatch glob matchers
  - stop-rule checks for ALL 9 contract rules (full coverage):
      max_iteration_count_reached, patch_would_touch_forbidden_files,
      claude_codex_goal_satisfaction_disagreement, patch_size_exceeds_max,
      any_acceptance_gate_returns_undefined, repeated_finding_class_unresolved,
      cross_document_drift_detected, validator_reviewer_disagreement,
      closure_cross_verification_failed.

The script does NOT call Anthropic API or dispatch reviewers; that remains the
orchestrator's job. This script is an enforcement helper for stop-rule + scope
+ acceptance-gate enforcement. Reviewer dispatch + consensus synthesis remain
out-of-band orchestrator responsibilities.

USAGE
-----
  # 1. Validate goal_packet
  python -m consensus_mcp._self_drive validate <goal_packet.yaml>

  # 2. Record state transition
  python -m consensus_mcp._self_drive transition <goal_packet.yaml> <new_state> [--note "<text>"]

  # 3. Check stop rules
  python -m consensus_mcp._self_drive check_stop_rules <goal_packet.yaml> <iteration_dir>

  # 4. Evaluate acceptance gates
  python -m consensus_mcp._self_drive evaluate_gates <goal_packet.yaml>

  # 5. Verify scope (changed files match allowed_files)
  python -m consensus_mcp._self_drive verify_scope <goal_packet.yaml>

  # 6. Close (combined check; pass iff all gates green + all stop rules clear + scope clean)
  python -m consensus_mcp._self_drive close <goal_packet.yaml> <iteration_dir>

OUTPUT
------
JSON to stdout. Exit 0 = pass; non-zero = fail (stop rule fired or gate not met).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


STOP_RULES_REQUIRED_BY_CONTRACT = (
    "max_iteration_count_reached",
    "patch_would_touch_forbidden_files",
    "patch_size_exceeds_max",
    "any_acceptance_gate_returns_undefined",
    "repeated_finding_class_unresolved",
    "cross_document_drift_detected",
    "validator_reviewer_disagreement",
    "claude_codex_goal_satisfaction_disagreement",
    "closure_cross_verification_failed",
)
STOP_RULES_IMPLEMENTED = (
    "max_iteration_count_reached",
    "patch_would_touch_forbidden_files",
    "claude_codex_goal_satisfaction_disagreement",
    "patch_size_exceeds_max",
    "any_acceptance_gate_returns_undefined",
    "repeated_finding_class_unresolved",
    "cross_document_drift_detected",
    "validator_reviewer_disagreement",
    "closure_cross_verification_failed",
)

VALID_STATES = {
    "goal_received",
    "packet_built",
    "reviews_dispatched",
    "reviews_sealed",
    "consensus_ready",
    "patch_planned",
    "validator_dry_run_passed",
    "patch_applied",
    "verification_passed",
    "quorum_close_passed",
    "blocked_needs_operator",
}

TERMINAL_STATES = {"quorum_close_passed", "blocked_needs_operator"}

REQUIRED_GOAL_PACKET_FIELDS = {
    "schema_version",
    "pilot_id",
    "goal",
    "allowed_files",
    "max_iterations",
    "validators_required",
    "acceptance_gates",
    "stop_conditions",
    "authorization",
}


def _resolve_repo_root() -> Path:
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent


def _scope_signature(goal_packet: dict) -> str:
    """Canonical sha256 over the safety-critical field set per Round 9 F4.

    Covers every field that constrains safety, scope, validation, or closure. Excludes
    only authorization.scope_signature (self-reference) and authorization.authorized_at_utc
    (timestamp metadata). authorization.authorized_by IS signed.
    """
    auth = goal_packet.get("authorization", {}) or {}
    payload = {
        "goal": goal_packet.get("goal", {}),
        "allowed_files": goal_packet.get("allowed_files", []),
        "allowed_sections": goal_packet.get("allowed_sections", []),
        "forbidden_files": goal_packet.get("forbidden_files", []),
        "max_iterations": goal_packet.get("max_iterations"),
        "max_patch_size": goal_packet.get("max_patch_size"),
        "validators_required": goal_packet.get("validators_required", []),
        "acceptance_gates": goal_packet.get("acceptance_gates", []),
        "stop_conditions": goal_packet.get("stop_conditions", []),
        "operator_escalation_triggers": goal_packet.get("operator_escalation_triggers", []),
        "authorized_by": auth.get("authorized_by"),
    }
    canonical = yaml.safe_dump(yaml.safe_load(yaml.safe_dump(payload)), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_posix(path: str) -> str:
    """Normalize a path string to POSIX separators.

    iter-0036 xplat-rev-005 fix: git always emits POSIX separators (`/`), but
    user-authored goal_packet patterns may carry Windows-style `\\` separators.
    Normalize both sides before matching so the comparison is platform-stable.
    """
    return path.replace("\\", "/") if path else path


def _path_matches_pattern(path: str, pattern: str) -> bool:
    """Match a single repo-relative path against one allowed_files / forbidden_files pattern.

    Semantics per Round 9 F6:
      - exact file:        "consensus_mcp/_self_drive.py" matches that path only
      - directory prefix:  pattern with trailing "/" matches any path inside that directory
      - glob pattern:      pattern containing *, ?, or [ goes through fnmatch.fnmatchcase
      - no overmatch:      bare names like "scripts" do NOT match "scripts_old/foo.py"

    iter-0036 xplat-rev-005 fix:
      - Normalize both path and pattern to POSIX separators before comparison.
      - Use ``fnmatch.fnmatchcase`` so matching is case-sensitive on every host
        (default ``fnmatch.fnmatch`` is case-insensitive on Windows because it
        consults ``os.path.normcase``). Git paths are case-sensitive everywhere;
        the matcher must follow git, not the host filesystem.
    """
    if not pattern:
        return False
    p = _to_posix(path)
    pat = _to_posix(pattern)
    if p == pat:
        return True
    if pat.endswith("/") and p.startswith(pat):
        return True
    if any(ch in pat for ch in ("*", "?", "[")):
        return fnmatch.fnmatchcase(p, pat)
    return False


def _partition_patterns(patterns) -> tuple[set[str], list[str], list[str]]:
    """Split a pattern list into (exact_paths, dir_prefixes, glob_patterns).

    iter-0036 perf-rev-003 fix: precompute the partition once per scope-check
    so the inner forbidden / allowed loops can use O(1) set membership for
    exact paths and only iterate over the glob list. Repeated calls to
    ``_path_matches_pattern`` over N changed files * M patterns become
    O(N) + O(N * P_glob) where P_glob is typically << M.

    All patterns are pre-normalized to POSIX separators here (xplat-rev-005)
    so call sites can simply normalize the candidate path and compare directly.
    """
    exacts: set[str] = set()
    dirs: list[str] = []
    globs: list[str] = []
    for raw in (patterns or []):
        if not raw:
            continue
        pat = _to_posix(raw)
        if pat.endswith("/"):
            dirs.append(pat)
        elif any(ch in pat for ch in ("*", "?", "[")):
            globs.append(pat)
        else:
            exacts.add(pat)
    return exacts, dirs, globs


def _match_partitioned(path: str, exacts: set[str], dirs: list[str], globs: list[str]) -> str | None:
    """Return the matched pattern (or None) using the partitioned form.

    Used by the forbidden-files and scope loops to avoid the per-pair
    ``_path_matches_pattern`` work. The returned pattern is the POSIX-normalized
    form (suitable for stop-rule entries).
    """
    p = _to_posix(path)
    if p in exacts:
        return p
    for d in dirs:
        if p.startswith(d):
            return d
    for g in globs:
        if fnmatch.fnmatchcase(p, g):
            return g
    return None


def _path_in_scope(path: str, patterns) -> bool:
    return any(_path_matches_pattern(path, p) for p in (patterns or []))


def _collect_changed_files(repo_root: Path) -> list[str]:
    """Return the de-duplicated union of git's three change-views.

    Per iter-0012 F2 (codex 2026-05-10 expert verdict): the prior implementation
    used only `git diff --cached --name-only`, which silently misses unstaged
    edits and untracked files. An agent could edit a forbidden file without
    staging it and bypass `patch_would_touch_forbidden_files` / scope checks.

    This helper covers all three:
      1. `git diff --cached --name-only`           (staged)
      2. `git diff --name-only`                    (unstaged tracked)
      3. `git ls-files --others --exclude-standard` (untracked, gitignore-honoring)

    Subprocess failures on any individual view are absorbed (same graceful
    behavior as the prior cached-only path); whatever could be collected is
    returned. Returns sorted unique repo-relative paths. Empty list if all
    three fail or there are no changes.
    """
    seen: set[str] = set()
    invocations = [
        ["git", "diff", "--cached", "--name-only"],
        ["git", "diff", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    # iter-0036 bareexc-rev-009 fix: narrow the per-invocation except clause
    # from bare ``Exception`` to the concrete subprocess/IO classes, and
    # aggregate per-view diagnostics so a caller can surface partial-view
    # failures separately from clean empty output.
    # iter-0036 xplat-rev-003 fix: explicit utf-8 decoding + replace on errors
    # for every git subprocess so Windows hosts with non-utf-8 console code
    # pages don't crash on filenames with extended characters.
    failures: list[dict] = []
    for cmd in invocations:
        try:
            r = subprocess.run(
                cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
            for ln in r.stdout.splitlines():
                p = ln.strip()
                if p:
                    seen.add(p)
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append({
                "cmd": " ".join(cmd),
                "exception_type": type(exc).__name__,
                "exception": str(exc),
            })
            continue
    _collect_changed_files.last_failures = failures  # type: ignore[attr-defined]
    return sorted(seen)


def cmd_validate(args) -> int:
    """Validate goal_packet schema; recompute scope_signature; verify match."""
    packet = yaml.safe_load(Path(args.goal_packet).read_text(encoding="utf-8"))
    missing = REQUIRED_GOAL_PACKET_FIELDS - set(packet.keys())
    if missing:
        print(json.dumps({"valid": False, "error": "missing_required_fields", "missing": sorted(missing)}))
        return 1
    auth = packet.get("authorization", {})
    if "authorized_by" not in auth or auth.get("authorized_by") != "operator":
        print(json.dumps({"valid": False, "error": "authorization_missing_or_not_operator"}))
        return 1
    expected_sig = _scope_signature(packet)
    recorded_sig = auth.get("scope_signature")
    sig_match = recorded_sig == expected_sig
    print(json.dumps({
        "valid": True,
        "pilot_id": packet["pilot_id"],
        "scope_signature_match": sig_match,
        "scope_signature_expected": expected_sig,
        "scope_signature_recorded": recorded_sig,
        "allowed_files_count": len(packet["allowed_files"]),
        "acceptance_gates_count": len(packet["acceptance_gates"]),
        "max_iterations": packet["max_iterations"],
    }))
    return 0 if sig_match else 2


def cmd_transition(args) -> int:
    """Record state transition. State must be in VALID_STATES."""
    if args.new_state not in VALID_STATES:
        print(json.dumps({"ok": False, "error": "invalid_state", "valid_states": sorted(VALID_STATES)}))
        return 1
    print(json.dumps({
        "ok": True,
        "state": args.new_state,
        "terminal": args.new_state in TERMINAL_STATES,
        "note": args.note,
    }))
    return 0


def _evaluate_one_gate(gate: dict, repo_root: Path) -> dict:
    """Evaluate a single acceptance gate. Returns the per-gate result dict.

    Result-dict keys (per cmd_evaluate_gates contract):
      - id: gate id
      - passed: bool
      - One of:
          exit_code + stdout_tail (clean run)
          error: "no_check_command"  (empty/missing check field)
          exception: <str>           (subprocess raised)
    """
    gate_id = gate.get("id", "?")
    check = gate.get("check", "")
    if not check:
        return {"id": gate_id, "passed": False, "error": "no_check_command"}
    try:
        r = subprocess.run(
            check,
            shell=True,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        return {
            "id": gate_id,
            "passed": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout_tail": r.stdout.splitlines()[-1] if r.stdout else "",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        # iter-0036 bareexc-rev-005 fix: narrow from bare ``except Exception``
        # and include the exception type in the returned result so the operator
        # can distinguish (e.g.) TimeoutExpired from PermissionError without
        # needing to re-run the gate manually.
        return {
            "id": gate_id,
            "passed": False,
            "exception": str(exc),
            "exception_type": type(exc).__name__,
        }


def _read_yaml_or_empty(path: Path) -> dict:
    """Best-effort yaml load; returns {} on missing/parse failure rather than raising.

    iter-0036 bareexc-rev-001 fix: narrow from bare ``except Exception`` to the
    specific IO/decode/parse error classes so unrelated programmer errors
    (TypeError, AttributeError, KeyboardInterrupt) propagate instead of being
    silently swallowed. The behavior on missing/unreadable/unparseable files
    is unchanged.
    """
    try:
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}


def _extract_goal_satisfied(review: dict):
    """Pull goal_satisfied from a review yaml. Try top-level first, then overall_position.

    Returns the value (typically bool) or None if not present.
    """
    if "goal_satisfied" in review:
        return review.get("goal_satisfied")
    op = review.get("overall_position") or {}
    if isinstance(op, dict) and "goal_satisfied" in op:
        return op.get("goal_satisfied")
    return None


def _extract_finding_ids(review: dict, *, kind: str) -> set:
    """Pull finding ids from a review yaml. Tolerates either claude or codex shape.

    kind="claude" -> non_blocking_suggestions[].id (also blocking_objections[].id)
    kind="codex"  -> findings[].id (also blocking_objections[].id)
    """
    ids = set()
    if not isinstance(review, dict):
        return ids
    if kind == "claude":
        for entry in (review.get("non_blocking_suggestions") or []):
            if isinstance(entry, dict) and entry.get("id"):
                ids.add(entry["id"])
        for entry in (review.get("blocking_objections") or []):
            if isinstance(entry, dict) and entry.get("id"):
                ids.add(entry["id"])
    elif kind == "codex":
        for entry in (review.get("findings") or []):
            if isinstance(entry, dict) and entry.get("id"):
                ids.add(entry["id"])
        for entry in (review.get("blocking_objections") or []):
            if isinstance(entry, dict) and entry.get("id"):
                ids.add(entry["id"])
    return ids


def _canonical_sha256_of_yaml_file(p: Path) -> str:
    """Canonical-full sha256: hashlib.sha256(yaml.safe_dump(yaml.safe_load(...), sort_keys=True)).

    Matches the canonical-full form used everywhere else in this package
    (audit_append_event._canonical_sha256, review_write_and_seal, etc.).
    """
    loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    canonical = yaml.safe_dump(loaded, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_referenced_path(ref_path: str, iter_dir: Path) -> Path:
    """Resolve a reference path string (typically repo-relative) into a Path.

    Heuristics:
      1. If absolute, return as-is.
      2. Try repo_root / ref_path (the canonical case for iteration_artifacts).
      3. If ref_path has NO directory components (bare basename), fall back
         to iter_dir / Path(ref_path).name (semantically "iter-relative").
      4. Otherwise return the (non-existent) repo_root / ref_path so the
         caller sees the missing-file state instead of silently resolving
         to a same-named file elsewhere.
    Returned path may not exist; callers must check.

    iter-0035 codex-rev-004 + claude-rev-004 fix: previously step 3 applied
    to ANY missing ref_path, allowing a same-named file in iter_dir to
    satisfy a drift hash check intended for a wholly different repo-root
    location. The basename fallback is now restricted to bare basenames.
    """
    p = Path(ref_path)
    if p.is_absolute():
        return p
    repo_root = _resolve_repo_root()
    candidate = repo_root / ref_path
    if candidate.exists():
        return candidate
    # iter-dir-local fallback restricted to bare basenames (no "/" or "\\").
    # Paths with directory components MUST resolve under repo_root or be
    # reported as missing — they may not collapse to a same-name file in
    # iter_dir.
    if "/" not in ref_path and "\\" not in ref_path:
        return iter_dir / Path(ref_path).name
    return candidate


def cmd_check_stop_rules(args) -> int:
    """Check the implemented stop rules against current state. Return non-zero if any fire.

    NOTE (post-task #28): full 9/9 contract coverage (closure_cross_verification_failed
    added). Runtime is O(gate_count x
    per_gate_runtime) because 'any_acceptance_gate_returns_undefined' re-evaluates
    each gate, plus O(referenced_files) canonical-sha recomputation for
    'cross_document_drift_detected'. For goal_packets with expensive gates this
    function can take minutes. Callers that need fast metadata-only checks should
    structure their loop to avoid repeated invocations.
    """
    packet = yaml.safe_load(Path(args.goal_packet).read_text(encoding="utf-8"))
    iter_dir = Path(args.iteration_dir)
    repo_root = _resolve_repo_root()

    stop_rules_fired = []

    # iter-0036 perf-rev-002 + perf-rev-004 fix: per-invocation memoization
    # for YAML reads and canonical-sha computations. cmd_check_stop_rules
    # reads claude-review.yaml / codex-review.yaml / consensus.yaml /
    # review-packet.yaml multiple times across separate stop-rule branches;
    # without a memo, large reviews are parsed up to 4× per invocation. The
    # cache is invalidated by (resolved Path, mtime_ns, size) so a concurrent
    # writer cannot poison it across invocations.
    read_yaml_memo: dict[tuple, dict] = {}
    canonical_sha_memo: dict[tuple, str] = {}

    def _file_stamp(path: Path):
        """Cache key fragment from a path's stat. Returns None if file missing."""
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _memo_read_yaml(path: Path) -> dict:
        try:
            resolved = path.resolve()
        except OSError:
            return _read_yaml_or_empty(path)
        stamp = _file_stamp(path)
        if stamp is None:
            # File missing — _read_yaml_or_empty returns {} but we don't cache
            # under a synthetic key (the file might appear during this run for
            # tests that race; cheap to re-stat).
            return _read_yaml_or_empty(path)
        key = (resolved, stamp)
        if key not in read_yaml_memo:
            read_yaml_memo[key] = _read_yaml_or_empty(path)
        return read_yaml_memo[key]

    def _memo_canonical_sha(path: Path) -> str:
        try:
            resolved = path.resolve()
        except OSError:
            return _canonical_sha256_of_yaml_file(path)
        stamp = _file_stamp(path)
        if stamp is None:
            return _canonical_sha256_of_yaml_file(path)
        key = (resolved, stamp)
        if key not in canonical_sha_memo:
            canonical_sha_memo[key] = _canonical_sha256_of_yaml_file(path)
        return canonical_sha_memo[key]

    # iter-0036 perf-rev-001 fix: cache the union-changed-files list and the
    # numstat/untracked outputs for the duration of this single
    # cmd_check_stop_rules invocation. Multiple stop-rule branches need them
    # and previously each branch re-shelled out. The cache is per-call (a
    # local variable), so it does NOT cross check_stop_rules boundaries.
    _changed_files_cache: list[str] | None = None

    def _cached_changed_files() -> list[str]:
        nonlocal _changed_files_cache
        if _changed_files_cache is None:
            _changed_files_cache = _collect_changed_files(repo_root)
        return _changed_files_cache

    audit_path = iter_dir / "independence-audit.yaml"
    iter_count = 0
    if audit_path.exists():
        # iter-0036 xplat-rev-004 fix: unify on read_text(encoding="utf-8")
        # for YAML loads; was previously read_bytes() here (inconsistent with
        # the rest of the file and brittle on Windows when an audit log carries
        # non-utf-8 bytes from a corrupted prior write).
        # iter-0036 perf-rev-002 fix: go through the per-invocation memo so
        # the audit log is only parsed once even though both the iteration-
        # count branch and the closure-cross-verification branch consult it.
        audit = _memo_read_yaml(audit_path)
        log = audit.get("audit_log", []) or []
        iter_count = sum(1 for e in log if e.get("event") == "patch_applied")

    if iter_count >= packet.get("max_iterations", 0):
        stop_rules_fired.append({"rule": "max_iteration_count_reached", "count": iter_count, "max": packet.get("max_iterations", 0)})

    forbidden = packet.get("forbidden_files", [])
    try:
        # iter-0012 F2: union of staged + unstaged + untracked, not just staged.
        # See _collect_changed_files docstring for the safety-gap rationale.
        # iter-0036 perf-rev-001 fix: route through per-invocation cache so the
        # patch_size rule below doesn't re-shell-out for the same data.
        changed = _cached_changed_files()
        # iter-0036 perf-rev-003 + xplat-rev-005 fix: partition forbidden
        # patterns once into (exact-set, dir-prefix-list, glob-list) for O(1)
        # exact membership + a single fnmatchcase pass over precomputed glob
        # patterns. Replaces the prior O(N_changed * N_forbidden) double loop.
        f_exacts, f_dirs, f_globs = _partition_patterns(forbidden)
        for c in changed:
            matched = _match_partitioned(c, f_exacts, f_dirs, f_globs)
            if matched is not None:
                stop_rules_fired.append({
                    "rule": "patch_would_touch_forbidden_files",
                    "forbidden": matched,
                    "changed": c,
                })
    except (OSError, subprocess.SubprocessError) as exc:
        # iter-0036 bareexc fix (companion to bareexc-rev-009): narrow from
        # bare ``Exception``. The only operations inside the try are
        # _collect_changed_files (subprocess) and _match_partitioned (pure
        # string ops). Anything outside those classes indicates a programmer
        # error and should propagate.
        stop_rules_fired.append({
            "rule": "git_check_failed",
            "exception_type": type(exc).__name__,
            "exception": str(exc),
        })

    # Rule: claude_codex_goal_satisfaction_disagreement
    # Fires iff BOTH claude-review.yaml and codex-review.yaml exist AND their
    # goal_satisfied values differ. If either review is missing the loop is in
    # flight; not yet a disagreement.
    #
    # If a review file EXISTS but fails YAML parse, record a
    # 'review_yaml_parse_failed' breadcrumb (parallel to existing
    # git_check_failed precedent below) so the operator gets a signal
    # rather than a silent no-fire.
    claude_path = iter_dir / "claude-review.yaml"
    codex_path = iter_dir / "codex-review.yaml"
    for review_path in (claude_path, codex_path):
        if review_path.exists():
            # iter-0036 bareexc-rev-006 fix: distinguish file-read failures
            # (OSError / UnicodeDecodeError) from yaml parse failures
            # (yaml.YAMLError). Operator gets a typed breadcrumb instead of a
            # generic Exception string; unrelated programmer errors still raise.
            try:
                yaml.safe_load(review_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                stop_rules_fired.append({
                    "rule": "review_yaml_read_failed",
                    "path": str(review_path),
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                })
            except yaml.YAMLError as exc:
                stop_rules_fired.append({
                    "rule": "review_yaml_parse_failed",
                    "path": str(review_path),
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                })
    if claude_path.exists() and codex_path.exists():
        claude_review = _memo_read_yaml(claude_path)
        codex_review = _memo_read_yaml(codex_path)
        claude_gs = _extract_goal_satisfied(claude_review)
        codex_gs = _extract_goal_satisfied(codex_review)
        if claude_gs is not None and codex_gs is not None and claude_gs != codex_gs:
            stop_rules_fired.append({
                "rule": "claude_codex_goal_satisfaction_disagreement",
                "claude_goal_satisfied": claude_gs,
                "codex_goal_satisfied": codex_gs,
                "reason": f"claude.goal_satisfied={claude_gs}, codex.goal_satisfied={codex_gs}, disagree",
            })

    # Rule: patch_size_exceeds_max
    # iter-0035 codex-rev-001/002 + claude-rev-001/002 fix: count the union
    # of staged + unstaged tracked changes (via `git diff --numstat HEAD`,
    # which covers both views) PLUS untracked file line counts. Previously
    # the code branched between staged-only and HEAD-only, missing the
    # unstaged-when-staged-present case AND missing untracked files entirely.
    # Codex pre-review (iter-0035 codex-rev-001/002) added two refinements:
    #   (a) do NOT dereference symlinks; count each as 1 (the link itself)
    #   (b) count files with no trailing newline correctly (text.count("\n")
    #       alone undercounts a 1-line no-newline file as 0)
    max_patch_size = packet.get("max_patch_size")
    if max_patch_size:
        try:
            total = 0
            # Tracked: staged + unstaged in one view via `git diff --numstat HEAD`
            # iter-0036 xplat-rev-003 fix: explicit utf-8 + errors=replace.
            ns = subprocess.run(
                ["git", "diff", "--numstat", "HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
            for line in ns.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                # Binary files report "-\t-"; treat as 0
                try:
                    added = int(parts[0]) if parts[0] != "-" else 0
                    deleted = int(parts[1]) if parts[1] != "-" else 0
                except ValueError:
                    continue
                total += added + deleted

            # Untracked: each new file contributes its full line count.
            # `git ls-files --others --exclude-standard` honors .gitignore.
            # iter-0036 xplat-rev-003 fix: explicit utf-8 + errors=replace.
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
            for line in untracked.stdout.splitlines():
                rel = line.strip()
                if not rel:
                    continue
                f = repo_root / rel
                try:
                    # codex-rev-001 (iter-0035 pre-review): don't dereference
                    # symlinks; count as 1 added line for the link text itself
                    # (matches what `git add -A && git commit` would commit).
                    if f.is_symlink():
                        total += 1
                        continue
                    with f.open("rb") as fh:
                        content = fh.read()
                    try:
                        text = content.decode("utf-8")
                    except UnicodeDecodeError:
                        # Binary file; mirror numstat's "-\t-" → 0 lines.
                        continue
                    # codex-rev-002 (iter-0035 pre-review): text.count("\n")
                    # counts \n separators only; a 1-line file with no
                    # trailing newline would count as 0. Add 1 when the
                    # content is non-empty and lacks a trailing newline.
                    lines = text.count("\n")
                    if text and not text.endswith("\n"):
                        lines += 1
                    total += lines
                except OSError:
                    continue

            if total > max_patch_size:
                stop_rules_fired.append({
                    "rule": "patch_size_exceeds_max",
                    "patch_size": total,
                    "max": max_patch_size,
                    "reason": f"patch_size={total}, max={max_patch_size}",
                })
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            # iter-0036 bareexc-rev-002 fix: narrow from bare ``Exception``
            # and emit a typed breadcrumb so the operator sees that patch-size
            # enforcement was DISABLED for this evaluation due to git/IO
            # trouble, rather than the rule silently passing.
            stop_rules_fired.append({
                "rule": "patch_size_check_failed",
                "exception_type": type(exc).__name__,
                "exception": str(exc),
            })

    # Rule: any_acceptance_gate_returns_undefined
    # In-process re-evaluation of every acceptance_gate. A gate is "undefined"
    # iff its result dict has key `exception` or key `error == 'no_check_command'`.
    undefined_gates = []
    for gate in packet.get("acceptance_gates", []) or []:
        result = _evaluate_one_gate(gate, repo_root)
        if "exception" in result:
            undefined_gates.append({"id": result["id"], "kind": "exception", "detail": result["exception"]})
        elif result.get("error") == "no_check_command":
            undefined_gates.append({"id": result["id"], "kind": "no_check_command"})
    if undefined_gates:
        stop_rules_fired.append({
            "rule": "any_acceptance_gate_returns_undefined",
            "undefined_gates": undefined_gates,
            "reason": f"{len(undefined_gates)} gate(s) undefined: " + ", ".join(
                f"{g['id']}({g['kind']})" for g in undefined_gates
            ),
        })

    # Rule: repeated_finding_class_unresolved
    # A non-blocking finding with the same id appears in BOTH current iteration's
    # review yamls AND the immediately-prior iteration's review yamls. Per-reviewer
    # intersection (claude-current ∩ claude-prior; codex-current ∩ codex-prior; OR'd).
    # Cross-reviewer matches do NOT fire (different id spaces).
    parent_dir = iter_dir.parent
    if parent_dir.exists():
        try:
            siblings = sorted(
                d.name for d in parent_dir.iterdir()
                if d.is_dir() and d.name.startswith("iteration-")
            )
        except OSError:
            siblings = []
        if iter_dir.name in siblings:
            idx = siblings.index(iter_dir.name)
            if idx > 0:
                prior_name = siblings[idx - 1]
                prior_dir = parent_dir / prior_name

                claude_prior = _memo_read_yaml(prior_dir / "claude-review.yaml")
                claude_curr = _memo_read_yaml(claude_path)
                codex_prior = _memo_read_yaml(prior_dir / "codex-review.yaml")
                codex_curr = _memo_read_yaml(codex_path)

                claude_overlap = (
                    _extract_finding_ids(claude_curr, kind="claude")
                    & _extract_finding_ids(claude_prior, kind="claude")
                )
                codex_overlap = (
                    _extract_finding_ids(codex_curr, kind="codex")
                    & _extract_finding_ids(codex_prior, kind="codex")
                )
                recurring = sorted(claude_overlap | codex_overlap)
                if recurring:
                    stop_rules_fired.append({
                        "rule": "repeated_finding_class_unresolved",
                        "prior_iteration": prior_name,
                        "recurring_ids": recurring,
                        "reason": (
                            f"{len(recurring)} finding(s) reappeared from "
                            f"{prior_name}: {', '.join(recurring)}"
                        ),
                    })

    # Rule: cross_document_drift_detected
    # Validate explicit, named sha256 fields against actual canonical-full sha
    # of the referenced files. Anti-false-positive: only check explicit fields
    # named below, NOT recursive scan for any *sha256* key.
    # NOTE: Codex `dispatch_provenance` shas are sealed inside the T6 archive
    # and use a different sha algorithm (raw-bytes sha256 via _sha256_str on
    # the goal_packet text, not canonical-full yaml.safe_dump sha). The drift
    # rule does not second-guess them.
    drift_findings = []

    def _check_drift(claim_label: str, ref_path: str, claimed_sha: str):
        """Append a drift finding if the claimed_sha doesn't match the actual file."""
        if not claimed_sha or not ref_path:
            return
        target = _resolve_referenced_path(ref_path, iter_dir)
        if not target.exists():
            drift_findings.append({
                "claim": claim_label,
                "referenced_path": ref_path,
                "claimed_sha": claimed_sha,
                "reason": "referenced_file_missing",
            })
            return
        try:
            # iter-0036 perf-rev-004 fix: route through per-invocation memo so
            # a single file referenced from multiple claim labels (e.g. the
            # same review-packet.yaml cited by both iteration_artifacts and
            # consensus.reviewed_artifacts) is only hashed once.
            actual = _memo_canonical_sha(target)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            # iter-0036 bareexc-rev-008 fix: narrow from bare ``Exception`` to
            # the concrete IO/decode/parse error classes. Unrelated programmer
            # errors (e.g. TypeError from upstream refactors) now propagate
            # instead of being swallowed into a drift-finding entry.
            drift_findings.append({
                "claim": claim_label,
                "referenced_path": ref_path,
                "claimed_sha": claimed_sha,
                "reason": f"canonical_sha_compute_failed: {type(exc).__name__}: {exc}",
            })
            return
        if actual != claimed_sha:
            drift_findings.append({
                "claim": claim_label,
                "referenced_path": ref_path,
                "claimed_sha": claimed_sha,
                "actual_sha": actual,
            })

    review_packet_path = iter_dir / "review-packet.yaml"
    review_packet = _memo_read_yaml(review_packet_path)
    for art in (review_packet.get("iteration_artifacts") or []):
        if isinstance(art, dict):
            sha = art.get("canonical_sha256")
            ref = art.get("path")
            if sha and ref:
                _check_drift(
                    f"review-packet.yaml.iteration_artifacts.{Path(ref).name}.canonical_sha256",
                    ref, sha,
                )

    claude_review_for_drift = _memo_read_yaml(claude_path)
    claude_reviewed_sha = claude_review_for_drift.get("reviewed_packet_sha256")
    if claude_reviewed_sha and review_packet_path.exists():
        _check_drift(
            "claude-review.yaml.reviewed_packet_sha256",
            str(review_packet_path), claude_reviewed_sha,
        )

    consensus = _memo_read_yaml(iter_dir / "consensus.yaml")
    reviewed_artifacts = consensus.get("reviewed_artifacts") or {}
    if isinstance(reviewed_artifacts, dict):
        for art_name, art_block in reviewed_artifacts.items():
            if isinstance(art_block, dict):
                sha = art_block.get("canonical_sha256")
                ref = art_block.get("path")
                if sha and ref:
                    _check_drift(
                        f"consensus.yaml.reviewed_artifacts.{art_name}.canonical_sha256",
                        ref, sha,
                    )

    for finding in drift_findings:
        entry = {"rule": "cross_document_drift_detected"}
        entry.update(finding)
        stop_rules_fired.append(entry)

    # Rule: validator_reviewer_disagreement
    # Validator status read from any of these paths (first match wins):
    #   1. review-packet.yaml -> verification_checks.acceptance_gates_evaluated
    #      .{all_passed|status} (original synthetic path)
    #   2. review-packet.yaml -> verification_results.acceptance_gates
    #      .{all_passed|status} (real-world iter-0009 path)
    #   3. iter_dir/verification.yaml -> verification_checks.acceptance_gates_evaluated
    #      .{all_passed|status} (separate verification.yaml fallback)
    # Reviewer position: goal_satisfied (top-level or overall_position) +
    # blocking_objections (list).
    # Disagreement cases:
    #   - validator PASS but a reviewer says goal_satisfied=False or has blockers -> fire
    #   - validator FAIL but BOTH reviewers say goal_satisfied=True with empty blockers -> fire
    def _coerce_validator_status(node):
        """Read all_passed (bool) then status (str) off a dict; return 'pass'|'fail'|None."""
        if not isinstance(node, dict):
            return None
        if isinstance(node.get("all_passed"), bool):
            return "pass" if node["all_passed"] else "fail"
        s = node.get("status")
        if isinstance(s, str) and s.lower() in ("pass", "fail"):
            return s.lower()
        return None

    validator_status = None  # None | "pass" | "fail"
    # Path 1 + 2: from review-packet.yaml
    vc = review_packet.get("verification_checks") if isinstance(review_packet, dict) else None
    age_synth = vc.get("acceptance_gates_evaluated") if isinstance(vc, dict) else None
    validator_status = _coerce_validator_status(age_synth)
    if validator_status is None:
        vr = review_packet.get("verification_results") if isinstance(review_packet, dict) else None
        age_real = vr.get("acceptance_gates") if isinstance(vr, dict) else None
        validator_status = _coerce_validator_status(age_real)
    # Path 3: tertiary fallback - separate verification.yaml file in iter_dir
    if validator_status is None:
        verification_yaml = _memo_read_yaml(iter_dir / "verification.yaml")
        v_vc = verification_yaml.get("verification_checks") if isinstance(verification_yaml, dict) else None
        v_age = v_vc.get("acceptance_gates_evaluated") if isinstance(v_vc, dict) else None
        validator_status = _coerce_validator_status(v_age)

    if validator_status is not None:
        claude_v = _memo_read_yaml(claude_path) if claude_path.exists() else None
        codex_v = _memo_read_yaml(codex_path) if codex_path.exists() else None

        def _reviewer_clean(r):
            """Reviewer says 'goal satisfied AND no blockers'.

            iter-0035 codex-rev-002 + claude-rev-002 fix: check
            blocking_objections BEFORE the goal_satisfied early-return.
            A reviewer that emits blockers but forgets goal_satisfied
            previously had its blocker silently ignored — `_reviewer_clean`
            returned None and the caller's `clean is False` identity check
            failed, dropping the reviewer from `disagreers`.
            """
            if not isinstance(r, dict):
                return None
            blockers = r.get("blocking_objections") or []
            if blockers:
                return False
            gs = _extract_goal_satisfied(r)
            if gs is None:
                return None
            return gs is True

        claude_clean = _reviewer_clean(claude_v) if claude_v is not None else None
        codex_clean = _reviewer_clean(codex_v) if codex_v is not None else None

        if validator_status == "pass":
            disagreers = []
            if claude_clean is False:
                disagreers.append("claude")
            if codex_clean is False:
                disagreers.append("codex")
            if disagreers:
                stop_rules_fired.append({
                    "rule": "validator_reviewer_disagreement",
                    "validator_status": "pass",
                    "disagreeing_reviewers": disagreers,
                    "reason": (
                        f"validator=pass but {','.join(disagreers)} reviewer(s) "
                        f"flagged goal_satisfied=False or non-empty blocking_objections"
                    ),
                })
        elif validator_status == "fail":
            # Fire iff BOTH reviewers say clean (ignoring failed gates).
            if claude_clean is True and codex_clean is True:
                stop_rules_fired.append({
                    "rule": "validator_reviewer_disagreement",
                    "validator_status": "fail",
                    "disagreeing_reviewers": ["claude", "codex"],
                    "reason": (
                        "validator=fail but both reviewers say "
                        "goal_satisfied=True with empty blocking_objections"
                    ),
                })

    # Rule: closure_cross_verification_failed (Task #28; refined v5 Finding 1)
    # Three-part gate: cross_family + hash_match + freshness. Fires when:
    #   - last_mutation event exists (apply_step_landed in audit log), AND
    #   - a closing-verdict review (claude or codex) exists that came AFTER
    #     last_mutation.timestamp, AND
    #   - the verdict fails any of the three invariant checks.
    # If no last_mutation: no fire (close paths without mutation aren't gated).
    # If no close-attempt review came after last_mutation: no fire (loop in flight).
    # iter-0035 codex-rev-003 + claude-rev-003 fix: emit a stop_rule entry
    # when the _closure_invariant module fails to import. Previously a bare
    # `except Exception` silently set _check_inv=None, dropping runtime
    # coverage from the 9/9 static contract to 8/9 with no operator signal.
    # Now we narrow the except to (ImportError, ModuleNotFoundError) so
    # unrelated exceptions propagate, and emit closure_invariant_module_
    # unavailable to make the failure visible.
    try:
        from consensus_mcp._closure_invariant import (
            check_closure_invariant as _check_inv,
            last_mutation_from_audit as _lm_from_audit,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        _check_inv = None
        _lm_from_audit = None
        stop_rules_fired.append({
            "rule": "closure_invariant_module_unavailable",
            "exception": f"{type(exc).__name__}: {exc}",
        })

    if _check_inv is not None and audit_path.exists():
        # iter-0036 xplat-rev-004 fix: unify on read_text(encoding="utf-8").
        # iter-0036 perf-rev-002 fix: routed through the per-invocation memo
        # so the audit log is shared with the iteration-count branch above
        # instead of being re-parsed.
        audit = _memo_read_yaml(audit_path)
        log = audit.get("audit_log", []) or []
        last_mutation = _lm_from_audit(log)
        if last_mutation is not None:
            lm_ts = last_mutation.get("timestamp") or last_mutation.get("timestamp_utc") or ""
            # Find the most recent of {claude-review.yaml, codex-review.yaml}
            # whose created_at_utc came AFTER last_mutation.timestamp.
            candidates = []
            for review_path in (claude_path, codex_path):
                if review_path.exists():
                    review = _memo_read_yaml(review_path)
                    closer_ts = review.get("created_at_utc")
                    if closer_ts and closer_ts > lm_ts:
                        candidates.append((closer_ts, review_path, review))
            # Asymmetric with T6 by design (per Task #28 v4 spec): this stop
            # rule does NOT fire on "mutation + no fresh review" — that state is
            # operational normal during in-flight iterations. The fail-closed
            # gate is at T6 (audit_append_event for iteration_closed events).
            # Real loop.run_goal-driven close requires both reviews; this rule
            # only adds extra safety when reviews ARE present and stale/self.
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                _, closer_path, closer_verdict = candidates[0]
                inv_result = _check_inv(last_mutation, closer_verdict)
                if not inv_result["ok"]:
                    closer_actor = closer_verdict.get("actor")
                    lm_actor = last_mutation.get("actor")
                    closer_actor_id = (
                        closer_actor.get("id") if isinstance(closer_actor, dict) else closer_actor
                    )
                    lm_actor_id = (
                        lm_actor.get("id") if isinstance(lm_actor, dict) else lm_actor
                    )
                    stop_rules_fired.append({
                        "rule": "closure_cross_verification_failed",
                        "checks": inv_result["checks"],
                        "reason": inv_result["reason"],
                        "last_mutation_actor": lm_actor_id,
                        "closer_actor": closer_actor_id,
                        "closer_path": str(closer_path),
                    })

    unimplemented = [r for r in STOP_RULES_REQUIRED_BY_CONTRACT if r not in STOP_RULES_IMPLEMENTED]
    print(json.dumps({
        "stop_rules_fired": stop_rules_fired,
        "ok": len(stop_rules_fired) == 0,
        "coverage": {
            "implemented": list(STOP_RULES_IMPLEMENTED),
            "required_by_contract": list(STOP_RULES_REQUIRED_BY_CONTRACT),
            "unimplemented": unimplemented,
            "warning": (
                "PARTIAL helper. Orchestrator must check unimplemented rules out-of-band."
                if unimplemented else
                "FULL contract coverage (9/9 stop rules codified including closure_cross_verification_failed)."
            ),
        },
    }))
    return 0 if not stop_rules_fired else 1


def cmd_evaluate_gates(args) -> int:
    """Evaluate each acceptance_gate's check command. Pass iff all return exit 0."""
    packet = yaml.safe_load(Path(args.goal_packet).read_text(encoding="utf-8"))
    repo_root = _resolve_repo_root()
    results = [_evaluate_one_gate(g, repo_root) for g in packet.get("acceptance_gates", [])]
    all_passed = all(g["passed"] for g in results)
    print(json.dumps({"all_passed": all_passed, "gates": results}))
    return 0 if all_passed else 1


def cmd_verify_scope(args) -> int:
    """Verify changed files (git index + working tree + untracked) are within allowed_files scope.

    iter-0012 F2: scope check uses _collect_changed_files (staged + unstaged +
    untracked union), not `git diff --cached` alone. Prior cached-only behavior
    silently passed if forbidden/out-of-scope edits were left unstaged.
    """
    packet = yaml.safe_load(Path(args.goal_packet).read_text(encoding="utf-8"))
    allowed = list(packet.get("allowed_files", []))
    repo_root = _resolve_repo_root()
    # iter-0036: _collect_changed_files now absorbs subprocess/IO failures
    # into its `last_failures` diagnostic and returns whatever it could
    # collect, so the surrounding try/except is no longer needed. (Bare
    # ``except Exception`` here would also have masked unrelated programmer
    # errors.)
    changed = _collect_changed_files(repo_root)

    # iter-0036 perf-rev-003 + xplat-rev-005 fix: partition once and use the
    # partitioned matcher to avoid the O(N * M) double loop hidden inside
    # ``_path_in_scope``.
    a_exacts, a_dirs, a_globs = _partition_patterns(allowed)
    out_of_scope = [
        c for c in changed
        if _match_partitioned(c, a_exacts, a_dirs, a_globs) is None
    ]
    has_allowed_sections = bool(packet.get("allowed_sections"))
    print(json.dumps({
        "in_scope": len(out_of_scope) == 0,
        "changed_count": len(changed),
        "out_of_scope": out_of_scope,
        "allowed_sections_evaluated": False,
        "allowed_sections_note": (
            "allowed_sections present in goal_packet but section-level scope is NOT evaluated by this helper "
            "(per Round 9 F6: section-level scope requires T9/T10 integration; not in iter-0009 stabilization scope). "
            "Orchestrator must verify allowed_sections out-of-band."
            if has_allowed_sections else "no allowed_sections specified in goal_packet"
        ),
    }))
    return 0 if not out_of_scope else 1


def cmd_close(args) -> int:
    """Combined: validate + check_stop_rules + evaluate_gates + verify_scope. Pass iff all pass."""
    overall = {"validate": None, "stop_rules": None, "gates": None, "scope": None}
    rc_validate = cmd_validate(argparse.Namespace(goal_packet=args.goal_packet))
    overall["validate"] = rc_validate == 0
    rc_stop = cmd_check_stop_rules(argparse.Namespace(goal_packet=args.goal_packet, iteration_dir=args.iteration_dir))
    overall["stop_rules"] = rc_stop == 0
    rc_gates = cmd_evaluate_gates(argparse.Namespace(goal_packet=args.goal_packet))
    overall["gates"] = rc_gates == 0
    rc_scope = cmd_verify_scope(argparse.Namespace(goal_packet=args.goal_packet))
    overall["scope"] = rc_scope == 0
    can_close = all(overall.values())
    print(json.dumps({"can_close": can_close, "components": overall}, default=str))
    return 0 if can_close else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 4 bounded self-drive enforcement harness.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("validate")
    sp.add_argument("goal_packet")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("transition")
    sp.add_argument("goal_packet")
    sp.add_argument("new_state")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_transition)

    sp = sub.add_parser("check_stop_rules")
    sp.add_argument("goal_packet")
    sp.add_argument("iteration_dir")
    sp.set_defaults(func=cmd_check_stop_rules)

    sp = sub.add_parser("evaluate_gates")
    sp.add_argument("goal_packet")
    sp.set_defaults(func=cmd_evaluate_gates)

    sp = sub.add_parser("verify_scope")
    sp.add_argument("goal_packet")
    sp.set_defaults(func=cmd_verify_scope)

    sp = sub.add_parser("close")
    sp.add_argument("goal_packet")
    sp.add_argument("iteration_dir")
    sp.set_defaults(func=cmd_close)

    ns = p.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
