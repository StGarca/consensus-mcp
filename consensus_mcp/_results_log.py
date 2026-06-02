"""v1.19.0 result logging - author a per-iteration results record + upsert the
durable JSONL ledger.

See docs/design-consults/v1.19.0-result-logging.md (converged design) and
schemas/results-v1.schema.json (the shared contract - the authored record MUST
validate against it).

Storage (per the design):
  - Co-located detail: consensus-state/active/<iteration_id>/iteration-results.yaml
    (human-readable, gitignored via active/iteration-*/).
  - Authoritative ledger: consensus-state/state/results-v1.jsonl (one current
    snapshot per iteration_id, gitignored). UPSERT-by-iteration_id, atomic
    tmp+os.replace, schema-validated BEFORE replace.

Record building derives findings from the iteration's sealed review passes
(claude/codex/gemini-review.yaml), the converged-plan.yaml when present, and the
audit events. Findings are deduped by id within an iteration. Disposition:

  validated_fixed   - finding id linked to an apply_step_landed.finding_ids,
                      or a closure finding_disposition says validated_fixed
  dismissed_refuted - a closure finding_disposition says so (carries evidence_ref)
  deferred / open    - otherwise (deferred by default; open is the schema-allowed
                      synonym for a finding raised but not acted on)

`counts` aggregates by_severity, validated, dismissed, deferred, fixes_applied.

This module is read-mostly: it reads iteration artifacts and writes ONLY the two
result files. The audit close hook calls it under the existing single-writer
lock discipline (one orchestrator per iteration), so no separate lock is taken
here.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import yaml

from consensus_mcp._paths import state_root

SCHEMA_VERSION = 1

# Sealed review-pass files, mapped to their reviewer family. Mirrors
# consensus_get_iteration_outcome._KNOWN_CONTRIB_FILES.
#
# v1.20.0: host-peer-review.yaml is a SAME-FAMILY supplementary blind
# SWE-reviewer. Its `family` is taken from the sealed actor.model_family (the
# host family, e.g. claude) at read time rather than hardcoded here, because the
# host family is host-dependent; the static default below is None.
_REVIEW_FILES: dict[str, str | None] = {
    "claude-review.yaml": "claude",
    "claude-proposal.yaml": "claude",
    "codex-review.yaml": "codex",
    "gemini-review.yaml": "gemini",
    "host-peer-review.yaml": None,
}


def _is_supplementary(parsed: dict) -> bool:
    """A sealed pass is a supplementary (host_peer / same-family) reviewer when
    it is explicitly weight=supplementary OR gate_eligible=false. Content-driven
    so a host_peer enabled under any profile name is detected, not just the
    built-in filename."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("weight") == "supplementary":
        return True
    if parsed.get("gate_eligible") is False:
        return True
    prov = parsed.get("dispatch_provenance")
    if isinstance(prov, dict):
        if prov.get("weight") == "supplementary" or prov.get("gate_eligible") is False:
            return True
        if prov.get("adapter") == "host_peer":
            return True
    return False


def _family_for_pass(parsed: dict, static_family: str | None) -> str | None:
    """Resolve a pass's reviewer family: prefer the sealed actor.model_family
    (authoritative - host_peer's family is host-dependent), else the static
    per-file default."""
    if isinstance(parsed, dict):
        actor = parsed.get("actor")
        if isinstance(actor, dict) and actor.get("model_family"):
            return actor.get("model_family")
    return static_family

_VALID_SEVERITIES = {"low", "medium", "high", "blocking", "critical"}
_DEFAULT_SEVERITY = "medium"


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _schema_path() -> Path:
    return Path(__file__).resolve().parent / "schemas" / "results-v1.schema.json"


def _load_schema() -> dict:
    return json.loads(_schema_path().read_text(encoding="utf-8"))


def _safe_load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalize_severity(value) -> str:
    if isinstance(value, str) and value.lower() in _VALID_SEVERITIES:
        return value.lower()
    return _DEFAULT_SEVERITY


# ---------------------------------------------------------------------------
# Collection: sealed passes -> raw findings (with provenance)
# ---------------------------------------------------------------------------


