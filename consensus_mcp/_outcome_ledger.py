"""Append-only external-outcome adjudication ledger (Plan 2 substrate).

This is the "useful-finding" signal source for the weighted-consensus learner
(converged spec 2026-05-24, D1/D2/D5). Records are written ONLY from EXTERNAL
adjudication - an objective machine outcome (a test/smoke red->green, a
falsification result) or an audited operator disposition - NEVER by an AI agent
grading itself or a peer. The writer enforces no-self-grade (D5b) by REJECTING any
record whose adjudicator is a panel/AI agent. Format is append-only JSONL; AI code
may READ the ledger but can never originate credit.
"""
from __future__ import annotations

import json
from pathlib import Path

GOLD = "gold"            # objective machine-checkable outcome (test/smoke/falsification)
SECONDARY = "secondary"  # audited operator disposition (with evidence)
_VALID_TIERS = {GOLD, SECONDARY}

# Adjudicator identities that may NEVER write usefulness credit (no-self-grade).
# Panel/AI families + generic agent words. Credit must come from operator/CI/test.
_FORBIDDEN_ADJUDICATOR_TOKENS = (
    "claude", "orchestrator", "host_peer", "host-peer", "codex", "gemini",
    "kimi", "grok", "agent", "ai", "llm", "model", "panel", "assistant", "gpt", "chatgpt",
)
_REQUIRED_FIELDS = (
    "finding_id", "contributor", "domain", "tier", "useful",
    "iteration_index", "model_version", "adjudicator", "evidence_ref",
)


def _validate_record(record: dict) -> None:
    if not isinstance(record, dict):
        raise ValueError("outcome record must be an object")
    missing = [f for f in _REQUIRED_FIELDS if f not in record]
    if missing:
        raise ValueError(f"outcome record missing required fields: {missing}")
    if record["tier"] not in _VALID_TIERS:
        raise ValueError(f"tier must be one of {sorted(_VALID_TIERS)}, got {record['tier']!r}")
    if not isinstance(record["useful"], bool):
        raise ValueError("'useful' must be a bool")
    if type(record["iteration_index"]) is not int:
        raise ValueError("'iteration_index' must be an int")
    for field in ("finding_id", "contributor", "domain", "model_version",
                  "adjudicator", "evidence_ref"):
        if not isinstance(record[field], str) or not record[field].strip():
            raise ValueError(f"'{field}' must be a non-empty string")
    adj = record["adjudicator"].strip().lower()
    if not adj:
        raise ValueError("'adjudicator' is required (operator / ci / external-test)")
    hit = [t for t in _FORBIDDEN_ADJUDICATOR_TOKENS if t in adj]
    if adj in {record["contributor"].strip().lower(), record["model_version"].strip().lower()}:
        hit.append("contributor/model identity")
    if hit:
        raise ValueError(
            f"no-self-grade violation: adjudicator {record['adjudicator']!r} is an "
            f"AI/panel agent ({hit}); usefulness credit must originate from an external "
            f"source (operator / ci / external-test)"
        )
    if not str(record["evidence_ref"]).strip():
        raise ValueError("'evidence_ref' is required (link/text proving the outcome)")


def append_outcome(ledger_path: str | Path, record: dict) -> None:
    """Validate + append one outcome record to the append-only JSONL ledger.
    Raises ValueError for malformed records or a no-self-grade violation."""
    _validate_record(record)
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def record_gold(ledger_path: str | Path, results_record: dict, row_id: str, *,
                useful: bool, evidence_ref: str, adjudicator: str,
                iteration_index: int) -> dict:
    """Record an external adjudication of an exact, sealed disposition row.

    The caller must identify the external adjudicator; no operator identity is
    supplied automatically on behalf of an AI host. Host decisions never call
    this API and never become gold merely by being accepted.
    """
    host = (results_record.get("convergence") or {}).get("host_disposition") or {}
    rows = [r for r in host.get("ideas", []) if r["id"] == row_id]
    if len(rows) != 1 or not host.get("dispositions_sha256"):
        raise ValueError("idea must identify one row in sealed host dispositions")
    row = rows[0]
    if not row.get("model"):
        raise ValueError("cannot record per-model gold without a recorded model identity")
    record = {
        "iteration_id": results_record["iteration_id"], "finding_id": row_id,
        "contributor": row["model"], "model_version": row["model"],
        "domain": "general", "tier": GOLD, "useful": useful,
        "iteration_index": iteration_index, "adjudicator": adjudicator,
        "evidence_ref": evidence_ref,
        "dispositions_sha256": host["dispositions_sha256"],
    }
    prior = read_outcomes(ledger_path)
    if any(o.get("iteration_id") == record["iteration_id"]
           and o["finding_id"] == row_id and o["tier"] == GOLD for o in prior):
        raise ValueError("gold outcome already recorded for this iteration and idea")
    append_outcome(ledger_path, record)
    return record


def read_outcomes(ledger_path: str | Path) -> list[dict]:
    """Read all VALID outcome records (empty list if the ledger does not exist).

    The no-self-grade firewall must hold at the TRUST BOUNDARY the learner consumes,
    not only at write time (codex-rev-001): a tampered or legacy row - e.g. one with
    an AI adjudicator or a malformed shape that bypassed/predates append_outcome - is
    SKIPPED here, so it can never reach the learner. Invalid rows are quarantined
    (skipped), not fatal, so one bad row can't deny-service the whole ledger."""
    path = Path(ledger_path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            _validate_record(record)  # re-validate at the read boundary
        except (json.JSONDecodeError, ValueError, TypeError):
            continue  # quarantine: never feed an unvalidated/AI-authored row to the learner
        out.append(record)
    return out
