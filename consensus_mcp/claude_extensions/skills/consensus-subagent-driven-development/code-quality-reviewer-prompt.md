<!-- Vendored from Superpowers (c) 2025 Jesse Vincent, github.com/obra/superpowers, v5.1.0 @ f2cbfbe, MIT. Companion file of the adapted consensus skill; vendored to resolve a dangling reference (3-family review 2026-05-23). -->
# Code Quality Reviewer Prompt Template

Use this template when dispatching a code quality reviewer subagent.

**Purpose:** Verify implementation is well-built (clean, tested, maintainable)

**Only dispatch after spec compliance review passes.**

```
Task tool (general-purpose):
  Review the task's diff against the code-quality criteria below. This is the fast
  INNER-LOOP check; the BINDING code review is a consensus Workflow B sealed
  cross-family review (consensus:requesting-code-review), not this local pass.

  DESCRIPTION: [task summary, from implementer's report]
  PLAN_OR_REQUIREMENTS: Task N from [plan-file]
  BASE_SHA: [commit before task]
  HEAD_SHA: [current commit]
```

**In addition to standard code quality concerns, the reviewer should check:**
- Does each file have one clear responsibility with a well-defined interface?
- Are units decomposed so they can be understood and tested independently?
- Is the implementation following the file structure from the plan?
- Did this implementation create new files that are already large, or significantly grow existing files? (Don't flag pre-existing file sizes - focus on what this change contributed.)

**Code reviewer returns:** Strengths, Issues (Critical/Important/Minor), Assessment
