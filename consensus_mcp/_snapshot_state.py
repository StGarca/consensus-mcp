"""Point-in-time snapshot/restore for the gitignored consensus-state/ tree.

Per iter-0012 codex-approved design: force-adds gitignored iteration history
into an orphan git branch (`consensus-state-snapshots`) so it survives
git clean / fresh clones / accidental rm. Each snapshot is one tagged commit
on the orphan branch containing the full consensus-state tree at that moment.

Architecture:
- Orphan branch (no shared history with main) — git storage dedupes blobs
  across snapshots so the on-disk cost is bounded by actual delta size.
- Each snapshot uses a `git worktree` so the main working tree is never
  touched during snapshot/restore.
- Tag format: `snapshot-<ISO-UTC>[-<label>]` where ISO is normalized to
  `YYYY-MM-DDTHHMMSSZ` (no colons; some refspecs reject colons in tags).
- Label is operator-supplied via `--label`; sanitized to ^[A-Za-z0-9_-]{1,64}$.

CLI surface (per iter-0012 F5a):
  snapshot [--label <free-text>]
  list [--limit N]
  restore --tag <snapshot-tag> [--dry-run] [--iteration <id>] [--force] [--abort-on-dirty]
  diff --tag <snapshot-tag>

Per iter-0012 F6a + F9d: restore auto-pre-snapshots the current dirty state
before overwriting (--force skips; --abort-on-dirty refuses if dirty).

Per iter-0012 F9a: remote durability is opt-in by operator via
`git push origin consensus-state-snapshots`. README documents.

NOT an MCP tool. CLI-only. Operator invokes from the repo root:
  python -m consensus_mcp._snapshot_state <command> [args]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from consensus_mcp._dispatch_base import (
    RepoRootResolutionError,
    _resolve_repo_root,
)


SNAPSHOT_BRANCH = "consensus-state-snapshots"
SNAPSHOT_TAG_PREFIX = "snapshot-"
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Iteration directory names look like `iteration-NNNN-slug` or
# `iteration-audit-YYYY-MM-DD-slug`. The pattern below allows alphanumerics,
# dots (for date components), hyphens, and underscores, but rejects path
# separators and ".." traversal — preventing codex-rev-001 round-3 (critical).
ITERATION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SNAPSHOTTED_PATHS = ("consensus-state",)  # Top-level dirs included in each snapshot.


class SnapshotError(RuntimeError):
    """Raised on any snapshot/restore failure with a specific operator message."""


def _run_git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run git with the given args; raise SnapshotError on failure (when check=True)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise SnapshotError(
            f"git {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result


def _iso_utc_now() -> str:
    """Return UTC timestamp as YYYY-MM-DDTHHMMSSZ (no colons — git-tag safe)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _sanitize_label(label: str | None) -> str | None:
    """Validate operator-supplied label; return as-is if valid, raise if not.

    Empty/None labels return None (tag will have no suffix).
    """
    if label is None or label == "":
        return None
    if not LABEL_PATTERN.match(label):
        raise SnapshotError(
            f"label {label!r} fails sanitization regex ^[A-Za-z0-9_-]{{1,64}}$. "
            f"Use alphanumeric, hyphens, or underscores only; max 64 chars."
        )
    return label


def _build_tag(label: str | None = None) -> str:
    """Construct a snapshot tag name from current UTC + optional label.

    NOTE: this returns a candidate; uniqueness is enforced by
    _next_unique_tag() before commit so same-second snapshots don't collide
    (codex-rev-001 in iter-0013 round 2 — same-second tag collision risk).
    """
    iso = _iso_utc_now()
    label = _sanitize_label(label)
    if label:
        return f"{SNAPSHOT_TAG_PREFIX}{iso}-{label}"
    return f"{SNAPSHOT_TAG_PREFIX}{iso}"


def _next_unique_tag(repo_root: Path, candidate: str) -> str:
    """codex-rev-001 fix: ensure tag uniqueness before commit.

    If candidate already exists, append `-N` (monotonic 1..) until a free name
    is found. Bounded retry so we never spin forever in pathological cases.
    """
    for attempt in range(0, 1000):
        tag = candidate if attempt == 0 else f"{candidate}-{attempt}"
        rev = _run_git(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
                       cwd=repo_root, check=False)
        if rev.returncode != 0:
            return tag
    raise SnapshotError(
        f"could not allocate unique tag after 1000 attempts on candidate {candidate!r}; "
        f"manually prune some snapshot tags or use a different --label"
    )


