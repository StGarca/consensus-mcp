# Workflow B audit target — v1.15.4 repo-presentation + CI + branch doctrine

Operator-directed policy decision (operator chose "fast-forward
main to latest + fix CI" over running a consult; this audits the
IMPLEMENTATION). Config/doc/version only — NO engine/config/behavior
code touched.

## Verified diagnosis (first-hand, not inferred)

- GitHub default branch = `main`; `origin/main` frozen at `64f70ec`
  (v1.13.0 era). All v1.14.x→v1.15.3 work on `v*` branches/tags.
- `.github/workflows/test.yml` triggered ONLY on `branches: [main]`
  → GitHub Actions CI **dormant v1.13.0→v1.15.3** (every release
  verified by local pytest only; CI never ran on it). This is the
  most serious finding — stated honestly, not as "CI green".
- GitHub labels folders by last default-branch commit →
  `.github/workflows/` showed `ff0164f "...(v2.0.0)"` (the
  memory-confirmed hallucinated-version commit, reverted in content
  by `5e86cca`); `consensus_mcp/` showed the v1.13.0 commit.
- `git merge-base --is-ancestor origin/main HEAD` → TRUE. main→tip
  is a CLEAN FAST-FORWARD: zero commits on main absent from the
  tip; no force-push, no history rewrite. The v2.0.0 commit stays
  in honest linear history; GitHub will relabel folders by the
  newest commit that touched them.

## Changes

- **`.github/workflows/test.yml`**: `on.push.branches` and
  `on.pull_request.branches` now `[main, 'v*']` (was `[main]`).
  Comment explains the dormancy bug. No secrets used by the
  workflow (pure pytest + validator self-tests), so running on
  release branches introduces no secret-exposure risk.
- **`consensus-workflow/SKILL.md`**: Branch convention rewritten —
  `main` = latest released state; every cut fast-forwards `main` to
  the tag (NEW cut-sequence step 7, with a
  `git merge-base --is-ancestor` STOP guard against accidental
  force-update); dev continues on `v<next>`; no merge-back. Updating
  the bundled doctrine here prevents re-introducing the exact
  currency-drift v1.15.3 fixed. Swept: zero residual stale
  "main frozen / never merge back" language remains.
- **CHANGELOG.md**: 1.15.4 entry (root cause + the 4 changes +
  honest "CI was dormant" statement).
- **pyproject.toml**: 1.15.4.dev0 → 1.15.4.
- **README.md**: the accessible ~150-line rewrite (committed
  a89827f earlier this branch; in scope for the audit's accuracy
  check).

## The main fast-forward (executed AFTER this audit passes)

`git push origin <v1.15.4 tag>^{}:refs/heads/main` — a clean
fast-forward only; the cut-sequence step 7 ancestor-check is
re-run immediately before pushing. If it ever returns false, STOP
(do not force).

## Audit questions

- Q1 goal_satisfied: do the changes correctly fix presentation+CI
  and is the evolved doctrine self-consistent with the rest of the
  release-cut sequence in the same skill?
- Q2 (security): does triggering CI on `v*` branches expose any
  secret or allow untrusted code execution with privilege? (The
  workflow uses no `secrets.*`; confirm.)
- Q3 (safety): is the fast-forward genuinely non-destructive — any
  scenario where `main` history or a commit reachable only from
  main would be lost? Verify the ancestor relationship + the step-7
  guard.
- Q4: any residual stale "main frozen" language, OR any NEW
  invented version/forward-reference introduced (anti-recurrence)?
- Q5: blocking objections? State the differential/prior you used.
