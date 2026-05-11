"""validate_disposition_index.py - Phase 0 disposition-index validator.

Per spec section 23 phase_0_deliverables, claude-rev-005, codex-rev-021,
claude-rev-024 (known_blocker_section_marking_rule), codex-rev-030
(section 24 index-only).

Audits the multi-agent-consensus-mcp spec MD against its own disposition
index (section 24) and frontmatter known_blockers. Detects the class of
bugs that have repeatedly slipped through manual review:
  - dead promoted_to references (codex-rev-002, codex-rev-015)
  - missing archived_at files (would-have-caught codex-rev-013)
  - archived_at files present in working tree but not git-tracked
  - known_blockers entries without inline section-body markers
    (codex-rev-025, codex-rev-026)
  - section 24 prose drift (codex-rev-001/012/020/030)

Output: structured report (YAML + JSON) suitable for v1.5 work-list extraction.

Usage:
  python consensus_mcp/validators/validate_disposition_index.py [--spec PATH] [--out PATH]

Exit codes:
  0 - validator ran cleanly; report written
  2 - validator could not run (spec missing, parse error, etc.)

NOTE: a non-empty findings list does NOT cause non-zero exit. The validator
always exits 0 when it ran successfully, regardless of findings count.
Findings are reported in the output artifact for downstream tooling to
decide what to do with them. This is intentional per Path C: empirical
report drives operator/v1.5 decisions, validator does not gate.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEC = REPO_ROOT / "docs" / "architecture" / "orchestration-spec.md"
DEFAULT_OUT = REPO_ROOT / "consensus-state" / "state" / "validate-disposition-index-report.yaml"


def _read_spec(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"spec not found: {path}")
    return path.read_text(encoding="utf-8")


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split YAML frontmatter from body. Returns (frontmatter_text, body_text)."""
    if not text.startswith("---\n"):
        raise SystemExit("spec lacks YAML frontmatter (no '---\\n' opener)")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise SystemExit("spec frontmatter not closed (no '---\\n' terminator)")
    return text[4:end], text[end + 5:]


def _parse_yaml(text: str) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit("pyyaml required (pip install pyyaml)")
    return yaml.safe_load(text) or {}


def _all_section_headings(body: str) -> dict[str, tuple[int, str]]:
    """Returns {heading_id: (line_no, raw_heading)} for every '## N.' or '### N.M' heading.
    heading_id format: 'section_N' for '## N.', 'section_N_M' for '### N.M'."""
    out: dict[str, tuple[int, str]] = {}
    for line_no, line in enumerate(body.splitlines(), start=1):
        m = re.match(r"^(#+)\s+(\d+)(?:\.(\d+))?\.\s+(.+)$", line)
        if m:
            major = m.group(2)
            minor = m.group(3)
            if minor:
                key = f"section_{major}_{minor}"
            else:
                key = f"section_{major}"
            out[key] = (line_no, line.strip())
    return out


def _extract_section_body(body: str, section_num: str) -> str:
    """Returns text between '## <section_num>.' heading and the next '## ' heading."""
    pattern = rf"^##\s+{re.escape(section_num)}\."
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(pattern, line):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^##\s+\d+\.", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


def _extract_yaml_blocks_from_section(section_text: str) -> list[dict]:
    """All ```yaml ... ``` fenced blocks, parsed."""
    blocks = re.findall(r"```yaml\s*\n(.*?)\n```", section_text, re.DOTALL)
    parsed: list[dict] = []
    for b in blocks:
        try:
            v = _parse_yaml(b)
            if v is not None:
                parsed.append(v)
        except Exception as e:
            parsed.append({"_yaml_parse_error": str(e), "_raw": b[:200]})
    return parsed