def _orphan_branch_exists(repo_root: Path) -> bool:
    """True iff consensus-state-snapshots branch exists locally."""
    result = _run_git(
        ["rev-parse", "--verify", "--quiet", SNAPSHOT_BRANCH],
        cwd=repo_root,
        check=False,
    )
    return result.returncode == 0


def _list_snapshot_tags(repo_root: Path) -> list[tuple[str, str]]:
    """Return [(tag, iso_or_label_part), ...] sorted newest-first.

    Tags are matched against the SNAPSHOT_TAG_PREFIX. ISO sort is lexicographic
    on the YYYY-MM-DDTHHMMSSZ format, so newest-first is reverse-sort.
    """
    result = _run_git(
        ["tag", "-l", f"{SNAPSHOT_TAG_PREFIX}*"],
        cwd=repo_root,
    )
    tags = [t.strip() for t in result.stdout.splitlines() if t.strip()]
    # Sort by the ISO portion (chars after prefix); newest first.
    tags.sort(reverse=True)
    return [(t, t[len(SNAPSHOT_TAG_PREFIX):]) for t in tags]


def _snapshot_via_worktree(
    repo_root: Path,
    tag: str,
    label: str | None,
) -> str:
    """Create a snapshot using a temporary worktree so the main tree is untouched.

    Per codex-rev-001 round-2 fix: the supplied `tag` is treated as a candidate;
    if it collides with an existing tag (same-second snapshot), the actual tag
    used is suffixed `-1`, `-2`, etc. The returned string is the ACTUAL tag.

    Flow:
      1. Ensure orphan branch exists (create empty if not).
      2. `git worktree add --detach <tmp> <branch-or-empty>`.
      3. Wipe the worktree (it should contain only the prior snapshot's files,
         or be empty for the initial snapshot).
      4. Copy current consensus-state/ into the worktree.
      5. `git add -f -A` (force = bypass .gitignore), commit, advance branch
         to that commit, tag.
      6. Remove the worktree.

    Returns the created tag name.
    """
    # Step 1: ensure orphan branch exists. If not, create it with a dummy commit.
    if not _orphan_branch_exists(repo_root):
        _init_orphan_branch(repo_root)

    # Step 1b: resolve tag collisions BEFORE doing any commit work.
    tag = _next_unique_tag(repo_root, tag)

    # Step 2: worktree add
    with tempfile.TemporaryDirectory(prefix="consensus-snap-") as tmpdir:
        worktree = Path(tmpdir) / "wt"
        _run_git(["worktree", "add", "--detach", str(worktree), SNAPSHOT_BRANCH], cwd=repo_root)
        try:
            # Step 3: wipe worktree contents EXCEPT .git pointer
            for child in worktree.iterdir():
                if child.name == ".git":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()

            # Step 4: copy current snapshotted paths from main tree
            for rel in SNAPSHOTTED_PATHS:
                src = repo_root / rel
                if not src.exists():
                    continue
                dst = worktree / rel
                shutil.copytree(src, dst)

            # Step 5: add (force) + commit + tag in the worktree.
            _run_git(["add", "-f", "-A"], cwd=worktree)
            status = _run_git(["status", "--porcelain"], cwd=worktree).stdout.strip()
            if not status:
                # No content change since last snapshot — empty commit so the
                # tag is still timestamped at this moment.
                _run_git(
                    ["commit", "--allow-empty", "-m",
                     f"snapshot: {tag} (no changes)"],
                    cwd=worktree,
                )
            else:
                _run_git(
                    ["commit", "-m", f"snapshot: {tag}" + (f" - {label}" if label else "")],
                    cwd=worktree,
                )
            # Tag the new HEAD in the worktree (tags are global to the repo).
            _run_git(["tag", "-a", tag, "-m", f"consensus-state snapshot at {tag}"], cwd=worktree)
            # Record the new sha BEFORE removing the worktree.
            new_sha = _run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
        finally:
            # Step 6: release the worktree FIRST (otherwise the branch is "in
            # use" and we can't branch -f it). Prune for defense-in-depth
            # per codex-rev-001 round-4.
            _run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo_root, check=False)
            _run_git(["worktree", "prune"], cwd=repo_root, check=False)
        # Step 7: advance the real branch ref to the commit we just made.
        # Worktree was --detach so HEAD advanced but the branch ref didn't.
        _run_git(["branch", "-f", SNAPSHOT_BRANCH, new_sha], cwd=repo_root)
    return tag


