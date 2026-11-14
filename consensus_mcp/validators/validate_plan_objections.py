"""Validate plan objection structure and immutable sealed source metadata."""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


def validate_plan_objections(document, expected_rows: list[dict], *, require_complete=True) -> list[str]:
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" /
                         "plan-objections.schema.json").read_text(encoding="utf-8"))
    errors = [f"plan-objections schema: {e.message}" for e in Draft202012Validator(schema).iter_errors(document)]
    if errors:
        return errors
    expected = {r["id"]: r for r in expected_rows}
    seen = set()
    for row in document["objections"]:
        rid = row["id"]
        if rid in seen:
            errors.append(f"{rid}: duplicate id")
        seen.add(rid)
        source = expected.get(rid)
        if source is None or any(row[k] != source[k] for k in ("pass", "reviewer", "model", "kind", "summary")):
            errors.append(f"{rid}: source metadata differs from sealed review")
        if row["response"] == "rebutted" and not row["evidence"].strip():
            errors.append(f"{rid}: rebutted requires evidence")
        if row["response"] in {"accepted", "rebutted"} and not row["host_response"].strip():
            errors.append(f"{rid}: response requires host_response")
        if (row["user_decision"] is None) != (row["decided_by"] is None):
            errors.append(f"{rid}: user decisions require decided_by: user")
        if row["kind"] in {"unfaithful", "unrelated_change"} and row["user_decision"] == "send_back":
            errors.append(f"{rid}: confirmation objections require a user yay or nay")
    if require_complete:
        errors.extend(f"{rid}: missing objection" for rid in sorted(expected.keys() - seen))
    return errors