def _git_ls_files(path: Path) -> bool:
    """Returns True if `path` is git-tracked."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", str(path.relative_to(REPO_ROOT))],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _git_check_ignore(path: Path) -> bool:
    """Returns True if `path` is git-ignored (would not be tracked if added)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", str(path.relative_to(REPO_ROOT))],
            capture_output=True,
            text=True,
            check=False,
        )
        # exit 0 = matched a gitignore pattern (could be ignore or unignore)
        # We need to actually check whether the file would be tracked.
        # If exit 0 with no output, file is ignored. If output starts with !, unignored.
        if result.returncode != 0:
            return False
        out = result.stdout.strip()
        if not out:
            return True
        # Format: <path>:<line>:<pattern> <file>
        parts = out.split("\t")
        if len(parts) >= 1:
            rule = parts[0].split(":")[-1]
            return not rule.startswith("!")
        return True
    except Exception:
        return False


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_stdout(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception:
        return None


def _git_dirty_summary() -> dict:
    status = _git_stdout(["status", "--short", "--untracked-files=all"])
    if status is None:
        return {"available": False}
    entries = [line for line in status.splitlines() if line.strip()]
    limit = 60
    return {
        "available": True,
        "is_dirty": len(entries) > 0,
        "entry_count": len(entries),
        "entries": entries[:limit],
        "truncated": len(entries) > limit,
    }


def _dependency_version(dist_name: str) -> str | None:
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _build_provenance(spec_path: Path) -> dict:
    archive_index_rel = None
    disposition_ledger_rel = None
    try:
        spec_text = _read_spec(spec_path)
        fm_text, _body = _split_frontmatter(spec_text)
        frontmatter = _parse_yaml(fm_text)
        archive_index_rel = frontmatter.get("review_archive_index")
        disposition_ledger_rel = frontmatter.get("disposition_ledger")
    except Exception:
        pass

    archive_index_path = REPO_ROOT / archive_index_rel if archive_index_rel else None
    disposition_ledger_path = REPO_ROOT / disposition_ledger_rel if disposition_ledger_rel else None

    return {
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "command_line": sys.argv,
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "dependency_versions": {
            "PyYAML": _dependency_version("PyYAML"),
        },
        "git": {
            "head": _git_stdout(["rev-parse", "HEAD"]),
            "branch": _git_stdout(["branch", "--show-current"]),
            "dirty_summary": _git_dirty_summary(),
        },
        "inputs": {
            "spec_path": str(spec_path.relative_to(REPO_ROOT)) if spec_path.is_relative_to(REPO_ROOT) else str(spec_path),
            "spec_sha256": _sha256_file(spec_path),
            "archive_index_path": archive_index_rel,
            "archive_index_sha256": _sha256_file(archive_index_path) if archive_index_path else None,
            "disposition_ledger_path": disposition_ledger_rel,
            "disposition_ledger_sha256": _sha256_file(disposition_ledger_path) if disposition_ledger_path else None,
            "validator_script_path": "consensus_mcp/validators/validate_disposition_index.py",
            "validator_script_sha256": _sha256_file(Path(__file__).resolve()),
        },
    }


def validate_disposition_index(spec_path: Path) -> dict:
    """Run all validation passes; return structured report."""
    findings: list[dict] = []

    spec_text = _read_spec(spec_path)
    fm_text, body = _split_frontmatter(spec_text)
    frontmatter = _parse_yaml(fm_text)
    headings = _all_section_headings(body)

    # ---- Section 24 disposition extraction ----
    sec_24 = _extract_section_body(body, "24")
    if not sec_24:
        findings.append({
            "id": "MISSING_SECTION_24",
            "severity": "blocking",
            "claim": "spec has no section 24 (disposition index)",
        })
        return _wrap(findings, frontmatter, spec_path)

    sec_24_blocks = _extract_yaml_blocks_from_section(sec_24)

    resolved: list[dict] = []
    archived: list[dict] = []
    deferred: list[dict] = []
    pending_blocks: list[dict] = []
    status_counts: dict | None = None
    for blk in sec_24_blocks:
        if not isinstance(blk, dict):
            continue
        if "resolved" in blk and isinstance(blk["resolved"], list):
            resolved.extend(blk["resolved"])
        if "archived" in blk and isinstance(blk["archived"], list):
            archived.extend(blk["archived"])
        if "deferred" in blk and isinstance(blk["deferred"], list):
            deferred.extend(blk["deferred"])
        if "status_counts" in blk and isinstance(blk["status_counts"], dict):
            status_counts = blk["status_counts"]
        for k, v in blk.items():
            if k.startswith("pending_v") and isinstance(v, list):
                pending_blocks.extend(v)

    # v1.7.6 (operator finding 2026-05-08 high-2): compare narrative status_counts
    # against actual list lengths. Catches the deferred-count drift class.
    if isinstance(status_counts, dict):
        for key, actual_list in [("resolved", resolved), ("archived", archived), ("deferred", deferred)]:
            claimed = status_counts.get(key)
            if isinstance(claimed, int) and claimed != len(actual_list):
                findings.append({
                    "id": "STATUS_COUNT_LIST_LENGTH_DRIFT",
                    "severity": "high",
                    "field": f"status_counts.{key}",
                    "claimed": claimed,
                    "actual_list_length": len(actual_list),
                    "claim": f"section 24 status_counts.{key}={claimed} but actual {key} list has {len(actual_list)} entries",
                })

    # ---- Validator 1: archived_at files exist + git-tracked ----
    for entry in archived:
        if not isinstance(entry, dict):
            continue
        path_str = entry.get("archived_at")
        if not path_str:
            findings.append({
                "id": "ARCHIVED_ENTRY_MISSING_PATH",
                "severity": "high",
                "entry_id": entry.get("id", "<unknown>"),
                "claim": "archived entry has no archived_at path",
            })
            continue
        path = REPO_ROOT / path_str
        if not path.exists():
            findings.append({
                "id": "ARCHIVED_FILE_MISSING",
                "severity": "blocking",
                "entry_id": entry.get("id"),
                "path": path_str,
                "claim": "archived_at file does not exist on disk",
            })
            continue
        if not _git_ls_files(path):
            ignored = _git_check_ignore(path)
            findings.append({
                "id": "ARCHIVED_FILE_NOT_TRACKED",
                "severity": "high" if ignored else "medium",
                "entry_id": entry.get("id"),
                "path": path_str,
                "git_ignored": ignored,
                "claim": "archived_at file exists but is not git-tracked"
                + (" AND is gitignored" if ignored else " (not yet staged)"),
            })

    # ---- Validator 2: promoted_to section references resolve ----
    def _check_promoted_to(entry: dict, source_block: str):
        target = entry.get("promoted_to")
        if target is None:
            return
        if isinstance(target, str):
            targets = [target]
        elif isinstance(target, list):
            targets = list(target)
        else:
            return
        for t in targets:
            if not isinstance(t, str):
                continue
            # frontmatter, archive_index_yaml etc. are not section refs
            if t in {"frontmatter", "archive_index_yaml"} or "yaml" in t or "ledger" in t:
                continue
            if not t.startswith("section_"):
                continue
            if t not in headings:
                findings.append({
                    "id": "PROMOTED_TO_DEAD_REFERENCE",
                    "severity": "high",
                    "entry_id": entry.get("id"),
                    "promoted_to": t,
                    "source_block": source_block,
                    "claim": f"promoted_to references nonexistent section: {t!r}",
                    "note": "claude-rev-005 / codex-rev-021 finding-class",
                })

    for entry in resolved:
        if isinstance(entry, dict):
            _check_promoted_to(entry, "resolved")
    for entry in pending_blocks:
        if isinstance(entry, dict):
            _check_promoted_to(entry, "pending_v1_5")

    # ---- Validator 3: known_blockers each have inline section-body markers (claude-rev-024) ----
    known_blockers = frontmatter.get("known_blockers") or {}
    flat_blockers: list[tuple[str, str]] = []  # (bucket, blocker_id)
    if isinstance(known_blockers, dict):
        for bucket, items in known_blockers.items():
            if isinstance(items, list):
                for item in items:
                    flat_blockers.append((bucket, str(item)))

    # Map blocker -> spec section it affects (heuristic: id mentions section number, OR look up promoted_to)
    section_disable_markers = [
        "TEMPORARILY_DISABLED",
        "DISABLED_PENDING",
        "BROKEN_PENDING",
        "DEPRECATED_WHILE_DISABLED",
        "_deprecated_while_disabled",
        "ENGAGED_WHILE_CONTRACT_BLOCKED",
        "DO_NOT_RUN",
    ]

    # For each known_blocker, find its disposition entry to learn target section
    blocker_to_target: dict[str, list[str]] = {}
    for entry in resolved + pending_blocks:
        if not isinstance(entry, dict):
            continue
        eid = entry.get("id")
        if not eid:
            continue
        target = entry.get("promoted_to") or entry.get("proposed_target")
        if target is None:
            continue
        if isinstance(target, str):
            target = [target]
        if isinstance(target, list):
            blocker_to_target[eid] = [t for t in target if isinstance(t, str)]

    for bucket, blocker_id in flat_blockers:
        # Strip parenthetical suffix like "(mitigated pass-12 disable)"
        clean_id = re.sub(r"\s*\(.*\)$", "", blocker_id).strip()
        targets = blocker_to_target.get(clean_id, [])
        section_targets = [t for t in targets if t.startswith("section_")]
        if not section_targets:
            continue  # blocker doesn't reference an active section
        for sec_id in section_targets:
            sec_num = sec_id.replace("section_", "").replace("_", ".")
            sec_body = _extract_section_body(body, sec_num)
            if not sec_body:
                continue
            has_marker = any(marker in sec_body for marker in section_disable_markers)
            if not has_marker:
                findings.append({
                    "id": "KNOWN_BLOCKER_SECTION_LACKS_DISABLE_MARKER",
                    "severity": "high",
                    "blocker_id": clean_id,
                    "bucket": bucket,
                    "target_section": sec_id,
                    "claim": f"known_blocker {clean_id!r} affects {sec_id} but section body has no inline DISABLED marker",
                    "note": "claude-rev-024 known_blocker_section_marking_rule",
                })

    # ---- Validator 4: section 24 index-only invariant (codex-rev-030 / claude-rev-027) ----
    # Heuristic: count fields that look like prose (rationale, summary, note, claim, proposed_resolution)
    prose_field_names = {"rationale", "summary", "note", "claim", "proposed_resolution",
                         "interpretation", "verdict", "load_bearing_question"}
    prose_count = 0
    for blk in sec_24_blocks:
        if not isinstance(blk, dict):
            continue
        prose_count += _count_prose_fields(blk, prose_field_names)
    if prose_count > 0:
        findings.append({
            "id": "SECTION_24_INDEX_ONLY_VIOLATION",
            "severity": "medium",
            "prose_field_count": prose_count,
            "claim": f"section 24 declared index-only but contains {prose_count} prose field(s) "
                     f"({', '.join(sorted(prose_field_names))})",
            "note": "codex-rev-001/012/020/030; claude-rev-027 structural contradiction",
        })

    # ---- Validator 5: phase 0 deliverable scripts not gitignored (codex-rev-018 followup) ----
    sec_23 = _extract_section_body(body, "23")
    sec_23_text = sec_23
    declared_scripts = re.findall(r"consensus_mcp/validators/[\w_]+\.py", sec_23_text)
    for script_rel in set(declared_scripts):
        script_path = REPO_ROOT / script_rel
        ignored = _git_check_ignore(script_path)
        if ignored:
            findings.append({
                "id": "PHASE_0_SCRIPT_GITIGNORED",
                "severity": "high",
                "path": script_rel,
                "claim": f"phase 0 deliverable {script_rel} would be gitignored",
                "note": "codex-rev-018 finding-class",
            })

    # ---- Validator 6: archive index pass list matches section 24 archived list ----
    # Honor frontmatter review_archive_index. If the field is explicitly null/false/empty,
    # skip validator 6 entirely (test fixtures use this). If the field is missing,
    # fall back to canonical path (real spec runs always set the field, so this fallback
    # only triggers for legacy / partial specs).
    archive_index_path = None
    if "review_archive_index" in frontmatter:
        archive_index_rel = frontmatter["review_archive_index"]
        if archive_index_rel:  # non-empty string => use it
            archive_index_path = REPO_ROOT / archive_index_rel
        # else explicitly null/false/empty => skip
    else:
        # field absent: fallback to canonical
        canonical = REPO_ROOT / "consensus-state" / "archive" / "review-passes" / "index.yaml"
        if canonical.exists():
            archive_index_path = canonical
    archive_index_pass_ids: set[str] = set()
    if archive_index_path is not None and archive_index_path.exists():
        try:
            ai = _parse_yaml(archive_index_path.read_text(encoding="utf-8"))
            for p in ai.get("passes", []) or []:
                if isinstance(p, dict) and "id" in p:
                    archive_index_pass_ids.add(p["id"])
        except Exception as e:
            findings.append({
                "id": "ARCHIVE_INDEX_PARSE_ERROR",
                "severity": "high",
                "path": "consensus-state/archive/review-passes/index.yaml",
                "claim": f"archive index parse error: {e}",
            })

    spec_archived_ids: set[str] = {e.get("id") for e in archived if isinstance(e, dict) and e.get("id")}

    only_in_index = archive_index_pass_ids - spec_archived_ids
    only_in_spec = spec_archived_ids - archive_index_pass_ids
    if archive_index_path is None:
        # validator 6 skipped because no archive index path configured
        only_in_index = set()
        only_in_spec = set()
    if only_in_index:
        findings.append({
            "id": "ARCHIVE_INDEX_HAS_PASSES_NOT_IN_SPEC_24",
            "severity": "medium",
            "passes": sorted(only_in_index),
            "claim": "archive index lists passes that section 24 archived block does not",
        })
    if only_in_spec:
        findings.append({
            "id": "SPEC_24_HAS_PASSES_NOT_IN_ARCHIVE_INDEX",
            "severity": "medium",
            "passes": sorted(only_in_spec),
            "claim": "section 24 archived block lists passes that archive index does not",
        })

    return _wrap(findings, frontmatter, spec_path, headings_count=len(headings),
                 archived_count=len(archived), resolved_count=len(resolved),
                 pending_count=len(pending_blocks), deferred_count=len(deferred))


def _count_prose_fields(obj, prose_field_names: set[str]) -> int:
    count = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in prose_field_names and isinstance(v, str) and len(v) > 0:
                count += 1
            count += _count_prose_fields(v, prose_field_names)
    elif isinstance(obj, list):
        for item in obj:
            count += _count_prose_fields(item, prose_field_names)
    return count


def _wrap(findings: list[dict], frontmatter: dict, spec_path: Path, **stats) -> dict:
    severity_counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    return {
        "schema_version": 1,
        "validator": "validate_disposition_index.py",
        "validator_version": "0.2.0-v1.6-provenance",
        "spec_version_validated": frontmatter.get("status", "<unknown>"),
        "active_contract_readiness": frontmatter.get("active_contract_readiness", "<not-set>"),
        "provenance": _build_provenance(spec_path),
        "stats": {
            "total_findings": len(findings),
            "severity_counts": severity_counts,
            **stats,
        },
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--json", action="store_true", help="emit JSON to stdout in addition to YAML to --out")
    args = p.parse_args(argv)

    report = validate_disposition_index(args.spec)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        args.out.write_text(yaml.safe_dump(report, sort_keys=False, default_flow_style=False), encoding="utf-8")
    except ImportError:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        sev = report["stats"]["severity_counts"]
        print(f"validate_disposition_index: {report['stats']['total_findings']} finding(s) "
              f"({sev}) -> {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
