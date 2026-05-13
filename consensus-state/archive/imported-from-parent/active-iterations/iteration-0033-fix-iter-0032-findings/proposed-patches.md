# iter-0033 proposed patches — pre-implementation review target

These are claude-authored proposed fixes for the 4 iter-0032 findings (2 codex
HIGH/MEDIUM + 2 claude MEDIUM/LOW). They are presented here for **codex
pre-implementation review** BEFORE landing — workflow #3 hardening: claude as
fix-author, codex as code reviewer before commit.

Reviewer task: assess each proposed patch for (a) correctness against the
finding, (b) regression risk on adjacent code, (c) scope compliance, (d) test
coverage adequacy. NO existing code on disk reflects these patches yet.

---

## Patch 1 — codex-rev-001 + claude-rev-001 (HIGH)

**Defect**: `_normalize_relative_to_repo` raises `OutsideRepoPathError` for
operator-supplied outside-repo paths to `--iteration-dir`, `--goal-packet`,
`--prompt-template`, `--schema`, or `--review-target`. The 5 calls live BEFORE
the main try/except (lines 1225-1240 of `_dispatch_codex.py`), so a
containment rejection escapes as an unhandled exception. Operator sees a raw
Python traceback; no `dispatch_failed` event lands; the visibility TUI cannot
see containment-rejected dispatches.

**Approach**:
1. Compute `log_path` immediately after `repo_root` is resolved (currently at
   line 1258, move to right after the existing try/except for `_resolve_repo_root`).
