"""validate_review.py - Phase 0 review-artifact validator (P0-V1).

Per spec section 9 (review schema), section 13 (corroboration ownership rule),
and section 23 phase_0_deliverables. Audits a reviewer-emitted YAML artifact
(codex-review.yaml or claude-review.yaml) against the section 9 schema and section 13
corroboration rules.

Detects:
  - missing required top-level keys
  - invalid enum values (agent, stance, confidence, severity, claim_class,
    ambiguity_type, proposed_resolution_method, owner, challenge_result)
  - empty assumptions_challenged (substantive_pushback violation)
  - empty blocking_objections + missing/empty no_objection_rationale.text
  - blocking_objection missing required_change
  - clarifying_question owner=operator without non-empty blocks
  - corroborated_by appearing anywhere in reviewer artifact (synthesizer-only
    per codex-rev-009 / section 13)

Output: structured report (YAML by default, JSON via --json) suitable for
downstream tooling.

Usage:
  python consensus_mcp/validators/validate_review.py --review PATH [--out PATH] [--json]

Exit codes:
  0 - validator ran cleanly; report written
  2 - validator could not run (review missing, parse error)

Findings count does NOT gate exit code (Path C / consistent with
validate_disposition_index.py).
"""
from __future__ import annotations
import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # executed as a script: prefer the co-located source tree
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from consensus_mcp.validators._shared import _dependency_version, _sha256_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / "consensus-state" / "state" / "validate-review-report.yaml"

REQUIRED_TOP_LEVEL_KEYS = [
    "schema_version",
    "agent",
    "stance",
    "iteration_id",
    "reviewed_packet_sha256",
    "overall_position",
    "blocking_objections",
    "assumptions_challenged",
]

VALID_AGENTS = {"codex", "claude"}
VALID_STANCES = {"implementation_realist", "methodology_critic"}
VALID_CONFIDENCE = {"low", "medium", "high"}
VALID_SEVERITY = {"blocking", "high", "medium", "low"}
VALID_CLAIM_CLASS = {"safety", "correctness", "scope", "test", "methodology", "security", "process"}
VALID_AMBIGUITY = {
    "repo-discoverable", "peer-resolvable", "test-resolvable",
    "operator-owned", "external-unavailable", "production-approval",
    "destructive-action",
}
VALID_RESOLUTION_METHOD = {"read_repo", "peer_ask", "bounded_test", "operator"}
VALID_OWNER = {"agent", "operator"}
VALID_CHALLENGE_RESULT = {"cleared", "failed", "partially-cleared"}


def _parse_yaml_file(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit("pyyaml required (pip install pyyaml)")
    if not path.exists():
        raise SystemExit(f"review file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SystemExit(f"yaml parse error in {path}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"review file root must be a mapping: {path}")
    return data


def _build_provenance(review_path: Path) -> dict:
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
        "inputs": {
            "review_path": str(review_path.relative_to(REPO_ROOT)) if review_path.is_relative_to(REPO_ROOT) else str(review_path),
            "review_sha256": _sha256_file(review_path),
            "validator_script_path": "consensus_mcp/validators/validate_review.py",
            "validator_script_sha256": _sha256_file(Path(__file__).resolve()),
        },
    }


def _contains_corroborated_by(obj, path: str = "") -> list[str]:
    """Recurse into review structure; return list of dotted paths where
    corroborated_by appears."""
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}" if path else k
            if k == "corroborated_by":
                hits.append(sub)
            hits.extend(_contains_corroborated_by(v, sub))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(_contains_corroborated_by(item, f"{path}[{i}]"))
    return hits


