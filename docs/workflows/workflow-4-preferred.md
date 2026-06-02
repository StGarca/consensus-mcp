---
name: Workflow A preferred for claude-authored fixes
description: When claude is the fix-author, default to Workflow A (was workflow #4) - codex pre-implementation review of proposed patches - over Workflow B (was workflow #3 - implement-then-review).
type: feedback
originSessionId: 3dc1e744-0c21-449b-80ee-09dff754acb7
---

> **As of v1.14.4: workflow #4 was renamed to Workflow A; workflow #3
> was renamed to Workflow B.** Filename preserved for stable cross-
> references. Numeric aliases (3, 4) still resolve in goal_packets but
> emit `DeprecationWarning`; scheduled for removal in a future minor.

When claude is authoring fixes or new code in this project, default to Workflow A (was workflow #4): claude-as-fix-author with codex pre-implementation review. The sequence is: author proposed patches as unified diffs in a review-target doc, embed via `_author_review_packet`, dispatch codex for pre-implementation review, integrate codex feedback, THEN implement.

**Why:** Workflow A catches refinements pre-commit instead of post-commit. iter-0033 empirical proof 2026-05-10: codex pre-review caught 2/2 real defects in claude's proposed Patch 1 (missing OSError catch in preflight + reviewer_id/pass_id anchor inconsistency); without pre-review these would have landed as latent iter-0034 followup defects. Operator explicitly preferred this workflow after seeing Workflow B (was #3 - claude-implements-then-codex-reviews) and Workflow A in action across iter-0032/iter-0033. Cost ~30-60s extra codex dispatch; benefit zero followup iterations.

**How to apply:** Whenever claude is fix-author for the consensus pipeline / consensus-mcp subsystem (i.e., NOT when codex authored a patch via the codex-fix-author cycle), follow these steps:

1. Create iteration dir `consensus-state/active/iteration-NNNN-<slug>/`
2. Author goal_packet.yaml with `fix_author_policy: permissive` and allowed_files = the touched files
3. Author proposed-patches.md containing each fix as a unified diff + per-patch rationale + reviewer questions
4. Run `_author_review_packet --files <proposed-patches.md>,<touched code files>` to embed content (MANDATORY - codex sandbox can't read paths; iter-0033 first dispatch blocked on this)
5. Dispatch codex with `--review-target <review-packet.yaml>` for pre-implementation review
6. Read codex findings; integrate any non-blocking feedback into the patches BEFORE writing any code to disk
7. THEN implement the integrated patches via Edit
8. Run pytest/smoke/gates to verify; bump `G_pytest_dispatch_codex` baseline if new tests added
9. Author post-implementation claude-review.yaml confirming integrations + closure-certificate.yaml + iteration-outcome.yaml
10. Section-24 sync via `_sync_section_24 --apply`
11. Commit everything in one go

**When NOT to apply:** trivially mechanical fixes (one-line typo, doc-only edits where ceremony exceeds value); when codex itself authored the patch via the codex-fix-author cycle (then workflow #1 applies - codex authors, claude verifies per `project_codex_fix_author_directive.md`).

**Workflow taxonomy** (for cross-reference):
- #1 codex-fix-author: codex finds + authors patch, claude verifies (operator-locked directive)
- #2 Flavor B subsystem review: pre-existing code reviewed by both codex + claude
- #3 design-then-claude-implements: bidirectional design then claude implements unilaterally, codex reviews after commit
- #4 claude-fix-author with codex pre-review: claude proposes patches, codex pre-reviews diffs, claude implements integrated patches (preferred per this rule)
