# Design consult: iter-0012 — namespace-import parent iterations + orphan-branch snapshot/restore

**Context for reviewer (codex):** consensus-mcp is a standalone extract of `upstream-26.4.16/agent-loop/`. The parent project has 41 iteration dirs (iter-0000..iter-0042 + 5 audit iterations) and 53 sealed pass YAMLs that did NOT come over during extraction. The standalone restarted at iter-0001 and now has iter-0001..iter-0011.

Separately: `consensus-state/active/iteration-*/` and `consensus-state/archive/review-passes/*.yaml` are `.gitignore`d. NO iteration history has ever been committed to git (verified empirically). This is a structural data-loss risk — a single `git clean -fdX` wipes everything; a fresh clone has zero iteration state.

Maintainer's two governance choices for this iteration (NOT under codex review — already decided):

- **Import strategy**: namespace (parent's history lands under `consensus-state/archive/imported-from-parent/`; standalone's iter-0001..0011 unchanged). NO renumbering, NO content rewriting.
- **Backup mechanism**: orphan git branch `consensus-state-snapshots` (no shared history with main) — force-added gitignored tree, tagged by ISO timestamp.

This consult covers the architectural choices BELOW those decisions.

## Findings

### Finding 1 — Layout of `consensus-state/archive/imported-from-parent/`

Proposed:

```
consensus-state/archive/imported-from-parent/
├── README.md                  # one-page provenance note: source repo, extraction date, etc.
├── source-manifest.yaml       # machine-readable: source path, sha256 of each imported dir
├── active-iterations/         # mirror of parent's agent-loop/active/
│   ├── iteration-0000/
│   ├── iteration-0001/
│   ├── ...
│   ├── iteration-0042-supporting-audit-fixes/
│   ├── iteration-audit-2026-05-11-bare-except/
│   └── ...
└── archive-review-passes/     # mirror of parent's agent-loop/archive/review-passes/
    ├── 2026-05-09-iteration-0019-...
    └── ... (53 files)
```

**F1a (recommended)**: as above — TWO mirror subdirs (active-iterations + archive-review-passes) under one root. Preserves the parent's logical layout 1:1, scoped under an unambiguous "imported-from-parent" namespace.

**F1b**: flatten into a single tree (e.g., everything under `imported-from-parent/iterations/`) discarding the active/archive distinction. Simpler but loses parent's structure.

Trade-off: F1a is faithful to source structure (auditability). F1b is one fewer level of nesting (cosmetic). Recommend F1a.

### Finding 2 — Filename collision between parent and standalone archive

Parent's `archive/review-passes/` has 53 files; standalone's has 27. The parent has files for iter-0001..0007 (extraction-review, consensus-resume-spec, etc.) — **same numeric IDs** as standalone's iter-0001..0007. But content is DIFFERENT (parent is older project's history; standalone's are this repo's new design consults).

If we copy parent's files into `imported-from-parent/archive-review-passes/`, NO collision (they live under the namespace root). If we ever MERGED into a single archive flat dir, collisions WOULD happen.

**Decision**: with F1a, isolated; no merging. Confirm: codex agree this isolation is sufficient, or should we additionally rewrite parent's filenames to add a `parent-` prefix for unambiguous flatten-later semantics?

### Finding 3 — `source-manifest.yaml` shape

Each imported entry stamped with its source path + content hash so future audits can verify the import wasn't tampered with:

```yaml
schema_version: 1
source:
  repo: upstream-26.4.16
  parent_path: C:\Users\<you>\Downloads\upstream-26.4.16\agent-loop
  extraction_commit_in_standalone: ff0164f
  imported_at_utc: <timestamp>
  imported_by: cli:claude-orchestrator
entries:
  - source: agent-loop/active/iteration-0000
    target: consensus-state/archive/imported-from-parent/active-iterations/iteration-0000
    sha256_tree: <sha256 over canonical-sorted file contents>
  - source: agent-loop/active/iteration-0001
    ...
```

`sha256_tree` = sha256 of the concatenation of `<rel_path>\0<sha256(content)>\n` for each file in the dir, sorted by rel_path. This is what `_compute_per_patch_base_sha` in the codex-text-fallback path computes — same canonical form. Re-using the existing helper would be cleanest.

**F3a (recommended)**: include `source-manifest.yaml` with full entry list and per-entry sha256_tree.

**F3b**: just a README; no machine-readable manifest. Simpler but no integrity-verify path.

Trade-off: F3a is honest provenance. F3b is faster but invites future "did this import get corrupted?" questions with no answer. Recommend F3a.

