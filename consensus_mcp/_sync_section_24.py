"""section-24 archive-sync helper. Closes the recurring failure class codified
as iter-0013 prerequisite #2 (per codex 2026-05-10 v2 guardrail #2).

Each closed iteration produces a sealed codex pass that gets entered in
consensus-state/archive/review-passes/index.yaml. The spec md (section 24) mirrors
that index. The mirror is currently MANUAL - when it drifts, the disposition
validator flags ARCHIVE_INDEX_HAS_PASSES_NOT_IN_SPEC_24 and the MCP server
refuses to boot in smoke (4 manual fixes required this session: iter-0009/0010/
0011/0012).

This helper detects drift and optionally fixes it.

USAGE
-----

  # Detect drift (exit 1 if drift, 0 if synced)
  python -m consensus_mcp._sync_section_24

  # Auto-fix drift (exit 0 on success, 2 on apply failure)
  python -m consensus_mcp._sync_section_24 --apply

DESIGN
------

Source of truth: archive/review-passes/index.yaml. Section 24 of the spec
md is the mirror. The helper extracts (id, archived_at) tuples from each
and computes the diff. --apply appends missing entries before the closing
``` of the archived: block in the spec md, and bumps the archived count
in status_counts.archived.

The helper does NOT remove or reorder entries; only appends missing ones.
Operator can hand-edit the spec md for restructuring; auto-sync only catches
drift in the additive direction (which is the empirically-recurring class).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


def _resolve_repo_root() -> Path:
    """M1 (consult iteration-m1-hardening-design-4d7d2469) Q2 shim over the
    ONE blessed resolver (_paths.resolve_repo_root): env override(s) >
    cwd-ancestor containment-marker walk > RepoRootError. The old
    `Path(__file__).resolve().parent.parent` fallback anchored an installed
    wheel/pipx run at site-packages; this maintenance CLI is run from the
    source repo (whose `consensus-state/` dir is a walk marker) or with the
    env override the release gate already sets."""
    from consensus_mcp._paths import resolve_repo_root
    return resolve_repo_root()


def _archive_index_path() -> Path:
    return _resolve_repo_root() / "consensus-state" / "archive" / "review-passes" / "index.yaml"


def _spec_md_path() -> Path:
    return _resolve_repo_root() / "docs" / "architecture" / "orchestration-spec.md"


def __getattr__(name: str):
    """M1 (consult iteration-m1-hardening-design-4d7d2469) Q2: lazy module
    attributes (PEP 562, pattern: tools/patch_stage_and_dry_run.py) replacing
    the import-time REPO_ROOT/ARCHIVE_INDEX_PATH/SPEC_MD_PATH captures so
    resolution is per-call and env changes after import take effect."""
    if name == "REPO_ROOT":
        return _resolve_repo_root()
    if name == "ARCHIVE_INDEX_PATH":
        return _archive_index_path()
    if name == "SPEC_MD_PATH":
        return _spec_md_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _archive_pass_ids() -> list[str]:
    """Return list of pass ids from consensus-state/archive/review-passes/index.yaml."""
    data = yaml.safe_load(_archive_index_path().read_text(encoding="utf-8")) or {}
    return [p.get("id", "") for p in data.get("passes", []) if p.get("id")]


def _section_24_pass_ids(spec_text: str) -> list[str]:
    """Extract pass ids from the section 24 archived block.

    The block sits between ``archived:`` (in a ```yaml fence) and the next
    closing ``` of the same fence. Each entry is shaped:
      - {id: <pass-id>, archived_at: "<path>"}
    or:
      - {id: <pass-id>, archived_at: "<path>", ...other fields...}

    We extract the id field directly via a regex that doesn't require parsing
    the whole markdown - section 24 has comment lines, multi-line wrapping,
    and id-only entries that resist round-trip yaml parsing.
    """
    # Find the "archived:" block in section 24 (not the same as status_counts.archived).
    # status_counts.archived is a number; the listing is a yaml list following
    # `archived:\n  - {id: ...}` patterns. We look for the FIRST `archived:` that
    # is followed by id entries on subsequent lines.
    ids: list[str] = []
    in_archived_list = False
    fence_after_archived: int | None = None
    lines = spec_text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not in_archived_list:
            if stripped == "archived:" and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("- {id:"):
                in_archived_list = True
            continue
        if stripped.startswith("```"):
            fence_after_archived = i
            break
        m = re.search(r"\{id:\s*([A-Za-z0-9_\-\.]+)", line)
        if m:
            ids.append(m.group(1))
    return ids


def _archived_count_in_spec(spec_text: str) -> int | None:
    """Read status_counts.archived (the integer count) from the spec md.

    Multiple `status_counts:` and `archived:` strings exist in narrative prose.
    The structural status_counts block lives in the index-section yaml fence
    and starts with `status_counts:` at column 0. Anchor on that.
    """
    block_match = re.search(
        r"^status_counts:.*?^\s*archived:\s*(\d+)",
        spec_text,
        re.MULTILINE | re.DOTALL,
    )
    return int(block_match.group(1)) if block_match else None


def detect_drift() -> dict:
    """Return drift report dict.

    {
      "in_index_only": [<pass ids missing from section 24>],
      "in_section_24_only": [<pass ids in section 24 but not index>],
      "synced": <bool>,
      "archive_index_pass_count": <int>,
      "section_24_pass_count": <int>,
      "spec_archived_count": <int from status_counts.archived>,
    }
    """
    index_ids = _archive_pass_ids()
    spec_text = _spec_md_path().read_text(encoding="utf-8")
    section_ids = _section_24_pass_ids(spec_text)
    spec_count = _archived_count_in_spec(spec_text)
    in_index_only = [i for i in index_ids if i not in section_ids]
    in_section_24_only = [i for i in section_ids if i not in index_ids]
    return {
        "synced": not in_index_only and not in_section_24_only,
        "in_index_only": in_index_only,
        "in_section_24_only": in_section_24_only,
        "archive_index_pass_count": len(index_ids),
        "section_24_pass_count": len(section_ids),
        "spec_archived_count": spec_count,
    }


def _archive_path_for(pass_id: str) -> str | None:
    """Look up archive path from the index for a given pass id.

    Index entries vary in field name across legacy / real-codex shapes:
      - legacy: `archived_at: <path>`
      - real-codex (post-iter-0009): `path: <path>`
    Try both.
    """
    data = yaml.safe_load(_archive_index_path().read_text(encoding="utf-8")) or {}
    for p in data.get("passes", []):
        if p.get("id") == pass_id:
            return p.get("archived_at") or p.get("path")
    return None


def apply_drift_fix(report: dict) -> dict:
    """Append missing index-only entries to section 24, bump status_counts.archived.

    Returns dict with applied=true and new counts, OR error.

    Section 24 isn't restructured; only additive: append before closing ```.
    """
    if not report["in_index_only"]:
        return {"applied": False, "reason": "no in_index_only drift; nothing to do"}

    spec_text = _spec_md_path().read_text(encoding="utf-8")
    lines = spec_text.splitlines(keepends=False)

    # Locate end of archived: block (closing fence after the first `archived:` list).
    in_archived_list = False
    fence_after_archived: int | None = None
    for i, line in enumerate(lines):
        if not in_archived_list:
            if (
                line.strip() == "archived:"
                and i + 1 < len(lines)
                and lines[i + 1].lstrip().startswith("- {id:")
            ):
                in_archived_list = True
            continue
        if line.strip().startswith("```"):
            fence_after_archived = i
            break
    if fence_after_archived is None:
        return {"applied": False, "error": "could not locate archived: block closing fence in spec md"}

    new_entries: list[str] = []
    for pass_id in report["in_index_only"]:
        archive_path = _archive_path_for(pass_id)
        if not archive_path:
            return {"applied": False, "error": f"pass {pass_id} missing archived_at in index"}
        new_entries.append(
            f'  # auto-synced by _sync_section_24.py (added missing pass-id entry)'
        )
        new_entries.append(
            f'  - {{id: {pass_id}, archived_at: "{archive_path}"}}'
        )

    # Insert new entries BEFORE the closing fence line (fence_after_archived).
    inserted = lines[:fence_after_archived] + new_entries + lines[fence_after_archived:]

    # Bump status_counts.archived. Anchor to `status_counts:` so we don't bump
    # a stray `archived: N` reference in narrative prose. We track when we've
    # seen `status_counts:` and only modify the FIRST `archived:` after it.
    out_lines: list[str] = []
    in_status_counts = False
    bumped = False
    for line in inserted:
        if not bumped:
            if re.match(r"^\s*status_counts:\s*$", line):
                in_status_counts = True
            elif in_status_counts:
                m = re.match(r"^(\s*archived:\s*)(\d+)(.*)$", line)
                if m:
                    new_count = int(m.group(2)) + len(report["in_index_only"])
                    line = f"{m.group(1)}{new_count}{m.group(3)}"
                    bumped = True
        out_lines.append(line)

    _spec_md_path().write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    new_report = detect_drift()
    return {
        "applied": True,
        "added_pass_ids": report["in_index_only"],
        "post_apply_synced": new_report["synced"],
        "post_apply_section_24_pass_count": new_report["section_24_pass_count"],
        "post_apply_spec_archived_count": new_report["spec_archived_count"],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus_mcp._sync_section_24",
        description="Detect/fix archive-index <-> spec-md section-24 drift.",
    )
    p.add_argument("--apply", action="store_true", help="auto-apply drift fix (append missing entries + bump count)")
    ns = p.parse_args(argv)

    report = detect_drift()
    if report["synced"]:
        print(json.dumps({"synced": True, **report}, indent=2))
        return 0

    if not ns.apply:
        # Drift detected; report and exit 1.
        print(json.dumps({"synced": False, **report}, indent=2))
        return 1

    fix = apply_drift_fix(report)
    print(json.dumps({"synced": False, "drift": report, "fix": fix}, indent=2))
    return 0 if fix.get("post_apply_synced") else 2


if __name__ == "__main__":
    sys.exit(main())