def validate_review(review_path: Path) -> dict:
    """Validate a single reviewer artifact; return structured report."""
    findings: list[dict] = []
    review = _parse_yaml_file(review_path)

    # ---- Required top-level keys ----
    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in review:
            findings.append({
                "id": "MISSING_REQUIRED_KEY",
                "severity": "high",
                "field": key,
                "claim": f"required top-level key missing: {key!r}",
            })

    # ---- Enum: agent ----
    agent = review.get("agent")
    if agent is not None and agent not in VALID_AGENTS:
        findings.append({
            "id": "INVALID_ENUM_VALUE",
            "severity": "medium",
            "field": "agent",
            "value": agent,
            "claim": f"agent={agent!r} not in {sorted(VALID_AGENTS)}",
        })

    # ---- Enum: stance (warning per section 4: future stances may be added) ----
    stance = review.get("stance")
    if stance is not None and stance not in VALID_STANCES:
        findings.append({
            "id": "INVALID_ENUM_VALUE",
            "severity": "medium",
            "field": "stance",
            "value": stance,
            "claim": f"stance={stance!r} not in {sorted(VALID_STANCES)} (warning: future stances may extend this set)",
        })

    # ---- overall_position bools + confidence enum ----
    op = review.get("overall_position")
    if isinstance(op, dict):
        for bool_field in ("implementation_ready", "production_ready"):
            if bool_field in op and not isinstance(op[bool_field], bool):
                findings.append({
                    "id": "INVALID_ENUM_VALUE",
                    "severity": "medium",
                    "field": f"overall_position.{bool_field}",
                    "value": op[bool_field],
                    "claim": f"overall_position.{bool_field} must be bool",
                })
        conf = op.get("confidence")
        if conf is not None and conf not in VALID_CONFIDENCE:
            findings.append({
                "id": "INVALID_ENUM_VALUE",
                "severity": "medium",
                "field": "overall_position.confidence",
                "value": conf,
                "claim": f"confidence={conf!r} not in {sorted(VALID_CONFIDENCE)}",
            })

    # ---- blocking_objections ----
    blockers = review.get("blocking_objections")
    if blockers is None:
        blockers = []
    if not isinstance(blockers, list):
        blockers = []

    for i, b in enumerate(blockers):
        if not isinstance(b, dict):
            continue
        bid = b.get("id", f"<index {i}>")
        # severity enum
        sev = b.get("severity")
        if sev is not None and sev not in VALID_SEVERITY:
            findings.append({
                "id": "INVALID_ENUM_VALUE",
                "severity": "medium",
                "field": f"blocking_objections[{i}].severity",
                "value": sev,
                "claim": f"severity={sev!r} on {bid} not in {sorted(VALID_SEVERITY)}",
            })
        # claim_class enum
        cc = b.get("claim_class")
        if cc is not None and cc not in VALID_CLAIM_CLASS:
            findings.append({
                "id": "INVALID_ENUM_VALUE",
                "severity": "medium",
                "field": f"blocking_objections[{i}].claim_class",
                "value": cc,
                "claim": f"claim_class={cc!r} on {bid} not in {sorted(VALID_CLAIM_CLASS)}",
            })
        # required_change non-empty
        rc = b.get("required_change")
        if not isinstance(rc, str) or not rc.strip():
            findings.append({
                "id": "BLOCKING_OBJECTION_MISSING_REQUIRED_CHANGE",
                "severity": "high",
                "field": f"blocking_objections[{i}].required_change",
                "entry_id": bid,
                "claim": f"blocking objection {bid} missing non-empty required_change",
            })

    # ---- assumptions_challenged non-empty ----
    ac = review.get("assumptions_challenged")
    if not isinstance(ac, list) or len(ac) == 0:
        findings.append({
            "id": "EMPTY_ASSUMPTIONS_CHALLENGED",
            "severity": "high",
            "field": "assumptions_challenged",
            "claim": "assumptions_challenged is empty; substantive_pushback rule requires at least one entry",
        })
    else:
        for i, a in enumerate(ac):
            if not isinstance(a, dict):
                continue
            cr = a.get("challenge_result")
            if cr is not None and cr not in VALID_CHALLENGE_RESULT:
                findings.append({
                    "id": "INVALID_ENUM_VALUE",
                    "severity": "medium",
                    "field": f"assumptions_challenged[{i}].challenge_result",
                    "value": cr,
                    "claim": f"challenge_result={cr!r} not in {sorted(VALID_CHALLENGE_RESULT)}",
                })

    # ---- no_objection_rationale required when no blockers ----
    if len(blockers) == 0:
        nor = review.get("no_objection_rationale") or {}
        text = nor.get("text") if isinstance(nor, dict) else None
        if not isinstance(text, str) or not text.strip():
            findings.append({
                "id": "NO_BLOCKERS_WITHOUT_RATIONALE",
                "severity": "high",
                "field": "no_objection_rationale.text",
                "claim": "blocking_objections is empty but no_objection_rationale.text is missing or empty",
            })

    # ---- clarifying_questions enums + operator-blocks rule ----
    cqs = review.get("clarifying_questions")
    if isinstance(cqs, list):
        for i, q in enumerate(cqs):
            if not isinstance(q, dict):
                continue
            qid = q.get("id", f"<index {i}>")
            at = q.get("ambiguity_type")
            if at is not None and at not in VALID_AMBIGUITY:
                findings.append({
                    "id": "INVALID_ENUM_VALUE",
                    "severity": "medium",
                    "field": f"clarifying_questions[{i}].ambiguity_type",
                    "value": at,
                    "claim": f"ambiguity_type={at!r} on {qid} not in {sorted(VALID_AMBIGUITY)}",
                })
            prm = q.get("proposed_resolution_method")
            if prm is not None and prm not in VALID_RESOLUTION_METHOD:
                findings.append({
                    "id": "INVALID_ENUM_VALUE",
                    "severity": "medium",
                    "field": f"clarifying_questions[{i}].proposed_resolution_method",
                    "value": prm,
                    "claim": f"proposed_resolution_method={prm!r} on {qid} not in {sorted(VALID_RESOLUTION_METHOD)}",
                })
            owner = q.get("owner")
            if owner is not None and owner not in VALID_OWNER:
                findings.append({
                    "id": "INVALID_ENUM_VALUE",
                    "severity": "medium",
                    "field": f"clarifying_questions[{i}].owner",
                    "value": owner,
                    "claim": f"owner={owner!r} on {qid} not in {sorted(VALID_OWNER)}",
                })
            if owner == "operator":
                blocks = q.get("blocks")
                if not isinstance(blocks, list) or len(blocks) == 0:
                    findings.append({
                        "id": "OPERATOR_QUESTION_MISSING_BLOCKS",
                        "severity": "medium",
                        "field": f"clarifying_questions[{i}].blocks",
                        "entry_id": qid,
                        "claim": f"clarifying_question {qid} owner=operator but blocks is empty/missing",
                    })

    # ---- corroborated_by anywhere in artifact (section 13) ----
    for hit_path in _contains_corroborated_by(review):
        findings.append({
            "id": "CORROBORATED_BY_FORBIDDEN",
            "severity": "high",
            "path": hit_path,
            "claim": f"reviewer artifact contains corroborated_by at {hit_path!r}; synthesizer-only per codex-rev-009 / section 13",
        })

    return _wrap(findings, review, review_path)


def _wrap(findings: list[dict], review: dict, review_path: Path) -> dict:
    severity_counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    return {
        "schema_version": 1,
        "validator": "validate_review.py",
        "validator_version": "0.1.0",
        "review_agent": review.get("agent", "<unknown>"),
        "review_iteration_id": review.get("iteration_id", "<unknown>"),
        "provenance": _build_provenance(review_path),
        "stats": {
            "total_findings": len(findings),
            "severity_counts": severity_counts,
        },
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--review", type=Path, required=True, help="reviewer YAML to validate")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--json", action="store_true", help="emit JSON to stdout in addition to YAML to --out")
    args = p.parse_args(argv)

    report = validate_review(args.review)

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
        print(f"validate_review: {report['stats']['total_findings']} finding(s) "
              f"({sev}) -> {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