def _init_orphan_branch(repo_root: Path) -> None:
    """Initialize the consensus-state-snapshots orphan branch with one empty commit.

    Uses a temp worktree so the main working tree isn't disturbed.

    Per codex-rev-001 round-4: BEFORE removing the worktree, detach HEAD
    inside it so the orphan branch is no longer "checked out by" that
    worktree. Then `worktree remove --force` cleans up, and a final
    `worktree prune` clears any stale worktree records that could block
    future `branch -f` operations. Defense-in-depth.
    """
    with tempfile.TemporaryDirectory(prefix="consensus-snap-init-") as tmpdir:
        worktree = Path(tmpdir) / "wt"
        _run_git(
            ["worktree", "add", "--orphan", "-b", SNAPSHOT_BRANCH, str(worktree)],
            cwd=repo_root,
        )
        try:
            # Reset any files git may have copied (--orphan inherits files).
            for child in worktree.iterdir():
                if child.name == ".git":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            # Empty commit. Worktree was created with `--orphan -b <branch>`
            # so the branch is checked out; commit advances the branch ref.
            _run_git(
                ["commit", "--allow-empty", "-m",
                 "init: consensus-state-snapshots orphan branch (iter-0013)"],
                cwd=worktree,
            )
            # codex-rev-001 round-4: detach HEAD in this worktree so the
            # branch is released BEFORE we tear the worktree down.
            _run_git(["checkout", "--detach", "HEAD"], cwd=worktree, check=False)
        finally:
            _run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo_root, check=False)
            # Final safety: prune any stale worktree records from .git/worktrees/
            # so subsequent `branch -f` operations don't see ghost claims.
            _run_git(["worktree", "prune"], cwd=repo_root, check=False)


def _validate_iteration_name(iteration: str) -> str:
    """codex-rev-001 round-3 fix: reject path traversal / separators in --iteration.

    Returns the validated iteration name (unchanged) or raises SnapshotError
    with a clear operator message. Rejects:
      - empty / None
      - path separators (/, \\)
      - drive letters / absolute paths
      - `..` traversal anywhere in the string
      - leading dot
      - characters outside [A-Za-z0-9._-]
      - length > 128
    """
    if not iteration:
        raise SnapshotError("iteration name cannot be empty")
    if not ITERATION_NAME_PATTERN.match(iteration):
        raise SnapshotError(
            f"iteration name {iteration!r} is unsafe: must match "
            f"^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$ "
            f"(no separators, no leading dot, no '..' traversal)"
        )
    if ".." in iteration:
        raise SnapshotError(
            f"iteration name {iteration!r} contains '..' which is a path "
            f"traversal attempt; refusing"
        )
    # Defensive: ensure Path parsing yields exactly this one component.
    parts = Path(iteration).parts
    if len(parts) != 1 or parts[0] != iteration:
        raise SnapshotError(
            f"iteration name {iteration!r} resolves to multiple path "
            f"components {parts!r}; expected exactly one"
        )
    return iteration


def _path_matches_subtree(path: str, sub: str) -> bool:
    """codex-rev-003 fix: proper boundary match for iteration-subtree filtering.

    `path.startswith(sub)` matches sibling iterations (e.g., sub='iteration-0001'
    spuriously matches 'iteration-00010' or 'iteration-0001-foo'). Use exact-or-
    slash-boundary semantics so `iteration-0001` only matches `iteration-0001/...`
    or itself.
    """
    return path == sub or path.startswith(sub + "/")


def _extract_tag_to_tempdir(repo_root: Path, tag: str, tmpdir: Path) -> Path:
    """Materialize the snapshot tag's tree at a temporary worktree.

    codex-rev-001 + codex-rev-005 fix: instead of `git checkout <tag> -- paths`
    (which writes to the main repo index AND can affect tracked-file state) or
    streaming `git archive` through text-mode stdout (which corrupts binary
    bytes), use `git worktree add --detach <tag>` which creates an isolated
    materialization at `tmpdir`. Caller is responsible for `git worktree remove
    --force` to clean up.
    """
    rev = _run_git(["rev-parse", "--verify", tag], cwd=repo_root, check=False)
    if rev.returncode != 0:
        raise SnapshotError(f"tag {tag!r} not found")
    worktree = tmpdir / "extract"
    _run_git(["worktree", "add", "--detach", str(worktree), tag], cwd=repo_root)
    return worktree


