"""Lane lifecycle + containment for architect-build (workflow D).

Consult Q1 (2026-06-10, 4/4): the builder edits FILES ONLY; this module is
the ONLY component that runs git against the lane (supervisor-owned git).
Layers implemented here:
  L1 lane path under .consensus/architect/<goal-id>/lane/ (via _architect_paths)
  L3 supervisor-owned git with hooks neutralized + scrubbed env
  L4 post-build lane scan: symlinks/junctions forbidden, outside-lane
     hardlinks forbidden, .git pointer verified against the main gitdir
  L5 main-repo integrity snapshot/check (root-cause-independent safeguard)
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from consensus_mcp import _architect_paths as ap

_GIT_TIMEOUT = 120


class LaneError(RuntimeError):
    """Raised on lane lifecycle/containment failures."""


def _scrubbed_env() -> dict:
    env = dict(os.environ)
    # Neutralize user/system git config surprises; hooks are neutralized
    # per-invocation via -c core.hooksPath (GIT_CONFIG_GLOBAL would also
    # drop user identity needed for commits in tests).
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


_EMPTY_HOOKS_DIR: Path | None = None


def _empty_hooks_dir() -> str:
    global _EMPTY_HOOKS_DIR
    if _EMPTY_HOOKS_DIR is None or not _EMPTY_HOOKS_DIR.is_dir():
        _EMPTY_HOOKS_DIR = Path(tempfile.mkdtemp(prefix="consensus-no-hooks-"))
    return str(_EMPTY_HOOKS_DIR)


def _git(cwd: Path, *args: str) -> str:
    """Run git with hooks neutralized; raise LaneError on failure."""
    cmd = ["git", "-c", f"core.hooksPath={_empty_hooks_dir()}", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=_GIT_TIMEOUT, env=_scrubbed_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaneError(f"git {' '.join(args)} failed to launch: {exc}") from exc
    if proc.returncode != 0:
        raise LaneError(
            f"git {' '.join(args)} exited {proc.returncode}: "
            f"{proc.stderr.strip()[:500]}"
        )
    return proc.stdout


def create_lane(repo_root: Path, goal: Path, branch: str, base_sha: str) -> Path:
    """git worktree add the lane at base_sha. Idempotent if the lane already
    exists on the SAME branch; collides loudly otherwise."""
    repo_root = Path(repo_root)
    lane = ap.lane_dir(goal)
    if lane.exists():
        # Resume path: the builder has already touched this lane, so the
        # containment guard must run BEFORE any supervisor git op - a
        # rewritten gitdir: pointer must never receive a git invocation
        # (same invariant commit_lane/lane_diff enforce).
        lane = _require_lane_contained(repo_root, lane)
        try:
            current = _git(lane, "rev-parse", "--abbrev-ref", "HEAD").strip()
        except LaneError as exc:
            raise LaneError(f"lane dir exists but is not a worktree: {exc}") from exc
        if current != branch:
            raise LaneError(
                f"lane exists on branch {current!r}, expected {branch!r}"
            )
        return lane
    existing = _git(repo_root, "branch", "--list", branch).strip()
    if existing:
        raise LaneError(
            f"branch {branch!r} already exists; goal-id collision "
            f"(consult Q7.6) - pick a new goal id or clean up the old lane"
        )
    lane.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "worktree", "add", "-b", branch, str(lane), base_sha)
    return lane


def _lane_branch(repo_root: Path, lane: Path) -> str | None:
    """The branch checked out in the lane worktree, read from the MAIN
    repo's worktree list - never by invoking git inside the lane (the
    create_lane/commit_lane invariant: a rewritten gitdir: pointer must
    not receive a git invocation). None for detached/unlisted lanes."""
    try:
        resolved = lane.resolve()
    except OSError:
        return None
    current: Path | None = None
    for line in _git(repo_root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):].strip())
        elif line.startswith("branch ") and current is not None:
            try:
                matches = current.resolve() == resolved
            except OSError:
                matches = False
            if matches:
                return line[len("branch "):].strip().removeprefix("refs/heads/")
    return None


def remove_lane(repo_root: Path, goal: Path) -> None:
    """Prune the lane worktree AND its branch. The branch is what makes a
    goal-id collision sticky (create_lane refuses while it exists), so
    removal must clear both or the operator-facing collision advice
    ('clean up the old lane') would be a dead end.

    This is the one DESTRUCTIVE lane op, so it anchors containment before
    the git op - but PATH-ONLY (lstat + resolve under the architect root),
    never the full .git pointer check of _require_lane_contained: a
    goal/lane symlinked onto another registered worktree must never receive
    'worktree remove --force' / 'branch -D', while a tampered-but-delivered
    lane must STAY removable (cleanup of a closed goal must not deadlock on
    pointer tamper)."""
    repo_root = Path(repo_root)
    goal = Path(goal)
    lane = ap.lane_dir(goal)
    for p in (goal, lane):
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return  # no goal / no lane: nothing to remove (idempotent)
        except OSError as exc:
            raise LaneError(f"cannot lstat {p}: {exc}") from exc
        if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st):
            raise LaneError(
                f"{p} is a symlink/junction - refusing destructive "
                f"lane removal"
            )
    try:
        resolved = lane.resolve()
        architect_root = repo_root.resolve().joinpath(*ap.GOAL_ROOT_PARTS)
    except OSError as exc:
        raise LaneError(f"cannot resolve lane containment: {exc}") from exc
    if architect_root not in resolved.parents:
        raise LaneError(
            f"lane {resolved} does not resolve under the architect root "
            f"{architect_root} - refusing destructive removal"
        )
    branch = _lane_branch(repo_root, resolved)
    _git(repo_root, "worktree", "remove", "--force", str(resolved))
    if branch:
        _git(repo_root, "branch", "-D", branch)


def _main_gitdir(repo_root: Path) -> Path:
    """The MAIN repo's gitdir (asked of git itself, never of lane content)."""
    repo_root = Path(repo_root)
    gitdir = Path(_git(repo_root, "rev-parse", "--git-dir").strip())
    if not gitdir.is_absolute():
        gitdir = repo_root / gitdir
    return gitdir


