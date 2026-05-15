# iter-0035 proposed patches — pre-implementation review target

Claude-authored fix patches for the 4 iter-0034 findings against
`_self_drive.py`. Codex declined patch authorship on all 4 in iter-0034
citing context limitations; per operator-locked workflow #4, claude
proposes here and codex pre-reviews the diffs BEFORE implementation.

Reviewer task: assess each patch for correctness, regression risk,
scope, and test coverage. NO code on disk reflects these patches yet.

---

## Patch 1 — codex-rev-001 + claude-rev-001 (HIGH)

**Defect**: `patch_size_exceeds_max` counts only staged diff (if any
staged exists) OR HEAD diff (if no staged); never counts untracked.
Operator can stage 5 lines, leave 5000 unstaged → check passes. New
untracked files (any size) never count.

**Approach**: count the union of (a) tracked changes since HEAD via
`git diff --numstat HEAD` (covers staged + unstaged in one view) plus
(b) untracked file line counts (each untracked file contributes its
total line count as "added"). This mirrors what `git add -A && git
commit` would commit.

```diff
--- a/scripts/agent_loop_mcp/_self_drive.py
+++ b/scripts/agent_loop_mcp/_self_drive.py
@@ -443,42 +443,72 @@ def cmd_check_stop_rules(args) -> int:
     # Rule: patch_size_exceeds_max
-    # Sum added+deleted across `git diff --cached --numstat` (or `--numstat HEAD`
-    # if nothing staged). Fire iff total > max_patch_size. None/0 cap = no fire.
+    # iter-0035 codex-rev-001 fix: count the union of staged + unstaged
+    # tracked changes (via `git diff --numstat HEAD`) PLUS untracked file
+    # line counts. Previously the code branched between staged-only and
+    # HEAD-only, missing unstaged-when-staged-present and missing
+    # untracked files entirely.
     max_patch_size = packet.get("max_patch_size")
     if max_patch_size:
         try:
+            total = 0
+            # Tracked: staged + unstaged in one view via `git diff --numstat HEAD`
             ns = subprocess.run(
-                ["git", "diff", "--cached", "--numstat"],
+                ["git", "diff", "--numstat", "HEAD"],
                 cwd=str(repo_root),
                 capture_output=True,
                 text=True,
                 check=False,
                 timeout=30,
             )
             numstat_text = ns.stdout
-            if not numstat_text.strip():
-                ns = subprocess.run(
-                    ["git", "diff", "--numstat", "HEAD"],
-                    cwd=str(repo_root),
-                    capture_output=True,
-                    text=True,
-                    check=False,
-                    timeout=30,
-                )
-                numstat_text = ns.stdout
-            total = 0
             for line in numstat_text.splitlines():
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
+
+            # Untracked: each new file contributes its full line count.
+            # `git ls-files --others --exclude-standard` honors .gitignore.
+            untracked = subprocess.run(
+                ["git", "ls-files", "--others", "--exclude-standard"],
+                cwd=str(repo_root),
+                capture_output=True,
+                text=True,
+                check=False,
+                timeout=30,
+            )
+            for line in untracked.stdout.splitlines():
+                rel = line.strip()
+                if not rel:
+                    continue
+                f = repo_root / rel
+                try:
+                    # Count lines; treat binary as 0 (no decode).
+                    with f.open("rb") as fh:
+                        content = fh.read()
+                    # If decode fails, treat as binary (0 lines).
+                    try:
+                        text = content.decode("utf-8")
+                    except UnicodeDecodeError:
+                        continue
+                    total += text.count("\n")
+                except OSError:
+                    continue
+
             if total > max_patch_size:
                 stop_rules_fired.append({
                     "rule": "patch_size_exceeds_max",
                     "patch_size": total,
                     "max": max_patch_size,
                     "reason": f"patch_size={total}, max={max_patch_size}",
                 })
         except Exception:
```

**Test coverage**: 3 new regression tests:
1. `test_patch_size_counts_unstaged_when_staged_present` — stage 5 lines, leave 5000 unstaged → fires.
2. `test_patch_size_counts_untracked_files` — no staged or unstaged changes, but a 5000-line untracked file → fires.
3. `test_patch_size_treats_untracked_binary_as_zero` — untracked binary file (e.g., random bytes) doesn't crash or inflate count.

---

## Patch 2 — codex-rev-002 + claude-rev-002 (HIGH)

