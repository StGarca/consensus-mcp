"""Trace-to-harness proposal tool for safe Loop-4 improvement.

This tool is intentionally proposal-only. It reads existing consensus trace
ledgers, writes a human-readable YAML proposal, and never mutates source,
configuration, rubrics, prompts, or workflow files. Any resulting harness change
must go through the normal consensus consult/build/delivery-token lifecycle.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from consensus_mcp import _paths

SCHEMA = {
    "name": "harness.propose",
    "description": (
        "Read existing consensus trace ledgers and write a proposal-only "
        "harness_proposal.yaml for human-approved rubric/prompt/workflow/test "
        "improvements. Never mutates source/config; proposals require a later "
        "consensus consult/build and delivery token."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "output_path": {
                "type": "string",
                "description": "Optional output path. Defaults to consensus-state/state/harness-proposal.yaml.",
            },
            "max_records": {
                "type": "integer",
                "minimum": 1,
                "default": 200,
                "description": "Maximum records to sample across trace ledgers.",
            },
        },
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "proposal_path": {"type": "string"},
            "evidence_count": {"type": "integer"},
            "recommendation_count": {"type": "integer"},
            "error": {"type": "string"},
        },
        "required": ["ok"],
    },
}

_ALLOWED_PROPOSAL_FILES = [
    "consensus_mcp/dispatch_templates/**",
    "consensus_mcp/looper_plan/rubrics/**",
    "consensus_mcp/validators/**",
    "consensus_mcp/tests/**",
    "docs/workflows/**",
    "docs/superpowers/specs/**",
]

_TRACE_FILES = (
    "results-v1.jsonl",
    "outcome-ledger.jsonl",
    "dispatch-log.jsonl",
    "audit-log.jsonl",
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_jsonl(path: Path, *, max_records: int) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if len(rows) >= max_records:
            break
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            rows.append({"_source": path.name, "_line": line_no, "kind": "malformed_jsonl"})
            continue
        if isinstance(raw, dict):
            raw = dict(raw)
            raw["_source"] = path.name
            raw["_line"] = line_no
            rows.append(raw)
    return rows


def _summarize_record(row: dict[str, Any]) -> str:
    source = row.get("_source", "trace")
    if row.get("kind") == "malformed_jsonl":
        return f"{source}:{row.get('_line')}: malformed JSONL row"
    if source == "results-v1.jsonl":
        iteration = row.get("iteration_id", "unknown-iteration")
        findings = row.get("findings") if isinstance(row.get("findings"), list) else []
        counts = Counter(str(f.get("severity", "unknown")) for f in findings if isinstance(f, dict))
        if counts:
            return f"{source}:{row.get('_line')}: {iteration} findings by severity {dict(counts)}"
        return f"{source}:{row.get('_line')}: {iteration} results record"
    if source == "outcome-ledger.jsonl":
        return f"{source}:{row.get('_line')}: outcome useful={row.get('useful')} finding={row.get('finding_id')} evidence={row.get('evidence_ref')}"
    if source == "dispatch-log.jsonl":
        return f"{source}:{row.get('_line')}: dispatch event={row.get('event')} reviewer={row.get('reviewer_id') or row.get('contributor')} ok={row.get('ok')}"
    if source == "audit-log.jsonl":
        return f"{source}:{row.get('_line')}: audit event={row.get('event')} actor={row.get('actor')}"
    return f"{source}:{row.get('_line')}: trace row"


def _recommendations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    severities: Counter[str] = Counter()
    dispatch_failures = 0
    malformed = 0
    useful_false = 0
    for row in rows:
        if row.get("kind") == "malformed_jsonl":
            malformed += 1
        if row.get("_source") == "results-v1.jsonl":
            findings = row.get("findings") if isinstance(row.get("findings"), list) else []
            for f in findings:
                if isinstance(f, dict):
                    severities[str(f.get("severity", "unknown"))] += 1
        if row.get("_source") == "dispatch-log.jsonl" and row.get("ok") is False:
            dispatch_failures += 1
        if row.get("_source") == "outcome-ledger.jsonl" and row.get("useful") is False:
            useful_false += 1

    if severities:
        recs.append({
            "id": "harness-rec-001",
            "summary": "Review recent findings by severity and tighten reviewer/spec prompts where defects repeat.",
            "rationale": f"Observed findings by severity: {dict(severities)}.",
            "candidate_files": [
                "consensus_mcp/dispatch_templates/**",
                "consensus_mcp/looper_plan/rubrics/**",
                "consensus_mcp/tests/**",
            ],
        })
    if dispatch_failures:
        recs.append({
            "id": "harness-rec-002",
            "summary": "Add or improve dispatch preflight diagnostics for recurring reviewer CLI failures.",
            "rationale": f"Observed {dispatch_failures} failed dispatch trace row(s).",
            "candidate_files": [
                "docs/workflows/**",
                "consensus_mcp/tests/**",
                "consensus_mcp/dispatch_templates/**",
            ],
        })
    if useful_false:
        recs.append({
            "id": "harness-rec-003",
            "summary": "Analyze refuted/low-usefulness findings and adjust rubrics to reduce reviewer noise.",
            "rationale": f"Observed {useful_false} outcome row(s) marked useful=false.",
            "candidate_files": [
                "consensus_mcp/looper_plan/rubrics/**",
                "consensus_mcp/dispatch_templates/**",
            ],
        })
    if malformed:
        recs.append({
            "id": "harness-rec-004",
            "summary": "Add trace-ledger validation tests or repair tooling for malformed JSONL rows.",
            "rationale": f"Observed {malformed} malformed trace row(s).",
            "candidate_files": [
                "consensus_mcp/validators/**",
                "consensus_mcp/tests/**",
            ],
        })
    if not recs:
        recs.append({
            "id": "harness-rec-001",
            "summary": "Inspect accumulated trace evidence for a narrow, human-approved harness improvement.",
            "rationale": "Trace rows exist but did not match a specialized heuristic; preserve evidence for human review.",
            "candidate_files": ["docs/workflows/**", "consensus_mcp/tests/**"],
        })
    return recs


def _schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / "harness_proposal.schema.json"


def _validate_allowed_files(proposal: dict[str, Any]) -> None:
    allowed = proposal.get("allowed_files")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("proposal.allowed_files must be a non-empty list")
    allowed_set = set(_ALLOWED_PROPOSAL_FILES)
    for item in allowed:
        if item not in allowed_set:
            raise ValueError(f"proposal allowed_files entry is outside harness scope: {item!r}")
    for rec in proposal.get("recommendations", []):
        for item in rec.get("candidate_files", []):
            if item not in allowed_set:
                raise ValueError(f"recommendation candidate file is outside harness scope: {item!r}")


def build_proposal(*, max_records: int = 200) -> dict[str, Any]:
    if max_records < 1:
        raise ValueError("max_records must be >= 1")
    state = _paths.state_root()
    trace_dir = state / "state"
    rows: list[dict[str, Any]] = []
    per_file_budget = max(1, max_records // len(_TRACE_FILES))
    for name in _TRACE_FILES:
        rows.extend(_read_jsonl(trace_dir / name, max_records=per_file_budget))
    if not rows:
        raise FileNotFoundError(
            f"no trace rows found under {trace_dir}; expected one of {', '.join(_TRACE_FILES)}"
        )
    evidence = [
        {"source": row.get("_source"), "line": row.get("_line"), "summary": _summarize_record(row)}
        for row in rows[:max_records]
    ]
    proposal = {
        "schema_version": 1,
        "kind": "harness_improvement_proposal",
        "generated_at_utc": _now_utc(),
        "state_root": str(state),
        "safety_policy": {
            "proposal_only": True,
            "no_source_mutation": True,
            "requires_human_approval": True,
            "requires_consensus_review_before_edits": True,
            "note": "This file is advisory. It does not approve, apply, or mutate harness changes.",
        },
        "allowed_files": list(_ALLOWED_PROPOSAL_FILES),
        "evidence": evidence,
        "recommendations": _recommendations(rows),
        "next_steps": [
            "Start a consensus consult scoped to the proposed allowed_files.",
            "Have at least two non-host reviewer families vet the harness change.",
            "Apply edits only after approval, tests, and delivery tokens.",
        ],
    }
    _validate_allowed_files(proposal)
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    jsonschema.validate(instance=proposal, schema=schema)
    return proposal


def handle(output_path: str | None = None, max_records: int = 200) -> dict:
    try:
        proposal = build_proposal(max_records=max_records)
        out = Path(output_path) if output_path else _paths.state_root() / "state" / "harness-proposal.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")
        return {
            "ok": True,
            "proposal_path": str(out),
            "evidence_count": len(proposal["evidence"]),
            "recommendation_count": len(proposal["recommendations"]),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Generate a proposal-only harness improvement YAML from trace ledgers.")
    parser.add_argument("--output-path")
    parser.add_argument("--max-records", type=int, default=200)
    ns = parser.parse_args(argv)
    result = handle(output_path=ns.output_path, max_records=ns.max_records)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
