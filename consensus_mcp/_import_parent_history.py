"""One-time import: parent project's iteration history -> this repo's archive.

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

Default parent: C:\\Users\\<you>\\Downloads\\upstream-26.4.16\\agent-loop
(maintainer-supplied; see project_parent_project memory).

The script refuses to overwrite an existing import target unless --force is
passed. Idempotent intent: re-running with the same parent should produce the
same byte-for-byte target tree (sha256_tree hashes are checked).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from consensus_mcp._dispatch_base import (
    RepoRootResolutionError,
    _resolve_repo_root,
)


# No default parent path - this is a one-time, machine-specific maintenance
# tool; a hardcoded personal absolute path does not belong in a public package.
# Callers MUST pass --parent explicitly (see main()).
DEFAULT_PARENT = None


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


def _build_manifest_structure(
    parent_agent_loop: Path,
    target_root: Path,
    parent_active: Path,
    parent_archive_passes: Path,
    imported_at_utc: str,
    extraction_commit: str,
    imported_by: str = "cli:_import_parent_history",
) -> dict:
    """Pure function: compute canonical manifest dict from source state.

    Used BOTH by the initial-import path (write fresh) AND by the idempotency
    skip check (render expected to compare against on-disk bytes). Pure
    function-of-source means hash equality of two runs implies content equality.
    """
    target_active_preview = target_root / "active-iterations"
    target_archive_preview = target_root / "archive-review-passes"
    manifest: dict = {
        "schema_version": 1,
        "source": {
            "repo": "upstream-26.4.16",
            "parent_path": str(parent_agent_loop),
            "extraction_commit_in_standalone": extraction_commit,
            "imported_at_utc": imported_at_utc,
            "imported_by": imported_by,
        },
        "entries": [],
    }
    for child in sorted(parent_active.iterdir()):
        if not child.is_dir():
            continue
        manifest["entries"].append({
            "source": str(child.relative_to(parent_agent_loop.parent)),
            "target": str((target_active_preview / child.name).relative_to(target_root.parent)),
            "kind": "iteration_dir",
            "sha256_tree": _sha256_of_tree(child),
        })
    if parent_archive_passes.is_dir():
        for f in sorted(parent_archive_passes.iterdir()):
            if not f.is_file():
                continue
            manifest["entries"].append({
                "source": str(f.relative_to(parent_agent_loop.parent)),
                "target": str((target_archive_preview / f.name).relative_to(target_root.parent)),
                "kind": "archive_pass",
                "sha256_content": _sha256_of_file(f),
            })
    return manifest


def _render_readme(parent_agent_loop: Path, imported_at_utc: str, extraction_commit: str) -> str:
    """Canonical README content. Pure function of (parent, timestamp, commit)
    so idempotency check can compare existing-on-disk to expected."""
    return (
        "# Imported parent project history\n\n"
        "This directory contains a one-time import of iteration history from "
        "the project consensus-mcp was extracted from "
        f"(`{parent_agent_loop}`).\n\n"
        "**Provenance**: see `source-manifest.yaml` for per-entry sha256_tree + content hashes.\n\n"
        "## Layout\n\n"
        "- `active-iterations/iteration-NNNN/` - mirrors parent's `agent-loop/active/iteration-NNNN/`\n"
        "- `archive-review-passes/*.yaml` - mirrors parent's `agent-loop/archive/review-passes/`\n\n"
        "**This subtree is gitignored** (per the project-wide `.gitignore` for "
        "`consensus-state/archive/review-passes/*.yaml`). Durability is provided by "
        "the orphan branch `consensus-state-snapshots` (see "
        "`consensus_mcp/_snapshot_state.py`).\n\n"
        f"Imported: {imported_at_utc}\n"
        f"Source: {parent_agent_loop}\n"
        f"Standalone extraction commit: {extraction_commit}\n"
    )


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

    manifest: dict = _build_manifest_structure(
        parent_agent_loop=parent_agent_loop,
        target_root=target_root,
        parent_active=parent_active,
        parent_archive_passes=parent_archive_passes,
        imported_at_utc=_iso_utc_now(),
        extraction_commit=extraction_commit,
    )

    target_active = target_root / "active-iterations"
    target_archive = target_root / "archive-review-passes"

    if dry_run:
        # Manifest already pre-built via _build_manifest_structure; just return.
        return manifest

    # iter-0014 codex-rev-001/002 fix: byte-for-byte idempotency. If --force
    # AND both the source AND target trees match what we'd produce, skip the
    # rewrite. We verify BOTH sides - checking only source hashes (as the
    # initial fix did) misses the case where the target was modified or
    # corrupted, leaving stale content despite a success report.
    existing_manifest_path = target_root / "source-manifest.yaml"
    target_active_preview = target_root / "active-iterations"
    target_archive_preview = target_root / "archive-review-passes"
    if force and existing_manifest_path.exists():
        try:
            existing = yaml.safe_load(existing_manifest_path.read_text(encoding="utf-8"))
            existing_hashes = {
                e["target"]: (e.get("sha256_tree") or e.get("sha256_content"))
                for e in (existing or {}).get("entries", [])
            }
            # Compute what we'd write now from the SOURCE.
            source_hashes: dict[str, str | None] = {}
            for child in sorted(parent_active.iterdir()):
                if not child.is_dir():
                    continue
                tgt_rel = str((target_active_preview / child.name).relative_to(target_root.parent))
                source_hashes[tgt_rel] = _sha256_of_tree(child)
            if parent_archive_passes.is_dir():
                for f in sorted(parent_archive_passes.iterdir()):
                    if not f.is_file():
                        continue
                    tgt_rel = str((target_archive_preview / f.name).relative_to(target_root.parent))
                    source_hashes[tgt_rel] = _sha256_of_file(f)

            # codex-rev-001 round-1 fix: ALSO verify the TARGET on-disk content
            # matches the expected hashes. If target drifted (manual edit,
            # corruption), force a rewrite.
            target_hashes: dict[str, str | None] = {}
            for child in sorted(target_active_preview.iterdir()) if target_active_preview.is_dir() else []:
                if not child.is_dir():
                    continue
                tgt_rel = str(child.relative_to(target_root.parent))
                target_hashes[tgt_rel] = _sha256_of_tree(child)
            for f in sorted(target_archive_preview.iterdir()) if target_archive_preview.is_dir() else []:
                if not f.is_file():
                    continue
                tgt_rel = str(f.relative_to(target_root.parent))
                target_hashes[tgt_rel] = _sha256_of_file(f)

            # codex-rev-002 round-2 fix: enforce full target-tree inventory match.
            # The prior version only checked KNOWN files matched; it missed
            # the case of UNEXPECTED files (stray content under target_root).
            # If anything is in the target that we wouldn't write, force a
            # full rewrite - byte-for-byte idempotency means "target tree
            # IS what we'd produce", not just "target tree CONTAINS what we'd
            # produce".
            expected_inventory = {
                str((target_root / "README.md").relative_to(target_root.parent)),
                str((target_root / "source-manifest.yaml").relative_to(target_root.parent)),
            } | set(source_hashes.keys())

            actual_inventory: set[str] = set()
            for f in target_root.rglob("*"):
                if f.is_dir():
                    # Special-case the two top-level subdirs as "inventory
                    # roots" - we measure files OR top-level directory hashes
                    # to match source_hashes' shape.
                    rel = str(f.relative_to(target_root.parent))
                    if rel in expected_inventory:
                        actual_inventory.add(rel)
                    continue
                # Regular files: README + manifest at root, archive pass files,
                # OR files nested under an iteration-dir entry. Track only the
                # raw path when nothing covers it; track the covering parent
                # entry when one does (codex-rev-001 round-3 patch).
                rel = str(f.relative_to(target_root.parent))
                covered_by_expected_entry = False
                for expected in expected_inventory:
                    if rel.startswith(expected + os.sep) or rel.startswith(expected + "/"):
                        actual_inventory.add(expected)
                        covered_by_expected_entry = True
                        break
                if not covered_by_expected_entry:
                    actual_inventory.add(rel)

            # codex-rev-001 round-7 fix: render EXPECTED README + manifest from
            # SOURCE state (not from existing manifest), preserving only the
            # original imported_at_utc on the skip path. Prior version compared
            # existing manifest to itself (trivially true even with corrupt
            # metadata that preserved hashes). This version computes what
            # canonical bytes WOULD be and compares to disk.
            existing_imported_at = (
                (existing or {}).get("source", {}).get("imported_at_utc", "")
            )
            existing_extraction_commit = (
                (existing or {}).get("source", {}).get("extraction_commit_in_standalone", extraction_commit)
            )
            expected_readme = _render_readme(
                parent_agent_loop, existing_imported_at, existing_extraction_commit
            )
            expected_manifest_struct = _build_manifest_structure(
                parent_agent_loop=parent_agent_loop,
                target_root=target_root,
                parent_active=parent_active,
                parent_archive_passes=parent_archive_passes,
                imported_at_utc=existing_imported_at,
                extraction_commit=existing_extraction_commit,
            )
            expected_manifest_text = yaml.safe_dump(
                expected_manifest_struct, sort_keys=False, default_flow_style=False
            )
            try:
                actual_readme = (target_root / "README.md").read_text(encoding="utf-8")
            except OSError:
                actual_readme = ""
            try:
                actual_manifest_text = existing_manifest_path.read_text(encoding="utf-8")
            except OSError:
                actual_manifest_text = ""
            provenance_files_intact = (
                actual_readme == expected_readme
                and actual_manifest_text == expected_manifest_text
            )

            if (
                existing_hashes
                and existing_hashes == source_hashes
                and existing_hashes == target_hashes
                and expected_inventory == actual_inventory
                and provenance_files_intact
            ):
                # Source AND target (inventory, hashes, AND provenance bytes)
                # all match - true idempotent no-op. Preserve original
                # imported_at_utc by returning the existing manifest unchanged.
                return existing or {}
        except (yaml.YAMLError, OSError, KeyError):
            # Existing manifest corrupted or unreadable; fall through and
            # rewrite from scratch.
            pass

    # Actual import.
    if target_root.exists() and force:
        shutil.rmtree(target_root)
    target_root.mkdir(parents=True, exist_ok=True)
    target_active.mkdir(parents=True, exist_ok=True)
    target_archive.mkdir(parents=True, exist_ok=True)

    # Copy files only (manifest entries already pre-computed from source via
    # _build_manifest_structure - single source of truth so idempotency
    # check can compare against canonical expected output).
    for child in sorted(parent_active.iterdir()):
        if not child.is_dir():
            continue
        dst = target_active / child.name
        shutil.copytree(child, dst)

    if parent_archive_passes.is_dir():
        for f in sorted(parent_archive_passes.iterdir()):
            if not f.is_file():
                continue
            dst = target_archive / f.name
            shutil.copy2(f, dst)

    # Write manifest + README.
    (target_root / "source-manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    (target_root / "README.md").write_text(
        _render_readme(
            parent_agent_loop,
            manifest["source"]["imported_at_utc"],
            manifest["source"]["extraction_commit_in_standalone"],
        ),
        encoding="utf-8",
    )

    return manifest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus_mcp._import_parent_history",
        description="One-time import of parent project's iteration history into this repo's archive.",
    )
    p.add_argument("--parent", type=Path, default=DEFAULT_PARENT, required=DEFAULT_PARENT is None,
                   help="Path to parent project's agent-loop dir (required)")
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
