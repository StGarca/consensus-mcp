# Fix response — Workflow B pass-2 (codex pass-1 + gemini pass-1)

gemini pass-1: goal_satisfied=true, 0 blocking, 0 findings (ran
with GEMINI_CLI_TRUST_WORKSPACE unset — v1.15.2 trust fix still
proven end-to-end).

codex pass-1: 1 BLOCKING (codex-rev-001) — verified CORRECT, a real
internal inconsistency I introduced when evolving the branch
doctrine. Integrated in full.

## codex-rev-001 (blocking) — ACCEPTED, RESOLVED

**Finding:** under the NEW main=just-cut-tag doctrine, the README
*inside the v1.15.4 tag* becomes the GitHub landing page the moment
`main` fast-forwards. The tag's README still said install
`@v1.15.3` / "Current: v1.15.3" because the old cut-sequence bumped
README only POST-tag on the next dev branch (correct under
main-frozen, broken under main=tag). So v1.15.4 would ship without
actually fixing the presentation defect it exists to fix —
verified at README.md:54 (`@v1.15.3`) and :113 ("Current:
v1.15.3").

**Root cause:** I evolved the branch model but did not reconcile
the README-bump-timing step with it. Legitimate blocking catch.

**Fix (both parts of codex's recommendation):**
1. README.md bumped on the release branch (pre-tag): all
   `@v1.15.3` → `@v1.15.4`; Status → "Current: v1.15.4" with a
   one-line note on the release/branch-model fix.
2. Cut-sequence doctrine reconciled: README install/status
   currency is now **pre-tag step 3** (before `git tag`, so the
   tag — which `main` fast-forwards onto — is correct at tag
   time). The post-tag dev-branch step (now step 10) is rewritten
   as a **verify-only no-op**, explicitly stating the bump moved
   pre-tag in v1.15.4. Steps renumbered 1-12 cleanly (no dup).
3. Anti-currency-drift: the Branch-convention cross-reference
   "cut-sequence step 7" → "step 8" (the renumber shifted the
   fast-forward step) — fixed so the doctrine has no stale
   internal step pointer (the exact lesson from v1.15.3).

## Verification

- No code touched (CI yaml + skill md + README + CHANGELOG +
  pyproject version). Full suite re-run (gate).
- Grep gates: README has zero `@v1.15.3`; Status = v1.15.4;
  cut-sequence is 1..12 with README pre-tag (step 3) + verify-only
  (step 10) + fast-forward (step 8); no stale step cross-ref.

This pass-2 is verification: confirm codex-rev-001 fully resolved,
the cut-sequence is internally self-consistent, and no NEW
inconsistency or invented version was introduced.