**Defect**: `_reviewer_clean` returns None when `goal_satisfied` missing
BEFORE checking `blocking_objections`. Caller's `if claude_clean is False`
fails identity check on None, so blocker is silently ignored when
validator_status="pass".

**Approach**: check blockers FIRST; if blockers non-empty, return False
regardless of goal_satisfied presence. Only fall back to the None
"in-flight" semantic when there's no signal at all (no blockers AND no
goal_satisfied).

```diff
--- a/scripts/agent_loop_mcp/_self_drive.py
+++ b/scripts/agent_loop_mcp/_self_drive.py
@@ -679,9 +679,16 @@ def cmd_check_stop_rules(args) -> int:
         def _reviewer_clean(r):
             """Reviewer says 'goal satisfied AND no blockers'."""
             if not isinstance(r, dict):
                 return None
+            # iter-0035 codex-rev-002 fix: check blocking_objections BEFORE
+            # the goal_satisfied early-return. A reviewer that emits
+            # blockers but forgets goal_satisfied: false would previously
+            # have its blocker silently ignored (return None -> caller's
+            # identity check `clean is False` failed -> not in disagreers).
+            blockers = r.get("blocking_objections") or []
+            if blockers:
+                return False
             gs = _extract_goal_satisfied(r)
-            blockers = r.get("blocking_objections") or []
             if gs is None:
                 return None
             return gs is True and not blockers
```

**Test coverage**: 2 new regression tests:
1. `test_reviewer_with_blockers_but_missing_goal_satisfied_is_not_clean` —
   author a synthetic claude-review.yaml with `blocking_objections: [x]`
   and no `goal_satisfied` field; assert `validator_reviewer_disagreement`
   fires under validator pass.
2. `test_reviewer_with_no_signal_returns_in_flight` — author a review
   with neither blockers nor goal_satisfied; assert it's treated as
   in-flight (no disagreement fired).

---

## Patch 3 — codex-rev-003 + claude-rev-003 (MEDIUM)

**Defect**: `from agent_loop_mcp._closure_invariant import ...` inside
bare `try: ... except Exception:` silently sets `_check_inv = None` on
ANY exception. Static 9/9 coverage; runtime can be 8/9 with no operator
signal.

**Approach**: emit a `closure_invariant_module_unavailable` stop_rule
entry when the import fails. Narrow the except to
`(ImportError, ModuleNotFoundError)` so unrelated exceptions (e.g., a
NameError during module load) propagate as a hard failure rather than
silent skip.

```diff
--- a/scripts/agent_loop_mcp/_self_drive.py
+++ b/scripts/agent_loop_mcp/_self_drive.py
@@ -726,11 +726,23 @@ def cmd_check_stop_rules(args) -> int:
     #     last_mutation.timestamp, AND
     #   - the verdict fails any of the three invariant checks.
     # If no last_mutation: no fire (close paths without mutation aren't gated).
     # If no close-attempt review came after last_mutation: no fire (loop in flight).
+    # iter-0035 codex-rev-003 fix: emit a stop_rule entry when the
+    # _closure_invariant module fails to import. Previously a bare
+    # `except Exception` silently set _check_inv=None, dropping
+    # runtime coverage from 9/9 to 8/9 with no operator signal. Now we
+    # narrow the except to (ImportError, ModuleNotFoundError) and emit
+    # closure_invariant_module_unavailable so the failure is visible.
+    _check_inv_import_error: Exception | None = None
     try:
         from agent_loop_mcp._closure_invariant import (
             check_closure_invariant as _check_inv,
             last_mutation_from_audit as _lm_from_audit,
         )
-    except Exception:
+    except (ImportError, ModuleNotFoundError) as exc:
         _check_inv = None
         _lm_from_audit = None
+        _check_inv_import_error = exc
+        stop_rules_fired.append({
+            "rule": "closure_invariant_module_unavailable",
+            "exception": f"{type(exc).__name__}: {exc}",
+        })

     if _check_inv is not None and audit_path.exists():
```

**Test coverage**: 1 new regression test:
1. `test_closure_invariant_import_failure_emits_stop_rule` — monkeypatch
   `sys.modules['agent_loop_mcp._closure_invariant']` to None (forces
   ImportError on re-import), invoke `cmd_check_stop_rules`, assert
   `closure_invariant_module_unavailable` appears in stop_rules_fired.

---

## Patch 4 — codex-rev-004 + claude-rev-004 (MEDIUM)

**Defect**: `_resolve_referenced_path` basename fallback. When
`repo_root / ref_path` doesn't exist, returns `iter_dir / basename(ref_path)`.
A same-name collision can satisfy drift hash check from the wrong file.