def _collect_findings_from_passes(iter_dir: Path) -> dict[str, dict]:
    """Read sealed review passes; return {finding_id: finding-record} deduped by id.

    The FIRST pass to mention an id wins for provenance (source_reviewer /
    source_pass_id); later mentions of the same id are merged only to fill a
    missing severity. Findings without an id are skipped (cannot be tracked).
    """
    out: dict[str, dict] = {}
    for fname, family in _REVIEW_FILES.items():
        fpath = iter_dir / fname
        if not fpath.exists():
            continue
        parsed = _safe_load_yaml(fpath)
        if not isinstance(parsed, dict):
            continue
        reviewer = parsed.get("reviewer_id")
        if reviewer is None:
            actor = parsed.get("actor")
            if isinstance(actor, dict):
                reviewer = actor.get("id")
        pass_id = parsed.get("pass_id")
        resolved_family = _family_for_pass(parsed, family)
        supplementary = _is_supplementary(parsed)
        findings = parsed.get("findings")
        if not isinstance(findings, list):
            continue
        for raw in findings:
            if not isinstance(raw, dict):
                continue
            fid = raw.get("id")
            if not isinstance(fid, str) or not fid:
                continue
            severity = _normalize_severity(raw.get("severity"))
            if fid not in out:
                out[fid] = {
                    "id": fid,
                    "severity": severity,
                    "source_reviewer": reviewer,
                    "source_pass_id": pass_id,
                    "_family": resolved_family,
                    "_supplementary": supplementary,
                }
            else:
                # Dedup: keep first provenance, but upgrade a defaulted severity
                # if a later pass declares a real one.
                if out[fid]["severity"] == _DEFAULT_SEVERITY and severity != _DEFAULT_SEVERITY:
                    out[fid]["severity"] = severity
    return out


def _collect_cited_pass_ids(iter_dir: Path) -> list[str]:
    """Pull cited_pass_ids from converged-plan.yaml when present (provenance)."""
    cp_path = iter_dir / "converged-plan.yaml"
    if not cp_path.exists():
        return []
    parsed = _safe_load_yaml(cp_path)
    if not isinstance(parsed, dict):
        return []
    cited = parsed.get("cited_pass_ids")
    if isinstance(cited, list):
        return [str(c) for c in cited]
    return []


def _convergence_from_plan(iter_dir: Path) -> dict | None:
    cp_path = iter_dir / "converged-plan.yaml"
    if not cp_path.exists():
        return None
    parsed = _safe_load_yaml(cp_path)
    if not isinstance(parsed, dict):
        return None
    conv: dict = {"converged": True}
    gsb = parsed.get("goal_satisfied_by")
    if isinstance(gsb, list):
        conv["goal_satisfied_by"] = [str(x) for x in gsb]
    cited = _collect_cited_pass_ids(iter_dir)
    if cited and "goal_satisfied_by" not in conv:
        conv["goal_satisfied_by"] = cited
    rule = parsed.get("convergence_rule") or parsed.get("rule")
    if isinstance(rule, str):
        conv["rule"] = rule
    return conv


def _reviewers_from_passes(findings_by_id: dict[str, dict], iter_dir: Path) -> list[dict]:
    """Distinct reviewers (name + family [+ supplementary]) across sealed passes.

    v1.20.0: a same-family supplementary host_peer reviewer is tagged
    `supplementary: true` so the scorecard can separate its (same-family)
    findings from independent cross-family review. The flag is only emitted when
    true, keeping the record shape unchanged for the existing cross-family
    reviewers.
    """
    seen: dict[str, dict] = {}
    for fname, family in _REVIEW_FILES.items():
        fpath = iter_dir / fname
        if not fpath.exists():
            continue
        parsed = _safe_load_yaml(fpath)
        if not isinstance(parsed, dict):
            continue
        name = parsed.get("reviewer_id")
        if name is None:
            actor = parsed.get("actor")
            if isinstance(actor, dict):
                name = actor.get("id")
        if not isinstance(name, str) or not name:
            continue
        if name not in seen:
            seen[name] = {
                "family": _family_for_pass(parsed, family),
                "supplementary": _is_supplementary(parsed),
            }
    reviewers: list[dict] = []
    for n, meta in seen.items():
        entry: dict = {"name": n, "family": meta["family"]}
        if meta["supplementary"]:
            entry["supplementary"] = True
        reviewers.append(entry)
    return reviewers


# ---------------------------------------------------------------------------
# Audit events -> fixes (apply_step_landed.finding_ids) + provenance
# ---------------------------------------------------------------------------