def _restore_from_tag(
    repo_root: Path,
    tag: str,
    iteration: str | None = None,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Restore consensus-state/ files from the named snapshot tag.

    Per codex-rev-001 + codex-rev-002 + codex-rev-003 fixes:
      - Use temp-worktree extraction + filesystem copy (no main index touch).
      - Clean the target restore scope BEFORE copying (so files absent from
        the snapshot are removed, not left as hybrid state).
      - Use _path_matches_subtree for --iteration boundary safety.

    iter-0014 (codex-rev-001 round-6): returns a structured dict with separate
    `copied` and `deleted` lists. Dry-run validates missing-iteration the SAME
    way as a real restore and reports the deletions that scope-cleanup would
    perform.

    Pre-snapshot safety is the caller's responsibility (see cmd_restore).
    """
    if iteration:
        # codex-rev-001 round-3 fix: validate as a single safe path component
        # BEFORE interpolating into the scope path. Prevents `..` and similar
        # traversal that would otherwise let rmtree target the repo root.
        iteration = _validate_iteration_name(iteration)
        # Restrict to one iteration's subtree under consensus-state/active/.
        scope_subpaths = [f"consensus-state/active/{iteration}"]
    else:
        # Full restore: every top-level snapshotted path.
        scope_subpaths = list(SNAPSHOTTED_PATHS)

    with tempfile.TemporaryDirectory(prefix="consensus-restore-") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        try:
            worktree = _extract_tag_to_tempdir(repo_root, tag, tmpdir)
        except SnapshotError:
            raise

        try:
            # Enumerate files in the snapshot's tree that fall under each scope.
            paths_to_restore: list[str] = []
            for scope in scope_subpaths:
                src_scope_root = worktree / scope
                if not src_scope_root.exists():
                    continue
                if src_scope_root.is_file():
                    paths_to_restore.append(scope)
                else:
                    for f in src_scope_root.rglob("*"):
                        if f.is_dir():
                            continue
                        paths_to_restore.append(f.relative_to(worktree).as_posix())

            # iter-0014 codex-rev-001 fix: validate missing-iteration in dry-run
            # too (was only checked at real-restore time, so dry-run gave a
            # false safety signal).
            if not paths_to_restore and iteration:
                raise SnapshotError(
                    f"iteration {iteration!r} not found in snapshot {tag!r} — "
                    f"nothing to restore. Check `list` for snapshots that "
                    f"include this iteration."
                )

            # iter-0014 codex-rev-001 fix: compute the deletion set so dry-run
            # (and real restore) reports complete plan, not just additions.
            snapshot_set = set(paths_to_restore)
            paths_to_delete: list[str] = []
            for scope in scope_subpaths:
                tgt_scope = repo_root / scope
                if not tgt_scope.exists():
                    continue
                if tgt_scope.is_file():
                    rel = scope
                    if rel not in snapshot_set:
                        paths_to_delete.append(rel)
                else:
                    for f in tgt_scope.rglob("*"):
                        if f.is_dir():
                            continue
                        rel = f.relative_to(repo_root).as_posix()
                        if rel not in snapshot_set:
                            paths_to_delete.append(rel)

            if dry_run:
                return {"copied": paths_to_restore, "deleted": paths_to_delete}

            # codex-rev-002 fix: clean the target scope BEFORE copying so files
            # absent from the snapshot are removed (no hybrid state).
            for scope in scope_subpaths:
                tgt_scope = repo_root / scope
                if tgt_scope.exists():
                    if tgt_scope.is_file():
                        tgt_scope.unlink()
                    else:
                        shutil.rmtree(tgt_scope)

            # Copy each file from the worktree to the working tree.
            for rel in paths_to_restore:
                src = worktree / rel
                dst = repo_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            return {"copied": paths_to_restore, "deleted": paths_to_delete}
        finally:
            _run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo_root, check=False)


def _detect_dirty_paths(repo_root: Path, target_tag: str | None = None) -> list[str]:
    """Return paths under SNAPSHOTTED_PATHS that are not faithfully captured
    by ANY snapshot — either modified-not-snapshotted OR deleted-not-snapshotted.

    Per codex-rev-002 round-3 fix: also detect DELETIONS (paths in the latest
    snapshot or `target_tag` that no longer exist on disk). The prior version
    only walked extant files; a deleted file produced an empty `dirty` list,
    so `restore` skipped the auto-pre-snapshot and silently lost the deletion.

    `target_tag` (optional): when set, also compare against THIS specific
    snapshot so deletions vs the upcoming restore target are flagged
    irrespective of other snapshot history.

    Returns deduplicated list of repo-relative POSIX paths.
    """
    if not _orphan_branch_exists(repo_root):
        return []

    # Build the set of (path, blob_oid) tuples present in any snapshot commit
    # AND specifically the target tag (if given).
    commits_to_scan: list[str] = []
    log_result = _run_git(
        ["log", "--format=%H", SNAPSHOT_BRANCH],
        cwd=repo_root,
        check=False,
    )
    if log_result.returncode == 0:
        commits_to_scan += [c.strip() for c in log_result.stdout.splitlines() if c.strip()]
    if target_tag:
        rev = _run_git(["rev-parse", "--verify", "--quiet", target_tag],
                       cwd=repo_root, check=False)
        if rev.returncode == 0:
            commits_to_scan.append(rev.stdout.strip())

    snapshotted: set[tuple[str, str]] = set()
    target_paths: set[str] = set()
    target_sha = (
        _run_git(["rev-parse", "--verify", "--quiet", target_tag], cwd=repo_root, check=False).stdout.strip()
        if target_tag else ""
    )
    for commit in commits_to_scan:
        tree_result = _run_git(["ls-tree", "-r", commit], cwd=repo_root, check=False)
        if tree_result.returncode != 0:
            continue
        for line in tree_result.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            meta = parts[0].split()
            if len(meta) < 3:
                continue
            oid, path = meta[2], parts[1]
            snapshotted.add((path, oid))
            if commit == target_sha:
                target_paths.add(path)

    snapshotted_paths = {p for p, _ in snapshotted}
    snapshotted_oids_by_path: dict[str, set[str]] = {}
    for p, o in snapshotted:
        snapshotted_oids_by_path.setdefault(p, set()).add(o)

    dirty: set[str] = set()

    # 1) Modified-not-snapshotted: any current file whose (path, oid) isn't in any snapshot.
    for rel in SNAPSHOTTED_PATHS:
        root = repo_root / rel
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if f.is_dir():
                continue
            rel_path = f.relative_to(repo_root).as_posix()
            # Compute the git blob OID via `git hash-object`, NOT a raw Python
            # SHA-1 over working-tree bytes. With core.autocrlf=true (and no
            # .gitattributes) git stores LF-normalized blobs while the working
            # tree keeps CRLF, so a Python hash over raw bytes would never match
            # the stored OID -> every CRLF text file would read as always-dirty
            # (H-4). `git hash-object` applies the repo's clean filter so the
            # OID matches the stored blob.
            hash_result = _run_git(
                ["hash-object", "--", str(f)],
                cwd=repo_root,
                check=False,
            )
            if hash_result.returncode != 0:
                # git unavailable / unreadable file: conservatively treat as
                # dirty so a real change is never silently skipped.
                dirty.add(rel_path)
                continue
            oid = hash_result.stdout.strip()
            if rel_path not in snapshotted_paths or oid not in snapshotted_oids_by_path.get(rel_path, set()):
                dirty.add(rel_path)

    # 2) Deletion-dirty (codex-rev-002 round-3): paths in the target tag (or
    # any snapshot if no target given) that no longer exist on disk under
    # SNAPSHOTTED_PATHS. Without this, a restore that reintroduces a deleted
    # file would silently overwrite the operator's deletion intent.
    candidate_paths = target_paths if target_paths else snapshotted_paths
    for path in candidate_paths:
        if not path.startswith(tuple(p + "/" for p in SNAPSHOTTED_PATHS)) and \
           path not in SNAPSHOTTED_PATHS:
            continue
        if not (repo_root / path).exists():
            dirty.add(path)

    return sorted(dirty)


# ----- subcommands -----


def cmd_snapshot(args: argparse.Namespace, repo_root: Path) -> int:
    """Create a new snapshot. Optional --label appended to the tag."""
    try:
        tag = _build_tag(args.label)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        created = _snapshot_via_worktree(repo_root, tag, args.label)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"snapshot created: {created}")
    print(f"  branch: {SNAPSHOT_BRANCH}")
    print(f"  restore: python -m consensus_mcp._snapshot_state restore --tag {created}")
    return 0


def cmd_list(args: argparse.Namespace, repo_root: Path) -> int:
    """List snapshot tags, newest first."""
    if not _orphan_branch_exists(repo_root):
        print("no snapshots yet — create one with: python -m consensus_mcp._snapshot_state snapshot")
        return 0
    tags = _list_snapshot_tags(repo_root)
    if args.limit is not None:
        tags = tags[: args.limit]
    if not tags:
        print(f"branch {SNAPSHOT_BRANCH!r} exists but has no snapshot-* tags")
        return 0
    for tag, suffix in tags:
        # Show commit summary for context.
        result = _run_git(
            ["log", "-1", "--format=%cI %s", tag],
            cwd=repo_root,
            check=False,
        )
        info = result.stdout.strip() if result.returncode == 0 else "(commit info unavailable)"
        print(f"{tag}\n  {info}")
    return 0


def cmd_restore(args: argparse.Namespace, repo_root: Path) -> int:
    """Restore consensus-state from the named tag.

    Safety per iter-0012 F6a + F9d:
      - default: auto-pre-snapshot before restoring
      - --force: skip auto-pre-snapshot AND skip dirty-conflict check
      - --abort-on-dirty: refuse if dirty conflicts exist
      - --dry-run: print what would change; nothing applied
      - --iteration <id>: limit restore to that iteration's subtree
    """
    if not _orphan_branch_exists(repo_root):
        print(f"error: branch {SNAPSHOT_BRANCH!r} does not exist — no snapshots to restore", file=sys.stderr)
        return 1

    # codex-rev-001 round-3: fail-fast on unsafe --iteration BEFORE any
    # dirty-check, dry-run, or snapshot work.
    if args.iteration:
        try:
            _validate_iteration_name(args.iteration)
        except SnapshotError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # codex-rev-001 round-6 fix: tag validation must run BEFORE dry-run too —
    # prior version only validated tag in the real-restore branch, so dry-run
    # could accept a branch or commit ref that real restore would reject.
    # Asymmetric validation defeated the dry-run safety goal.
    rev = _run_git(["rev-parse", "--verify", "--quiet", f"refs/tags/{args.tag}"],
                   cwd=repo_root, check=False)
    if rev.returncode != 0:
        print(f"error: tag {args.tag!r} not found", file=sys.stderr)
        return 2

    if args.dry_run:
        try:
            plan = _restore_from_tag(repo_root, args.tag, iteration=args.iteration, dry_run=True)
        except SnapshotError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        copied, deleted = plan["copied"], plan["deleted"]
        print(f"DRY RUN: restore plan for tag {args.tag!r}")
        print(f"  would COPY {len(copied)} file(s) from snapshot")
        for p in copied[:10]:
            print(f"    + {p}")
        if len(copied) > 10:
            print(f"    + ... ({len(copied) - 10} more)")
        print(f"  would DELETE {len(deleted)} file(s) absent from snapshot (scope cleanup)")
        for p in deleted[:10]:
            print(f"    - {p}")
        if len(deleted) > 10:
            print(f"    - ... ({len(deleted) - 10} more)")
        return 0

    if not args.force:
        # Pass the target tag so deletion-dirty detection (codex-rev-002 round-3)
        # catches deletions specifically relevant to the upcoming restore.
        dirty = _detect_dirty_paths(repo_root, target_tag=args.tag)
        if dirty:
            if args.abort_on_dirty:
                print(
                    f"error: {len(dirty)} file(s) under consensus-state/ have "
                    f"content not present in any snapshot. Refusing per --abort-on-dirty.",
                    file=sys.stderr,
                )
                for p in dirty[:10]:
                    print(f"  {p}", file=sys.stderr)
                if len(dirty) > 10:
                    print(f"  ... ({len(dirty) - 10} more)", file=sys.stderr)
                return 3
            # Auto-pre-snapshot (per F6a).
            pre_label = f"pre-restore-{args.tag.removeprefix(SNAPSHOT_TAG_PREFIX)[:32]}"
            # Sanitize label (target tag may contain chars not in our pattern).
            pre_label = re.sub(r"[^A-Za-z0-9_-]", "_", pre_label)[:64]
            try:
                pre_tag = _build_tag(pre_label)
                _snapshot_via_worktree(repo_root, pre_tag, pre_label)
                print(f"auto-pre-snapshot: {pre_tag} (captures {len(dirty)} dirty file(s))")
            except SnapshotError as exc:
                print(f"error: auto-pre-snapshot failed: {exc}", file=sys.stderr)
                print("hint: re-run with --force to skip the safety snapshot", file=sys.stderr)
                return 1

    try:
        result = _restore_from_tag(repo_root, args.tag, iteration=args.iteration, dry_run=False)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"restored from tag {args.tag!r}: "
        f"{len(result['copied'])} file(s) copied, "
        f"{len(result['deleted'])} file(s) deleted (scope cleanup)"
    )
    if args.iteration:
        print(f"  iteration filter: {args.iteration}")
    return 0


def cmd_diff(args: argparse.Namespace, repo_root: Path) -> int:
    """Diff current consensus-state/ against the named snapshot tag.

    codex-rev-004 + codex-rev-005 fix:
      - Capture and PRINT the diff output (prior version discarded it).
      - Use a temp worktree for snapshot materialization, not text-mode
        `git archive` piped through Python (which corrupts binary content).
      - Return non-zero when differences are detected.

    Exit codes:
      0 — current matches snapshot (no diff)
      1 — differences detected (output printed)
      2 — tag not found / branch missing
    """
    if not _orphan_branch_exists(repo_root):
        print(f"error: branch {SNAPSHOT_BRANCH!r} does not exist", file=sys.stderr)
        return 2
    rev = _run_git(["rev-parse", "--verify", args.tag], cwd=repo_root, check=False)
    if rev.returncode != 0:
        print(f"error: tag {args.tag!r} not found", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="consensus-diff-") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        worktree = _extract_tag_to_tempdir(repo_root, args.tag, tmpdir)
        try:
            any_diff = False
            for rel in SNAPSHOTTED_PATHS:
                tgt = worktree / rel
                cur = repo_root / rel
                # `git diff --no-index` works on filesystem paths; capture+print.
                # check=False because diff exit code 1 means "differences exist".
                result = _run_git(
                    ["diff", "--no-index", "--",
                     str(tgt) if tgt.exists() else os.devnull,
                     str(cur) if cur.exists() else os.devnull],
                    cwd=repo_root,
                    check=False,
                )
                if result.stdout:
                    print(result.stdout, end="")
                    any_diff = True
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr)
                if result.returncode not in (0, 1):
                    # 0 = no diff; 1 = diff found; anything else is an error.
                    return 2
            return 1 if any_diff else 0
        finally:
            _run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo_root, check=False)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus_mcp._snapshot_state",
        description="Snapshot/restore the gitignored consensus-state/ tree on an orphan git branch.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="Create a snapshot of consensus-state/")
    p_snap.add_argument("--label", default=None, help="Optional human-readable label (^[A-Za-z0-9_-]{1,64}$)")

    p_list = sub.add_parser("list", help="List snapshot tags, newest first")
    p_list.add_argument("--limit", type=int, default=None, help="Show only the N most recent")

    p_rest = sub.add_parser("restore", help="Restore consensus-state/ from a snapshot tag")
    p_rest.add_argument("--tag", required=True, help="Snapshot tag to restore from")
    p_rest.add_argument("--iteration", default=None, help="Restore only this iteration's subtree")
    p_rest.add_argument("--dry-run", action="store_true", help="List files that would change; nothing modified")
    p_rest.add_argument("--force", action="store_true",
                        help="Skip auto-pre-snapshot AND skip dirty-conflict check (operator accepts loss)")
    p_rest.add_argument("--abort-on-dirty", action="store_true",
                        help="Refuse if any consensus-state file has content not in any snapshot")

    p_diff = sub.add_parser("diff", help="Diff current consensus-state/ against a snapshot tag")
    p_diff.add_argument("--tag", required=True, help="Snapshot tag to compare against")

    ns = p.parse_args(argv)
    try:
        repo_root = _resolve_repo_root()
    except RepoRootResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4

    dispatch = {
        "snapshot": cmd_snapshot,
        "list": cmd_list,
        "restore": cmd_restore,
        "diff": cmd_diff,
    }
    return dispatch[ns.cmd](ns, repo_root)


if __name__ == "__main__":
    sys.exit(main())
