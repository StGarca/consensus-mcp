"""One-time import: parent project's iteration history → this repo's archive.

Per iter-0012 verdict (F1a + F3a): mirror parent's `agent-loop/active/` and
`agent-loop/archive/review-passes/` into
`consensus-state/archive/imported-from-parent/{active-iterations,archive-review-passes}/`
and emit a `source-manifest.yaml` with per-entry sha256_tree for integrity
verification.

This is a ONE-TIME import. After it runs, the target subtree should be
captured in the first orphan-branch snapshot so it's permanently recoverable.

USAGE
-----
  python -m consensus_mcp._import_parent_history \
      --parent <path-to-parent-agent-loop-dir> \
      [--target <override-target-dir>] \
      [--dry-run]

Default parent: C:\\Users\\steve\\Downloads\\ebook2audiobook-26.4.16\\agent-loop
(maintainer-supplied; see project_parent_project memory).

The script refuses to overwrite an existing import target unless --force is
passed. Idempotent intent: re-running with the same parent should produce the
same byte-for-byte target tree (sha256_tree hashes are checked).
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from consensus_mcp._dispatch_base import (
    RepoRootResolutionError,
    _resolve_repo_root,
)


DEFAULT_PARENT = Path(r"C:\Users\steve\Downloads\ebook2audiobook-26.4.16\agent-loop")


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_of_tree(directory: Path) -> str:
    """Canonical sha256 of a directory's contents.

    Format: for each file (sorted by relative POSIX path), append
    `<rel_path>\\0<sha256(content)>\\n`; sha256 the concatenation.

    Matches the canonical form used by _compute_per_patch_base_sha's
    text-fallback path in _dispatch_base.py.
    """
    if not directory.is_dir():
        return ""
    entries: list[tuple[str, str]] = []
    for f in sorted(directory.rglob("*")):
        if f.is_dir():
            continue
        rel = f.relative_to(directory).as_posix()
        content = f.read_bytes()
        entries.append((rel, hashlib.sha256(content).hexdigest()))
    entries.sort()
    canonical = "\n".join(f"{rel}\0{h}" for rel, h in entries)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_history(
    parent_agent_loop: Path,
    target_root: Path,
    extraction_commit: str = "ff0164f",
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Mirror parent agent-loop into target. Returns the source-manifest dict.

    Layout produced (per iter-0012 F1a):
      target_root/
        README.md
        source-manifest.yaml
        active-iterations/        (copies of parent/active/iteration-*/ )
        archive-review-passes/    (copies of parent/archive/review-passes/*.yaml)
    """
    if not parent_agent_loop.is_dir():
        raise RuntimeError(f"parent agent-loop path does not exist: {parent_agent_loop}")

    parent_active = parent_agent_loop / "active"
    parent_archive_passes = parent_agent_loop / "archive" / "review-passes"

    if not parent_active.is_dir():
        raise RuntimeError(f"parent has no active/ dir at {parent_active}")

    if target_root.exists() and any(target_root.iterdir()) and not force:
        raise RuntimeError(
            f"target {target_root} exists and is non-empty. "
            f"Use --force to re-import (existing content will be removed)."
        )

    manifest: dict = {
        "schema_version": 1,
        "source": {
            "repo": "ebook2audiobook-26.4.16",
            "parent_path": str(parent_agent_loop),
            "extraction_commit_in_standalone": extraction_commit,
            "imported_at_utc": _iso_utc_now(),
            "imported_by": "cli:_import_parent_history",
        },
        "entries": [],
    }

    target_active = target_root / "active-iterations"
    target_archive = target_root / "archive-review-passes"

    if dry_run:
        # Just enumerate.
        for child in sorted(parent_active.iterdir()):
            if not child.is_dir():
                continue
            manifest["entries"].append({
                "source": str(child.relative_to(parent_agent_loop.parent)),
                "target": str((target_active / child.name).relative_to(target_root.parent)),
                "kind": "iteration_dir",
                "sha256_tree": _sha256_of_tree(child),
            })
        if parent_archive_passes.is_dir():
            for f in sorted(parent_archive_passes.iterdir()):
                if not f.is_file():
                    continue
                manifest["entries"].append({
                    "source": str(f.relative_to(parent_agent_loop.parent)),
                    "target": str((target_archive / f.name).relative_to(target_root.parent)),
                    "kind": "archive_pass",
                    "sha256_content": _sha256_of_file(f),
                })
        return manifest

    # Actual import.
    if target_root.exists() and force:
        shutil.rmtree(target_root)
    target_root.mkdir(parents=True, exist_ok=True)
    target_active.mkdir(parents=True, exist_ok=True)
    target_archive.mkdir(parents=True, exist_ok=True)

    # Copy active/iteration-*/
    for child in sorted(parent_active.iterdir()):
        if not child.is_dir():
            continue
        dst = target_active / child.name
        shutil.copytree(child, dst)
        manifest["entries"].append({
            "source": str(child.relative_to(parent_agent_loop.parent)),
            "target": str(dst.relative_to(target_root.parent)),
            "kind": "iteration_dir",
            "sha256_tree": _sha256_of_tree(dst),
        })

    # Copy archive/review-passes/*
    if parent_archive_passes.is_dir():
        for f in sorted(parent_archive_passes.iterdir()):
            if not f.is_file():
                continue
            dst = target_archive / f.name
            shutil.copy2(f, dst)
            manifest["entries"].append({
                "source": str(f.relative_to(parent_agent_loop.parent)),
                "target": str(dst.relative_to(target_root.parent)),
                "kind": "archive_pass",
                "sha256_content": _sha256_of_file(dst),
            })

    # Write manifest + README.
    (target_root / "source-manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    readme = (
        "# Imported parent project history\n\n"
        "This directory contains a one-time import of iteration history from "
        "the project consensus-mcp was extracted from "
        f"(`{parent_agent_loop}`).\n\n"
        "**Provenance**: see `source-manifest.yaml` for per-entry sha256_tree + content hashes.\n\n"
        "## Layout\n\n"
        "- `active-iterations/iteration-NNNN/` — mirrors parent's `agent-loop/active/iteration-NNNN/`\n"
        "- `archive-review-passes/*.yaml` — mirrors parent's `agent-loop/archive/review-passes/`\n\n"
        "**This subtree is gitignored** (per the project-wide `.gitignore` for "
        "`consensus-state/archive/review-passes/*.yaml`). Durability is provided by "
        "the orphan branch `consensus-state-snapshots` (see "
        "`consensus_mcp/_snapshot_state.py`).\n\n"
        f"Imported: {manifest['source']['imported_at_utc']}\n"
        f"Source: {manifest['source']['parent_path']}\n"
        f"Standalone extraction commit: {manifest['source']['extraction_commit_in_standalone']}\n"
    )
    (target_root / "README.md").write_text(readme, encoding="utf-8")

    return manifest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus_mcp._import_parent_history",
        description="One-time import of parent project's iteration history into this repo's archive.",
    )
    p.add_argument("--parent", type=Path, default=DEFAULT_PARENT,
                   help="Path to parent project's agent-loop dir")
    p.add_argument("--target", type=Path, default=None,
                   help="Target dir (default: <repo>/consensus-state/archive/imported-from-parent)")
    p.add_argument("--dry-run", action="store_true",
                   help="Don't copy; print manifest summary only")
    p.add_argument("--force", action="store_true",
                   help="Overwrite target if non-empty")
    ns = p.parse_args(argv)

    try:
        repo_root = _resolve_repo_root()
    except RepoRootResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4

    target_root = ns.target if ns.target is not None else (
        repo_root / "consensus-state" / "archive" / "imported-from-parent"
    )

    try:
        manifest = import_history(
            parent_agent_loop=ns.parent,
            target_root=target_root,
            dry_run=ns.dry_run,
            force=ns.force,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    n_iters = sum(1 for e in manifest["entries"] if e["kind"] == "iteration_dir")
    n_passes = sum(1 for e in manifest["entries"] if e["kind"] == "archive_pass")
    print(f"{'DRY-RUN: ' if ns.dry_run else ''}import summary:")
    print(f"  parent: {ns.parent}")
    print(f"  target: {target_root}")
    print(f"  iteration dirs: {n_iters}")
    print(f"  archive passes: {n_passes}")
    if not ns.dry_run:
        print(f"  manifest: {target_root / 'source-manifest.yaml'}")
        print(f"  next: python -m consensus_mcp._snapshot_state snapshot --label initial-baseline-post-import")
    return 0


if __name__ == "__main__":
    sys.exit(main())