def _is_reparse_point(st) -> bool:
    # NTFS directory junctions are reparse points that S_ISLNK misses;
    # Path.is_junction is 3.12+ and the floor is 3.11, so read the raw
    # attribute (absent -> 0 on POSIX). 'init platform consistency':
    # identical scan semantics on Windows.
    return bool(
        getattr(st, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _derive_repo_root(lane: Path) -> Path | None:
    """Invert the L1 layout <repo_root>/.consensus/architect/<goal-id>/lane.
    The lane path is supervisor-chosen (never builder-controlled), so the
    derivation is exactly as trustworthy as the lane argument itself."""
    parents = lane.parents
    depth = len(ap.GOAL_ROOT_PARTS) + 1  # <goal-id> plus the root parts
    if (
        lane.name == ap.LANE_DIRNAME
        and len(parents) > depth
        and tuple(p.name for p in parents[1:depth])
        == tuple(reversed(ap.GOAL_ROOT_PARTS))
    ):
        return parents[depth]
    return None


_GITDIR_LINE_RE = re.compile(r"^gitdir:\s*(.+?)\s*$")


def _git_pointer_violations(lane: Path, repo_root: Path | None = None) -> list[str]:
    """The lane/.git pointer is the ONE path that redirects every
    supervisor-owned (L3) git op, so it gets NO exemption: it must be a
    regular non-symlink, non-reparse file whose single 'gitdir: <path>'
    line resolves under the MAIN repo's gitdir/worktrees/. Anything else
    is a violation (root-cause-independent doctrine)."""
    pointer = lane / ".git"
    try:
        st = os.lstat(pointer)
    except OSError as exc:
        return [f"lane .git pointer unreadable: {exc}"]
    if stat.S_ISLNK(st.st_mode):
        return ["lane .git pointer is a symlink"]
    if _is_reparse_point(st):
        return ["lane .git pointer is a reparse point (junction)"]
    if not stat.S_ISREG(st.st_mode):
        return ["lane .git pointer is not a regular file"]
    try:
        text = pointer.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"lane .git pointer unreadable: {exc}"]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    match = _GITDIR_LINE_RE.fullmatch(lines[0]) if len(lines) == 1 else None
    if match is None:
        return ["lane .git pointer content is not a single gitdir: line"]
    target = Path(match.group(1))
    if not target.is_absolute():
        target = lane / target
    if repo_root is None:
        repo_root = _derive_repo_root(lane)
    if repo_root is None:
        return [
            "lane .git pointer unverifiable: lane is not laid out as "
            f"{'/'.join(ap.GOAL_ROOT_PARTS)}/<goal-id>/{ap.LANE_DIRNAME} "
            "and no repo_root was supplied"
        ]
    try:
        target = target.resolve()
        worktrees = (_main_gitdir(Path(repo_root)) / "worktrees").resolve()
    except (OSError, LaneError) as exc:
        return [f"lane .git pointer unverifiable: {exc}"]
    if worktrees not in target.parents:
        return [
            "lane .git pointer redirected outside the main gitdir "
            f"worktrees: {target}"
        ]
    return []


def _require_lane_contained(repo_root: Path, lane: Path) -> Path:
    """repo_root anchors containment for supervisor git: the lane must
    resolve under repo_root's architect root and its .git pointer must
    target the main gitdir's worktrees - LaneError otherwise, BEFORE any
    git op runs against the lane."""
    repo_root = Path(repo_root).resolve()
    lane = Path(lane).resolve()
    architect_root = repo_root.joinpath(*ap.GOAL_ROOT_PARTS)
    if architect_root not in lane.parents:
        raise LaneError(
            f"lane {lane} does not resolve under the architect root "
            f"{architect_root}"
        )
    problems = _git_pointer_violations(lane, repo_root)
    if problems:
        raise LaneError("; ".join(problems))
    return lane


def commit_lane(repo_root: Path, lane: Path, message: str) -> str:
    """Supervisor-owned add -A + commit in the lane. Empty diff is fine -
    returns the current lane HEAD either way. repo_root anchors containment:
    the lane must live under its architect root with an intact .git pointer."""
    lane = _require_lane_contained(repo_root, lane)
    _git(lane, "add", "-A")
    staged = _git(lane, "status", "--porcelain").strip()
    if staged:
        _git(lane, "commit", "-m", message)
    return _git(lane, "rev-parse", "HEAD").strip()


def lane_diff(repo_root: Path, lane: Path, base_sha: str) -> str:
    """Diff base..HEAD inside the lane; repo_root anchors the same
    containment check as commit_lane."""
    lane = _require_lane_contained(repo_root, lane)
    return _git(lane, "diff", f"{base_sha}..HEAD")


def scan_lane_integrity(lane: Path, repo_root: Path | None = None) -> list[str]:
    """Symlinks and Windows reparse points (junctions) anywhere in the lane
    are violations and are never descended into (Path.is_symlink misses
    junctions and rglob walks through them, so the walk prunes explicitly);
    hardlinks whose inode also lives outside the lane are violations
    (st_nlink exceeding the count of lane paths sharing the same
    (st_dev, st_ino) - a pair living entirely inside the lane is fine).
    The lane/.git pointer gets NO exemption: it must be a regular
    non-symlink file whose gitdir target resolves under the main repo's
    gitdir/worktrees/ (repo_root is derived from the L1 layout when
    omitted)."""
    lane = Path(lane).resolve()
    violations: list[str] = list(_git_pointer_violations(lane, repo_root))
    lane_inode_counts: dict[tuple[int, int], int] = {}
    files: list[tuple[Path, os.stat_result]] = []

    def _check_entry(p: Path) -> bool:
        """Record violations for p; True means do not descend further."""
        try:
            st = os.lstat(p)
        except OSError as exc:
            violations.append(
                f"unstatable lane entry: {p.relative_to(lane)} ({exc})"
            )
            return True
        if stat.S_ISLNK(st.st_mode):
            violations.append(f"symlink in lane: {p.relative_to(lane)}")
            return True
        if _is_reparse_point(st):
            violations.append(
                f"reparse point (junction) in lane: {p.relative_to(lane)}"
            )
            return True
        if stat.S_ISREG(st.st_mode):
            key = (st.st_dev, st.st_ino)
            lane_inode_counts[key] = lane_inode_counts.get(key, 0) + 1
            files.append((p, st))
        return False

    for dirpath, dirnames, filenames in os.walk(lane):
        base = Path(dirpath)
        # topdown walk: pruning dirnames in place stops the descent BEFORE
        # outside content pollutes the intra-lane inode counts.
        dirnames[:] = [d for d in dirnames if not _check_entry(base / d)]
        for name in filenames:
            _check_entry(base / name)
    for p, st in files:
        if st.st_nlink > lane_inode_counts[(st.st_dev, st.st_ino)]:
            violations.append(
                f"hardlink with outside-lane inode: "
                f"{p.relative_to(lane)} (nlink={st.st_nlink})"
            )
    return violations


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "absent"


_GOAL_ROOT_PREFIX = "/".join(ap.GOAL_ROOT_PARTS) + "/"


def _status_line_is_goal_dir(line: str) -> bool:
    """True iff EVERY path side of a --porcelain v1 line is ROOT-ANCHORED
    under the architect goal root.

    Porcelain paths are repo-root-relative, so a bare substring match would
    also exclude e.g. vendor/foo/.consensus/architect/evil.sh - a blind
    spot in the L5 detector. Renames carry two sides ('orig -> dest'); a
    rename OUT of the goal dir keeps its outside path visible because ALL
    sides must match. Special-char paths arrive C-quoted; only the outer
    quotes matter for the prefix test (the prefix itself has no escapes),
    and an unparseable side fails the match - fail-closed, the line stays
    visible."""
    body = line[3:] if len(line) > 3 else ""
    sides = [s.strip() for s in body.split(" -> ")]
    if not sides or not any(sides):
        return False
    for side in sides:
        if side.startswith('"') and side.endswith('"') and len(side) >= 2:
            side = side[1:-1]
        if not side.replace("\\", "/").startswith(_GOAL_ROOT_PREFIX):
            return False
    return True


def snapshot_main_integrity(repo_root: Path) -> dict:
    """Record main working-tree status, refs, hooks + config hashes.

    Paths under the goal root are EXCLUDED from the status view (root-
    anchored, per-side - see _status_line_is_goal_dir) because the goal dir
    mutates during normal supervisor operation."""
    repo_root = Path(repo_root)
    status = [
        line for line in _git(
            repo_root, "status", "--porcelain", "--untracked-files=all"
        ).splitlines()
        if not _status_line_is_goal_dir(line)
    ]
    refs = _git(
        repo_root, "for-each-ref", "--format=%(refname) %(objectname)",
        "refs/heads",
    ).strip()
    gitdir = _main_gitdir(repo_root)
    hooks = sorted(
        f"{p.name}:{_hash_file(p)}"
        for p in (gitdir / "hooks").glob("*") if p.is_file()
    )
    return {
        "head": _git(repo_root, "rev-parse", "HEAD").strip(),
        "status": status,
        "refs": refs,
        "hooks": hooks,
        "config_sha": _hash_file(gitdir / "config"),
    }


def snapshot_goal_artifacts(goal: Path) -> dict[str, str]:
    """sha256 of every file under the goal dir EXCEPT the lane/ subtree
    (builder-writable by design).

    The frozen verification gate executes builder-authored lane content
    UNSANDBOXED (operator command, shell=True, cwd=lane), and the cycle-N
    approval artifacts (review.yaml / ruling.yaml) are content-hash seals,
    not authenticity signatures - mere filesystem access can forge them.
    This snapshot/check pair is the L5-style root-cause-independent guard
    for that window: snapshot before the command runs, compare after, and
    ANY goal-artifact delta is a containment breach. Symlinked
    subdirectories are not followed (os.walk default), so a link planted
    during the run surfaces as a created path, never as a traversal."""
    goal = Path(goal)
    hashes: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(goal):
        base = Path(dirpath)
        if base == goal and ap.LANE_DIRNAME in dirnames:
            dirnames.remove(ap.LANE_DIRNAME)
        for name in filenames:
            p = base / name
            hashes[p.relative_to(goal).as_posix()] = _hash_file(p)
    return hashes


def check_goal_artifacts(goal: Path, before: dict[str, str]) -> list[str]:
    """Compare a fresh snapshot_goal_artifacts to `before`; every created,
    deleted, or modified non-lane goal artifact is a violation (the
    supervisor writes its own artifacts strictly OUTSIDE the guarded
    window, so there is no expected delta)."""
    after = snapshot_goal_artifacts(goal)
    return _diff_hashes(before, after, "goal artifact")


def _diff_hashes(before: dict[str, str], after: dict[str, str],
                 label: str) -> list[str]:
    violations: list[str] = []
    for rel in sorted(set(before) | set(after)):
        b, a = before.get(rel), after.get(rel)
        if b == a:
            continue
        if b is None:
            violations.append(f"{label} created: {rel}")
        elif a is None:
            violations.append(f"{label} deleted: {rel}")
        else:
            violations.append(f"{label} modified: {rel}")
    return violations


def snapshot_architect_tree(repo_root: Path, exclude_lane: Path) -> dict[str, str]:
    """sha256 of every file under <repo_root>/.consensus/architect EXCEPT
    the active lane subtree.

    The DECISIVE-EXPERIMENT finding (2026-06-10): codex --sandbox
    workspace-write does NOT confine writes to --cd; a builder/verification
    subprocess can write anywhere, including a SIBLING goal's sealed
    artifacts or the architect root - paths that snapshot_main_integrity
    blanket-excludes (the whole .consensus/architect subtree) and
    snapshot_goal_artifacts misses (only the ACTIVE goal). This whole-tree
    snapshot is the superset guard: the only path legitimately written
    during a guarded build/verification window is the active lane, so ANY
    delta elsewhere under the architect root is a containment breach. The
    lane is keyed by resolved path so a symlinked lane cannot smuggle an
    exclusion."""
    arch_root = Path(repo_root).joinpath(*ap.GOAL_ROOT_PARTS)
    try:
        excl = exclude_lane.resolve()
    except OSError:
        excl = exclude_lane
    hashes: dict[str, str] = {}
    if not arch_root.is_dir():
        return hashes
    for dirpath, dirnames, filenames in os.walk(arch_root):
        base = Path(dirpath)
        # prune the active lane subtree (resolved compare, not name match)
        kept = []
        for d in dirnames:
            try:
                resolved = (base / d).resolve()
            except OSError:
                resolved = base / d
            if resolved != excl:
                kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            p = base / name
            hashes[p.relative_to(arch_root).as_posix()] = _hash_file(p)
    return hashes


def check_architect_tree(repo_root: Path, before: dict[str, str],
                         exclude_lane: Path) -> list[str]:
    """Compare a fresh snapshot_architect_tree to `before`; any delta
    outside the active lane is a containment breach (the supervisor writes
    its own artifacts OUTSIDE the guarded window, so zero delta is
    expected)."""
    after = snapshot_architect_tree(repo_root, exclude_lane)
    return _diff_hashes(before, after, "architect-tree artifact")


def check_main_integrity(repo_root: Path, before: dict, *, lane_branch: str | None = None) -> list[str]:
    """Compare a fresh snapshot to `before`; lane branch ref churn is the one
    EXPECTED delta (the supervisor itself commits there)."""
    after = snapshot_main_integrity(repo_root)
    violations: list[str] = []
    if after["head"] != before["head"]:
        violations.append(
            f"main HEAD changed: {before['head']} -> {after['head']}"
        )
    if after["status"] != before["status"]:
        violations.append(
            f"main working tree changed: {sorted(set(after['status']) ^ set(before['status']))[:10]}"
        )
    def _ref_map(text: str) -> dict:
        out = {}
        for line in text.splitlines():
            if " " in line:
                name, sha = line.rsplit(" ", 1)
                out[name] = sha
        return out
    rb, ra = _ref_map(before["refs"]), _ref_map(after["refs"])
    skip = f"refs/heads/{lane_branch}" if lane_branch else None
    for name in sorted(set(rb) | set(ra)):
        if name == skip:
            continue
        if rb.get(name) != ra.get(name):
            violations.append(f"ref changed: {name}")
    if after["hooks"] != before["hooks"]:
        violations.append("hooks changed")
    if after["config_sha"] != before["config_sha"]:
        violations.append("repo config changed")
    return violations
