# Workflow B audit target — v1.15.5 post-rewrite doctrine reconciliation

The operator-authorized `git filter-repo` rewrite is ALREADY DONE,
verified, and force-pushed to github.com/stgarca. This iteration
audits only the DOCTRINE/DOC reconciliation that must ship with it.
Doc-only; no engine/config/behavior code touched.

## Context (verified, executed)

- Rules: literal `upstream`→`upstream`,
  `stgarca`→`stgarca`, applied to ALL blobs + commit/tag
  messages, 18 branches + 66 tags (127 commits).
- Pre-push verification PASSED: `git grep`/`git log --all` show
  ZERO `upstream`/`stgarca` across every ref; 17
  release tags + all branches present; full suite 968 passed / 1
  skipped / 0 regressions on the rewritten tip (token replacement
  internally consistent — e.g. `_import_parent_history.py` +
  `test_import_parent_history.py` rewritten together).
- Pre-rewrite full backup bundle stored OUTSIDE the repo.
- Remote re-pointed to `github.com/stgarca/consensus-mcp`; all
  branches + tags force-pushed; `origin/main` == local verified.

## Changes under audit (v1.15.5)

1. **`consensus-workflow/SKILL.md` — sanctioned-exception
   carve-out.** The v1.15.4 doctrine says `main` is never
   force-pushed. The rewrite force-pushed everything. Without
   reconciliation the bundled skill self-contradicts the action
   (the exact currency-drift v1.15.3/v1.15.4 fixed). The carve-out
   states a full-history rewrite is the ONE non-routine reason to
   force-push `main`, gated on: explicit operator authorization OF
   THE REWRITE + verified `git bundle --all` backup outside repo +
   pre-push verification (grep/log clean all refs, tags present,
   suite green) + re-verify origin==local. Cites the 2026-05-15
   precedent + the SHA-change consequence.
2. **`consensus-state/README.md`** (NEW, tracked — verified not
   gitignored). Documents the runtime-state tree (what's
   gitignored vs tracked), recovery via the snapshot orphan
   branch, and provenance (generic "upstream", no upstream).
   A fresh commit adding it relabels the GitHub `consensus-state/`
   folder off the old root commit the operator flagged — the
   doctrine-correct fix (no further history surgery).
3. **CHANGELOG 1.15.5** — artifact-scoped truth: every published
   tag SHA changed; tag-pinned `pipx …@vX.Y.Z` still works (tags
   moved), raw-SHA pins / old `stgarca` clones are dead;
   canonical repo = github.com/stgarca/consensus-mcp.
4. README install URLs + Status → `@v1.15.5` (cut-sequence step 3,
   pre-tag, per the v1.15.4 doctrine since the tag's README is the
   landing page).

## Audit questions

- Q1 goal_satisfied: is the carve-out self-consistent with the
  surrounding fast-forward doctrine + 12-step cut sequence (no
  contradiction, no stale step cross-refs, not a blanket
  force-push license)?
- Q2: is the CHANGELOG's artifact-scoped-truth statement accurate
  and not over/under-claiming (tag-pinned vs raw-SHA impact)?
- Q3: consensus-state/README.md — accurate vs the real .gitignore
  (only `active/iteration-*/`, `state/*.jsonl|*.yaml`,
  `archive/review-passes/*.yaml` ignored)? Any upstream
  residue or new invented claim?
- Q4: any blocking objection? State the differential/prior used.