### Finding 4 — Orphan branch structure

Proposed `consensus-state-snapshots` branch is orphan (no shared history with main). Each snapshot is ONE commit containing the full `consensus-state/` tree at that moment. Tagged `snapshot-YYYY-MM-DDTHHMMSS` with timestamp in UTC.

Initial baseline commit lands AFTER the import (so the baseline includes parent's 41 iterations).

The orphan branch is small: ~5MB per snapshot today; growth is bounded by iteration cadence. Even 100 snapshots is ~500MB — manageable.

**F4a (recommended)**: one tagged commit per snapshot. Cheap, auditable, restorable to any tag.

**F4b**: incremental commits (each snapshot is a delta on the prior). Smaller per-snapshot but harder to restore to a single point (need git checkout + history walk). Adds risk of dependency.

Recommend F4a — `git` already does incremental storage internally via packfiles, so the on-disk cost is similar; F4a is operationally simpler.

### Finding 5 — Snapshot/restore CLI surface

Proposed new module: `consensus_mcp/_snapshot_state.py` (parallel to `_dispatch_codex.py` etc.).

CLI:

```
python -m consensus_mcp._snapshot_state snapshot [--label <free-text>]
python -m consensus_mcp._snapshot_state list [--limit N]
python -m consensus_mcp._snapshot_state restore --tag <snapshot-tag> [--dry-run] [--iteration <id>]
python -m consensus_mcp._snapshot_state diff --tag <snapshot-tag>     # show what changed since
```

- `snapshot`: stashes current main HEAD, force-adds `consensus-state/` tree, commits to `consensus-state-snapshots` branch, tags `snapshot-<iso-utc>`, restores main HEAD. Optional `--label` appends to tag for human readability (e.g., `snapshot-2026-05-13T01:30Z-pre-iter-0011-commit`).
- `list`: enumerates tags, shows date + label + commit summary.
- `restore`: checks out the tagged commit's `consensus-state/` files into the working tree. `--dry-run` lists what would change without modifying. `--iteration <id>` restores ONLY that iteration's subtree.
- `diff`: shows what's changed in working tree since the named snapshot.

**F5a (recommended)**: full surface as above (`snapshot`, `list`, `restore`, `diff`).

**F5b**: minimal surface — just `snapshot` and `restore`. Add the rest later if needed.

Trade-off: F5a is more code but addresses common operator questions ("what's in this snapshot?" / "what's changed since?") in-tool. F5b ships faster.

### Finding 6 — Restore safety

The `restore` command overwrites the working tree's `consensus-state/`. If the operator has in-flight iteration work, that work is at risk.

Proposed safety:

- Default: BEFORE restoring, run a snapshot of current state. Operator gets back a tag they can re-restore if the restore was a mistake.
- `--force` to skip the pre-restore snapshot (operator confirms they don't want the safety net).
- `--dry-run` prints the file list that would change; nothing modified.

**F6a (recommended)**: auto-snapshot before restore (with `--force` to skip).

**F6b**: prompt-confirm before restore. Interactive, breaks scripting.

**F6c**: no safety net; operator's responsibility.

Recommend F6a.

### Finding 7 — When to trigger snapshots

Options:

- **Manual only** (operator runs `snapshot` on demand)
- **On iteration close** (hook into `consensus close` or audit_append_event for `iteration_closed` event)
- **On destructive operations** (wrap `git clean` etc. — out of scope for this iteration; documentation only)
- **Periodic via cron-equivalent** (out of scope; operator's choice)

**F7a (recommended for iter-0013)**: manual only. The orchestrator is responsible for snapshotting at sensible moments. Auto-snapshot on `iteration_closed` is good but adds coupling; defer to future iteration.

**F7b**: include `iteration_closed` auto-snapshot in iter-0013.

Recommend F7a (manual) for iter-0013, with `iteration_closed` snapshot as iter-0014 followup if useful.

### Finding 8 — Integration with `iter-0011` (gemini adapter) commit timing

iter-0011 is paused mid-flight (tests green, codex review + commit held). If we land iter-0012 BEFORE iter-0011 commits:

1. iter-0012 ships archive import + snapshot tooling
2. Initial baseline snapshot captures pre-iter-0011 state
3. iter-0011 codex review + commit lands next
4. Second snapshot captures post-iter-0011 state

This gives us snapshots at clean boundaries. Recommended ordering.

### Finding 9 — Q9 follow-ups (post codex round 1)

Codex round-1 (pass1, sealed 2026-05-13T01:36:03Z) approved Q1-Q8 verbatim and flagged four items under Q9 to resolve before iter-0013 implementation. Proposed resolutions:

**F9a — Remote-push / clone-restore expectations**

The `consensus-state-snapshots` branch is local-only by default. Operator opts in to remote durability by `git push origin consensus-state-snapshots`. From a fresh clone, restore requires:

```
git fetch origin consensus-state-snapshots:consensus-state-snapshots
git fetch origin --tags
python -m consensus_mcp._snapshot_state restore --tag snapshot-<iso>
```

The `_snapshot_state.py` module emits this exact help text in `restore --help`. README documents the push/clone flow.

**F9b — Retention policy**

NO automatic deletion. Operator manages. The `list` command shows tag age; `prune` is a future addition (out of scope for iter-0013). Documentation includes a manual prune recipe (`git tag -d <tag> && git push origin --delete <tag>`).

**F9c — Tag naming sanitization**

Tag format: `snapshot-<iso-utc>[-<label>]`. The `<label>` is operator-supplied via `--label`. Sanitize before tagging:

- Regex: `^[A-Za-z0-9_-]{1,64}$`
- Reject and exit with a clear error if the label fails the regex (no silent transformation — operator sees what's wrong)
- Empty label = no suffix; tag is just `snapshot-<iso-utc>`

ISO format is normalized to `YYYY-MM-DDTHHMMSSZ` (no colons — git tags reject colons on some refspecs).

**F9d — Restore conflict behavior for dirty in-flight state**

Before `restore` overwrites the working tree's `consensus-state/`:

1. Compute the set of files that would change (diff between current disk and target snapshot's tree).
2. For each file that would change AND has on-disk modifications not present in ANY snapshot (i.e., currently dirty work that hasn't been snapshotted): record as "dirty conflict."
3. Behavior:
   - Default (no flags): auto-pre-snapshot (per F6a) captures the dirty state, then restore proceeds.
   - `--force`: skip auto-pre-snapshot AND skip the dirty-check; user accepts loss.
   - `--abort-on-dirty`: refuse if dirty conflicts exist, do nothing.
4. The pre-snapshot tag uses `--label pre-restore-<short-target-tag>` so the operator can find their unsaved state easily.

## Open scope questions

**Q1.** F1 layout: F1a (two mirror subdirs under namespace root) or F1b (flat)? Recommend F1a.

**Q2.** F2 filename safety: rely on the namespace root for collision-isolation (current proposal), or additionally prefix parent's archive filenames with `parent-` for flatten-later safety?

**Q3.** F3 manifest: F3a (full source-manifest.yaml with sha256_tree per entry) or F3b (README only)? Recommend F3a.

**Q4.** F4 snapshot commits: F4a (one tagged commit per snapshot) or F4b (incremental deltas)? Recommend F4a.

**Q5.** F5 CLI surface: F5a (snapshot+list+restore+diff) or F5b (minimal — snapshot+restore only)? Recommend F5a.

**Q6.** F6 restore safety: F6a (auto-pre-restore snapshot, --force to skip), F6b (interactive confirm), or F6c (no safety)? Recommend F6a.

**Q7.** F7 trigger: F7a (manual only for iter-0013, defer auto-trigger to iter-0014) or F7b (include iteration_closed auto-snapshot now)? Recommend F7a.

**Q8.** F8 ordering: confirm iter-0012 ships BEFORE iter-0011 commit, so baseline snapshot captures pre-iter-0011 state.

**Q9.** Are F9a-F9d resolutions for the round-1 Q9 follow-ups acceptable, or any further additions / changes needed? F9a = remote-push docs in `restore --help` + README; F9b = manual retention (no auto-prune); F9c = `^[A-Za-z0-9_-]{1,64}$` label sanitization + ISO normalized `YYYY-MM-DDTHHMMSSZ`; F9d = dirty-conflict detection with auto-pre-snapshot default, `--force` + `--abort-on-dirty` overrides.

## Your task

Emit ONE finding only. severity: low (design + scoping).

- `recommendation`: a single string of the form:
  `"Q1: <F1a|F1b>; Q2: <namespace-isolation|prefix-also>; Q3: <F3a|F3b>; Q4: <F4a|F4b>; Q5: <F5a|F5b>; Q6: <F6a|F6b|F6c>; Q7: <F7a|F7b>; Q8: <agree|reorder>; Q9: <none|added: ...>"`
- `risk`: short rationale (~4 sentences) covering the highest-impact picks
- No `patch_proposal` needed — maintainer implements after the verdict (iter-0013).

Empty findings is NOT acceptable.