2. Derive a best-effort `reviewer_id` / `pass_id` BEFORE the normalize calls
   so the error event has anchors (fall back to "unknown-pre-normalize" when
   args weren't supplied).
3. Wrap the 5 `_normalize_relative_to_repo` calls in a try/except that catches
   `OutsideRepoPathError`, emits a structured `dispatch_failed` event with
   `error_type="OutsideRepoPathError"`, prints `{"ok": false, ...}` JSON, and
   returns rc=5.

```diff
--- a/scripts/agent_loop_mcp/_dispatch_codex.py
+++ b/scripts/agent_loop_mcp/_dispatch_codex.py
@@ -1212,29 +1212,71 @@ def main(argv: list[str] | None = None) -> int:
     ns = p.parse_args(argv)

     # Per v1.10.4 F1: fail-closed if repo_root can't be validated.
     try:
         repo_root = _resolve_repo_root()
     except RepoRootResolutionError as exc:
         # We don't have a valid log_path yet; print error to stderr-equivalent JSON.
         print(json.dumps({"ok": False, "error": str(exc), "error_type": "RepoRootResolutionError"}))
         return 4

-    # Per v1.10.4 F5: normalize all operator-supplied relative paths against
-    # repo_root, NOT the process cwd. The codex subprocess runs with --cd repo_root,
-    # so all path frames must agree.
-    iter_dir = _normalize_relative_to_repo(ns.iteration_dir, repo_root)
-    iter_dir.mkdir(parents=True, exist_ok=True)
-    iteration_id = iter_dir.name
-
-    template_path = (
-        _normalize_relative_to_repo(ns.prompt_template, repo_root)
-        if ns.prompt_template
-        else (Path(__file__).parent / "dispatch_templates" / "codex_review_template.md")
-    )
-    schema_path = (
-        _normalize_relative_to_repo(ns.schema, repo_root)
-        if ns.schema
-        else (Path(__file__).parent / "dispatch_templates" / "codex_review_schema.json")
-    )
-    goal_packet_path = _normalize_relative_to_repo(ns.goal_packet, repo_root)
-    review_target_normalized = _normalize_relative_to_repo(ns.review_target, repo_root)
+    # v1.10.5 iter-0033 codex-rev-001 fix: compute log_path immediately so the
+    # preflight normalize calls can log a structured dispatch_failed event on
+    # containment rejection. Previously the calls below raised
+    # OutsideRepoPathError before the main try/except, leaving operators with
+    # a raw traceback and the audit log silent.
+    log_path = repo_root / "agent-loop" / "state" / "dispatch-log.jsonl"
+
+    # Best-effort reviewer / pass identifiers for pre-normalize failure events.
+    # These may be overwritten below once iter_dir resolves cleanly.
+    _pre_iter_id = Path(ns.iteration_dir).name or "unknown-iteration"
+    reviewer_id = ns.reviewer_id or f"codex-{_pre_iter_id}-1"
+    pass_id = ns.pass_id or f"{reviewer_id}-pass1"
+
+    # Per v1.10.4 F5: normalize all operator-supplied relative paths against
+    # repo_root, NOT the process cwd. The codex subprocess runs with --cd repo_root,
+    # so all path frames must agree.
+    # v1.10.5 iter-0033: wrap in try/except so OutsideRepoPathError emits a
+    # structured failure event instead of an uncaught traceback.
+    try:
+        iter_dir = _normalize_relative_to_repo(ns.iteration_dir, repo_root)
+        iter_dir.mkdir(parents=True, exist_ok=True)
+        iteration_id = iter_dir.name
+
+        template_path = (
+            _normalize_relative_to_repo(ns.prompt_template, repo_root)
+            if ns.prompt_template
+            else (Path(__file__).parent / "dispatch_templates" / "codex_review_template.md")
+        )
+        schema_path = (
+            _normalize_relative_to_repo(ns.schema, repo_root)
+            if ns.schema
+            else (Path(__file__).parent / "dispatch_templates" / "codex_review_schema.json")
+        )
+        goal_packet_path = _normalize_relative_to_repo(ns.goal_packet, repo_root)
+        review_target_normalized = _normalize_relative_to_repo(ns.review_target, repo_root)
+    except OutsideRepoPathError as exc:
+        _log_dispatch(log_path, {
+            "event": "dispatch_failed",
+            "error_type": "OutsideRepoPathError",
+            "error": str(exc),
+            "reviewer_id": reviewer_id,
+            "pass_id": pass_id,
+            "iteration_id": _pre_iter_id,
+            "timeout_seconds": ns.timeout_seconds,
+        })
+        print(json.dumps({"ok": False, "error": str(exc), "error_type": "OutsideRepoPathError"}))
+        return 5
```

(The existing `reviewer_id = ns.reviewer_id or f"codex-{iteration_id}-1"` /
`pass_id = ns.pass_id or f"{reviewer_id}-pass1"` / `log_path = repo_root /...`
lines below the normalize block are now redundant and should be removed; the
canonical iteration_id (from `iter_dir.name`) overwrites `_pre_iter_id` once
normalization succeeds. The diff above doesn't show that delete but it's part
of the patch — confirm acceptable.)

**Test coverage**: add `test_outside_repo_review_target_via_main_emits_structured_failure`
that invokes `_dispatch_codex.main()` with `--review-target` pointing outside
repo, asserts rc=5, asserts stdout JSON has `"error_type": "OutsideRepoPathError"`,
asserts `dispatch_failed` event with same error_type lands in dispatch-log.

---

## Patch 2 — codex-rev-002 + claude-rev-002 (MEDIUM)

**Defect**: `_assemble_dispatches` in `_visibility_tui.py:104-118` keys state
by `pass_id` only. Two iterations reusing the same `pass_id` collapse into
one entry; later terminal events incorrectly close unrelated active dispatches;
TUI under-reports stalls (the failure mode it was built to surface).

**Approach**: key by `(iteration_id, pass_id)` tuple. Preserve a `pass_id`-only
fallback for legacy events that lack `iteration_id` (defensive; no current
event in the log lacks it, but the migration cost is one line).

```diff
--- a/scripts/agent_loop_mcp/_visibility_tui.py
+++ b/scripts/agent_loop_mcp/_visibility_tui.py
@@ -101,17 +101,23 @@ def _assemble_dispatches(events: list[dict]) -> dict:
           "recent":   [end_event, ...],     # last 5 terminal events, newest first
         }
     """
-    by_pass: dict[str, dict] = {}  # pass_id -> {"start": ev, "end": ev}
+    # iter-0033 codex-rev-002 fix: key by (iteration_id, pass_id) tuple so a
+    # pass_id reused across iterations doesn't collapse two dispatches into
+    # one. iteration_id may be missing on legacy events; fall back to None
+    # which still keys uniquely-per-pass_id within the legacy bucket.
+    by_key: dict[tuple[str | None, str], dict] = {}
     for ev in events:
         pass_id = ev.get("pass_id") or ev.get("reviewer_id")
         if not pass_id:
             continue
-        entry = by_pass.setdefault(pass_id, {"start": None, "end": None})
+        iter_id = ev.get("iteration_id")
+        key = (iter_id, pass_id)
+        entry = by_key.setdefault(key, {"start": None, "end": None})
         kind = ev.get("event")
         if kind == "dispatch_start":
             entry["start"] = ev
         elif kind in ("dispatch_done", "dispatch_failed", "dispatch_refused"):
             entry["end"] = ev

     active = [
-        v["start"] for v in by_pass.values() if v["start"] and not v["end"]
+        v["start"] for v in by_key.values() if v["start"] and not v["end"]
     ]
     recent_terminal = [
-        v["end"] for v in by_pass.values() if v["end"]
+        v["end"] for v in by_key.values() if v["end"]
     ]
```

**Test coverage**: add `test_assemble_pass_id_collision_across_iterations`
that synthesizes two iterations sharing pass_id "p1" where only one has a
terminal event, asserts both are tracked separately (the un-terminated one
appears in active, the other in recent).

---

## Patch 3 — claude-rev-003 (MEDIUM)

**Defect**: `_normalize_relative_to_repo` containment check uses
`Path.relative_to()` which is case-sensitive in string comparison. Windows
filesystem is case-insensitive — same on-disk file can be addressed as
mixed-case paths. False-positive containment rejection possible on Windows.

**Approach**: on Windows, if the standard `relative_to()` fails, retry the
containment check with case-folded string compare before raising
OutsideRepoPathError.

```diff
--- a/scripts/agent_loop_mcp/_dispatch_codex.py
+++ b/scripts/agent_loop_mcp/_dispatch_codex.py
@@ -180,12 +180,29 @@ def _normalize_relative_to_repo(path_str: str | None, repo_root: Path) -> Path |
     p = Path(path_str)
     resolved = p.resolve() if p.is_absolute() else (repo_root / p).resolve()
     repo_root_resolved = repo_root.resolve()
+    contained = False
     try:
         resolved.relative_to(repo_root_resolved)
+        contained = True
     except ValueError:
+        # iter-0033 claude-rev-003 fix: Path.relative_to is case-sensitive
+        # in string compare. Windows filesystem is case-insensitive; mixed-
+        # case repo_root vs path can trigger a false-positive containment
+        # rejection. On Windows, retry with case-folded string compare.
+        import sys as _sys
+        if _sys.platform == "win32":
+            resolved_lc = str(resolved).lower().replace("\\", "/")
+            root_lc = str(repo_root_resolved).lower().replace("\\", "/")
+            if resolved_lc == root_lc or resolved_lc.startswith(root_lc.rstrip("/") + "/"):
+                contained = True
+    if not contained:
         raise OutsideRepoPathError(
             f"path {path_str!r} resolves to {resolved} which is outside repo_root "
             f"{repo_root_resolved}. agent-loop-mcp dispatch only reads files inside "
             f"the repo. Move the file into the repo or pass a path relative to it."
         )
     return resolved
```

**Test coverage**: add `test_containment_case_insensitive_on_windows`
that synthesizes mixed-case repo_root and an inside-repo path with different
casing; asserts containment passes on Windows (skipped on non-win32). On
Linux this test is a no-op (skip).

---

## Patch 4 — claude-rev-004 (LOW)

**Defect**: New containment regression tests exercise the helper
`_normalize_relative_to_repo` directly. No test exercises the full
`_dispatch_codex.main()` entry where containment fires on operator-supplied
args. A future refactor that relocates the containment call could silently
bypass it end-to-end.

**Approach**: this is fundamentally the same test as the one in Patch 1's
test coverage section. Single end-to-end main()-entry containment test covers
both findings. No separate patch needed; mark this finding as resolved by
Patch 1's test addition.

---

## Cross-cutting verification (acceptance)

After landing all 4 patches:
- pytest: must add ≥ 2 new tests (containment-via-main + pass_id-collision);
  optionally +1 for Windows case-fold (skip on non-win32). New baseline:
  current 359 + 2 or 3.
- smoke: 60/60 unchanged
- gates: 11/11 unchanged after staging + commit
- `G_pytest_dispatch_codex` test count baseline bumped if Patch 1's
  end-to-end test lands in `test_dispatch_codex.py`

## Out of scope for iter-0033

- v1.10.5 review_target_path / hash plumbing — already landed in commit `6e4aa822`.
- TUI display logic beyond `_assemble_dispatches` — not implicated by codex findings.
- `_dispatch_codex.py` template/schema unrelated to containment — out of scope.

## Reviewer questions

Focused asks for codex (non-exclusive):

1. **Patch 1 placement**: is the try/except boundary correctly drawn? Are
   there OTHER exception types `_normalize_relative_to_repo` could raise that
   should also route through the same structured failure path
   (e.g., `OSError` if `iter_dir.mkdir` fails)?
2. **Patch 1 rc=5**: appropriate vs existing rcs (1=invocation/parse, 2=other,
   3=smoke-env-gate, 4=repo-root-resolution)? Any collision risk?
3. **Patch 2 fallback semantics**: does the `(None, pass_id)` legacy fallback
   risk masking a legitimate iteration boundary issue? Alternative: drop the
   fallback and require iteration_id (stricter but breaks if any legacy
   event lacks it).
4. **Patch 3 Windows case-fold**: any edge case where `str(resolved).lower()`
   fails to canonicalize (e.g., NFC/NFD unicode normalization issues, 8.3
   short names that resolved() doesn't expand)?
5. **Patch 4 elision**: is collapsing claude-rev-004 into Patch 1's test
   appropriate, or should it be a separately authored test for clarity?
