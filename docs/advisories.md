# Advisories

Standing channel for "shipped artifact has known doctrine drift"
notices. Each advisory names the affected versions, the issue, the
correct version to upgrade to, and any user action required.

Format: append-only, newest first. Older advisories may be marked
**resolved** when no users remain on the affected versions, but
the entry stays for the historical record.

---

## Advisory 2026-05-14: v1.14.0 + v1.14.1 bundled-skill drift

**Affected versions:** `v1.14.0`, `v1.14.1`

**Severity:** Doctrine drift (no functional regression in
consensus-mcp itself). Affects the bundled `consensus-workflow`
SKILL.md content shipped in the wheel and installed by the
Claude Code bootstrap pack.

**Issue:**
- `v1.14.0` ships a bundled skill that incorrectly documents a
  PyPI publish step in the release cut sequence. consensus-mcp
  is NOT registered on PyPI; releases are git-tag-only via
  `pipx install git+https://github.com/.../@vX.Y.Z`. The PyPI
  step in the skill was added in error during the v1.14.0 cut
  and propagates misleading release-procedure documentation to
  every project that runs `consensus init --install-claude-code`
  against this version.
- `v1.14.1` ships a partially-corrected skill (PyPI step
  removed) but is missing the "Verify before invent" and
  "Artifact-scoped claims" doctrine sections that landed in
  v1.14.2.

**Correct version:** `v1.14.3` (or any later release).

**User action required:** Upgrade installs that pulled
`@v1.14.0` or `@v1.14.1`:

```
pipx install --force git+https://github.com/stgarca/consensus-mcp.git@v1.14.3
```

If you have run `consensus init --install-claude-code` against
v1.14.0 or v1.14.1, re-run it against v1.14.3 to refresh the
project-local skill copy (the bootstrap pack copies the bundled
skill into the project's `.claude/skills/` directory at install
time; old installs retain the stale copy).

**Provenance:**
- Originating audit: `iter-audit-2026-05-14-pypi-invention`
  (workflow #4 weighted-synthesis convergence; codex + gemini +
  claude all approved with no blocking objections).
- Doctrine fix landed: `v1.14.2` tag, commit `12eca6c`.
- Follow-up audit: `iter-audit-2026-05-14-three-followup-gaps`
  shipped this advisory mechanism + README install-URL bump in
  `v1.14.3`.

---