def _load_audit_log(iter_dir: Path) -> list:
    audit_path = iter_dir / "independence-audit.yaml"
    if not audit_path.exists():
        return []
    parsed = _safe_load_yaml(audit_path)
    if not isinstance(parsed, dict):
        return []
    log = parsed.get("audit_log")
    return log if isinstance(log, list) else []


def _fixes_from_audit(audit_log: list) -> dict[str, dict]:
    """Map finding_id -> {patch_id, files} from apply_step_landed events.

    Reads the additive optional fields: finding_ids, files_touched, fix_summary
    (plus the legacy nested last_mutation.files_touched). An apply event with no
    finding_ids links to nothing (legacy/back-compat: those finding dispositions
    stay deferred unless a closure disposition says otherwise).
    """
    fixes: dict[str, dict] = {}
    for e in audit_log or []:
        if not isinstance(e, dict) or e.get("event") != "apply_step_landed":
            continue
        finding_ids = e.get("finding_ids")
        if not isinstance(finding_ids, list) or not finding_ids:
            continue
        patch_id = e.get("patch_id")
        files = e.get("files_touched")
        if not isinstance(files, list):
            files = e.get("files_modified")
        if not isinstance(files, list):
            nested = e.get("last_mutation")
            if isinstance(nested, dict) and isinstance(nested.get("files_touched"), list):
                files = nested["files_touched"]
        files = [str(p) for p in files] if isinstance(files, list) else []
        for fid in finding_ids:
            if not isinstance(fid, str) or not fid:
                continue
            fixes[fid] = {"patch_id": patch_id, "files": files}
    return fixes


def _last_apply_event_id(audit_log: list) -> str | None:
    last = None
    for e in audit_log or []:
        if isinstance(e, dict) and e.get("event") == "apply_step_landed":
            last = e.get("event_id") or last
    return last


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------


def build_results_record(
    iter_dir: Path,
    *,
    finding_dispositions: list[dict] | None = None,
    source_audit_event_id: str | None = None,
    source_audit_yaml_post_sha256: str | None = None,
) -> dict:
    """Author the per-iteration results record (schema-v1 shape).

    Args:
      iter_dir: consensus-state/active/<iteration_id> path.
      finding_dispositions: optional closure-time dispositions
        [{id, disposition, evidence_ref?}] (from iteration_closed). Overrides
        the apply-derived disposition for matching ids.
      source_audit_event_id / source_audit_yaml_post_sha256: provenance link to
        the closing audit event (filled by the close hook).
    """
    iter_dir = Path(iter_dir)
    iteration_id = iter_dir.name

    findings_by_id = _collect_findings_from_passes(iter_dir)
    audit_log = _load_audit_log(iter_dir)
    fixes = _fixes_from_audit(audit_log)

    # Index explicit closure dispositions by finding id.
    disp_by_id: dict[str, dict] = {}
    for d in finding_dispositions or []:
        if isinstance(d, dict) and isinstance(d.get("id"), str):
            disp_by_id[d["id"]] = d

    # A closure disposition can reference a finding not present in any sealed
    # pass (e.g. a fix author logging a fix for an externally-tracked finding).
    # Surface it so counts reflect reality.
    for fid in disp_by_id:
        if fid not in findings_by_id:
            findings_by_id[fid] = {
                "id": fid,
                "severity": _DEFAULT_SEVERITY,
                "source_reviewer": None,
                "source_pass_id": None,
                "_family": None,
            }

    findings: list[dict] = []
    counts_validated = 0
    counts_dismissed = 0
    counts_deferred = 0
    counts_open = 0
    fixes_applied = 0
    by_severity: dict[str, int] = {}

    for fid in sorted(findings_by_id):
        raw = findings_by_id[fid]
        severity = raw["severity"]
        by_severity[severity] = by_severity.get(severity, 0) + 1

        record_finding: dict = {
            "id": fid,
            "severity": severity,
            "source_reviewer": raw.get("source_reviewer"),
            "source_pass_id": raw.get("source_pass_id"),
        }

        explicit = disp_by_id.get(fid)
        fix = fixes.get(fid)

        disposition: str
        evidence_ref = None
        if explicit is not None:
            disposition = explicit.get("disposition") or "deferred"
            evidence_ref = explicit.get("evidence_ref")
            if disposition not in {"validated_fixed", "dismissed_refuted", "deferred", "open"}:
                disposition = "deferred"
        elif fix is not None:
            disposition = "validated_fixed"
        else:
            disposition = "deferred"

        # validated_fixed should carry the fix block when we have one.
        if disposition == "validated_fixed":
            if fix is not None:
                record_finding["fix"] = {
                    "patch_id": fix.get("patch_id"),
                    "files": fix.get("files", []),
                }
                fixes_applied += 1
            counts_validated += 1
        elif disposition == "dismissed_refuted":
            counts_dismissed += 1
            if evidence_ref:
                record_finding["evidence_ref"] = evidence_ref
        elif disposition == "open":
            counts_open += 1
        else:
            counts_deferred += 1

        if evidence_ref and "evidence_ref" not in record_finding:
            record_finding["evidence_ref"] = evidence_ref

        record_finding["disposition"] = disposition
        findings.append(record_finding)

    counts: dict = {
        "by_severity": by_severity,
        "validated": counts_validated,
        "dismissed": counts_dismissed,
        "deferred": counts_deferred,
        "fixes_applied": fixes_applied,
    }
    if counts_open:
        counts["open"] = counts_open

    record: dict = {
        "consensus_results_schema_version": SCHEMA_VERSION,
        "iteration_id": iteration_id,
        "record_updated_utc": _now_utc(),
        "findings": findings,
        "counts": counts,
        "confidence": "authoritative",
        "backfilled": False,
    }

    convergence = _convergence_from_plan(iter_dir)
    if convergence is not None:
        record["convergence"] = convergence

    reviewers = _reviewers_from_passes(findings_by_id, iter_dir)
    if reviewers:
        record["reviewers"] = reviewers

    if source_audit_event_id is not None:
        record["source_audit_event_id"] = source_audit_event_id
    else:
        last_apply = _last_apply_event_id(audit_log)
        if last_apply is not None:
            record["source_audit_event_id"] = last_apply

    if source_audit_yaml_post_sha256 is not None:
        record["source_audit_yaml_post_sha256"] = source_audit_yaml_post_sha256

    return record


