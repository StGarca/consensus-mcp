# Overnight autonomous plan: smooth init + full automated superpower→consensus-host integration

**Terminal goal:** consensus-mcp where (1) `consensus-init` gives a SMOOTH first-run
experience — installs the vendored skills + enforcement hooks + host agents,
idempotent, fail-soft, clear guidance — and (2) the consensus HOST AUTOMATICALLY runs
the integrated superpowers-derived workflow (brainstorming→Workflow A consult;
requesting/receiving-code-review→Workflow B; verification→sealed gate; PreToolUse/Stop
hooks enforce; orchestrator + host_peer agents dispatch). All code-reviewed via
consensus audits and committed to GitHub. **Version ceiling: 1.3.**

## Autonomy contract (operator-authorized 2026-05-22)
- DRIVE to completion. Do not stop for the operator at gates.
- Any design question / doubt → resolve via a **consensus consult** (Workflow A),
  then continue. Do NOT ask the operator.
- Every non-trivial change → **consensus Workflow B audit** before commit; fix
  findings (audit→fix→re-audit loop) until clean.
- Commit per logical unit; push to GitHub at each milestone.
- Version numbers are fluid ("just a number"); the v1.22 working-tree WIP may be
  touched/committed freely. Stay ≤ v1.3.
- Maximize parallel-agent dispatch + keep ≥1 background task in flight so the loop
  self-resumes; ScheduleWakeup only if a genuine idle gap appears.
- STOP only at: terminal goal reached; an irreversible-outward action that needs the
  single confirm (force-push / destructive / public-remote landing-page move — flag,
  don't block on routine pushes the operator already authorized); or a blocker
  consensus cannot resolve.

## Milestones

### M1 — v1.21 cleared + integrated  [IN FLIGHT]
- kimi loop-2 re-audit (codex+gemini already clean) → clearance.
- finishing-a-development-branch: merge `v1.21` → main; cut `v1.21.0` tag; push;
  fast-forward main; open next dev branch.

### M2 — Smooth init experience  (→ v1.22)
- RECON + Workflow B audit of `consensus-init` / `--install-claude-code` end-to-end:
  what installs, what's missing, UX gaps, graceful degradation, first-run clarity.
- Implement **agents-init** (the original ask): host roles as real `.claude/agents/*.md`
  at init + `host_peer_review_yaml` activation (converged plan
  `iteration-consensus-agents-init-design-2026-05-22`).
- Ensure skills + hooks (settings.json activation) + host agents all install cleanly,
  idempotently, fail-soft; clear status lines; first-run guidance.
- audit → fix → re-audit clean → commit → push.

### M3 — Full automated superpower integration into the consensus host  (→ v1.3)
- Wire vendored skills + enforcement hooks + host agents so the host runs the
  integrated workflow END-TO-END automatically (brainstorm→Workflow A; review→
  Workflow B; verify→sealed gate; hooks enforce; orchestrator + host_peer dispatch).
- End-to-end dogfood: a real change driven entirely through the integrated path.
- audit → fix → re-audit clean → commit → push.

### M4 — Consolidation + release (≤ v1.3)
- Full suite green; docs (init guide + integration guide); CHANGELOG; README @ version.
- Cut the release tag (≤ v1.3) per the release cadence; push; fast-forward main.

## Per-step cadence (every implementation unit)
implement (subagent-driven / parallel, TDD) → full suite green → consensus Workflow B
audit → fix loop until clean → scoped commit → push at milestone.

## Ledger (updated as the run proceeds)
- M1: kimi re-audit in flight; codex+gemini clean.
