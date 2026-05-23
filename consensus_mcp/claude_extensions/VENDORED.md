# Vendored Skills Ledger

Provenance ledger for the Superpowers skills vendored (copied + adapted) into
consensus-mcp. Source: [github.com/obra/superpowers](https://github.com/obra/superpowers),
MIT, (c) 2025 Jesse Vincent. See `NOTICE` for the license text.

All ten skills are a **snapshot** of Superpowers **v5.1.0 @ commit `f2cbfbe`** —
not a live fork. Re-sync is manual and selective (see `docs/consensus/vendoring.md`).

Every vendored `SKILL.md` retains an MIT attribution comment at the top, has its
frontmatter `name` rewritten to `consensus-<name>`, has every `superpowers:<x>`
skill cross-reference rewritten to `consensus:<x>`, has `docs/superpowers/...`
save paths repointed to `docs/consensus/...`, and carries a one-line precedence
header. The four "spine" skills carry additional consensus hand-off adaptations.

| skill | source path | version | sha | adaptation |
|-------|-------------|---------|-----|------------|
| consensus-brainstorming | skills/brainstorming/SKILL.md | 5.1.0 | f2cbfbe | spine: terminal "get USER approval" → hand off to consensus Workflow A consult (CONVERGED-PLAN is the approval; consensus is the approver, not the user); on convergence mint `.consensus/design-approved` marker (iteration_id, scope_glob, converged_plan_sha256, sealed_at_utc) then invoke consensus:writing-plans + ref-rewrite + precedence header |
| consensus-writing-plans | skills/writing-plans/SKILL.md | 5.1.0 | f2cbfbe | verbatim+ref-rewrite (superpowers:* → consensus:*, docs/superpowers/plans → docs/consensus/plans) + precedence header |
| consensus-executing-plans | skills/executing-plans/SKILL.md | 5.1.0 | f2cbfbe | verbatim+ref-rewrite (superpowers:* → consensus:*) + precedence header |
| consensus-subagent-driven-development | skills/subagent-driven-development/SKILL.md | 5.1.0 | f2cbfbe | verbatim+ref-rewrite (superpowers:* → consensus:*, docs/superpowers/plans → docs/consensus/plans) + precedence header |
| consensus-test-driven-development | skills/test-driven-development/SKILL.md | 5.1.0 | f2cbfbe | verbatim+ref-rewrite (no superpowers:* refs present; iron law preserved) + precedence header |
| consensus-requesting-code-review | skills/requesting-code-review/SKILL.md | 5.1.0 | f2cbfbe | spine: replace single-Claude reviewer-subagent dispatch with consensus Workflow B cross-family dispatch (reviewer_dispatch_codex + gemini/kimi per panel size); sealed cross-family audit IS the review; keep git-range diff prep + ref-rewrite + precedence header |
| consensus-receiving-code-review | skills/receiving-code-review/SKILL.md | 5.1.0 | f2cbfbe | spine: reframe "evaluate review feedback" → weigh the SEALED consensus panel findings on merit; record any dismissal with empirical evidence (grep the cited content first) + ref-rewrite + precedence header |
| consensus-verification-before-completion | skills/verification-before-completion/SKILL.md | 5.1.0 | f2cbfbe | spine: add to gate — before any completion claim mint/verify a delivery-readiness token (consensus_mcp/_delivery_readiness.py) + run gate_evaluate_production_with_scope_match (the token the consensus Stop hook checks); keep "evidence before claims" iron law + ref-rewrite + precedence header |
| consensus-finishing-a-development-branch | skills/finishing-a-development-branch/SKILL.md | 5.1.0 | f2cbfbe | verbatim+ref-rewrite (no superpowers:* skill refs; `~/.config/superpowers/worktrees` path repointed to `~/.config/consensus/worktrees`) + precedence header |
| consensus-using-git-worktrees | skills/using-git-worktrees/SKILL.md | 5.1.0 | f2cbfbe | verbatim+ref-rewrite (no superpowers:* skill refs; `~/.config/superpowers/worktrees` path repointed to `~/.config/consensus/worktrees`) + precedence header |