# ---------------------------------------------------------------------------
# JSONL upsert (atomic, schema-validated before replace)
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    return state_root() / "state" / "results-v1.jsonl"


def upsert_jsonl(record: dict, ledger_path: Path | None = None) -> Path:
    """UPSERT the single snapshot for record['iteration_id'] into the JSONL ledger.

    Reads the existing JSONL, replaces the line whose iteration_id matches (or
    appends), schema-validates the record BEFORE replacing, and writes atomically
    via tmp + os.replace.
    """
    jsonschema.validate(instance=record, schema=_load_schema())

    path = Path(ledger_path) if ledger_path is not None else _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    iteration_id = record["iteration_id"]

    lines: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            lines.append(line)

    new_line = json.dumps(record, sort_keys=True, ensure_ascii=False)
    replaced = False
    out_lines: list[str] = []
    for line in lines:
        try:
            existing = json.loads(line)
        except json.JSONDecodeError:
            # Preserve unparseable lines untouched (don't lose data).
            out_lines.append(line)
            continue
        if isinstance(existing, dict) and existing.get("iteration_id") == iteration_id:
            if not replaced:
                out_lines.append(new_line)
                replaced = True
            # Drop any duplicate snapshots for the same iteration_id.
        else:
            out_lines.append(line)
    if not replaced:
        out_lines.append(new_line)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def write_yaml(record: dict, iter_dir: Path) -> Path:
    """Write the human-readable co-located iteration-results.yaml."""
    yaml_path = Path(iter_dir) / "iteration-results.yaml"
    yaml_path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return yaml_path


def write_results_record(
    iter_dir: Path,
    *,
    finding_dispositions: list[dict] | None = None,
    source_audit_event_id: str | None = None,
    source_audit_yaml_post_sha256: str | None = None,
) -> dict:
    """Author + persist the per-iteration results record.

    Builds the record, schema-validates + UPSERTs into results-v1.jsonl, and
    writes the co-located iteration-results.yaml. Returns the record.
    """
    record = build_results_record(
        iter_dir,
        finding_dispositions=finding_dispositions,
        source_audit_event_id=source_audit_event_id,
        source_audit_yaml_post_sha256=source_audit_yaml_post_sha256,
    )
    upsert_jsonl(record)
    write_yaml(record, iter_dir)
    return record