**Approach**: Drop the basename fallback for paths with directory
components. Iter-dir-local fallback applies only to bare basenames
(semantically "this is iter-relative, no directory components implied").

```diff
--- a/scripts/agent_loop_mcp/_self_drive.py
+++ b/scripts/agent_loop_mcp/_self_drive.py
@@ -350,16 +350,28 @@ def _canonical_sha256_of_yaml_file(p: Path) -> str:
 def _resolve_referenced_path(ref_path: str, iter_dir: Path) -> Path:
     """Resolve a reference path string (typically repo-relative) into a Path.

     Heuristics:
       1. If absolute, return as-is.
       2. Try repo_root / ref_path (the canonical case for iteration_artifacts).
-      3. Fall back to iter_dir / basename(ref_path).
+      3. If ref_path has NO directory components (bare basename), fall back
+         to iter_dir / Path(ref_path).name (semantically iter-relative).
+      4. Otherwise return the (non-existent) repo_root / ref_path so the
+         caller sees the missing-file state instead of silently resolving
+         to a same-named file elsewhere.
     Returned path may not exist; callers must check.
+
+    iter-0035 codex-rev-004 fix: previously step 3 applied to ANY missing
+    ref_path, allowing a same-named file in iter_dir to satisfy a drift
+    hash check intended for a wholly different repo-root location.
     """
     p = Path(ref_path)
     if p.is_absolute():
         return p
     repo_root = _resolve_repo_root()
     candidate = repo_root / ref_path
     if candidate.exists():
         return candidate
-    # Iteration-dir-local fallback (e.g., test fixtures, or runtime where ref is iter-relative)
-    return iter_dir / Path(ref_path).name
+    # iter-0035 codex-rev-004: restrict iter-dir-local fallback to bare
+    # basenames (no directory components in ref_path). Paths with "/" or
+    # "\\" are expected to resolve under repo_root; return the missing
+    # candidate so the caller sees it as missing rather than collapsing
+    # to a same-name file in iter_dir.
+    if "/" not in ref_path and "\\" not in ref_path:
+        return iter_dir / Path(ref_path).name
+    return candidate
```

**Test coverage**: 2 new regression tests:
1. `test_resolve_referenced_path_basename_only_falls_back_to_iter_dir` —
   bare basename "foo.yaml" + missing repo-root file → iter_dir/foo.yaml.
2. `test_resolve_referenced_path_with_dirs_does_not_fall_back_to_iter_dir` —
   "wiki/notes/foo.md" + missing repo-root file → returns the (missing)
   repo_root/wiki/notes/foo.md candidate, NOT iter_dir/foo.md.

---

## Cross-cutting verification (acceptance)

After landing all 4 patches:
- pytest: +8 new tests (3+2+1+2). Local baseline 363 → 371 (or 363 if
  Windows-only test skipped on Linux, etc.).
- smoke: 60/60 unchanged
- gates: 11/11 unchanged after staging + commit
- `G_pytest_dispatch_codex` baseline unchanged (no test_dispatch_codex
  changes in iter-0035)

## Out of scope

- Bare-except policy doc (claude-rev-005 LOW): observational only, no
  code patch proposed.
- Test for `test_outside_repo_review_target_via_main_emits_structured_failure`
  semantics — already landed in iter-0033.

## Reviewer questions

Focused asks for codex (non-exclusive):

1. **Patch 1 untracked counting**: should symlinks (untracked) be counted
   by target file or as zero? Currently they'd open as the target file
   (Path.open follows symlinks). Acceptable, or do we need explicit
   `is_symlink()` exclusion?
2. **Patch 1 UnicodeDecodeError continue**: silent-skip on binary is the
   right policy, but should there be a breadcrumb entry (similar to
   git_check_failed) when MANY binaries are skipped?
3. **Patch 2 blocker-shape validation**: should `blocking_objections`
   require list items to have `id` field, or is "any non-empty list"
   sufficient? Current patch treats non-empty as not-clean regardless of
   item shape — intentional or too permissive?
4. **Patch 3 narrowed except**: `(ImportError, ModuleNotFoundError)` —
   should we also catch `SyntaxError` if a downstream tool patches the
   file mid-flight? Or treat that as a hard failure to surface?
5. **Patch 4 cross-platform path separator**: `"/" not in ref_path and
   "\\" not in ref_path` — robust against Path.PurePosixPath-style refs
   on Windows? Should we normalize via `os.sep` or os.path.split?
