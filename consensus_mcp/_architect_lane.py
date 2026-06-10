"""Lane lifecycle + containment for architect-build (workflow D).

Consult Q1 (2026-06-10, 4/4): the builder edits FILES ONLY; this module is
the ONLY component that runs git against the lane (supervisor-owned git).
Layers implemented here:
  L1 lane path under .consensus/architect/<goal-id>/lane/ (via _architect_paths)
  L3 supervisor-owned git with hooks neutralized + scrubbed env
  L4 post-build lane scan: symlinks forbidden, outside-lane hardlinks forbidden
  L5 main-repo integrity snapshot/check (root-cause-independent safeguard)
"""
from __future__ import annotations

import hashlib
import os
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


def remove_lane(repo_root: Path, goal: Path) -> None:
    lane = ap.lane_dir(goal)
    if lane.exists():
        _git(Path(repo_root), "worktree", "remove", "--force", str(lane))


def commit_lane(repo_root: Path, lane: Path, message: str) -> str:
    """Supervisor-owned add -A + commit in the lane. Empty diff is fine -
    returns the current lane HEAD either way."""
    lane = Path(lane)
    _git(lane, "add", "-A")
    staged = _git(lane, "status", "--porcelain").strip()
    if staged:
        _git(lane, "commit", "-m", message)
    return _git(lane, "rev-parse", "HEAD").strip()


def lane_diff(repo_root: Path, lane: Path, base_sha: str) -> str:
    return _git(Path(lane), "diff", f"{base_sha}..HEAD")


def scan_lane_integrity(lane: Path) -> list[str]:
    """Symlinks anywhere in the lane are violations; hardlinks whose inode
    also lives outside the lane are violations. .git pointer file excluded."""
    lane = Path(lane).resolve()
    violations: list[str] = []
    lane_dev_inodes: set[tuple[int, int]] = set()
    entries: list[Path] = []
    for p in lane.rglob("*"):
        if p.name == ".git" and p.parent == lane:
            continue
        entries.append(p)
        if p.is_symlink():
            violations.append(f"symlink in lane: {p.relative_to(lane)}")
            continue
        if p.is_file():
            st = p.stat(follow_symlinks=False)
            lane_dev_inodes.add((st.st_dev, st.st_ino))
    for p in entries:
        if p.is_symlink() or not p.is_file():
            continue
        st = p.stat(follow_symlinks=False)
        if st.st_nlink > 1:
            violations.append(
                f"hardlink with outside-lane inode suspected: "
                f"{p.relative_to(lane)} (nlink={st.st_nlink})"
            )
    return violations


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "absent"


def snapshot_main_integrity(repo_root: Path) -> dict:
    """Record main working-tree status, refs, hooks + config hashes.

    Paths under .consensus/architect/ are EXCLUDED from the status view -
    the goal dir mutates during normal supervisor operation."""
    repo_root = Path(repo_root)
    status = [
        line for line in _git(repo_root, "status", "--porcelain").splitlines()
        if ".consensus/architect/" not in line.replace("\\", "/")
    ]
    refs = _git(
        repo_root, "for-each-ref", "--format=%(refname) %(objectname)",
        "refs/heads",
    ).strip()
    gitdir = Path(_git(repo_root, "rev-parse", "--git-dir").strip())
    if not gitdir.is_absolute():
        gitdir = repo_root / gitdir
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
